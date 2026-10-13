"""判分阶段的提示词。

只有 :data:`JUDGE_PROMPT` 一个常量，不执行任何进程（约定同 ``data_world.prompts``）。

``JUDGE_PROMPT`` 是判分口径的**唯一文本来源**：包内判分由 ``grade.py`` 直接 import，
harbor 容器内则由 ``harbor.py`` 导出时生成自包含的 ``judge_prompt.py``——改这里会同时
改变两条路径，不要另起副本。
"""

from __future__ import annotations

#: 判分阶段：judge 按 rubric 逐条给 verdict。
#:
#: ``{report_path}`` 由调用方填成实际交付文件路径（容器 answer.txt），
#: 让 judge 看到的上下文与答题者被要求的交付物一致。
JUDGE_PROMPT = """你是一名严格、公正的数据分析报告评审。下面给你一份评分细则（rubric）和一份待评报告。
请逐条判定报告是否满足每个评分点。

[评分细则]
{rubric_text}

[待评报告（{report_path}）]
<report>
{report}
</report>

[判定规则]
1. 每个评分点自身写明了判定所需的一切——口径、数值或区间、以及它描述的是正确做法
   还是错误做法。你只依据 criterion 的文字和报告正文判定，不要再去设想别的标准。
2. 对每个评分点输出一个 verdict：
   - 正分评分点（points > 0）：报告实质满足该点则 status="satisfied"，否则 "not_satisfied"。
     正分点写的都是**确定性事实**——出题者已核实过，任何合法口径都会得到同一结论，
     所以只看结果对不对，不看用什么路径算出来的：实现方式、工具、中间步骤与 criterion
     描述的不同一律不扣，但结论性数值（行数、系数、方向与量级）应与 criterion 给出的
     数值或区间一致，或在合理误差内。criterion 给的是区间时，落在区间内即算满足。
   - 负分评分点（points < 0）：报告确实犯下该错误则 status="triggered"，否则 "not_triggered"。
     负分点写的是这道题上**已知会走偏**的做法或结论——要么是容易踩的具体错误，要么是
     一条看似合理但会得出错误数值的路径。报告没提到相关内容不算 triggered；必须报告
     明确采用了该做法或明确主张了该结论才 triggered。报告用了另一条口径、得到的数值
     与任何正分点都不冲突，这不构成 triggered——负分只惩罚被点名的那个错误。
3. 严格按报告内容判定，不要脑补报告没有的内容；也不要因为措辞不同而误判实质正确的内容。
4. 每条 reason 用中文一句话说明依据，不要大段照抄 criterion 原文。

[输出格式] 只输出一个 JSON 对象，不要输出任何其他文字：
{{
  "verdicts": [
    {{"id": "<评分点 id>", "status": "satisfied|not_satisfied|triggered|not_triggered", "reason": "<一句话>"}},
    ...
  ]
}}"""
