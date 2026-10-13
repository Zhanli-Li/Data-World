# Key regression tests

[中文](README.zh-CN.md) | **English**

This directory keeps only high-value offline regression tests that protect core contracts:

- `test_dag.py`: DAG alias normalization, topological layering, and rejection of duplicate nodes/edges, dangling endpoints, self-loops, missing mechanisms, and directed cycles.
- `test_sdk_security.py`: credential isolation for Claude subprocesses, no token on the command line, and CCR temporary-config permissions.
- `test_task_export_grade.py`: the reward range contract (always [0,1]: triggered negative items clamp to 0 rather than going negative, missing verdicts score 0, an all-negative rubric falls back to 0).

Run all tests:

```bash
python -m unittest discover -s tests -v
```
