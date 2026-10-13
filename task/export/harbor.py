"""导出阶段：``tasks.json`` → harbor 基准任务目录。

每题一个自包含任务目录（对齐 harbor 的目录约定）：

    dataworld-<world>-<task-dash>/
      instruction.md          题面 + env/→/app/data/ 映射 + 答案路径一句话
      task.toml               schema 1.1，超时/资源/artifacts
      environment/Dockerfile  ubuntu24.04 + 内部源 + COPY data/ /app/data/
      environment/data/       该世界 env/ 的副本（自包含，不依赖世界基础镜像）
      tests/test.sh           verifier 入口
      tests/grade.py          判分实现（本包 ``grade.py`` 的副本）
      tests/judge.py          judge 客户端（本包 ``judge.py`` 的副本）
      tests/judge_prompt.py   JUDGE_PROMPT（由本包 ``prompts.py`` 生成，自包含）
      tests/rubric.json       criterion+points，仅 judge 可见

三条硬约束：

- **防泄漏是双保险。** 导出时断言 instruction 不含 rubric 文本
  （``contract.check_no_leak``），镜像里再用白名单只 ``COPY data/`` 并在构建期
  断言 ``WORLD.md`` / ``dag.json`` / ``.build`` 不存在——题面泄漏和镜像泄漏是
  两条独立通路，各堵一次。
- **判分代码是拷贝而非复刻。** ``tests/`` 下的 grade/judge/prompts 直接拷本包
  源文件，不另写一份容器版；这是"评分语义只有一个实现"的落地方式。
- **凭据随任务目录走。** 评测平台没有运行期环境变量注入通道，judge 凭据
  写进每个任务的 ``tests/judge.env``，``test.sh`` 启动时 source。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

from .. import TaskGenError
from ..contract import build_rubric, check_no_leak, resolve_env_dir
from .judge import _BASE_URL_KEYS, _MODEL_KEYS, _TOKEN_KEYS, _first_env
from .prompts import JUDGE_PROMPT

#: 任务目录名前缀。
TASK_PREFIX = "dataworld"

#: 答题者的交付文件（容器内路径）；judge 与 verifier 都以它为唯一评分依据。
ANSWER_PATH = "/app/answer.txt"

#: 数据在容器内的挂载点。
DATA_DIR = "/app/data"

#: judge 凭据（写进每个任务的 tests/judge.env，test.sh 开头 source）。
#: 评测平台没有运行期环境变量注入通道，凭据只能随任务目录走。
#: 凭据本体在项目根 .env（task/cli.py 入口已 load_env），此处惰性读取——
#: 模块 import 时 .env 尚未加载，不能在模块级求值；也不硬编码进源码
#: （本文件进 git，密钥落历史即泄漏）。
#:
#: 解析顺序与 ``judge.py`` 完全一致（``JUDGE_*`` 优先、回落 ``ANTHROPIC_*``）：
#: 导出时的 judge 身份必须等于容器里 judge.py 会用的身份——两边各解析一次，
#: 顺序不同就会静默变成两个 judge。
def _judge_env() -> str:
    values = {
        "ANTHROPIC_AUTH_TOKEN": _first_env(_TOKEN_KEYS),
        "ANTHROPIC_BASE_URL": _first_env(_BASE_URL_KEYS),
        "ANTHROPIC_MODEL": _first_env(_MODEL_KEYS),
    }
    missing = [key for key, value in values.items() if not value]
    if missing:
        raise TaskGenError(
            "judge 凭据缺失（.env 或环境变量，JUDGE_* 或 ANTHROPIC_*）："
            f"{', '.join(missing)}")
    lines = [
        "# 判分 judge 网关凭据（test.sh 启动时 source；judge.py 回落读取 ANTHROPIC_*）",
        *(f"{key}={value}" for key, value in values.items()),
    ]
    judge_api = os.environ.get("JUDGE_API", "").strip()
    if judge_api:
        lines.append(f"JUDGE_API={judge_api}")
    return "\n".join(lines) + "\n"

#: task_id 尾号（拓扑深度层）→ harbor difficulty。
LAYER_DIFFICULTY = {"1": "medium", "2": "hard", "3": "hard"}

_KIND_ZH = {"cause": "原因", "consequence": "后果"}
_KIND_CONCLUSION = {"cause": "因果归因", "consequence": "后果推断"}

MAPPING_NOTE = (
    "\n> 数据路径说明：题面中提到的 `env/` 目录内容已挂载到 `/app/data/`"
    "（题面里的 `env/xxx` 即 `/app/data/xxx`）。\n"
)

TASK_TOML = """schema_version = "1.1"

# Collected into each trial directory after verification (see instruction.md).
artifacts = [
  "{answer}",
  "/logs/verifier/reward.txt",
  "/logs/verifier/details.json",
  "/logs/artifacts/",
  "/logs/agent/trajectory.json",
]

[task]
name = "{prefix}/{world}-{task_dash}"
description = {description}
authors = [{{ name = "data-world task" }}]
keywords = ["data-analysis", "causal-inference", "pandas", "chinese"]

[metadata]
difficulty_explanation = {difficulty_explanation}
category = "data-analysis"
difficulty = "{difficulty}"

[verifier]
timeout_sec = 1200.0
user = "verifier"

[agent]
timeout_sec = 3600.0
user = "agent"

[environment]
build_timeout_sec = 600.0
cpus = 2
memory_mb = 8192
storage_mb = 10240
allow_internet = true
"""

DOCKERFILE = """\
# Data-World 任务镜像（自包含）：数据在任务目录 environment/data/，
# 构建时 COPY 进 /app/data/，不依赖世界基础镜像。
# 防泄漏：白名单只 COPY data/，WORLD.md/.build/dag.json 绝不进镜像。
# 基础镜像用平台沙箱已预热的 codelab 镜像（ubuntu:24.04 在 dind 集群无预热，
# 会 RouteTagNotSatisfiedErr）；下面的 apt/pip 步骤全部做了存在性探测，
# 基础镜像里已有的不会重装。
# 2026-08-20：FROM 定为 registry 前缀短名——custom_prod 长路径在 dind-v4 路由不到
# 预热镜像（RouteTagNotSatisfiedErr），裸短名构建上下文无法定位，带
# registry-longcat.sankuai.com 前缀的短名是平台认可的形式。
FROM registry-longcat.sankuai.com/mtai_training_codelab_skiilsbench_1.0.0_fa67bf07:latest

# 内部镜像源 —— 构建期无公网。Ubuntu 24.04 (noble) 用 deb822 的
# /etc/apt/sources.list.d/ubuntu.sources；内部 PyPI 必须走 https。
ARG APT_MIRROR=http://mirrors.sankuai.com/ubuntu/
ARG PIP_INDEX=https://pip.sankuai.com/simple
ARG PIP_TRUSTED_HOST=pip.sankuai.com

# apt 换源只在 deb822 文件存在时做；python3/pip3 已存在则跳过安装。
RUN set -eux; \\
    if command -v apt-get >/dev/null 2>&1; then \\
        if [ -f /etc/apt/sources.list.d/ubuntu.sources ]; then \\
            sed -i "s|http://archive.ubuntu.com/ubuntu/|${APT_MIRROR}|g; \
s|http://security.ubuntu.com/ubuntu/|${APT_MIRROR}|g" \\
                /etc/apt/sources.list.d/ubuntu.sources; \\
        fi; \\
        if ! command -v python3 >/dev/null 2>&1 || ! command -v pip3 >/dev/null 2>&1; then \\
            apt-get update -qq; \\
            apt-get install -y -qq --no-install-recommends python3 python3-pip; \\
            rm -rf /var/lib/apt/lists/*; \\
        fi; \\
    fi

# 只装答题者的分析依赖；verifier 自身零第三方依赖（judge 走 stdlib urllib）。
# --break-system-packages 是新 pip 的 PEP668 开关，旧 pip 不认识就回落。
RUN set -eux; \\
    pip3 install --no-cache-dir --break-system-packages \\
        -i "${PIP_INDEX}" --trusted-host "${PIP_TRUSTED_HOST}" \\
        pandas pyarrow openpyxl scipy statsmodels h5py pypdf pillow \\
    || pip3 install --no-cache-dir \\
        -i "${PIP_INDEX}" --trusted-host "${PIP_TRUSTED_HOST}" \\
        pandas pyarrow openpyxl scipy statsmodels h5py pypdf pillow

WORKDIR /app

# task.toml 声明了 [agent]/[verifier] 的 user 字段，平台会切到对应用户执行，
# 镜像里必须真建出来；/app 放开写权限（agent 写 answer.txt，verifier 兜底写占位）。
RUN set -eux; \\
    id -u agent >/dev/null 2>&1 || useradd -m agent; \\
    id -u verifier >/dev/null 2>&1 || useradd -m verifier; \\
    mkdir -p /app/data; \\
    chmod 777 /app

COPY data/ /app/data/

# 数据对非 root 用户可读
RUN chmod -R a+rX /app/data

# 构建期断言：环境内不得出现 canonical truth 物料
RUN test ! -e /app/WORLD.md && test ! -e /app/dag.json && test ! -e /app/.build \\
    && test -d /app/data
"""

TEST_SH = f"""\
#!/bin/bash
# Data-World harbor verifier 入口：判 {ANSWER_PATH}，产物 reward.txt + details.json。
# judge 凭据由同目录 judge.env 提供（见下方 source 逻辑）；
# 任何失败都兜底写 reward 0 并 exit 0——verifier 非零退出会被 harbor 当作基础设施故障。
set -euo pipefail

ANSWER_PATH="${{ANSWER_PATH:-{ANSWER_PATH}}}"
REWARD_PATH="${{REWARD_PATH:-/logs/verifier/reward.txt}}"
DETAILS_PATH="${{DETAILS_PATH:-/logs/verifier/details.json}}"
ARTIFACT_DIR="${{ARTIFACT_DIR:-/logs/artifacts}}"
# 判分脚本与 rubric 与本文件同目录：容器里是 /tests/，本机复核时是导出目录里的
# tests/。用脚本自身位置定位而非硬编码 /tests，同一份 test.sh 两处都能跑。
TESTS_DIR="${{TESTS_DIR:-$(cd "$(dirname "${{BASH_SOURCE[0]}}")" && pwd)}}"
export ANSWER_PATH REWARD_PATH DETAILS_PATH
export RUBRIC_PATH="${{RUBRIC_PATH:-$TESTS_DIR/rubric.json}}"

# judge 凭据随任务目录走（评测平台没有运行期注入通道，见 judge.env）。
if [ -f "$TESTS_DIR/judge.env" ]; then
  set -a; . "$TESTS_DIR/judge.env"; set +a
fi

mkdir -p "$ARTIFACT_DIR"

if [ ! -f "$ANSWER_PATH" ] || [ ! -s "$ANSWER_PATH" ]; then
  mkdir -p "$(dirname "$ANSWER_PATH")"
  echo "[verifier] $ANSWER_PATH missing or empty after the agent run. Placeholder for artifact retention only." > "$ANSWER_PATH"
fi
# 运行器可能已把 ANSWER_PATH 收进 ARTIFACT_DIR；cp 源==目标会报错。
if [ "$ANSWER_PATH" != "$ARTIFACT_DIR/answer.txt" ]; then
  cp -f "$ANSWER_PATH" "$ARTIFACT_DIR/answer.txt"
fi

# judge 网关在内网：清掉公网出口代理，否则请求被拦。
unset http_proxy https_proxy HTTP_PROXY HTTPS_PROXY all_proxy ALL_PROXY || true

# grade.py 自身在任何失败下都会写 reward.txt + details.json（write_failure），
# 这里只做最后兜底。
set +e
python3 "$TESTS_DIR/grade.py" 2>&1
status=$?
set -e

if [ "$status" -ne 0 ]; then
  mkdir -p "$(dirname "$REWARD_PATH")" "$(dirname "$DETAILS_PATH")"
  echo 0 > "$REWARD_PATH"
  if [ ! -s "$DETAILS_PATH" ]; then
    printf '{{"reward": 0, "judge_error": "grade.py exited non-zero (status=%s) without writing details"}}\\n' \\
      "$status" > "$DETAILS_PATH"
  fi
fi
exit 0
"""

#: 拷进 ``tests/`` 的本子包模块（与本文件同目录）——判分代码是拷贝而非复刻，
#: 评分语义只有一个实现，不允许另写一份容器版。二者都不 import 本包
#: （``grade.py`` 有无包环境的 ImportError 分支），因此可以直接拷。
#:
#: ``prompts.py`` 不在此列：``grade.py`` 的无包分支按约定 import ``judge_prompt``，
#: 且 verifier 只需要 ``JUDGE_PROMPT`` 这一个常量。由 :func:`_write_judge_prompt`
#: 从同一个真值来源生成自包含模块，避免容器里多出一份可各自演化的提示词副本。
VERIFIER_MODULES = ("grade.py", "judge.py")


def _copy_data(src: Path, dst: Path) -> None:
    """只搬数据不碰元数据（DolphinFS 上非 owner 拷贝元数据会 EPERM）。"""
    for root, _dirs, files in os.walk(src):
        relative = Path(root).relative_to(src)
        (dst / relative).mkdir(parents=True, exist_ok=True)
        for name in files:
            shutil.copyfile(Path(root) / name, dst / relative / name)


def build_instruction(task: Dict[str, Any], env_dir: Path) -> str:
    """题面：背景与问题 + 答案写哪，不含任何 rubric 结论（check_no_leak 把关）。"""
    return "".join([
        task["task_prompt"].rstrip(),
        MAPPING_NOTE,
        f"\n\n把你的完整回答写入 `{ANSWER_PATH}`。\n",
    ])


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _write_judge_prompt(path: Path) -> None:
    """把 ``prompts.JUDGE_PROMPT`` 写成容器可直接 import 的自包含模块。

    生成而非手抄：判分提示词仍只在 ``prompts.py`` 定义一次，导出时序列化过去，
    两边不可能各自演化。
    """
    path.write_text(
        '"""判分提示词（由 task/export/harbor.py 从 task/export/prompts.py 自动生成，勿手改）。"""\n\n'
        f"JUDGE_PROMPT = {JUDGE_PROMPT!r}\n",
        encoding="utf-8")


def export_task(world: str, task: Dict[str, Any], env_dir: Path,
                out_root: Path) -> Tuple[Path, List[str]]:
    """导出一道题为 harbor 任务目录；返回 ``(目录, 泄漏警告)``。"""
    task_id = task["task_id"]
    task_dash = task_id.replace("_", "-")
    layer = task_id.rsplit("_", 1)[-1]
    kind = task["task_kind"]
    kind_zh = _KIND_ZH.get(kind, kind)

    instruction = build_instruction(task, env_dir)
    leaks = check_no_leak(world, task, instruction)

    task_dir = out_root / f"{TASK_PREFIX}-{world}-{task_dash}"
    if task_dir.exists():
        shutil.rmtree(task_dir)
    (task_dir / "environment").mkdir(parents=True)
    (task_dir / "tests").mkdir()
    # judge.env 含活 token、rubric.json 只该给判分侧看：任务目录整体 0700，
    # 共享机器上其他用户进不来。tests/ 与其内容保持常规 0755/0644——harbor
    # 上传 tests/ 用 tar/docker cp，属主与权限位原样保留进容器，而容器内
    # verifier 用户的 uid 与导出者不同，judge.env 若 0600 会让 verifier
    # 读不到它（test.sh 开头 source 失败，整题判分中断）。
    task_dir.chmod(0o700)

    if env_dir.is_dir():
        _copy_data(env_dir, task_dir / "environment" / "data")
    else:
        raise TaskGenError(f"{world}/{task_id}: 找不到 env 数据目录 {env_dir}")

    (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")
    (task_dir / "task.toml").write_text(TASK_TOML.format(
        answer=ANSWER_PATH, prefix=TASK_PREFIX, world=world, task_dash=task_dash,
        description=json.dumps(
            f"{kind_zh}分析：{task['target_node']}（data-world 世界 {world}）",
            ensure_ascii=False),
        difficulty_explanation=json.dumps(
            f"第 {layer} 层{kind_zh}分析题：需在 {world} 世界的多套独立数据系统中完成"
            f"数据清洗、关联与计算，并给出有 {DATA_DIR}/ 文件证据支撑的"
            f"{_KIND_CONCLUSION.get(kind, '分析')}结论。", ensure_ascii=False),
        difficulty=LAYER_DIFFICULTY.get(layer, "hard"),
    ), encoding="utf-8")
    (task_dir / "environment" / "Dockerfile").write_text(DOCKERFILE, encoding="utf-8")

    package_dir = Path(__file__).resolve().parent
    for name in VERIFIER_MODULES:
        shutil.copyfile(package_dir / name, task_dir / "tests" / name)
    _write_judge_prompt(task_dir / "tests" / "judge_prompt.py")
    (task_dir / "tests" / "test.sh").write_text(TEST_SH, encoding="utf-8")
    _make_executable(task_dir / "tests" / "test.sh")
    # 显式 0644 而非依赖 umask：容器内 verifier 用户与导出者 uid 不同，
    # umask 077 下默认的 0600 会让 verifier 读不到 judge.env（见上）。
    judge_env_path = task_dir / "tests" / "judge.env"
    judge_env_path.write_text(_judge_env(), encoding="utf-8")
    judge_env_path.chmod(0o644)
    (task_dir / "tests" / "rubric.json").write_text(
        json.dumps(build_rubric(world, task), ensure_ascii=False, indent=1),
        encoding="utf-8")
    return task_dir, leaks


def write_dataset_input(task_dirs: Sequence[Path], out_root: Path) -> None:
    """harbor 的数据集清单：每个任务目录一条 example。"""
    examples = [{"input": str(path.resolve()), "target": [""]} for path in task_dirs]
    (out_root / "synthesis_data_input.json").write_text(
        json.dumps({"examples": examples}, ensure_ascii=False, indent=4) + "\n",
        encoding="utf-8")


def export(args: argparse.Namespace) -> int:
    """批量导出；返回泄漏警告数（CLI 退出码用）。"""
    tasks_root = Path(args.tasks)
    if not tasks_root.is_dir():
        raise TaskGenError(f"出题产物根目录不存在: {tasks_root}")
    worlds = args.worlds_list or sorted(
        path.name for path in tasks_root.iterdir()
        if path.is_dir() and (path / "tasks.json").is_file())
    if not worlds:
        raise TaskGenError(f"{tasks_root} 下没有 tasks.json")
    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    task_dirs: List[Path] = []
    all_leaks: List[str] = []
    for world in worlds:
        tasks_path = tasks_root / world / "tasks.json"
        if not tasks_path.is_file():
            raise TaskGenError(f"{world}: 找不到 {tasks_path}")
        try:
            payload = json.loads(tasks_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise TaskGenError(f"{world}: tasks.json 不是合法 JSON: {exc}") from exc
        env_dir = resolve_env_dir(tasks_root, world, getattr(args, "world_root", None))
        count = 0
        for task in payload.get("tasks", []):
            if args.task_ids and task.get("task_id") not in args.task_ids:
                continue
            task_dir, leaks = export_task(world, task, env_dir, out_root)
            task_dirs.append(task_dir)
            all_leaks += leaks
            count += 1
        print(f"  {world}: {count} 个任务", flush=True)

    write_dataset_input(task_dirs, out_root)
    print(f"[export] {len(task_dirs)} 个任务 → {out_root}", flush=True)
    if all_leaks:
        print(f"[export] 泄漏警告 {len(all_leaks)} 条：", flush=True)
        for problem in all_leaks[:20]:
            print(f"  - {problem}", flush=True)
    return len(all_leaks)
