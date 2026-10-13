# dag_factory

[中文](README.zh-CN.md) | **English**

Independent upstream tool: compiles a persona or domain constraints into a causal DAG validated by `data_world.dag`.

```bash
python -m dag_factory --help
```

It connects to the main pipeline only through `dag.json`; it must not import `data_world.pipeline`, nor write DAG-generation state into world directories. The batch orchestration script lives in [`scripts/`](../scripts/README.md).
