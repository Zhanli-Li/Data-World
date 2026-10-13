# Examples

[中文](README.zh-CN.md) | **English**

Two minimal DAGs with their run scripts:

```bash
bash examples/agriculture_gen.sh
bash examples/ecommerce_gen.sh
```

The scripts launch `python -m data_world` from the repo root; model, endpoint, and token come from the local `.env` — no internal model paths are hardcoded. Output goes to `outputs/`; if the directory already exists, add `--resume` or `--force` explicitly as needed.
