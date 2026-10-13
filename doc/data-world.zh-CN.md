# data_world 完整用法

**中文** | [English](data-world.md)

`data_world` 是核心编译器：校验因果 DAG，生成统一 canonical state，将其投影为分析者可见的 `env/`，最后审计并生成 `WORLD.md`。基本用法见根目录 [README](../README.zh-CN.md) 的 Quick Start；本文覆盖完整参数与契约。

## 自定义端点

```bash
python -m data_world \
  --dag ./dag.json \
  --output ./world \
  --model ops \
  --endpoint "https://your-endpoint.example/v1/anthropic"
```

`--endpoint` 会按本次运行向 Claude Code 注入 `ANTHROPIC_BASE_URL`；未提供时默认取环境变量 `ANTHROPIC_BASE_URL`。鉴权默认读取 `ANTHROPIC_AUTH_TOKEN`。World Contract 阶段复用同一端点与鉴权，并自动剥去末尾的 `/anthropic` 后缀以适配 OpenAI 兼容接口，模型由 `--contract-model` 指定（默认取 `ANTHROPIC_MODEL`）。Claude 子进程只接收本次模型调用所需的 Token，不继承 judge、训练平台、云账户或 SSH agent 等无关凭据。

## 多个 world 的输出布局

如果 `--output` 是多个 world 的父目录，可以使用 `--name` 创建经过安全规范化的子目录：

```bash
python -m data_world \
  --dag examples/ecommerce_dag.json \
  --output outputs \
  --name ecommerce-world
```

## 恢复和覆盖

输出目录非空时，必须明确选择恢复或重建：

```bash
# 从 .build/progress.json 恢复已提交阶段
python -m data_world --dag ./dag.json --output ./world --resume

# 删除输出目录并从头重建
python -m data_world --dag ./dag.json --output ./world --force
```

`--resume` 会校验输入 DAG 的 SHA256 和拓扑层；DAG 被修改后不能继续复用旧进度。每个成功阶段都会原子更新 `.build/progress.json`；续跑时已完成阶段重新校验并跳过，未完成阶段继续执行。

## 轨迹采集（`--log-trajectories`）

开启后会把每个阶段的完整模型对话写到 `<world>/trajectories/<阶段>.jsonl`（可直接用于 SFT）。SDK 阶段（layer / projection / audit）通过本地 CCR 代理采集逐轮 tool-use，需要先安装并验证 CCR：

```bash
npm install -g @musistudio/claude-code-router@3.0.20
ccr --help
```

普通生成不需要安装 CCR。如果 `ccr` 不在 `PATH`，可在 `.env` 中设置 `CCR_BIN=/absolute/path/to/ccr`，也可通过 `--ccr-bin` 显式指定。CCR 由 pipeline 自动以 `serve` 模式启停（隔离的临时 HOME，不污染 `~/.claude-code-router`），请求/响应从 v3 的 SQLite 日志重组，本地代理端点已自动加入 `no_proxy`。上游 HTTP 429/5xx 默认重试 4 次；可用 `CCR_RETRY_COUNT` 调整（0–20）。

轨迹采集的运行时工具安装 home 可用环境变量 `DATA_WORLD_RUNTIME_HOME` 覆盖（默认取用户 home），用于把"工具安装目录"和每次运行的隔离 HOME 分开，避免重复安装 npm 包。

## npm 全局目录无写权限

如果 npm 全局目录没有写权限，可先配置用户级 prefix，确保其 `bin` 目录已加入 `PATH`（建议写入 shell 启动文件）：

```bash
npm config set prefix "$HOME/.npm-global"
export PATH="$HOME/.npm-global/bin:$PATH"
```

## 常用参数（data_world）

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--dag` | 必填 | 输入 DAG JSON |
| `--output` | 必填 | world 输出目录，或配合 `--name` 使用的父目录 |
| `--name` | 无 | 安全规范化后的 world 子目录名 |
| `--persona` | 通用数据分析师 | 最终分析受众；世界标题/背景/范围由 agent 从 DAG 内生推断 |
| `--seed` | `20260712` | 基础随机种子；第 \(i\) 层使用 `seed + i` |
| `--model` | `ops` | Claude Code agent 阶段模型名 |
| `--endpoint` | `ANTHROPIC_BASE_URL` | agent 与 contract 阶段共用 base_url；contract 阶段自动剥去 `/anthropic` |
| `--auth-token` | `ANTHROPIC_AUTH_TOKEN` | 自定义端点鉴权 token |
| `--contract-model` | `ANTHROPIC_MODEL` | World Contract 阶段单次 OpenAI 兼容调用的模型 |
| `--claude-bin` | `claude` | Claude Code 可执行文件 |
| `--max-turns` | `300` | 一般生成阶段最大 turns |
| `--timeout` | `21600` | 单次子进程/LLM 调用超时（秒） |
| `--sdk-retries` | `10` | 瞬时网络/服务错误重试次数 |
| `--stage-retries` | `5` | 阶段产物校验失败后的定向重试次数 |
| `--log-trajectories` | false | 采集各阶段完整对话到 `<world>/trajectories/`（SDK 阶段经 CCR） |
| `--ccr-bin` | `ccr`（`~/.npm-global/bin/ccr` 存在时优先） | claude-code-router 可执行文件 |
| `--trajectory-model` | `ANTHROPIC_MODEL` | CCR 转发 SDK 阶段请求所用真实模型名 |
| `--ccr-port` | `3456` | CCR 代理本地端口 |
| `--resume` | false | 恢复已有构建 |
| `--force` | false | 删除已有输出后重建 |

查看准确的完整参数：

```bash
python -m data_world --help
```

## 输出目录

```text
<world>/
├── dag.json
├── WORLD.md
├── env/
├── trajectories/            # 仅 --log-trajectories：contract.jsonl / layer_00.jsonl / ... / audit.jsonl
└── .build/
    ├── progress.json
    ├── world_contract.json
    ├── layers/
    │   ├── layer_00/
    │   │   ├── layer_result.json     # 机器可读账本：state_files / node_outputs / edge_checks
    │   │   ├── layer_00.md           # 这批状态的自述：设计理由、机制、取舍与下游提示
    │   │   └── ... canonical state
    │   └── ...
    ├── projection/
    │   ├── projection_result.json
    │   └── projection.md            # 投影自述：每个观测系统为何长这样、瑕疵背后的观测机制
    └── audit/
        └── audit_result.json
```

### 面向分析者的产物

- `env/`：真实分析现场可见的数据和 in-world 物料。
- `WORLD.md`：基于审计实测生成的世界说明、文件关系、质量机制、洞察与限制。
- `dag.json`：输入 DAG 的原始副本。

### 内部构建产物

`.build/` 保存恢复和审计所需的内部状态，包括 canonical state、生成脚本、参数、阶段结果和校验记录。它不属于分析者应直接使用的数据环境。

`env/` 中禁止出现：

- Python、Shell、Notebook 等过程脚本；
- latent/canonical truth；
- 构建 manifest、答案或审计报告；
- 空文件和 `__pycache__`。

## 失败与重试语义

项目区分两类重试：

- **SDK 重试**：`sdk.py`（`ClaudeRunner` 与 `LLMClient`）对限流、服务过载、连接重置、超时等瞬时错误进行指数退避。
- **Stage 重试**：`pipeline.py` 在模型已返回、但产物不满足结构或文件约束时，将具体校验错误反馈给下一次生成。

两类重试互不代偿：传输问题归 SDK，产物非法归 Stage（校验错误文本会回喂给下一次生成）。
