# DAG 格式与 dag_factory

**中文** | [English](dag.md)

DAG 是数据世界的**因果骨架**：`dag_factory` 负责生成它，`data_world` 负责消费它。两者的校验与拓扑分层复用同一份代码（`data_world.dag`），接受口径完全一致。

## DAG 输入格式

输入必须是一个 JSON object，包含非空 `nodes` 和 `edges` 数组。

最小示例：

```json
{
  "nodes": [
    {
      "label": "weather",
      "system": "weather station",
      "grain": "station-day",
      "variables": ["temperature", "rainfall"]
    },
    {
      "label": "crop_yield",
      "system": "farm survey",
      "grain": "farm-season",
      "variables": ["yield", "harvest_area"]
    }
  ],
  "edges": [
    {
      "src": "weather",
      "dst": "crop_yield",
      "note": "适宜温度和降水提高作物产量，但存在阈值：高温超过作物耐受点后产量转为下降（倒U）"
    }
  ]
}
```

节点约束：

- 节点标识可使用 `label`、`id` 或 `name`，运行时统一为 `label`。
- 每个节点必须包含非空 `variables`；`fields` 可作为兼容别名。
- `system` 和 `grain` 推荐显式提供，缺失时只会填入待规划占位值。

边约束：

- 端点可使用 `src/dst`、`source/target` 或 `from/to`。
- 每条边必须提供非空 `note`，用一句话如实描述该边的真实因果关系（可非线性/阈值/倒U/交互/滞后，不用 +/- 简化符号）；`mechanism` 可作为兼容别名。
- 不允许自环、重复边、悬空端点或任何有向环。

> 世界的标题、背景与范围（`world_identity`）由 contract 阶段的 agent 从 DAG 的 `name`/`description` 及各节点语义按第一性原理内生推断，无需人工传入。因此 DAG 应带有信息充分的 `name` 与 `description`——它们是世界身份的主要语义来源。

## 生成 DAG（dag_factory）

`dag_factory` 是 data_world 的**独立前置工具**：把一个分析者画像（persona）路由到某个数据分析领域，再让模型生成一张**纯净的领域因果 DAG**。它不进入 pipeline，唯一连接点就是产出的 `dag.json`——交给 `python -m data_world --dag` 消费。

三种使用模式：

```bash
# 1. 单张：指定领域号，或让它按 persona 自动路由
python -m dag_factory --category 4 --persona "供应链分析师" --out dags/ops.json

# 2. 领域批量：在若干领域上各铺 N 张
python -m dag_factory --batch --categories 1,2,3,4,5,6 --per-category 3 --out-dir dags

# 3. persona 文件：逐条按 persona 路由领域，每 persona 一张
python -m dag_factory --persona-file your_personas.jsonl --limit 200 --out-dir dags
```

`--persona-file` 接收每行一个 `{"persona": "..."}` 的 jsonl（需自行准备）；`--limit` 只取前 N 条，`--shuffle` 按 `--seed` 打乱后再取。若行内带整型 `index` 字段（分片脚本会透传），输出文件按 `p{index:06d}.json` 命名——同一 persona 重跑必落同一文件，天然可断点续跑、分片间去重。

### 常用参数（dag_factory）

| 参数 | 默认值 | 说明 |
|---|---:|---|
| `--persona` | 内置池随机 | 分析者画像；缺省时从内置池随机取 |
| `--category` | 无 | 领域编号 1..16；缺省则按 persona 路由 |
| `--out` | 无 | 单张模式输出路径 |
| `--batch` | false | 领域批量模式 |
| `--per-category` | `1` | 领域批量模式每个领域生成几张 |
| `--categories` | 全部 16 类 | 领域批量模式限定领域，逗号分隔 |
| `--persona-file` | 无 | persona jsonl；逐条路由出图，每 persona 一张 |
| `--limit` | 全部 | `--persona-file` 模式只取前 N 条 |
| `--shuffle` | false | `--persona-file` 模式按 `--seed` 打乱后再取 |
| `--out-dir` | 无 | 批量 / persona 模式输出目录 |
| `--attempts` | `5` | 单张 DAG 的校验失败重试上限 |
| `--seed` | `20260712` | 随机种子（persona / 领域轮询、打乱） |

查看准确的完整参数：

```bash
python -m dag_factory --help
```

## 并发脚本

`scripts/` 提供两个并发编排脚本，把上面的单条命令铺开到多进程：

```bash
# 只生成 DAG：领域模式，或 persona 分片模式（给 --persona-file 时把 jsonl 按 --workers 切片并发）
scripts/generate_dags_concurrent.sh --categories 1-4 --per-category 2 --workers 4
scripts/generate_dags_concurrent.sh --persona-file your_personas.jsonl --limit 200 --workers 8

# 扫描已有 DAG 并发生成 World（已完整的跳过，未完成的自动 --resume）
scripts/generate_worlds_concurrent.sh --dags-dir dags/concurrent --workers 2

# 逐 DAG 画像 + 轨迹采集：--persona-map 按 dag_file 覆盖 --persona；--ccr-bin 指定 CCR 可执行文件
scripts/generate_worlds_concurrent.sh --dags-dir dags/concurrent --workers 20 \
  --persona-map your_persona_map.jsonl --log-trajectories --ccr-bin /path/to/ccr
```

两个脚本都支持 `--dry-run` 先打印计划、`--force` 覆盖重建，详见各自的 `--help` 和 [`scripts/README.md`](../scripts/README.zh-CN.md)。
