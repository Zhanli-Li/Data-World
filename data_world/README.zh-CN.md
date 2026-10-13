# data_world

**中文** | [English](README.md)

核心数据世界编译器：校验因果 DAG，生成统一 canonical state，将其投影为分析者可见的 `env/`，最后审计并生成 `WORLD.md`。

```bash
python -m data_world --help
```

模块职责和必须保持的不变量见仓库根目录的 [`AGENTS.md`](../AGENTS.md)，用户侧输入、输出与恢复语义见 [`doc/data-world.md`](../doc/data-world.zh-CN.md)。本包不依赖 `dag_factory` 或 `task`；两者只能单向复用这里的基础能力。
