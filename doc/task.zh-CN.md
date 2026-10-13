# task：出题 / 导出

**中文** | [English](task.md)

`task` 是 data_world 的**独立后置工具**：只读取已经完成的 world，不进入世界生成 pipeline。它把数据世界编译成**带评分细则的分析题**——出题 agent 在只读世界副本里自由探索，交出「题面 + rubric」；导出阶段生成 Harbor 基准任务目录（判分在容器内进行）。

```text
<world>/ ──synth──▶ tasks.json ──export──▶ harbor 基准任务目录
```

## 两个阶段

```bash
# 出题：<world>/ → tasks.json（世界目录只读，绝不被改写）
python -m task synth --worlds ./outputs/worlds --out ./tasks -v

# 导出：tasks.json → harbor 基准任务目录
python -m task export --tasks ./tasks --out ./benchmark
```

断点续跑：`synth` 按 `tasks.json` 里已有的 task_id 跳过（采样是确定性的，重跑落到同一批目标）；`--force` 忽略已有产物全部重出；`--task-id` 只出指定题（可重复）。

共享机器上应通过环境变量（`.env` 的 `ANTHROPIC_*`）或运行平台的密钥机制注入凭据；`--auth-token` 等命令行 key 参数会进入 shell 历史，不应写进脚本。

## 出题的采样规则

给定 DAG 上的目标节点，能问的只有两件事：沿**入度**看「原因」（`cause`）、沿**出度**看「后果」（`consequence`）。采样只有一个维度：按**拓扑深度**分 K=4 层，每层各取该方向度数 Top-1 的节点，每世界最多 2K=8 道题（`cause_1..4` / `consequence_1..4`）。分层是为了避免全局 Top-K 退化成 hub 偏好；同度数按 `dag.json` 的节点顺序定序，保证重复采样落到同一批目标。

## 产物结构

```text
<tasks_out>/
└── <world>/
    ├── tasks.json              # 该世界全部题（题面 + rubric + 身份字段）
    ├── report.json             # n_tasks / n_failed / failures 摘要
    ├── world_copy/             # 只读世界副本（--no-keep-copy 时不留）
    └── <task_id>/
        └── run/                # 该题的 agent 工作目录
            ├── final_message.txt
            └── session.json
```

`env/` 的位置由一处解析（`contract.resolve_env_dir`）：先找出题留下的 `<tasks>/<world>/world_copy/env`，没有则回落 `--world-root` 指的 `<world_root>/<world>/env`。用 `--no-keep-copy` 出的题没有副本，`export` 必须传 `--world-root`：

```bash
python -m task export --tasks <out> --out <bench> --world-root outputs/worlds
```

## Harbor 导出

`export` 为每题生成一个自包含任务目录：`instruction.md`（题面 + `env/`→`/app/data/` 映射说明）、`task.toml`（超时/资源/artifacts）、`environment/Dockerfile`（可用 `--build-arg APT_MIRROR=... / PIP_INDEX=...` 换镜像源）、`environment/data/`（该世界 `env/` 的副本）、`tests/`（判分实现 + rubric + judge 凭据）。

判分代码是**拷贝而非复刻**：`tests/` 下的 `grade.py` / `judge.py` 直接拷本包源文件，`judge_prompt.py` 由同一个 `JUDGE_PROMPT` 生成——评分语义只有一个实现。防泄漏是双保险：导出时断言题面不含 rubric 文本（`check_no_leak`），镜像构建期再断言 `WORLD.md` / `dag.json` / `.build` 不存在。

judge 凭据随任务目录走（`tests/judge.env`）：评测平台没有运行期环境变量注入通道，凭据在导出时从 `.env` 读取写入，解析顺序与容器内 `judge.py` 一致（`JUDGE_*` 优先，回落 `ANTHROPIC_*`，见根目录 [`.env.example`](../.env.example)）。任务目录落盘即 0700，共享机器上其他用户读不到其中的 token 与 rubric。注意：把任务目录打包或上传即等于发布其中的 judge token，共享前请确认接收方可信。

## 深入阅读

rubric 的两节结构（`data_computation` / `insight_discovery`）、正分点「必要且确定」两关、负分项语义、防泄漏检查，以及每条设计背后的实测理由，见 [`task/README.md`](../task/README.zh-CN.md)。
