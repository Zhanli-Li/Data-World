#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root"

# Forward extra CLI options, such as --resume or --model; never overwrite implicitly.
exec python3 -m data_world \
  --dag examples/agriculture_dag.json \
  --output outputs/agriculture-world \
  --persona "农业经营分析师" \
  "$@"
