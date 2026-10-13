"""DAG Factory 命令行入口。

产出的 dag.json 满足 data-world ``--dag`` 的输入约束，两者仅靠该文件相连：

    # 单张：指定领域号，或让它按 persona 路由
    python -m dag_factory --category 4 --persona "供应链分析师" --out ./dags/ops.json

    # 批量：16 类各铺 N 张
    python -m dag_factory --batch --per-category 3 --out-dir ./dags

    # 批量：按 persona 文件（{"persona": ...} 的 jsonl）逐条路由出图
    python -m dag_factory --persona-file ./data/persona_da.jsonl --limit 200 --out-dir ./dags

    # 之后交给 data-world
    python -m data_world --dag ./dags/ops.json --output ./outputs --name ops-world
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
from pathlib import Path
from typing import List, Optional, Tuple

from . import DagFactoryError
from .categories import CATEGORIES
from .envfile import load_env, load_personas, load_personas_indexed
from .factory import choose_category, dag_display_meta, generate_dag
from .llm import LlmClient

# 尽早加载 .env：让下方各参数的 default=os.environ.get(...) 能接住 .env 里的值。
load_env()

_PERSONA_POOL = [
    "消费 App 的产品经理, 关注激活、留存曲线与功能采用",
    "中型制造企业的供应链总监, 关注需求预测、库存周转与缺货",
    "金融科技风控分析师, 关注欺诈检测与异常交易",
    "三甲医院的临床数据分析师, 关注住院流程、再入院与并发症",
    "公共卫生流行病学家, 关注疾病监测与疫苗干预效果",
    "生物信息学研究员, 关注 RNA-seq 表达差异与批次效应",
    "气候科学家, 关注气象时序、排放与极端天气事件",
    "电网运营的数据工程师, 关注负荷预测与设备异常停机",
    "城市交通研究员, 关注 OD 出行、拥堵与事故影响",
    "教育研究者, 关注学生留存、测评成绩与教学干预效果",
    "公共政策分析师, 关注政府项目覆盖、公平性与因果评估",
    "社会学家, 关注纵向调查面板、抽样偏差与态度变迁",
]

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def _slugify(value: str, limit: int = 48) -> str:
    normalized = _SAFE_NAME_RE.sub("-", value.strip()).strip("-._").lower()
    return normalized[:limit] or "dag"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dag-factory",
        description="生成纯净的领域因果 DAG（喂给 data-world 的 --dag）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--persona", help="分析者画像；缺省时从内置池随机取")
    parser.add_argument("--category", type=int, help=f"领域编号 1..{max(CATEGORIES)}；缺省则按 persona 路由")
    parser.add_argument("--out", help="单张模式：输出 dag.json 路径")
    parser.add_argument("--batch", action="store_true", help="批量模式：按领域铺开多张")
    parser.add_argument("--per-category", type=int, default=1, help="批量模式：每个领域生成几张")
    parser.add_argument("--categories", help="批量模式：限定领域编号，逗号分隔（默认全部 16 类）")
    parser.add_argument("--persona-file", help="persona jsonl（每行 {\"persona\": ...}）：逐条按 persona 路由出图，每 persona 一张")
    parser.add_argument("--limit", type=int, help="--persona-file 模式：只取前 N 条 persona（默认全部）")
    parser.add_argument("--shuffle", action="store_true", help="--persona-file 模式：按 --seed 打乱 persona 顺序后再取")
    parser.add_argument("--out-dir", help="批量模式：输出目录")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", "aws.claude-opus-4.8"),
                        help="生成 DAG 使用的模型")
    parser.add_argument("--endpoint", default=os.environ.get("ANTHROPIC_BASE_URL"),
                        help="OpenAI 兼容 base_url；含 /anthropic 后缀会自动剥去")
    parser.add_argument("--auth-token", default=os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    parser.add_argument("--attempts", type=int, default=5, help="单张 DAG 的校验失败重试上限")
    parser.add_argument("--seed", type=int, default=20260712, help="随机种子（persona/领域轮询）")
    parser.add_argument("--verbose", action="store_true", default=True)
    return parser


def _resolve_persona(explicit: Optional[str], rng: random.Random) -> str:
    return explicit or rng.choice(_PERSONA_POOL)


def _write_dag(dag: dict, path: Path, verbose: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        json.dump(dag, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    if verbose:
        print(f"[dag] 写出 {len(dag['nodes'])} nodes / {len(dag['edges'])} edges -> {path}", flush=True)


def _append_mapping(row: dict, path: Path) -> None:
    """每生成一个 DAG 就增量追加一行 dag<->persona<->category 映射。

    增量写而非进程末尾一次性写：分片进程中途被杀/崩溃时，已生成的 DAG 仍在映射里
    （末尾批量写会在崩溃时丢掉整份）。每个分片写独立文件，并发无冲突；偶发单行损坏
    也可由 DAG 文件名(行号)重建。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")


def _run_single(args: argparse.Namespace, client: LlmClient, rng: random.Random) -> int:
    if not args.out:
        raise DagFactoryError("单张模式必须提供 --out")
    persona = _resolve_persona(args.persona, rng)
    cid = choose_category(client, persona, explicit=args.category, verbose=args.verbose)
    if args.verbose:
        meta = dag_display_meta(cid)
        print(f"[factory] persona={persona!r} -> 领域[{cid}] {meta['category_name']}", flush=True)
    dag = generate_dag(client, persona, cid, attempts=args.attempts, verbose=args.verbose)
    _write_dag(dag, Path(args.out).expanduser(), args.verbose)
    return 0


def _run_batch(args: argparse.Namespace, client: LlmClient, rng: random.Random) -> int:
    if not args.out_dir:
        raise DagFactoryError("批量模式必须提供 --out-dir")
    if args.categories:
        try:
            targets: List[int] = [int(x) for x in args.categories.split(",") if x.strip()]
        except ValueError as exc:
            raise DagFactoryError(f"--categories 必须是逗号分隔的数字: {exc}") from exc
    else:
        targets = list(CATEGORIES)
    for cid in targets:
        if cid not in CATEGORIES:
            raise DagFactoryError(f"领域编号越界: {cid}（有效 1..{max(CATEGORIES)}）")

    out_dir = Path(args.out_dir).expanduser()
    ok_count, fail_count = 0, 0
    for cid in targets:
        for k in range(args.per_category):
            persona = _resolve_persona(args.persona, rng)
            try:
                dag = generate_dag(client, persona, cid, attempts=args.attempts, verbose=args.verbose)
            except (ValueError, DagFactoryError) as exc:
                fail_count += 1
                print(f"[batch] 领域[{cid}] 第 {k + 1}/{args.per_category} 张失败: {str(exc)[:160]}", flush=True)
                continue
            name = f"cat{cid:02d}_{_slugify(CATEGORIES[cid][0])}_{k + 1}.json"
            _write_dag(dag, out_dir / name, args.verbose)
            ok_count += 1
    print(f"[batch] 完成: 成功 {ok_count} 张, 失败 {fail_count} 张 -> {out_dir}", flush=True)
    return 0 if ok_count > 0 else 1


def _load_persona_items(args: argparse.Namespace) -> List[Tuple[int, str]]:
    """读取 (全局索引, persona) 列表。

    分片文件每行可带一个整型 ``index`` 字段透传全局位置；缺省时（如直接喂原始
    persona 文件）回退为在本次截断/打乱之后的行号。前者保证并发分片间文件名不冲突、
    可断点续跑，后者兼容单机直跑。
    """
    path = Path(args.persona_file).expanduser()
    raw_indexed: List[Tuple[int, str]] = []
    saw_explicit = False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        persona = row.get("persona") if isinstance(row, dict) else None
        if not (isinstance(persona, str) and persona.strip()):
            continue
        idx = row.get("index") if isinstance(row, dict) else None
        if isinstance(idx, int):
            saw_explicit = True
            raw_indexed.append((idx, persona.strip()))
        else:
            raw_indexed.append((-1, persona.strip()))
    if saw_explicit:
        # 显式 index 权威：直接用，忽略 --limit/--shuffle（分片阶段已定序）。
        return [(i, p) for i, p in raw_indexed if i >= 0]
    # 无显式 index：走标准加载口径（可 --shuffle/--limit），行号即全局索引。
    return load_personas_indexed(
        path, limit=args.limit, shuffle=args.shuffle, seed=args.seed,
    )


def _run_persona_file(args: argparse.Namespace, client: LlmClient) -> int:
    if not args.out_dir:
        raise DagFactoryError("--persona-file 模式必须提供 --out-dir")
    if args.limit is not None and args.limit < 1:
        raise DagFactoryError("--limit 必须 >= 1")
    items = _load_persona_items(args)
    if not items:
        raise DagFactoryError(f"persona 文件里没有可用的 persona: {args.persona_file}")

    out_dir = Path(args.out_dir).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    # 每个进程（分片）写自己的映射文件，避免并发抢写同一文件；末尾一次性落盘。
    # 映射文件名由 persona 文件名派生（分片阶段每个 shard 文件名唯一）。
    mapping_dir = out_dir / "_mapping"
    mapping_dir.mkdir(parents=True, exist_ok=True)
    mapping_path = mapping_dir / f"{Path(args.persona_file).stem}.jsonl"
    if args.verbose:
        print(f"[persona] 载入 {len(items)} 条 persona -> {out_dir}", flush=True)
    ok_count, fail_count, skip_count = 0, 0, 0
    for pos, (index, persona) in enumerate(items):
        # 与内容/领域无关的稳定文件名：同一 persona 重跑必落同一文件，天然可断点续跑。
        out_path = out_dir / f"p{index:06d}.json"
        if out_path.is_file() and out_path.stat().st_size > 0:
            skip_count += 1
            if args.verbose:
                print(f"[persona] 跳过已存在 p{index:06d} ({pos + 1}/{len(items)})", flush=True)
            continue
        cid = choose_category(client, persona, explicit=args.category, verbose=args.verbose)
        try:
            dag = generate_dag(client, persona, cid, attempts=args.attempts, verbose=args.verbose)
        except (ValueError, DagFactoryError) as exc:
            fail_count += 1
            print(f"[persona] p{index:06d} 失败 (领域[{cid}]): {str(exc)[:160]}", flush=True)
            continue
        _write_dag(dag, out_path, args.verbose)
        _append_mapping({
            "dag_file": out_path.name, "index": index, "persona": persona,
            "category": cid, "category_name": CATEGORIES[cid][0],
        }, mapping_path)
        ok_count += 1
    print(f"[persona] 完成: 新生成 {ok_count} 张, 跳过已有 {skip_count} 张, 失败 {fail_count} 张 -> {out_dir}", flush=True)
    return 0 if fail_count == 0 else 1


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.per_category < 1 or args.attempts < 1:
        raise SystemExit("--per-category 和 --attempts 必须 >= 1")
    rng = random.Random(args.seed)
    client = LlmClient(model=args.model, endpoint=args.endpoint,
                       auth_token=args.auth_token, verbose=args.verbose)
    try:
        if args.persona_file:
            return _run_persona_file(args, client)
        if args.batch:
            return _run_batch(args, client, rng)
        return _run_single(args, client, rng)
    except DagFactoryError as exc:
        raise SystemExit(f"dag-factory 错误: {exc}")
