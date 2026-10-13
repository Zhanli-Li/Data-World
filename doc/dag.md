# DAG Format and dag_factory

[中文](dag.zh-CN.md) | **English**

The DAG is the **causal skeleton** of a data world: `dag_factory` generates it, and `data_world` consumes it. Both reuse the same code (`data_world.dag`) for validation and topological layering, so their acceptance criteria are exactly identical.

## DAG Input Format

The input must be a JSON object containing non-empty `nodes` and `edges` arrays.

Minimal example:

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

Node constraints:

- A node identifier may use `label`, `id`, or `name`; it is normalized to `label` at runtime.
- Every node must contain non-empty `variables`; `fields` is accepted as a compatibility alias.
- `system` and `grain` are recommended to be provided explicitly; when missing, only to-be-planned placeholder values are filled in.

Edge constraints:

- Endpoints may use `src/dst`, `source/target`, or `from/to`.
- Every edge must provide a non-empty `note` that truthfully describes, in one sentence, the edge's real causal relationship (non-linear / threshold / inverted-U / interaction / lag effects are allowed; do not use simplified +/- notation); `mechanism` is accepted as a compatibility alias.
- Self-loops, duplicate edges, dangling endpoints, and any directed cycles are not allowed.

> The world's title, background, and scope (`world_identity`) are endogenously inferred from first principles by the contract-stage agent from the DAG's `name`/`description` and the semantics of each node, with no manual input required. Therefore the DAG should carry a sufficiently informative `name` and `description` — they are the primary semantic source of the world's identity.

## Generating DAGs (dag_factory)

`dag_factory` is an **independent upstream tool** of data_world: it routes an analyst persona to a data analysis domain, then has the model generate a **pure domain causal DAG**. It does not enter the pipeline; the only connection point is the produced `dag.json` — handed to `python -m data_world --dag` for consumption.

Three usage modes:

```bash
# 1. Single DAG: specify a category number, or let it route automatically by persona
python -m dag_factory --category 4 --persona "Supply chain analyst" --out dags/ops.json

# 2. Domain batch: spread N DAGs over each of several domains
python -m dag_factory --batch --categories 1,2,3,4,5,6 --per-category 3 --out-dir dags

# 3. Persona file: route the domain per persona, one DAG per persona
python -m dag_factory --persona-file your_personas.jsonl --limit 200 --out-dir dags
```

`--persona-file` accepts a jsonl with one `{"persona": "..."}` per line (you need to prepare it yourself); `--limit` takes only the first N entries, and `--shuffle` shuffles by `--seed` before taking them. If a line carries an integer `index` field (sharding scripts pass it through), the output file is named `p{index:06d}.json` — rerunning the same persona always lands on the same file, making resume natural and deduplicating across shards.

### Common Parameters (dag_factory)

| Parameter | Default | Description |
|---|---:|---|
| `--persona` | random from built-in pool | Analyst persona; when omitted, drawn randomly from the built-in pool |
| `--category` | none | Domain number 1..16; when omitted, routed by persona |
| `--out` | none | Output path for single-DAG mode |
| `--batch` | false | Domain batch mode |
| `--per-category` | `1` | How many DAGs to generate per domain in domain batch mode |
| `--categories` | all 16 categories | Restrict domains in domain batch mode, comma-separated |
| `--persona-file` | none | Persona jsonl; route and generate one DAG per persona |
| `--limit` | all | In `--persona-file` mode, take only the first N entries |
| `--shuffle` | false | In `--persona-file` mode, shuffle by `--seed` before taking entries |
| `--out-dir` | none | Output directory for batch / persona modes |
| `--attempts` | `5` | Retry limit for validation failures of a single DAG |
| `--seed` | `20260712` | Random seed (persona / domain round-robin, shuffling) |

For the exact full parameter list:

```bash
python -m dag_factory --help
```

## Concurrent Scripts

`scripts/` provides two concurrent orchestration scripts that fan the single commands above out to multiple processes:

```bash
# Generate DAGs only: domain mode, or persona shard mode (with --persona-file, the jsonl is sliced by --workers for concurrency)
scripts/generate_dags_concurrent.sh --categories 1-4 --per-category 2 --workers 4
scripts/generate_dags_concurrent.sh --persona-file your_personas.jsonl --limit 200 --workers 8

# Scan existing DAGs and generate worlds concurrently (complete ones are skipped, incomplete ones automatically --resume)
scripts/generate_worlds_concurrent.sh --dags-dir dags/concurrent --workers 2

# Per-DAG persona + trajectory capture: --persona-map overrides --persona by dag_file; --ccr-bin specifies the CCR executable
scripts/generate_worlds_concurrent.sh --dags-dir dags/concurrent --workers 20 \
  --persona-map your_persona_map.jsonl --log-trajectories --ccr-bin /path/to/ccr
```

Both scripts support `--dry-run` to print the plan first and `--force` to overwrite and rebuild; see each script's `--help` and [`scripts/README.md`](../scripts/README.md) for details.
