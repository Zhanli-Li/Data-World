# Task export

[中文](README.zh-CN.md) | **English**

Owns the Harbor export: exports each task in `tasks.json` into a self-contained Harbor benchmark task directory. Score computation is owned solely by `grade.py`; the grading assets (`grade.py` / `judge.py` / `judge_prompt.py` generated from `JUDGE_PROMPT`) are byte-copied into each task directory's `tests/` on export, so the container and this package share the same source. The full contract is in the parent [`README.md`](../README.md).
