# Task export

**中文** | [English](README.md)

负责 Harbor 导出：把 `tasks.json` 逐题导出为自包含的 harbor 基准任务目录。判分计算只由 `grade.py` 拥有；判分资产（`grade.py` / `judge.py` / 由 `JUDGE_PROMPT` 生成的 `judge_prompt.py`）随导出逐字节拷进任务目录的 `tests/`，容器与本包同源。详细契约见上级 [`README.md`](../README.zh-CN.md)。
