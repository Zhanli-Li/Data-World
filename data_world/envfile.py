"""零依赖的 .env 加载器（stdlib）。

不引入 python-dotenv：把项目根 .env 里的 KEY=VALUE 灌进 ``os.environ``。默认以
项目 .env 为权威覆盖同名进程变量；传 ``override=False`` 可保留调用方环境。加载后，
CLI 各参数的 ``default=os.environ.get(...)`` 会自然接住这些值。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _project_root() -> Path:
    # data_world/envfile.py -> data_world/ -> 项目根（data-world/）
    return Path(__file__).resolve().parent.parent


def load_env(path: Optional[Path] = None, *, override: bool = True) -> None:
    """把 .env 的 KEY=VALUE 载入 os.environ（默认覆盖 shell 残留值，.env 为权威源）。
    文件不存在时静默跳过。"""
    env_path = path or (_project_root() / ".env")
    if not env_path.is_file():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value
