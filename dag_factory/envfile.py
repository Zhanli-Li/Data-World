"""零依赖的本地文件加载器（stdlib）：.env 键值与 persona jsonl。

不引入 python-dotenv / pandas：只做最朴素的解析，喂给 CLI。
- ``load_env``：复用 ``data_world.envfile``，保持三套 CLI 的配置优先级一致。
- ``load_personas``：从 ``{"persona": ...}`` 的 jsonl 里逐行取 persona 字符串。
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import List, Optional, Tuple

from data_world.envfile import load_env


def load_personas(
    path: Path,
    *,
    limit: Optional[int] = None,
    shuffle: bool = False,
    seed: Optional[int] = None,
) -> List[str]:
    """从 jsonl 里读取 persona 列表：每行一个 ``{"persona": "..."}``。

    - 忽略空行；缺少非空 ``persona`` 字段或解析失败的行直接跳过。
    - ``shuffle`` 用独立 ``random.Random(seed)`` 打乱，不污染调用方的全局随机态。
    - ``limit`` 在（可选打乱之后）截断为前 N 条。
    """
    if not path.is_file():
        raise FileNotFoundError(f"persona 文件不存在: {path}")
    personas: List[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            value = json.loads(line).get("persona")
        except (json.JSONDecodeError, AttributeError):
            continue
        if isinstance(value, str) and value.strip():
            personas.append(value.strip())
    if shuffle:
        random.Random(seed).shuffle(personas)
    if limit is not None:
        personas = personas[:limit]
    return personas


def load_personas_indexed(
    path: Path,
    *,
    limit: Optional[int] = None,
    shuffle: bool = False,
    seed: Optional[int] = None,
) -> List[Tuple[int, str]]:
    """同 :func:`load_personas`，但每条附带其**全局稳定索引**。

    索引是 persona 在（可选打乱、截断之后的）最终序列里的位置，用于生成与内容无关、
    可断点续跑的输出文件名。分片并发时把该索引透传下去，可保证同一条 persona 在任何
    分片里都落到同一个输出文件，从而天然支持"已存在即跳过"的续跑。
    """
    personas = load_personas(path, limit=limit, shuffle=shuffle, seed=seed)
    return list(enumerate(personas))
