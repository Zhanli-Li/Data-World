# Data-World 用法文档

**中文** | [English](README.md)

按组件各一篇，覆盖完整用法与契约：

- [`dag.md`](dag.zh-CN.md)：DAG 输入格式、`dag_factory` 生成 DAG（单张 / 领域批量 / persona 文件）、并发编排脚本
- [`data-world.md`](data-world.zh-CN.md)：`data_world` 完整用法——参数表、输出目录契约、断点续跑、自定义端点、轨迹采集、失败与重试语义
- [`task.md`](task.zh-CN.md)：`task` 的出题（`synth`）、Harbor 导出（`export`）

深入阅读：

- [`task/README.md`](../task/README.zh-CN.md)：rubric 设计理由与不变量（改出题逻辑前必读）
- [`AGENTS.md`](../AGENTS.md)：架构约束与 13 条核心不变量（改代码前必读）
- 根目录 [`README.md`](../README.zh-CN.md)：项目概览、安装与 Quick Start

## 代码结构

```text
data_world/
├── __init__.py     # WorldGenError
├── __main__.py     # python -m data_world
├── cli.py          # CLI 与错误边界
├── pipeline.py     # 主编排器、进度和阶段校验
├── dag.py          # DAG 规范化、校验和图查询
├── fs.py           # 文件系统、路径安全、JSON 与哈希
├── prompts.py      # 模型提示词和产物协议
├── sdk.py          # Claude Code 子进程适配 + OpenAI 兼容单次调用与瞬时重试
└── trajectory.py   # 可选 CCR 生命周期与轨迹解析

dag_factory/        # 独立前置工具：persona → 领域路由 → 生成因果 DAG
├── __init__.py     # DagFactoryError
├── __main__.py     # python -m dag_factory
├── cli.py          # CLI：单张 / 领域批量 / persona 文件三种模式
├── categories.py   # 16 个数据分析领域及关键词兜底路由
├── factory.py      # 领域路由 + LLM 出图 + 复用 data_world.dag 校验重试
├── envfile.py      # 零依赖 .env 与 persona jsonl 加载器
└── llm.py          # OpenAI 兼容单次调用适配

task/               # 独立后置工具：出题 / Harbor 导出（结构见其 README）
scripts/            # 并发编排脚本（生成 DAG / 生成 World）
examples/           # 可直接运行的示例 DAG 与脚本
tests/              # 不依赖外部模型服务的关键回归测试
data/               # 本地 persona 等输入数据（Git 默认忽略，不随仓库分发）
outputs/            # 生成的数据世界产物（Git 默认忽略）
```

设计上避免通用 `utils.py` 和过细的 `stages/` 模块：底层能力按职责命名，阶段实现及其校验集中在 `pipeline.py`，以减少状态在多层模块间跳转。`dag_factory` 与主流程解耦，仅通过产出的 `dag.json` 与 data_world 相连，且复用 `data_world.dag` 的校验以保证口径一致。

## 开发与验证

安装 Python 依赖后，在仓库根目录（包含 `pyproject.toml` 的目录）运行：

```bash
bash scripts/check_repo.sh
```

检查脚本使用 `python` 命令；请先激活 Python 环境，确保该命令可用。检查包含编译、单元测试、CLI 冒烟、示例 JSON 校验与 shell 语法检查，不调用外部模型服务。

改代码前请阅读 [`AGENTS.md`](../AGENTS.md)，了解模块职责、产物契约与核心不变量。回归测试覆盖范围及单独运行命令见 [`tests/README.md`](../tests/README.zh-CN.md)。修改 CLI 行为或产物契约时，应同步更新中英文文档。

生成阶段会执行模型编写的代码；请使用隔离环境，并在发布或用于重要决策前检查生成产物。
