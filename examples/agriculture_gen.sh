#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m data_world \
  --dag examples/agriculture_dag.json \
  --output outputs/agriculture-world \
  --persona "农业经营分析师"
