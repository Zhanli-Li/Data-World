# dag_factory

**中文** | [English](README.md)

独立前置工具：把 persona 或领域约束编译为经过 `data_world.dag` 校验的因果 DAG。

```bash
python -m dag_factory --help
```

它与主流程只通过 `dag.json` 连接，不应 import `data_world.pipeline`，也不应把生成 DAG 的状态写入 world 目录。批量编排脚本位于 [`scripts/`](../scripts/README.zh-CN.md)。
