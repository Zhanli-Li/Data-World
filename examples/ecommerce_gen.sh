#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root"

# Forward extra CLI options, such as --resume or --model; never overwrite implicitly.
exec python3 -m data_world \
  --dag examples/ecommerce_dag.json \
  --output outputs/ecommerce-world \
  --persona "电商增长与体验分析师" \
  "$@"
