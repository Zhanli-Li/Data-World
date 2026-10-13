"""``task.export.grade.score`` 的 reward 量程契约：恒落在 [0,1]。

reward 会进入 benchmark 统计与 RL 数据，量程是下游的消费契约：
触发负分项最多把总分压到 0，不出现负 reward。
"""

from __future__ import annotations

import unittest

from task.export.grade import score


def _rubric(criteria: list) -> dict:
    return {"world": "w1", "task_id": "cause_1", "task_kind": "cause",
            "target_node": "x", "criteria": criteria}


CRITERIA = [
    {"id": "dc-1", "section": "data_computation", "criterion": "p1", "points": 2},
    {"id": "id-1", "section": "insight_discovery", "criterion": "p2", "points": 3},
    {"id": "id-2", "section": "insight_discovery", "criterion": "n1", "points": -2},
]


def _verdict(vid: str, status: str) -> dict:
    return {"id": vid, "status": status, "reason": "r"}


class ScoreRangeTests(unittest.TestCase):
    def test_all_satisfied_is_one(self) -> None:
        result = score(_rubric(CRITERIA), [
            _verdict("dc-1", "satisfied"), _verdict("id-1", "satisfied"),
            _verdict("id-2", "not_triggered")])
        self.assertEqual(1.0, result["reward"])

    def test_triggered_negative_clamps_to_zero_not_negative(self) -> None:
        result = score(_rubric(CRITERIA), [
            _verdict("dc-1", "not_satisfied"), _verdict("id-1", "not_satisfied"),
            _verdict("id-2", "triggered")])
        self.assertEqual(-2, result["raw_score"])  # raw 保留现场，reward 截断
        self.assertEqual(0.0, result["reward"])

    def test_partial_score(self) -> None:
        result = score(_rubric(CRITERIA), [
            _verdict("dc-1", "satisfied"), _verdict("id-1", "not_satisfied"),
            _verdict("id-2", "not_triggered")])
        self.assertEqual(0.4, result["reward"])

    def test_missing_verdicts_score_zero(self) -> None:
        result = score(_rubric(CRITERIA), [])
        self.assertEqual(0.0, result["reward"])
        self.assertEqual(["dc-1", "id-1", "id-2"], result["missing_verdicts"])

    def test_all_negative_rubric_reward_zero(self) -> None:
        criteria = [{"id": "id-2", "section": "insight_discovery",
                     "criterion": "n1", "points": -2}]
        result = score(_rubric(criteria), [_verdict("id-2", "triggered")])
        self.assertEqual(0.0, result["reward"])  # 分母为 0 的兜底，不产生 NaN


if __name__ == "__main__":
    unittest.main()
