#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$repo_root"

# 把编译和 import 缓存放到临时目录，检查结束后不在仓库留下测试垃圾。
check_cache="$(mktemp -d)"
trap 'rm -r "$check_cache"' EXIT
export PYTHONPYCACHEPREFIX="$check_cache"

python -m compileall -q data_world dag_factory task data_world_gen.py tests
python -m unittest discover -s tests -v

python -m data_world --help >/dev/null
python -m dag_factory --help >/dev/null
python -m task --help >/dev/null

for file in examples/*.json; do
    python -m json.tool "$file" >/dev/null
done

for file in examples/*.sh scripts/*.sh; do
    [[ -f "$file" ]] && bash -n "$file"
done

echo "repository checks passed"
