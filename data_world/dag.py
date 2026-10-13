from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, Tuple

from . import WorldGenError


def _node_label(node: Mapping[str, Any]) -> Optional[str]:
    value = node.get("label", node.get("id", node.get("name")))
    return value.strip() if isinstance(value, str) and value.strip() else None


def _edge_src(edge: Mapping[str, Any]) -> Any:
    return edge.get("src", edge.get("source", edge.get("from")))


def _edge_dst(edge: Mapping[str, Any]) -> Any:
    return edge.get("dst", edge.get("target", edge.get("to")))


def validate_and_normalize_dag(raw: Any) -> Dict[str, Any]:
    """校验 DAG 并生成运行时规范副本；原输入文件不会被改写。"""
    if not isinstance(raw, dict):
        raise WorldGenError("DAG 根必须是 JSON object")
    nodes, edges = raw.get("nodes"), raw.get("edges")
    if not isinstance(nodes, list) or not nodes:
        raise WorldGenError('DAG 必须包含非空 "nodes" 数组')
    if not isinstance(edges, list):
        raise WorldGenError('DAG 必须包含 "edges" 数组')

    labels: Set[str] = set()
    normalized_nodes: List[Dict[str, Any]] = []
    problems: List[str] = []
    for index, item in enumerate(nodes):
        if not isinstance(item, dict):
            problems.append(f"nodes[{index}] 不是 object")
            continue
        label = _node_label(item)
        if not label:
            problems.append(f"nodes[{index}] 缺少 label/id/name")
            continue
        if label in labels:
            problems.append(f"节点 label 重复: {label!r}")
            continue
        labels.add(label)
        node = dict(item)
        node["label"] = label
        variables = node.get("variables", node.get("fields", []))
        if not isinstance(variables, list) or not variables:
            problems.append(f"节点 {label!r} 缺少非空 variables/fields")
        else:
            node["variables"] = variables
        node.setdefault("system", "待规划观测系统")
        node.setdefault("grain", "待规划粒度")
        normalized_nodes.append(node)

    seen: Set[Tuple[str, str]] = set()
    normalized_edges: List[Dict[str, Any]] = []
    for index, item in enumerate(edges):
        if not isinstance(item, dict):
            problems.append(f"edges[{index}] 不是 object")
            continue
        src, dst = _edge_src(item), _edge_dst(item)
        if src not in labels:
            problems.append(f"edges[{index}] src={src!r} 不存在")
        if dst not in labels:
            problems.append(f"edges[{index}] dst={dst!r} 不存在")
        if src == dst:
            problems.append(f"edges[{index}] 是自环: {src!r}")
        if isinstance(src, str) and isinstance(dst, str):
            if (src, dst) in seen:
                problems.append(f"重复边: {src!r} -> {dst!r}")
            seen.add((src, dst))
        edge = dict(item)
        edge["src"], edge["dst"] = src, dst
        note = edge.get("note", edge.get("mechanism", ""))
        if not (isinstance(note, str) and note.strip()):
            problems.append(f"edges[{index}] 缺少 note(必须用一句话描述该边的真实因果关系)")
        edge["note"] = note
        normalized_edges.append(edge)

    if problems:
        raise WorldGenError("DAG 校验失败:\n  - " + "\n  - ".join(problems))
    dag = dict(raw)
    dag["nodes"], dag["edges"] = normalized_nodes, normalized_edges
    topo_layers(dag)
    return dag


def topo_layers(dag: Mapping[str, Any]) -> List[List[str]]:
    labels = [str(node["label"]) for node in dag["nodes"]]
    indegree = {label: 0 for label in labels}
    children: Dict[str, List[str]] = {label: [] for label in labels}
    for edge in dag["edges"]:
        src, dst = str(edge["src"]), str(edge["dst"])
        indegree[dst] += 1
        children[src].append(dst)
    remaining = set(labels)
    layers: List[List[str]] = []
    while remaining:
        layer = sorted(label for label in remaining if indegree[label] == 0)
        if not layer:
            raise WorldGenError(f"DAG 存在环，剩余节点: {sorted(remaining)}")
        layers.append(layer)
        for label in layer:
            remaining.remove(label)
            for child in children[label]:
                indegree[child] -= 1
    return layers


def incoming_edges(dag: Mapping[str, Any], labels: Iterable[str]) -> List[Dict[str, Any]]:
    wanted = set(labels)
    return [dict(edge) for edge in dag["edges"] if edge["dst"] in wanted]


def ancestors_for_layer(dag: Mapping[str, Any], labels: Iterable[str]) -> List[str]:
    wanted = set(labels)
    return sorted({str(edge["src"]) for edge in dag["edges"] if edge["dst"] in wanted})


def descendants(dag: Mapping[str, Any], seeds: Iterable[str]) -> Set[str]:
    children: Dict[str, List[str]] = {}
    for edge in dag["edges"]:
        children.setdefault(str(edge["src"]), []).append(str(edge["dst"]))
    result, queue = set(seeds), list(seeds)
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in result:
                result.add(child)
                queue.append(child)
    return result
