"""DAG-driven synthetic data-world compiler."""


class WorldGenError(RuntimeError):
    """可预期的输入、生成或产物校验错误。"""


__all__ = ["WorldGenError"]
