"""Task 命令行入口。

task 是 data-world 的**独立后置工具**：只消费已建成的世界目录，不参与世界生成，
两者仅靠 ``<world>/`` 这一个接口面相连（与 dag_factory 靠 ``dag.json`` 前置相连
互为镜像）。两个阶段各自独立可跑；synth 可断点续跑，export 幂等重建：

    # 出题：世界 → tasks.json（世界目录只读，绝不被改写）
    python -m task synth --worlds ./outputs/worlds --out ./tasks

    # 导出：tasks.json → harbor 基准任务目录
    python -m task export --tasks ./tasks --out ./benchmark

错误边界只在这里：预期错误（``TaskGenError``）转成非零退出码 + 简短提示，
不打印堆栈；其余异常照常抛出（约定同 ``data_world.cli`` / ``dag_factory.cli``）。
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import List, Optional

from data_world.envfile import load_env

from . import TaskGenError
from .export import export
from .synth import SYNTH_RETRIES, synth
from .runner import HARD_TIMEOUT_SEC, IDLE_TIMEOUT_SEC, StreamingClaudeRunner

# 尽早加载 .env：让下方各参数的 default=os.environ.get(...) 能接住 .env 里的值。
load_env()


def _split_csv(value: Optional[str]) -> Optional[List[str]]:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _add_session_args(parser: argparse.ArgumentParser) -> None:
    """Claude Code 会话参数；模型与端点由各阶段单独声明。"""
    parser.add_argument("--claude-bin", default=os.environ.get("CLAUDE_BIN", "claude"),
                        help="claude CLI 可执行文件")
    parser.add_argument("--max-turns", type=int, default=1000,
                        help="单次会话最大轮次（宽松上限：真正的兜底是 --idle-timeout "
                             "与 --hard-timeout，轮次不该成为约束）")
    parser.add_argument("--idle-timeout", type=float, default=IDLE_TIMEOUT_SEC,
                        help="流式事件之间的最大空档秒数（不是会话总时长）")
    parser.add_argument("--hard-timeout", type=float, default=HARD_TIMEOUT_SEC,
                        help="单次会话总时长上限（秒）")
    parser.add_argument("--sdk-retries", type=int, default=10,
                        help="瞬时传输错误的重试次数")
    parser.add_argument("--retry-base-delay", type=float, default=20.0,
                        help="瞬时错误首次重试前等待秒数；后续指数退避")
    parser.add_argument("--retry-max-delay", type=float, default=600.0,
                        help="瞬时错误单次退避等待上限秒数")
    parser.add_argument("-v", "--verbose", action="store_true", help="打印会话进度")


def _add_model_args(parser: argparse.ArgumentParser) -> None:
    """出题 agent 的模型/端点参数。"""
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL"),
                        help="出题 agent 的模型")
    parser.add_argument("--endpoint", default=os.environ.get("ANTHROPIC_BASE_URL"),
                        help="agent 端点（claude CLI 的 ANTHROPIC_BASE_URL）")
    # token 在构造 runner 时再从环境解析，避免 ArgumentDefaultsHelpFormatter
    # 把真实值打印进 ``task synth --help``。
    parser.add_argument("--auth-token", default=None,
                        help="agent 端点 token（默认读 ANTHROPIC_AUTH_TOKEN）")
    _add_session_args(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="task",
        description="把 data-world 建成的数据世界编译成带评分细则的分析题",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    subparsers = parser.add_subparsers(dest="stage", required=True)

    # ── synth ────────────────────────────────────────────────────────────────
    synth_parser = subparsers.add_parser(
        "synth", help="出题：<world>/ → tasks.json",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    synth_parser.add_argument("--worlds", required=True,
                              help="世界目录，或包含多个世界目录的根目录")
    synth_parser.add_argument("--out", required=True, help="出题产物根目录")
    synth_parser.add_argument("--limit", type=int, help="只处理前 N 个世界")
    synth_parser.add_argument("--task-id", dest="task_ids", action="append",
                              help="只出指定 task_id（可重复，如 cause_1）")
    synth_parser.add_argument("--retries", type=int, default=SYNTH_RETRIES,
                              help="产物非法时的重试次数（校验错误回喂）")
    synth_parser.add_argument("--force", action="store_true",
                              help="忽略已有产物，全部重出（默认断点续跑）")
    synth_parser.add_argument("--no-keep-copy", action="store_true",
                              help="出题后删掉只读世界副本（默认保留以便复核 rubric 数值）")
    _add_model_args(synth_parser)

    # ── export ───────────────────────────────────────────────────────────────
    export_parser = subparsers.add_parser(
        "export", help="导出：tasks.json → harbor 任务目录",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    export_parser.add_argument("--tasks", required=True, help="出题产物根目录")
    export_parser.add_argument("--out", required=True, help="harbor 任务输出目录")
    export_parser.add_argument("--world", dest="worlds_list", type=_split_csv,
                               help="只导出这些世界（逗号分隔）")
    export_parser.add_argument("--task-id", dest="task_ids", action="append",
                               help="只导出指定 task_id（可重复）")
    export_parser.add_argument("--world-root",
                               help="世界根目录；出题产物里没有 world_copy 时（沙箱出题用 "
                                    "--no-keep-copy）从这里取 <world>/env")
    return parser


def _make_runner(args: argparse.Namespace) -> StreamingClaudeRunner:
    """构造流式 agent 运行器。

    出题是长会话，一律走流式：超时判定基于事件空档而非整段生成时长，
    模型不必为了规避网关超时而压缩输出。
    """
    model = args.model
    auth_token = args.auth_token or os.environ.get("ANTHROPIC_AUTH_TOKEN")
    if not model or not model.strip():
        raise TaskGenError("未指定模型：用 --model 或在 .env 里设 ANTHROPIC_MODEL")
    model = model.strip()
    return StreamingClaudeRunner(
        executable=args.claude_bin, model=model,
        endpoint=args.endpoint, auth_token=auth_token,
        timeout=int(args.hard_timeout), max_turns=args.max_turns,
        retries=args.sdk_retries, verbose=args.verbose,
        retry_base_delay=args.retry_base_delay,
        retry_max_delay=args.retry_max_delay,
        idle_timeout=args.idle_timeout, hard_timeout=args.hard_timeout,
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.stage == "synth":
            return 1 if synth(args, _make_runner(args)) else 0
        if args.stage == "export":
            export(args)  # 泄漏警告不算失败：需要人看一眼，但不阻塞导出
            return 0
    except TaskGenError as exc:
        print(f"[task] 失败: {exc}")
        return 2
    except KeyboardInterrupt:
        print("[task] 已中断（产物可断点续跑）")
        return 130
    parser.error(f"未知阶段 {args.stage!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
