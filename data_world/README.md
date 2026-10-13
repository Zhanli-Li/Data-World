# data_world

[中文](README.zh-CN.md) | **English**

The core data-world compiler: validates the causal DAG, generates the unified canonical state, projects it into the analyst-visible `env/`, then audits and produces `WORLD.md`.

```bash
python -m data_world --help
```

Module responsibilities and the invariants that must be preserved are in the repo-root [`AGENTS.md`](../AGENTS.md); user-facing inputs, outputs, and resume semantics are in [`doc/data-world.md`](../doc/data-world.md). This package does not depend on `dag_factory` or `task`; those two may only reuse the foundations here, one-way.
