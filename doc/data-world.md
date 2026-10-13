# Complete data_world Usage

[中文](data-world.zh-CN.md) | **English**

`data_world` is the core compiler: it validates the causal DAG, generates a unified canonical state, projects it into the analyst-visible `env/`, and finally audits it and generates `WORLD.md`. For basic usage, see the Quick Start in the root [README](../README.md); this document covers the full parameters and contracts.

## Custom Endpoints

```bash
python -m data_world \
  --dag ./dag.json \
  --output ./world \
  --model ops \
  --endpoint "https://your-endpoint.example/v1/anthropic"
```

`--endpoint` injects `ANTHROPIC_BASE_URL` into Claude Code for the current run; when not provided, it defaults to the `ANTHROPIC_BASE_URL` environment variable. Authentication defaults to reading `ANTHROPIC_AUTH_TOKEN`. The World Contract stage reuses the same endpoint and credentials, and automatically strips the trailing `/anthropic` suffix to adapt to OpenAI-compatible interfaces; its model is specified by `--contract-model` (defaults to `ANTHROPIC_MODEL`). The Claude subprocess receives only the token required for the current model calls, and does not inherit unrelated credentials such as judge, training platform, cloud account, or SSH agent credentials.

## Output Layout for Multiple Worlds

If `--output` is the parent directory of multiple worlds, you can use `--name` to create a safely normalized subdirectory:

```bash
python -m data_world \
  --dag examples/ecommerce_dag.json \
  --output outputs \
  --name ecommerce-world
```

## Resume and Overwrite

When the output directory is non-empty, you must explicitly choose between resume and rebuild:

```bash
# Resume committed stages from .build/progress.json
python -m data_world --dag ./dag.json --output ./world --resume

# Delete the output directory and rebuild from scratch
python -m data_world --dag ./dag.json --output ./world --force
```

`--resume` validates the input DAG's SHA256 and topological layering; once the DAG has been modified, the old progress can no longer be reused. Every successful stage atomically updates `.build/progress.json`; on resume, completed stages are re-validated and skipped, while unfinished stages continue executing.

## Trajectory Capture (`--log-trajectories`)

When enabled, the complete model conversation of each stage is written to `<world>/trajectories/<stage>.jsonl` (directly usable for SFT). SDK stages (layer / projection / audit) capture per-turn tool-use through a local CCR proxy, which requires installing and verifying CCR first:

```bash
npm install -g @musistudio/claude-code-router@3.0.20
ccr --help
```

Normal generation does not require CCR. If `ccr` is not on `PATH`, you can set `CCR_BIN=/absolute/path/to/ccr` in `.env`, or specify it explicitly via `--ccr-bin`. CCR is automatically started and stopped in `serve` mode by the pipeline (with an isolated temporary HOME, so `~/.claude-code-router` is not polluted); requests/responses are reconstructed from the v3 SQLite logs, and the local proxy endpoint is automatically added to `no_proxy`. Upstream HTTP 429/5xx responses are retried 4 times by default; adjust with `CCR_RETRY_COUNT` (0–20).

The runtime tool installation home for trajectory capture can be overridden with the `DATA_WORLD_RUNTIME_HOME` environment variable (defaults to the user's home). This separates the "tool installation directory" from the per-run isolated HOME, avoiding repeated npm package installations.

## No Write Permission on the npm Global Directory

If you do not have write permission on the npm global directory, you can first configure a user-level prefix, and make sure its `bin` directory is added to `PATH` (it is recommended to write this into your shell startup file):

```bash
npm config set prefix "$HOME/.npm-global"
export PATH="$HOME/.npm-global/bin:$PATH"
```

## Common Parameters (data_world)

| Parameter | Default | Description |
|---|---:|---|
| `--dag` | required | Input DAG JSON |
| `--output` | required | World output directory, or a parent directory used together with `--name` |
| `--name` | none | Safely normalized world subdirectory name |
| `--persona` | general data analyst | Target analysis audience; the world title/background/scope are endogenously inferred by the agent from the DAG |
| `--seed` | `20260712` | Base random seed; layer \(i\) uses `seed + i` |
| `--model` | `ops` | Model name for Claude Code agent stages |
| `--endpoint` | `ANTHROPIC_BASE_URL` | Base URL shared by the agent and contract stages; the contract stage automatically strips `/anthropic` |
| `--auth-token` | `ANTHROPIC_AUTH_TOKEN` | Authentication token for custom endpoints |
| `--contract-model` | `ANTHROPIC_MODEL` | Model for the World Contract stage's single OpenAI-compatible call |
| `--claude-bin` | `claude` | Claude Code executable |
| `--max-turns` | `300` | Maximum turns for general generation stages |
| `--timeout` | `21600` | Timeout (seconds) for a single subprocess/LLM call |
| `--sdk-retries` | `10` | Retry count for transient network/service errors |
| `--stage-retries` | `5` | Targeted retry count after stage artifact validation fails |
| `--log-trajectories` | false | Capture the complete conversation of each stage into `<world>/trajectories/` (SDK stages go through CCR) |
| `--ccr-bin` | `ccr` (`~/.npm-global/bin/ccr` if present) | claude-code-router executable |
| `--trajectory-model` | `ANTHROPIC_MODEL` | The real model name CCR uses to forward SDK stage requests |
| `--ccr-port` | `3456` | Local port of the CCR proxy |
| `--resume` | false | Resume an existing build |
| `--force` | false | Delete existing output and rebuild |

For the exact full parameter list:

```bash
python -m data_world --help
```

## Output Directory

```text
<world>/
├── dag.json
├── WORLD.md
├── env/
├── trajectories/            # only with --log-trajectories: contract.jsonl / layer_00.jsonl / ... / audit.jsonl
└── .build/
    ├── progress.json
    ├── world_contract.json
    ├── layers/
    │   ├── layer_00/
    │   │   ├── layer_result.json     # machine-readable ledger: state_files / node_outputs / edge_checks
    │   │   ├── layer_00.md           # self-account of this batch of state: design rationale, mechanisms, trade-offs, and downstream hints
    │   │   └── ... canonical state
    │   └── ...
    ├── projection/
    │   ├── projection_result.json
    │   └── projection.md            # projection self-account: why each observation system looks the way it does, the observation mechanisms behind the flaws
    └── audit/
        └── audit_result.json
```

### Analyst-Facing Artifacts

- `env/`: the data and in-world materials visible at the real analysis site.
- `WORLD.md`: the world guide generated from audited measurements — file relationships, quality mechanisms, insights, and limitations.
- `dag.json`: a pristine copy of the input DAG.

### Internal Build Artifacts

`.build/` holds the internal state needed for resume and audit, including canonical state, generation scripts, parameters, stage results, and validation records. It is not part of the data environment that analysts should use directly.

The following must not appear in `env/`:

- Procedural scripts such as Python, Shell, or Notebook files;
- latent/canonical truth;
- build manifests, answers, or audit reports;
- empty files and `__pycache__`.

## Failure and Retry Semantics

The project distinguishes two kinds of retries:

- **SDK retries**: `sdk.py` (`ClaudeRunner` and `LLMClient`) applies exponential backoff to transient errors such as rate limiting, service overload, connection resets, and timeouts.
- **Stage retries**: when the model has returned but the artifacts do not satisfy structural or file constraints, `pipeline.py` feeds the concrete validation errors back into the next generation.

The two kinds of retries do not compensate for each other: transport problems belong to SDK, invalid artifacts belong to Stage (the validation error text is fed back into the next generation).
