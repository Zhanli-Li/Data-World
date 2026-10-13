"""DAG Factory —— data-world 的独立前置工具：把领域生成为纯净的因果 DAG。

与 data-world 主流程解耦：本包不进入 pipeline，也不被 data_world 反向 import。
唯一连接点是产出的 dag.json —— 交给 ``python -m data_world --dag dag.json`` 消费。

为避免重复实现，DAG 的校验与拓扑分层复用 ``data_world.dag``（单向软依赖）。
"""

from __future__ import annotations

__all__ = ["DagFactoryError"]


class DagFactoryError(Exception):
    """DAG 生成/校验阶段的预期错误（CLI 边界捕获后转为非零退出）。"""
