# Scripts

**中文** | [English](README.md)

面向批量运行的 Shell 编排层；单个 DAG / world 的业务逻辑仍由 Python CLI 拥有。

```bash
scripts/check_repo.sh
scripts/generate_dags_concurrent.sh --help
scripts/generate_worlds_concurrent.sh --help
```

`check_repo.sh` 运行离线编译、单元测试、三套 CLI smoke test、示例 JSON 和 Shell 语法检查，并把 Python 缓存隔离到临时目录。两个生成脚本都支持 `--dry-run`；正式运行前先用它核对输入目录、并发度和输出路径。生成物写入 `dags/` 或 `outputs/`，不应在本目录落运行日志。
