"""Task —— data-world 的独立后置工具：把已建成的数据世界编译成带评分细则的分析题。

与 data-world 主流程解耦：本包不进入 pipeline，也不被 data_world 反向 import。
唯一连接点是消费的 ``<world>/`` —— 由 ``python -m data_world`` 产出。

第一性原理：数据分析没有检索式的"多跳问答"。真正的分析题只有两类——沿 DAG
**出度**看「后果」、沿 DAG **入度**看「原因」。所以采样直接从原始 DAG 取目标
节点，每个目标出一道题，不做子图采样、不做表对预计算。

本包有两个阶段，产物 ``tasks.json`` 也可交下游评测工具消费：

    synth   <world>/ → tasks.json      出题：agent 在世界副本里自由探索后写出 task+rubric
    export  tasks.json → benchmark     导出：转成 harbor 基准任务目录

代码结构：

    task/
      contract.py   产物契约：schema、校验、rubric 打平、泄漏检查
      runner.py     流式 agent 运行器
      cli.py        子命令 + 错误边界
      synth/        出题：sampler（选目标）+ prompts + pipeline（只读副本、校验重试）
      export/       Harbor 导出：harbor（导出器）+ grade/judge/prompts（随容器 ship 的判分资产）

``contract.py`` 是 rubric 结构的唯一定义处，被出题提示词、产物校验与导出共用——
"契约由一处拥有"正是本包要守的不变量（详见 README.md）。

为避免重复实现，DAG 的读取与拓扑分层复用 ``data_world.dag``、模型调用复用
``data_world.sdk``、状态落盘复用 ``data_world.fs``、``.env`` 加载复用
``data_world.envfile``（均为单向软依赖，接受口径与上游完全一致）。

本模块只定义 :class:`TaskGenError`，刻意不 import 子包——子包都要从这里取
该异常，根模块反向 import 会形成循环。
"""

from __future__ import annotations

__all__ = ["TaskGenError"]


class TaskGenError(Exception):
    """出题阶段的预期错误（CLI 边界捕获后转为非零退出）。"""
