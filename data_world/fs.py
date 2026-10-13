"""文件系统、路径安全与 JSON 文件辅助函数。

该模块是无业务状态的底层能力层，不依赖 DAG、SDK 或生成管线。
参考 terminal-bench / verl 的规范，按具体能力命名为 ``fs``，避免创建
职责不明确的通用 ``utils.py``。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, List

from . import WorldGenError

_SAFE_NAME_RE = re.compile(r"[^a-zA-Z0-9._-]+")


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        stream.write(payload)
    try:
        os.replace(temporary, path)
    except OSError:
        # Some network/FUSE mounts (e.g. HDFS) do not support cross-file
        # rename reliably; fall back to an in-place write and drop the temp.
        with path.open("w", encoding="utf-8") as stream:
            stream.write(payload)
        try:
            temporary.unlink()
        except OSError:
            pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slugify(value: str, limit: int = 56) -> str:
    """把用户提供的 world 名称转为安全目录名。"""
    normalized = _SAFE_NAME_RE.sub("-", value.strip()).strip("-._").lower()
    return normalized[:limit] or "world"


def list_relative_files(root: Path, include_hidden: bool = True) -> List[str]:
    if not root.exists():
        return []
    files: List[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if not include_hidden and any(part.startswith(".") for part in relative.parts):
            continue
        files.append(relative.as_posix())
    return sorted(files)


def ensure_inside(path: Path, root: Path) -> None:
    """拒绝模型产物通过 ``..`` 或绝对路径逃逸指定工作目录。"""
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise WorldGenError(f"路径越界: {path} 不在 {root} 内") from exc


def format_json(value: Any) -> str:
    """将结构化上下文稳定地嵌入提示词。"""
    return json.dumps(value, ensure_ascii=False, indent=2)
