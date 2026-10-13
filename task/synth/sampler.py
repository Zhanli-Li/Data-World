"""从世界的原始 DAG 选择原因分析与后果分析的目标节点。

采样策略：原因（入度）与后果（出度）两个方向，各自按拓扑深度分 K 层
（K=4），每层取度数 Top-1 节点，每世界最多 2K 道题。
分层采样避免全局 Top-K 退化为 hub 偏好或在同度节点间任意选择。

DAG 的读取与校验复用 ``data_world.dag.validate_and_normalize_dag``：出题阶段
看到的节点标识、边端点与别名归一口径与生成阶段完全一致（label/id/name 与
src/dst/source/target/from/to 都已在上游统一）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping

from data_world import WorldGenError
from data_world.dag import validate_and_normalize_dag

from .. import TaskGenError

#: 每个方向的分层数（每层取度数 Top-1，每世界最多 2×N_STRATA 道题）。
N_STRATA = 4

#: 两个分析方向 → 该方向在 DAG 上对应的度数口径。
DEGREE_KIND = {"cause": "in_degree", "consequence": "out_degree"}


def _topo_depths(labels: List[str], edges: List[Mapping[str, Any]]) -> Dict[str, int]:
    """每个节点到根的最长路径长度（根=0）。

    用最长路径而非最短路径分层：节点的"分析深度"取决于它最远的上游链条有多长，
    这与 data_world 逐层提交 canonical state 的层序一致。
    """
    preds: Dict[str, List[str]] = {label: [] for label in labels}
    for edge in edges:
        src, dst = edge.get("src"), edge.get("dst")
        if src in preds and dst in preds:
            preds[dst].append(src)
    depth: Dict[str, int] = {}

    def resolve(label: str) -> int:
        if label not in depth:
            depth[label] = 1 + max(map(resolve, preds[label]), default=-1)
        return depth[label]

    for label in labels:
        resolve(label)
    return depth


def load_dag(world_dir: Path) -> Dict[str, Any]:
    """读取并校验世界目录里的 ``dag.json``（data_world 保证它是输入 DAG 的原始副本）。"""
    dag_path = Path(world_dir) / "dag.json"
    if not dag_path.is_file():
        raise TaskGenError(f"世界缺少 dag.json: {dag_path}")
    try:
        raw = json.loads(dag_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TaskGenError(f"dag.json 不是合法 JSON: {dag_path}: {exc}") from exc
    try:
        return validate_and_normalize_dag(raw)
    except WorldGenError as exc:
        raise TaskGenError(f"dag.json 校验失败: {dag_path}: {exc}") from exc


def select_targets(world_dir: Path, top_k: int = N_STRATA) -> List[Dict[str, Any]]:
    """两个方向各按拓扑深度分 ``top_k`` 层，每层取度数 Top-1。

    同度数时遵循 ``dag.json`` 的节点顺序，保证同一世界重复采样落到同一批目标
    （出题可断点续跑的前提）。度数 > 0 才有可分析的直接原因/后果；某层无候选时
    该层不出题、不硬凑——深层叶子无出边，故部分世界缺 ``consequence_K``。
    """
    if top_k < 1:
        raise TaskGenError(f"top_k 必须大于 0，收到 {top_k}")
    dag = load_dag(world_dir)
    nodes, edges = dag["nodes"], dag["edges"]
    order = {str(node["label"]): index for index, node in enumerate(nodes)}
    metadata = {str(node["label"]): node for node in nodes}
    depth = _topo_depths(list(order), edges)
    span = max(depth.values(), default=0) + 1

    adjacency: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        "cause": {label: [] for label in order},
        "consequence": {label: [] for label in order},
    }
    for edge in edges:
        src, dst = str(edge["src"]), str(edge["dst"])
        if src in order and dst in order:
            adjacency["cause"][dst].append(dict(edge))
            adjacency["consequence"][src].append(dict(edge))

    tasks: List[Dict[str, Any]] = []
    for mode, adjacent in adjacency.items():
        for stratum in range(top_k):
            candidates = [label for label, incident in adjacent.items()
                          if incident and depth[label] * top_k // span == stratum]
            if not candidates:
                continue
            best = min(candidates, key=lambda label: (-len(adjacent[label]), order[label]))
            tasks.append({
                "task_id": f"{mode}_{stratum + 1}",
                "mode": mode,
                "rank": stratum + 1,
                "target": best,
                "degree": len(adjacent[best]),
                "degree_kind": DEGREE_KIND[mode],
                "depth": depth[best],
                "stratum": stratum,
                "node": metadata[best],
                "direct_edges": adjacent[best],
            })
    return tasks
