# Scripts

[中文](README.zh-CN.md) | **English**

Shell orchestration layer for batch runs; the business logic for a single DAG / world stays with the Python CLIs.

```bash
scripts/check_repo.sh
scripts/generate_dags_concurrent.sh --help
scripts/generate_worlds_concurrent.sh --help
```

`check_repo.sh` runs offline compilation, unit tests, CLI smoke tests for all three CLIs, and example-JSON plus shell-syntax checks, isolating Python caches in a temporary directory. Both generation scripts support `--dry-run`; use it to verify the input directories, concurrency, and output paths before a real run. Generated artifacts go to `dags/` or `outputs/`; do not write run logs into this directory.
