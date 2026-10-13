"""DAG 生成核心：领域 → LLM 出图 → 校验（复用 data_world.dag）→ 重试回喂。

设计要点：
- 生成骨架移植自 ba-env-factory 的 ``generate_dag``：校验失败时把**具体原因 + 上次原始
  输出**回喂给 LLM 做针对性重试，而不是盲目重复。
- 校验与拓扑分层复用 ``data_world.dag.validate_and_normalize_dag``（单向软依赖），
  不重复实现，且与下游 data-world 的接受口径完全一致。
- prompt 沿用 ba-env 的"纯净领域因果图"取向：只描述变量簇之间的因果，贴合领域真实
  机理，node 必标 system+grain，edge 用 note 如实描述真实因果关系（不用 +/- 简化符号）；禁止分析方法/统计陷阱/悖论/元标签。
"""

from __future__ import annotations

import json
import re
import textwrap
import time
from typing import Any, Dict, List, Mapping, Optional

from data_world.dag import validate_and_normalize_dag

from .categories import CATEGORIES, keyword_category, render_catalog
from .llm import LlmClient

DAG_SYSTEM = (
    "你是资深数据建模专家。严格只输出一个 JSON 对象, 不要任何解释、前后缀或 markdown 代码围栏。"
)


def _parse_json_loose(text: str) -> Optional[Any]:
    """尽力从模型输出里抠出 JSON 对象（容忍代码围栏/前后缀噪声）。"""
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```[a-zA-Z]*\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped).strip()
    try:
        return json.loads(stripped)
    except Exception:
        pass
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(stripped[start:end + 1])
        except Exception:
            return None
    return None


def choose_category(
    client: LlmClient,
    persona: str,
    *,
    explicit: Optional[int] = None,
    attempts: int = 3,
    verbose: bool = False,
) -> int:
    """决定这次造哪个领域的 DAG：显式指定优先，否则 LLM 路由，失败关键词兜底。

    路由调用在两层上都做重试：``client.complete`` 内部已对**瞬时错误**指数退避；
    这里再对**内容无效**（LLM 返回但抠不出有效领域编号）额外重试 ``attempts`` 次，
    每次之间小幅退避。两层都耗尽后才退回关键词兜底 ``keyword_category``。
    """
    if explicit is not None:
        if explicit not in CATEGORIES:
            raise ValueError(f"category 必须是 1..{max(CATEGORIES)}")
        return explicit
    n = max(CATEGORIES)
    prompt = (
        f"下面是一个数据分析人物画像, 以及 {n} 个【数据分析领域】(跨学科)。"
        f"请按这个分析者所处的【领域/世界】来选(看 ta 分析的是哪类对象/数据), 而不是按分析方法。"
        f"只输出最贴合的【一个】领域编号(1-{n}), 不要解释。\n\n"
        f"画像: {persona}\n\n领域:\n{render_catalog()}\n\n只输出一个数字:"
    )
    last = ""
    for i in range(1, max(1, attempts) + 1):
        ok, res = client.complete(prompt)
        last = res or ""
        match = re.search(rf"\b([1-9]|1[0-6])\b", last)
        if ok and match and int(match.group(1)) in CATEGORIES:
            return int(match.group(1))
        if verbose:
            print(f"  [route] 第 {i}/{attempts} 次路由无有效编号: {last[:80]}", flush=True)
        if i < attempts:
            time.sleep(min(5 * (2 ** (i - 1)), 60))
    if verbose:
        print(f"  [route] 路由重试耗尽, 用关键词兜底: {last[:80]}", flush=True)
    return keyword_category(persona)


def _dag_prompt(persona: str, cid: int) -> str:
    name, subs = CATEGORIES[cid]
    subs_txt = "\n".join(f"  - {s}" for s in subs)
    return textwrap.dedent(f"""\
        为下面这个数据分析场景, 设计一张**纯净的领域因果 DAG**, 作为后续合成数据环境的骨架。
        它只描述这个领域里**变量簇之间的因果关系**(像"天气→销量""用药→指标""降雨→产量"那样干净直接),
        要**贴合该领域的真实实体与机理**(医疗就用诊疗/检验/再入院, 气候就用气象/排放/产量, 交通就用出行/路网/延误…),
        不要套用商业模板, 也不要涉及任何分析方法、统计陷阱、悖论或元标签。

        服务对象(persona): {persona}
        领域: [{cid}] {name}
        可参考的子主题:
        {subs_txt}

        要求:
        - 8~14 个节点(node), 每个节点是**一类/一簇变量**(不是单个变量): 用 label 命名, variables 列出代表性字段。
        - 节点横跨多个业务侧面(外生因素、运营动作、用户/客户、产品/交易、结果指标等), 能连成有纵深的因果链。
        - 每个节点必须标注 **system**(它应该落到哪个真实数据源系统, 例如 CRM/计费/埋点/EHR/检验系统/气象站/仪器日志……,
          同一系统可以承载多个节点, 但**不要凭空发明太多系统**, 4-6 个系统即可)和 **grain**(数据粒度, 如
          "事件级"/"按天聚合"/"维度表"/"SCD 缓慢变化维")——这两项是后续**按图施工**用的落地元数据, 必填。
        - 边(edge)表达**因果方向**: src 因→dst 果。**不要用 +/- 这种简化符号**, 而是在 note 里用一两句话
          如实说清这两个变量簇在真实世界里的因果关系——可以是单调增强/抑制, 也可以是**非线性(阈值/饱和/
          边际递减)、倒 U、交互调节(效应取决于第三个变量)、滞后(写明滞后时长, 如 30/60 天后显现)**等更贴近
          真实机理的形态; 宁可具体真实也不要压成线性。(时间本身作为数据里的一个普通字段/维度体现, 不需要在 DAG 里单独建模)
        - src/dst 必须引用上面某个 node 的 label。要有多条链路汇聚/分叉, 形成连通的有向无环图(不要孤立点、不要环)。

        只输出如下结构的 JSON(不要任何多余文字):
        {{
          "nodes": [{{"label": "天气", "variables": ["date","city","temp_c","precip_mm","is_rain"],
                     "system": "气象站开放数据", "grain": "按天聚合"}}],
          "edges": [{{"src": "促销力度", "dst": "订单量", "note": "促销提升订单, 但边际递减: 折扣超过~30%后增量趋于饱和"}}]
        }}
        """)


def generate_dag(
    client: LlmClient,
    persona: str,
    cid: int,
    *,
    attempts: int = 5,
    verbose: bool = False,
) -> Dict[str, Any]:
    """生成一张通过 data_world.dag 校验的纯净因果 DAG，返回规范化后的 dict。

    校验失败会把具体原因回喂给 LLM 做针对性重试；耗尽 attempts 仍失败则抛出最后的原因。
    """
    from data_world import WorldGenError  # 局部 import：仅用于捕获校验异常类型

    base_prompt = _dag_prompt(persona, cid)
    last_reason = "尚未生成"
    last_raw = ""
    for i in range(1, attempts + 1):
        if i == 1:
            prompt = base_prompt
        else:
            prompt = base_prompt + textwrap.dedent(f"""\n
                注意: 你上一次(第 {i - 1} 次)的输出没有通过校验, 具体问题如下, 请逐条修正:
                  - {last_reason}

                你上一次的原始输出(供你对照修正, 不要重复同样的错误):
                {last_raw[:1500]}

                请**只输出**修正后的合法 JSON(不要任何多余文字/前后缀/markdown 代码围栏)。""")
        ok, res = client.complete(prompt, system=DAG_SYSTEM)
        if not ok:
            last_reason = f"LLM 调用失败: {res[:200]}"
            last_raw = ""
            if verbose:
                print(f"  [dag] 第 {i}/{attempts} 次调用失败: {res[:80]}", flush=True)
            continue
        last_raw = res
        parsed = _parse_json_loose(res)
        if parsed is None:
            last_reason = "JSON 解析失败: 输出不是合法 JSON(可能带了多余文字/代码围栏/被截断)"
            if verbose:
                print(f"  [dag] 第 {i}/{attempts} 次 JSON 解析失败", flush=True)
            continue
        try:
            dag = validate_and_normalize_dag(parsed)
        except WorldGenError as exc:
            last_reason = str(exc)
            if verbose:
                print(f"  [dag] 第 {i}/{attempts} 次校验失败: {str(exc)[:120]}", flush=True)
            continue
        if verbose and i > 1:
            print(f"  [dag] 第 {i}/{attempts} 次重试成功", flush=True)
        return dag
    raise ValueError(f"DAG 生成耗尽 {attempts} 次重试, 最后原因: {last_reason}")


def dag_display_meta(cid: int) -> Mapping[str, Any]:
    """返回领域元信息（供 CLI 打印/命名用）。"""
    name, _ = CATEGORIES[cid]
    return {"category_id": cid, "category_name": name}
