from __future__ import annotations

import json
import unittest
from pathlib import Path

from data_world import WorldGenError
from data_world.dag import topo_layers, validate_and_normalize_dag


def _node(label: str) -> dict:
    return {"label": label, "variables": [f"{label}_value"]}


class DagValidationTests(unittest.TestCase):
    def test_aliases_are_normalized_without_mutating_input(self) -> None:
        raw = {
            "nodes": [
                {"id": "source", "fields": ["x"]},
                {"name": "left", "variables": ["y"]},
                {"label": "right", "variables": ["z"]},
            ],
            "edges": [
                {"source": "source", "target": "left", "mechanism": "x drives y"},
                {"from": "source", "to": "right", "note": "x drives z"},
            ],
        }

        dag = validate_and_normalize_dag(raw)

        self.assertEqual([node["label"] for node in dag["nodes"]],
                         ["source", "left", "right"])
        self.assertEqual(dag["nodes"][0]["variables"], ["x"])
        self.assertEqual(dag["edges"][0]["src"], "source")
        self.assertEqual(dag["edges"][0]["dst"], "left")
        self.assertEqual(dag["edges"][0]["note"], "x drives y")
        self.assertEqual(topo_layers(dag), [["source"], ["left", "right"]])
        self.assertNotIn("label", raw["nodes"][0])
        self.assertNotIn("src", raw["edges"][0])

    def test_structural_errors_are_rejected(self) -> None:
        cases = {
            "duplicate node": (
                {"nodes": [_node("a"), _node("a")], "edges": []},
                "节点 label 重复",
            ),
            "duplicate edge": (
                {
                    "nodes": [_node("a"), _node("b")],
                    "edges": [
                        {"src": "a", "dst": "b", "note": "first"},
                        {"src": "a", "dst": "b", "note": "second"},
                    ],
                },
                "重复边",
            ),
            "dangling endpoint": (
                {
                    "nodes": [_node("a")],
                    "edges": [{"src": "a", "dst": "missing", "note": "edge"}],
                },
                "不存在",
            ),
            "self loop": (
                {
                    "nodes": [_node("a")],
                    "edges": [{"src": "a", "dst": "a", "note": "loop"}],
                },
                "自环",
            ),
            "missing note": (
                {
                    "nodes": [_node("a"), _node("b")],
                    "edges": [{"src": "a", "dst": "b", "note": "  "}],
                },
                "缺少 note",
            ),
        }

        for name, (raw, message) in cases.items():
            with self.subTest(name=name):
                with self.assertRaisesRegex(WorldGenError, message):
                    validate_and_normalize_dag(raw)

    def test_examples_use_current_dag_contract(self) -> None:
        examples = Path(__file__).resolve().parents[1] / "examples"
        paths = sorted(examples.glob("*_dag.json"))
        self.assertTrue(paths, "No example DAGs found")
        for path in paths:
            with self.subTest(example=path.name):
                raw = json.loads(path.read_text(encoding="utf-8"))
                validate_and_normalize_dag(raw)
                for node in raw["nodes"]:
                    self.assertTrue(node["system"].strip())
                    self.assertTrue(node["grain"].strip())
                for edge in raw["edges"]:
                    self.assertEqual(set(edge), {"src", "dst", "note"})
                    self.assertTrue(edge["note"].strip())

    def test_cycle_is_rejected(self) -> None:
        raw = {
            "nodes": [_node("a"), _node("b")],
            "edges": [
                {"src": "a", "dst": "b", "note": "a drives b"},
                {"src": "b", "dst": "a", "note": "b drives a"},
            ],
        }

        with self.assertRaisesRegex(WorldGenError, "DAG 存在环"):
            validate_and_normalize_dag(raw)


if __name__ == "__main__":
    unittest.main()
