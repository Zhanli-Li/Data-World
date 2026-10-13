"""Harbor 导出：``tasks.json`` → harbor 基准任务目录。

本包只有导出一条路（本机评测已移除），但导出 ship 进容器的判分资产仍在包内：

- ``grade``     **评分语义的唯一实现**：verdict 容错解析 + reward 折算；
- ``judge``     judge 客户端（流式 SSE，零第三方依赖，凭据只从环境变量读）；
- ``prompts``   ``JUDGE_PROMPT`` 的唯一文本来源；
- ``harbor``    导出器，把 ``grade``/``judge`` 逐字节拷进任务目录的 ``tests/``。

判分资产与导出器同居一包的理由：容器里的 reward 必须来自这份源码本身——
``tests/`` 下的判分代码是**拷贝而非复刻**，不允许另写一份容器版各自演化。

注意 ``grade`` 在容器内是**脱离本包**独立运行的（``python3 tests/grade.py``），
它自带无包环境的 ImportError 分支；改动其 import 结构前请先看该文件顶部注释。
"""

from __future__ import annotations

from .harbor import export

__all__ = ["export"]
