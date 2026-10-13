# 关键回归测试

**中文** | [English](README.md)

本目录只保留能保护核心契约的高价值、离线回归测试：

- `test_dag.py`：DAG 别名规范化、拓扑分层，以及重复节点/边、悬空端点、自环、缺失机制和有向环的拒绝逻辑。
- `test_sdk_security.py`：Claude 子进程的凭据隔离、命令行无 Token，以及 CCR 临时配置权限。
- `test_task_export_grade.py`：reward 量程契约（恒 [0,1]：触发负分项压到 0 不为负、缺条记 0、全负 rubric 兜底为 0）。

运行全部测试：

```bash
python -m unittest discover -s tests -v
```
