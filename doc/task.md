# task: Task Synthesis / Export

[中文](task.zh-CN.md) | **English**

`task` is the **independent downstream tool** of data_world: it only reads completed worlds and never enters the world-generation pipeline. It compiles a data world into **analysis tasks with rubrics** — the task-synthesis agent explores freely in a read-only copy of the world and delivers a "task prompt + rubric"; the export stage generates Harbor benchmark task directories (grading runs inside the container).

```text
<world>/ ──synth──▶ tasks.json ──export──▶ Harbor benchmark task directories
```

## The Two Stages

```bash
# Task synthesis: <world>/ → tasks.json (the world directory is read-only and never modified)
python -m task synth --worlds ./outputs/worlds --out ./tasks -v

# Export: tasks.json → Harbor benchmark task directories
python -m task export --tasks ./tasks --out ./benchmark
```

Resume: `synth` skips task_ids already present in `tasks.json` (sampling is deterministic, so a re-run lands on the same targets); `--force` ignores existing artifacts and regenerates everything; `--task-id` synthesizes only the specified task (repeatable).

On shared machines, inject credentials through environment variables (`ANTHROPIC_*` in `.env`) or the secrets mechanism of the runtime platform; command-line key arguments such as `--auth-token` end up in shell history and should not be written into scripts.

## Sampling Rules for Task Synthesis

Given a target node on the DAG, only two things can be asked: looking along the **in-degree** for "causes" (`cause`), and looking along the **out-degree** for "consequences" (`consequence`). Sampling has a single dimension: split by **topological depth** into K=4 layers, take the node with the Top-1 degree in that direction from each layer, yielding at most 2K=8 tasks per world (`cause_1..4` / `consequence_1..4`). Layering prevents a global Top-K from degenerating into hub preference; degree ties are ordered by node order in `dag.json`, guaranteeing that repeated sampling lands on the same targets.

## Artifact Layout

```text
<tasks_out>/
└── <world>/
    ├── tasks.json              # all tasks of this world (task prompt + rubric + identity fields)
    ├── report.json             # n_tasks / n_failed / failures summary
    ├── world_copy/             # read-only copy of the world (not kept with --no-keep-copy)
    └── <task_id>/
        └── run/                # agent working directory for this task
            ├── final_message.txt
            └── session.json
```

The location of `env/` is resolved in one place (`contract.resolve_env_dir`): it first looks for `<tasks>/<world>/world_copy/env` left behind by task synthesis; if absent, it falls back to `<world_root>/<world>/env` pointed to by `--world-root`. Tasks synthesized with `--no-keep-copy` have no copy, so `export` must be passed `--world-root`:

```bash
python -m task export --tasks <out> --out <bench> --world-root outputs/worlds
```

## Harbor Export

`export` generates a self-contained task directory per task: `instruction.md` (task prompt + the `env/`→`/app/data/` mapping notes), `task.toml` (timeout/resources/artifacts), `environment/Dockerfile` (image mirrors can be switched via `--build-arg APT_MIRROR=... / PIP_INDEX=...`), `environment/data/` (a copy of that world's `env/`), and `tests/` (grading implementation + rubric + judge credentials).

The grading code is **copied, not reimplemented**: `grade.py` / `judge.py` under `tests/` are direct copies of this package's source files, and `judge_prompt.py` is generated from the same `JUDGE_PROMPT` — grading semantics have exactly one implementation. Leakage prevention is double-layered: at export time an assertion checks that the task prompt contains no rubric text (`check_no_leak`), and at image build time another assertion checks that `WORLD.md` / `dag.json` / `.build` do not exist.

Judge credentials travel with the task directory (`tests/judge.env`): the evaluation platform has no runtime environment-variable injection channel, so credentials are read from `.env` and written in at export time, with a resolution order identical to `judge.py` inside the container (`JUDGE_*` takes precedence, falling back to `ANTHROPIC_*`; see [`.env.example`](../.env.example) in the root directory). Task directories land on disk with mode 0700, so other users on a shared machine cannot read the tokens and rubrics inside. Note: packaging or uploading a task directory amounts to publishing the judge token inside it — confirm the recipient is trustworthy before sharing.

## Further Reading

For the two-section rubric structure (`data_computation` / `insight_discovery`), the "necessary and determinate" two-gate test for positive-scoring points, negative-item semantics, leakage checks, and the empirically tested rationale behind each design decision, see [`task/README.md`](../task/README.md).
