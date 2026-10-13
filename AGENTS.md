# AGENTS.md

This file defines the repository-level guidance for coding agents working on `data-world-gen`.

## Project purpose

`data-world-gen` is a DAG-driven synthetic data-world compiler. It turns a causal DAG into a coherent and auditable data-analysis environment rather than generating unrelated tables.

The pipeline separates four concerns:

1. **World contract** — define canonical entities, timeline, causal mechanisms, observation systems, datasets, and intended insights.
2. **Canonical state generation** — execute DAG topological layers in order; every layer consumes only committed parent state.
3. **Observation projection** — project canonical state through imperfect real-world observation systems into analyst-visible files.
4. **Independent audit** — verify files, causal edges, invariants, and insights, then produce `WORLD.md`.

## Common commands

```bash
# Show CLI help without installation
python -m data_world --help

# Equivalent compatibility entry point
python data_world_gen.py --help

# Editable installation
python -m pip install -e .

# Compile a world
python -m data_world \
  --dag /path/to/dag.json \
  --output /path/to/world-output \
  --model ops

# Resume an interrupted build
python -m data_world \
  --dag /path/to/dag.json \
  --output /path/to/world-output \
  --resume

# Rebuild an existing output directory
python -m data_world \
  --dag /path/to/dag.json \
  --output /path/to/world-output \
  --force

# Basic static checks
scripts/check_repo.sh
```

The `claude` executable must be available on `PATH`. A custom OpenAI-compatible Anthropic endpoint can be supplied with `--endpoint`; inject credentials through protected environment variables rather than command-line arguments, which are visible in shell history and process listings.

## Architecture

The dependency direction is intentionally small and one-way:

```text
cli.py
  └── pipeline.py
        ├── dag.py
        ├── fs.py
        ├── prompts.py
        ├── sdk.py
        └── trajectory.py
```

- `data_world/cli.py`: argument parsing, user-facing error boundary, process exit code.
- `data_world/pipeline.py`: stateful orchestration, stage retries, resume behavior, and artifact validation.
- `data_world/dag.py`: pure DAG normalization, validation, and graph queries.
- `data_world/fs.py`: stateless filesystem, path-safety, hash, and JSON helpers.
- `data_world/prompts.py`: prompts and model-facing artifact contracts; no process execution.
- `data_world/sdk.py`: model-call adapters and transient transport retries — `ClaudeRunner` (Claude Code subprocess, for tool-using stages) and `LLMClient` (single OpenAI-compatible chat call, for pure-reasoning stages such as the world contract).
- `data_world/trajectory.py`: optional SFT trajectory logging — `CCRProxy` manages a claude-code-router lifecycle so SDK stages route through a logging proxy, plus a parser that reassembles CCR debug logs into OpenAI-format conversations. CCR is an optional external npm tool, only needed with `--log-trajectories`.
- `data_world/__init__.py`: shared public exception only.
- `data_world_gen.py`: thin compatibility entry point; do not put business logic here.
- `dag_factory/`: independent upstream CLI; it produces validated `dag.json` files and depends one-way on `data_world.dag`.
- `task/`: independent downstream CLI; it consumes completed world directories and depends one-way on selected `data_world` modules.

## Execution and artifact contract

The execution order is fixed:

```text
initialize
  → compile world contract
  → generate canonical state layer by layer
  → project analyst-visible environment
  → audit the completed world
  → final validation
```

A successful output has this shape:

```text
<world>/
├── dag.json                 # byte-equivalent copy of the input DAG
├── WORLD.md                 # audited world and analysis guide
├── env/                     # analyst-visible data and in-world materials only
└── .build/
    ├── progress.json        # resumable pipeline state
    ├── world_contract.json
    ├── layers/              # canonical state, layer_result.json, layer_NN.md
    ├── projection/          # generation scripts, projection_result.json, projection.md
    └── audit/               # audit scripts and audit_result.json
```

Keep the boundary strict:

- `env/` may contain only realistic analyst-visible data and in-world materials.
- `.build/` contains canonical/latent state, scripts, parameters, intermediate results, and audits.
- Every stage leaves both a machine-checkable ledger and a narrative self-report: `layer_result.json` / `projection_result.json` record what was verified and with which numbers, while `layer_NN.md` / `projection.md` record design intent, mechanisms, abandoned alternatives, and downstream pitfalls — the things no JSON field can hold. Narrative self-reports stay in `.build/`; a real data dump never ships with its own design document, so one appearing in `env/` would hand the observation mechanisms and flaw causes to the analyst.
- `WORLD.md` documents the finished world but must not replace machine-checkable stage results.
- `dag.json` must remain an unchanged copy of the input file.

## Core invariants

Preserve these invariants in every change:

1. Validate the full DAG before creating generation artifacts.
2. Reject cycles, duplicate nodes/edges, dangling endpoints, and edges missing a non-empty `note`.
3. Execute topological layers sequentially; earlier canonical state is read-only.
4. Treat a layer as a transaction: all nodes in the layer commit together.
5. Keep entity IDs, time axes, units, and shared events consistent across layers.
6. Require every causal edge to have an explicit validation record.
7. Generate imperfect observed data from explicit observation mechanisms, never arbitrary noise alone.
8. Persist progress atomically after each committed stage.
9. On `--resume`, reject a changed DAG or changed topological layering.
10. Never allow model-produced paths to escape their assigned workspace.
11. Never place scripts, latent truth, manifests, or answers in `env/`.
12. Do not add a production mock/dry-run branch. Test orchestration with isolated tests or injected fakes instead of alternate source behavior.
13. Never put credentials in process arguments or persisted manifests; model-controlled child processes receive only the credential required for that call.

## Change guidelines

- Prefer cohesive modules over many one-function stage modules.
- Do not create a generic `utils.py`; put helpers in the module matching their concrete responsibility.
- Keep graph and filesystem helpers stateless and deterministic.
- Keep validation next to the pipeline stage whose output it validates.
- Do not bypass validation to accommodate model output; improve the prompt contract or normalize only well-defined aliases.
- Distinguish retry layers:
  - `sdk.py` retries transient transport/process failures.
  - `pipeline.py` retries invalid stage artifacts with the validation error fed back into the prompt.
- Update `README.md`, the relevant page under `doc/`, and this file when changing CLI options, module ownership, output layout, or invariants.
- All READMEs and `doc/` pages are bilingual: English is the default (`README.md` / `*.md`), Chinese lives in the sibling `*.zh-CN.md` — update both when changing documented behavior.
- Do not add dependencies unless the standard library cannot provide a clear and maintainable solution.

## Python style

- Support Python 3.9+ as declared in `pyproject.toml`.
- Use `from __future__ import annotations` in package modules.
- Add type hints to public functions and non-trivial private methods.
- Raise the owning package's public exception (`WorldGenError`, `DagFactoryError`, or `TaskGenError`) for expected input, generation, and artifact-validation failures.
- Use `pathlib.Path` for paths and UTF-8 for text files.
- Use `atomic_write_json` for persisted pipeline state.
- Keep compatibility entry points thin.
- Avoid broad exception handling except at the CLI boundary or a stage retry boundary.

## Verification checklist

For every substantive change:

1. Run `scripts/check_repo.sh` (compile, tests, CLI smoke tests, example JSON, and Shell syntax).
2. If the check script cannot run, execute its individual Python checks and document why.
3. Run `python -m data_world --help`, `python -m dag_factory --help`, and `python -m task --help`; confirm documented flags still match.
4. Exercise DAG validation with at least one valid DAG and malformed/cyclic DAGs.
5. For pipeline changes, verify fresh-run, `--resume`, and `--force` state transitions using tests with an injected runner; do not restore a runtime mock mode.
6. Confirm generated paths remain inside the expected world directory and `env/` contains no process artifacts.
