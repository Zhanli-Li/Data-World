"""出题：``<world>/`` → ``tasks.json``。

只读消费已建成的数据世界，产出带评分细则（rubric）的分析题。世界目录以**只读副本**
的形式交给出题 agent（见 ``pipeline._set_read_only``），因此出题不可能改写世界。

- ``sampler``   按拓扑深度分层选目标节点（cause 看入度、consequence 看出度）；
- ``prompts``   出题提示词；
- ``pipeline``  编排：只读副本、校验重试（错误回喂）、断点续跑、聚合。

评分细则的**结构**由 ``task.contract`` 定义，本子包只负责把它生产出来；
判分见 ``task.export``（harbor 容器内 verifier）或下游评测工具。
"""

from __future__ import annotations

from .pipeline import SYNTH_RETRIES, synth, synth_one, synth_world
from .sampler import select_targets

__all__ = [
    "SYNTH_RETRIES",
    "select_targets",
    "synth",
    "synth_one",
    "synth_world",
]
