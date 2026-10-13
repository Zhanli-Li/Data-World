from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional, Sequence

from . import WorldGenError
from .envfile import load_env
from .pipeline import DataWorldPipeline

# 尽早加载 .env：让下方各参数的 default=os.environ.get(...) 能接住 .env 里的值。
load_env()


def _default_ccr_bin() -> str:
    candidate = os.path.expanduser("~/.npm-global/bin/ccr")
    return candidate if os.path.isfile(candidate) else "ccr"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="data-world", description="从因果 DAG 编译统一、可复算的数据分析世界", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--dag", required=True, help="输入 DAG JSON 路径")
    parser.add_argument("--output", required=True, help="输出 world 目录；配合 --name 时为父目录")
    parser.add_argument("--name", help="可选 world 子目录名")
    parser.add_argument("--persona", default="通用数据分析师", help="最终分析受众；世界标题/背景/范围由 agent 从 DAG 内生推断")
    parser.add_argument("--seed", type=int, default=20260712)
    parser.add_argument("--model", default="ops", help="Claude Code agent 阶段的模型别名")
    parser.add_argument("--endpoint", default=os.environ.get("ANTHROPIC_BASE_URL"),
                        help="agent 与 contract 阶段共用的 base_url；contract 阶段会自动剥去末尾 /anthropic")
    parser.add_argument("--auth-token", default=os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    parser.add_argument("--contract-model", default=os.environ.get("ANTHROPIC_MODEL", "aws.claude-opus-4.8"),
                        help="World Contract 阶段单次 OpenAI 兼容调用使用的模型")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument("--max-turns", type=int, default=300)
    parser.add_argument("--timeout", type=int, default=21600)
    parser.add_argument("--sdk-retries", type=int, default=10)
    parser.add_argument("--stage-retries", type=int, default=5)
    parser.add_argument("--log-trajectories", action="store_true",
                        help="记录完整模型对话轨迹到 <world>/trajectories/<阶段>.jsonl（SDK 阶段经 CCR 代理采集）")
    parser.add_argument("--ccr-bin", default=os.environ.get("CCR_BIN") or _default_ccr_bin(),
                        help="claude-code-router 可执行文件（轨迹采集用）")
    parser.add_argument("--trajectory-model", default=os.environ.get("ANTHROPIC_MODEL", "aws.claude-opus-4.8"),
                        help="CCR provider 转发 SDK 阶段请求所用的真实模型名")
    parser.add_argument("--ccr-port", type=int, default=3456, help="CCR 代理本地端口")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true")
    mode.add_argument("--force", action="store_true")
    parser.add_argument("--verbose", action="store_true", default=True)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_turns < 1:
        raise SystemExit("turns 参数必须 >= 1")
    try:
        DataWorldPipeline(args).run()
        return 0
    except (WorldGenError, json.JSONDecodeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
