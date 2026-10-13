"""任务产物契约：schema、交付方式、校验与 rubric 打平。

本模块是 rubric 结构的**唯一定义处**，出题两侧都从这里取口径：

- ``prompts.py`` 用 :data:`OUTPUT_SCHEMA` 和 :data:`DELIVERY_MARKER` 告诉 agent 交什么、怎么交；
- ``pipeline.py`` 用 :func:`extract_task` + :func:`validate_task` 收产物，并用
  :func:`build_rubric` 把两部分 rubric 打平成带 id 的 criteria 统计评分点。

产物 ``tasks.json`` 经 ``task.export`` 导出为 harbor 基准任务（容器内判分），
或交下游评测工具消费；``build_rubric`` 打平出的 ``dc-N`` / ``id-N`` 编号是
判分结果与评分点的关联键。

契约由 prompts 声明、由此处校验——两者共用同一组常量，不允许各自演化出
"prompt 要求写文件、验证器却只认标记块"这类漂移（历史 task_try 的实际问题）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import TaskGenError

#: rubric 的两个部分。A 判数据操作是否正确，B 判洞察是否成立；顺序即 criteria id 前缀顺序。
SECTIONS: Tuple[str, ...] = ("data_computation", "insight_discovery")

#: criteria id 前缀（``dc-1`` / ``id-3``）。产物契约的一部分，改动即破坏历史判分数据可比性。
SECTION_PREFIX: Dict[str, str] = {"data_computation": "dc", "insight_discovery": "id"}

#: 单条分值区间：正分 [1,5]（关键事实最多 5），负分 [-3,-1]（错误结论扣分）；0 无意义。
POINTS_MIN, POINTS_MAX = -3, 5

#: 出题时只读世界副本在产物目录下的名字。它是产物布局契约的一部分：下游评测工具
#: 按 ``<tasks>/<world>/world_copy/env`` 找回该世界的 env/（副本不存在时回落原始
#: 世界目录），所以字面量收在 contract 而不是散在各消费方。
WORLD_COPY = "world_copy"

#: 世界 env/ 的相对位置：``<world 或 world_copy>/env``。
ENV_DIRNAME = "env"

#: 交付标记：agent 把最终 JSON 包在这对标记之间输出。
#:
#: 选标记块而非写文件，是因为出题 agent 的工作目录与世界副本相邻，写文件多一次
#: "产物落在哪"的歧义；标记块让"最终回复"成为唯一交付面，且能被 stdout 直接校验。
DELIVERY_BEGIN = "---TASKS_JSON_BEGIN---"
DELIVERY_END = "---TASKS_JSON_END---"

_DELIVERY_RE = re.compile(
    re.escape(DELIVERY_BEGIN) + r"\s*(.+?)\s*" + re.escape(DELIVERY_END), re.S)

#: agent 只需产出的两个字段。身份字段（task_id/task_kind/target_node/degree_kind/
#: degree）由 :func:`attach_identity` 从本次指派注入，**不经 agent 往返**——它们是
#: 我们自己的簿记：``task_id`` 来自 ``select_targets``，pipeline 全程用它做目录名、
#: ``tasks.json`` 的键与续跑判定，agent 填的那份只被拿来和指派比对一次就丢弃。
#: 让 agent 抄一遍我们已知的常量，只会凭空多出一类"抄错就整份重试"的失败可能。
AGENT_FIELDS: Tuple[str, ...] = ("task_prompt", "rubric")

#: 由 pipeline 注入的身份字段（顺序即写入 ``tasks.json`` 的顺序）。
IDENTITY_FIELDS: Tuple[str, ...] = (
    "task_id", "task_kind", "target_node", "degree_kind", "degree")

#: rubric 单条评分点的全部字段——描述与分值，没有第三个。
#:
#: 曾另有 ``evidence`` 存"裁判专用金标准"，与 ``criterion`` 边界天然模糊：两者都在
#: 描述同一个事实，可核验数值写哪边全凭出题者猜，而 judge 拿到的是两者拼接后的文本，
#: 拆成两个字段并没给它任何额外信息。合并后规则唯一：``criterion`` 自包含到能独立
#: 判定。这里显式列成元组是为了让校验器能拒掉多余字段——只写在提示词里的话，"顺手
#: 再补个 evidence"不会有任何报错，静默漂移回两字段。
ITEM_FIELDS: Tuple[str, ...] = ("criterion", "points")

OUTPUT_SCHEMA: Dict[str, Any] = {
    "tasks": [{
        "task_prompt": "业务日常那种短平快但明确的分析委托，一到两段",
        # 每条只有 criterion + points 两个字段。原先另有 evidence 存判定金标准，
        # 现已合并进 criterion：两者都在描述同一件事，出题者要猜数值写哪边（实测
        # 常见把数值写进 criterion 而 evidence 只剩一句复述），而 judge 拿到的是
        # 拼接后的文本，分两个字段并没有给它多余信息。合并后规则唯一：criterion
        # 必须自包含到能独立判定，可核验数值直接写在句子里。
        #
        # 正负号承载**认知状态**，不是重要性：
        # - 正分（确定性事实）：任何合法口径都会得到同一结果，故可安全奖励；
        # - 负分（潜在错误 / 不确定路径）：多条可辩护路径给出不同数值时，不能要求
        #   某一个数，只能把已知会走偏的方向写成负分项。
        # 这个区分只有实际跑过数的出题者才知道，无法模板化，所以两类各给一条示例。
        #
        # 负分项**没有**自己的 section：它按"判的是数据操作还是洞察"归入下面
        # 两节之一，只靠 points 为负来区分。schema 里各给一条负分示例，是因为
        # 只在提示词里说"另外再写几条负分项"会被读成"另开一节"——实测 35 道题里
        # 24 道自创了 negative / negative_scoring / negative_rubric /
        # negative_items 四种外挂节名，共 117 条负分项，而 build_rubric 只遍历
        # SECTIONS，这些条目在判分时被静默丢弃（占全部负分项的 87%）。
        "rubric": {
            "data_computation": [{
                "criterion": "一个绕不过去的预处理/清洗步骤（解析、粒度、关联键、缺失与"
                             "重复处理、该剔除的行）：跳过它下游结论就会错，且无论用哪条"
                             "合法路径都得到同一结果，句中写明文件、字段、口径与可核验"
                             "数值或区间",
                "points": 2,
            }, {
                "criterion": "答题者真实可能踩的具体清洗或口径错误，或一条会走偏的"
                             "不确定路径：写明这样算会得到什么数、正确口径下对照是多少",
                "points": -2,
            }],
            "insight_discovery": [{
                "criterion": "一个题面必答的分析结论或伪线索排除（分组比较、相关与滞后、"
                             "控制混杂后的量级）：缺了它答卷就不完整，方向、量级、限定"
                             "条件与支持它的可核验数值都写在句中",
                "points": 3,
            }, {
                "criterion": "答题者真实可能主张的具体错误结论：写明该结论是什么，"
                             "以及数据中反驳它的数值",
                "points": -2,
            }],
        },
    }]
}


def resolve_env_dir(tasks_root: Path, world: str,
                    world_root: Optional[Path] = None) -> Path:
    """定位该世界的 ``env/``：优先出题时留下的只读副本，副本不在则回落原始世界。

    Harbor 导出与下游评测都需要 ``env/``（导出把它拷进镜像的
    ``environment/data/``）。默认位置是出题产物里的 ``<tasks>/<world>/world_copy/env``。

    但大规模出题一般用 ``--no-keep-copy`` 不留副本（副本随世界数量线性占盘），
    产物里根本没有 ``world_copy``。回落到 ``<world_root>/<world>/env`` 即可：
    副本本来就是原始世界的逐字节拷贝，``env/`` 内容相同。只读性由调用方保证
    （导出是拷出去，不原地改）。
    """
    copied = tasks_root / world / WORLD_COPY / ENV_DIRNAME
    if copied.is_dir() or world_root is None:
        return copied
    return Path(world_root) / world / ENV_DIRNAME


def render_schema() -> str:
    """给 prompt 用的 schema 文本。"""
    return json.dumps(OUTPUT_SCHEMA, ensure_ascii=False, indent=2)


def extract_task(text: str) -> Optional[Any]:
    """从 agent 最终回复里取交付标记块内的 JSON；找不到或不可解析返回 None。"""
    if not text:
        return None
    match = _DELIVERY_RE.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except json.JSONDecodeError:
        return None


def attach_identity(task: Dict[str, Any], material: Dict[str, Any]) -> Dict[str, Any]:
    """给已校验的产物补上身份字段，返回字段有序的新 dict。

    身份是我们的簿记，不是 agent 的产出：``select_targets`` 已经确定了 ``task_id``、
    方向与目标节点，让 agent 再抄一遍只会凭空多出一类失败。这里单向注入，因此
    ``tasks.json`` 里这几个字段与指派**恒等**，不存在"校验通过但抄错"的中间态。

    身份在前、agent 产出在后：``tasks.json`` 是人要读的，先看是哪道题更顺。
    """
    identity = {
        "task_id": material["task_id"],
        "task_kind": material["mode"],
        "target_node": material["target"],
        "degree_kind": material["degree_kind"],
        "degree": material["degree"],
    }
    return {**identity, **{key: task[key] for key in AGENT_FIELDS}}


def validate_task(value: Any) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """校验双部分 rubric 契约。

    只校验 :data:`AGENT_FIELDS` 的结构，不对题面内容或 rubric 条目数做实质判断——
    探索与出题的自由度归 agent。身份字段不在校验范围内：它们由
    :func:`attach_identity` 注入，agent 交的产物里本就不该有。

    返回 ``(task, errors)``：errors 非空时 task 为 None，错误文本会回喂给下一次重试。
    """
    errors: List[str] = []
    if not isinstance(value, dict) or not isinstance(value.get("tasks"), list):
        return None, ["顶层必须包含 tasks 数组"]
    tasks = value["tasks"]
    if len(tasks) != 1 or not isinstance(tasks[0], dict):
        return None, ["tasks 必须恰含一个对象"]
    task = tasks[0]

    if not isinstance(task.get("task_prompt"), str) or not task["task_prompt"].strip():
        errors.append("task_prompt 必须是非空字符串")

    rubric = task.get("rubric")
    if not isinstance(rubric, dict):
        errors.append("rubric 必须是对象")
        return None, errors
    # 未知 section 必须报错回喂，不能默默留着：build_rubric 只遍历 SECTIONS，
    # 多出来的节会被静默丢弃——校验通过、判分时消失，是最难发现的一类错。
    unknown = [key for key in rubric if key not in SECTIONS]
    if unknown:
        errors.append(
            f"rubric 只允许 {list(SECTIONS)} 两节，多余的 {unknown} 不被判分："
            "负分项不单独成节，按判的是数据操作还是洞察归入这两节之一，"
            "用负 points 区分")
    for section in SECTIONS:
        items = rubric.get(section)
        if not isinstance(items, list) or not items:
            errors.append(f"rubric.{section} 必须是非空数组")
            continue
        for index, item in enumerate(items):
            label = f"rubric.{section}[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{label} 必须是对象")
                continue
            extra = [key for key in item if key not in ITEM_FIELDS]
            if extra:
                errors.append(
                    f"{label} 只允许 {list(ITEM_FIELDS)} 两个字段，多余的 {extra} "
                    "不被判分：可核验数值直接写进 criterion，不另开 evidence 之类的字段")
            if not isinstance(item.get("criterion"), str) or not item["criterion"].strip():
                errors.append(f"{label}.criterion 必须非空")
            points = item.get("points")
            if (not isinstance(points, int) or isinstance(points, bool)
                    or points == 0 or not POINTS_MIN <= points <= POINTS_MAX):
                errors.append(
                    f"{label}.points 必须是 [{POINTS_MIN},-1] 或 [1,{POINTS_MAX}] 的整数")
    # 至少一条正分：``grade.score`` 的分母是两节合计的正分总和，全为负分时分母为 0，
    # reward 对任何作答都恒等于 0——校验通过、判分静默失效，比报错难发现得多。
    # 只要求全局有正分而不要求每节都有：确定性事实集中在哪一节是这个世界数据的性质，
    # 逼一个口径分歧极大的 data_computation 硬凑正分点，正是本次改动要避免的。
    if not errors and not any(
            item["points"] > 0
            for section in SECTIONS for item in rubric.get(section, [])):
        errors.append(
            "rubric 至少要有一条正分评分点（两节合计），否则 reward 恒为 0："
            "负分只界定错误边界，无法表达答对了什么")
    return (task if not errors else None), errors


def build_rubric(world: str, task: Dict[str, Any]) -> Dict[str, Any]:
    """把两部分 rubric 打平为带 id 的 criteria（产物契约的一部分，供下游判分使用）。

    id 形如 ``dc-1`` / ``id-3``，section 内从 1 顺序编号。此编号是判分结果与
    评分点的关联键，必须稳定。
    """
    criteria = []
    for section in SECTIONS:
        prefix = SECTION_PREFIX[section]
        for index, item in enumerate(task["rubric"].get(section, []), 1):
            criteria.append({
                "id": f"{prefix}-{index}",
                "section": section,
                "criterion": item["criterion"],
                "points": item["points"],
            })
    if not criteria:
        raise TaskGenError(f"{world}/{task.get('task_id')}: rubric 为空，无法判分")
    return {
        "world": world,
        "task_id": task["task_id"],
        "task_kind": task["task_kind"],
        "target_node": task["target_node"],
        "criteria": criteria,
    }


#: 比对窗口：criterion 取前 N 字符，避免交付模板措辞误报。
_LEAK_WINDOW = 40

#: 路径与标识符——题面合法给出的部分，不构成泄漏。
#:
#: 连同紧挨其中的数字一起剥掉（``a.csv``、``metrics_v2``、``queue_5m``），但**不剥
#: 独立出现的数字**：``7500 行``、``高 3.2 个百分点`` 里的数字正是最强的结论信号。
_IDENT_RE = re.compile(r"[A-Za-z_][0-9A-Za-z_./\-]*|[0-9A-Za-z_./\-]*[A-Za-z_]")

#: 结论标志：出现其一即认为片段在陈述**结果**而非数据位置。
#:
#: 判据从"剩余自然语言长度"换成"是否含结论标志"，是因为长度在两个方向都失效：
#: 旧式 ``[\w...]+`` 的 ``\w`` 在 unicode 下**包含汉字**，"留存呈饱和曲线，12 个月后
#: 边际增益趋近 0"被剥成 ``，`` 而落到阈值下豁免——中文结论句照抄进题面一律不报；
#: 而只剥 ASCII 的话，``env/a.csv 与 env/b.csv 按 uid 关联`` 剩下"与按关联"够长，
#: 题面合法点名文件反而误报。真正区分两者的不是长度，是片段有没有在说"结果是多少"。
#: 合并 evidence 后 criterion 同时承载口径与结论，这条是唯一的泄漏兜底，两类错都
#: 不能有。
#:
#: 这份词表必然不全，漏掉的结论措辞会放过去。它是兜底而非主防线：不泄漏首先由出题
#: 提示词那条硬底线约束（见 ``task.synth.prompts``），这里只拦最明显的照抄。
_CONCLUSION_RE = re.compile(
    r"\d|[一二三四五六七八九十百千万倍成]|"
    r"显著|相关|因果|导致|驱动|上升|下降|增加|减少|高于|低于|大于|小于|超过|不足|"
    r"最强|最大|最小|主要|其次|反向|正向|负向|无关|抵消|放大|削弱|趋近|饱和|峰值")


def check_no_leak(world: str, task: Dict[str, Any], instruction: str) -> List[str]:
    """题面不得含 rubric 的结论性文本。

    题面合法可以给出文件路径、文件名与字段名（答题者需要知道数据在哪、有哪些列）。
    不合法的是结论：数值、方向、量级、排序。因此判定分两步——片段必须既出现在题面
    里，又在剥掉路径与标识符后仍带**结论标志**（数字、量级词、因果/比较词）才算泄漏。
    """
    def conclusive(fragment: str) -> bool:
        return bool(_CONCLUSION_RE.search(_IDENT_RE.sub("", fragment)))

    problems: List[str] = []
    for section in task["rubric"].values():
        for item in section:
            fragment = item["criterion"][:_LEAK_WINDOW]
            if fragment in instruction and conclusive(fragment):
                problems.append(
                    f"{world}/{task['task_id']}: 泄漏 criterion: {fragment!r}")
    return problems
