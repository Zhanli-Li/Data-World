# Data-World Usage Documentation

[中文](README.zh-CN.md) | **English**

One page per component, covering complete usage and contracts:

- [`dag.md`](dag.md): DAG input format, `dag_factory` DAG generation (single / domain batch / persona file), concurrent orchestration scripts
- [`data-world.md`](data-world.md): full `data_world` usage — parameter table, output directory contract, resume, custom endpoints, trajectory capture, failure and retry semantics
- [`task.md`](task.md): `task` task synthesis (`synth`) and Harbor export (`export`)

Further reading:

- [`task/README.md`](../task/README.md): rubric design rationale and invariants (must-read before changing task-synthesis logic)
- [`AGENTS.md`](../AGENTS.md): architectural constraints and 13 core invariants (must-read before changing code)
- Root [`README.md`](../README.md): project overview, installation, and Quick Start

## Code Structure

```text
data_world/
├── __init__.py     # WorldGenError
├── __main__.py     # python -m data_world
├── cli.py          # CLI and error boundary
├── pipeline.py     # main orchestrator, progress, and stage validation
├── dag.py          # DAG normalization, validation, and graph queries
├── fs.py           # filesystem, path safety, JSON, and hashing
├── prompts.py      # model prompts and artifact protocol
├── sdk.py          # Claude Code subprocess adapter + OpenAI-compatible single call with transient retries
└── trajectory.py   # optional CCR lifecycle and trajectory parsing

dag_factory/        # independent upstream tool: persona → domain routing → causal DAG generation
├── __init__.py     # DagFactoryError
├── __main__.py     # python -m dag_factory
├── cli.py          # CLI: three modes — single / domain batch / persona file
├── categories.py   # 16 data-analysis domains with keyword fallback routing
├── factory.py      # domain routing + LLM graph generation + validation retry reusing data_world.dag
├── envfile.py      # zero-dependency .env and persona jsonl loader
└── llm.py          # OpenAI-compatible single-call adapter

task/               # independent downstream tool: task synthesis / Harbor export (see its README for structure)
scripts/            # concurrent orchestration scripts (generate DAGs / generate worlds)
examples/           # ready-to-run example DAGs and scripts
tests/              # key regression tests that do not depend on external model services
data/               # local persona and other input data (ignored by Git by default, not distributed with the repo)
outputs/            # generated data-world artifacts (ignored by Git by default)
```

By design, there is no generic `utils.py` and no overly fine-grained `stages/` modules: low-level capabilities are named by responsibility, and stage implementations with their validation are concentrated in `pipeline.py`, to reduce state jumping across layers of modules. `dag_factory` is decoupled from the main pipeline, connected to data_world only through the `dag.json` it produces, and reuses `data_world.dag` validation to keep semantics consistent.

## Development and Verification

Run from the repository root (the directory containing `pyproject.toml`), after installing the Python dependencies:

```bash
bash scripts/check_repo.sh
```

The check script uses `python`; activate your Python environment first so that this command is available. It runs compilation, unit tests, CLI smoke tests, example JSON validation, and shell syntax checks. These checks do not call external model services.

Before changing code, read [`AGENTS.md`](../AGENTS.md) for module ownership, artifact contracts, and core invariants. See [`tests/README.md`](../tests/README.md) for regression-test coverage and individual test commands. Changes to CLI behavior or artifact contracts should update both the English and Chinese documentation.

Generation stages execute model-written code; use isolated infrastructure and review generated artifacts before release or consequential use.
