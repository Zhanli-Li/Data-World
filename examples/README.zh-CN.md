# Examples

**中文** | [English](README.md)

两组最小 DAG 和对应运行脚本：

```bash
bash examples/agriculture_gen.sh
bash examples/ecommerce_gen.sh
```

脚本从仓库根目录启动 `python -m data_world`，模型、端点和 Token 读取本地 `.env`，不会硬编码内部模型路径。目标输出位于 `outputs/`；目录已存在时请按需在命令中显式添加 `--resume` 或 `--force`。
