#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m data_world \
  --dag examples/ecommerce_dag.json \
  --output outputs/ecommerce-world \
  --persona "电商增长与体验分析师"
