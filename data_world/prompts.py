from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .fs import format_json

SYSTEM = """你是资深的合成数据世界架构师、因果建模工程师和数据生成程序员。你的唯一目标是：给定一张因果 DAG，忠实地把它模拟成一个"真实组织/系统长期运行后 dump 出来的数据环境"——不出题、不设计难度、不强加外部约束，只让这个世界像真的、内部自洽、可复算。
先构造统一的世界真值与因果演化，再通过多个不完美观测系统投影成数据文件。最终环境应像真实数据室：多来源、多格式、多粒度、规模与该系统真实运行相称、带有来自现实过程的瑕疵——而不是几张对齐的干净表。世界的规模、时间跨度、语言、复杂度都应由 DAG 与所属领域自然决定，不要人为钉死。
硬约束：世界事实/因果演化/观测系统/物理文件分离；历史 canonical state 只读；实体ID、时间轴、单位跨层一致；每条因果边在数据里数值上真实成立、可追踪、可独立复算；env 只含真实分析现场的数据与 in-world 物料；脚本、参数、latent state、校验结果只进 .build；固定随机种子并实际执行、检查生成程序。
每个阶段除了结构化结果，还要在自己的工作目录留一份 markdown 自述，讲清这一步的设计理由与机制——结构化结果记录"查过了、结果如何"，自述记录"为什么这么建、机制是什么、放弃了什么"，后继阶段和独立审计者要靠它理解你的意图。自述属于过程记录，只进 .build；env 里永远只有 in-world 的数据与物料。"""

CONTRACT_SCHEMA = {
    "schema_version": 1,
    "world_identity": {"title": "", "setting": "", "scope": "", "analysis_audience": ""},
    "timeline": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD", "cadence": "", "anchor_events": []},
    "entities": [{"id": "", "description": "", "population": "", "canonical_key": "", "attributes": []}],
    "mechanisms": [{"edge": "src->dst", "relation_form": "还原 DAG note 里的真实因果关系形态(单调增强/抑制、阈值/饱和、倒U、交互调节、滞后等)", "generative_mechanism": "", "heterogeneity": "", "time_behavior": "", "confounders": []}],
    "global_invariants": [], "observation_systems": [], "datasets": [], "causal_phenomena": [], "layer_contracts": [],
}


def contract(brief: Mapping[str, Any], dag: Mapping[str, Any], layers: Sequence[Sequence[str]], last_error: str = "") -> str:
    retry = f"\n上次输出校验失败，必须修正：{last_error}" if last_error else ""
    return SYSTEM + f"""

只做 World Contract 编译，不生成数据。分析立场（仅给定分析受众，seed 用于可复算；不提供世界标题/背景/范围——这些由你从 DAG 与领域自行推断）：
{format_json(brief)}
DAG（不可改写的依赖骨架）：
{format_json(dag)}
拓扑层：{format_json(layers)}{retry}

只输出一个合法 JSON 对象（可用 ```json 围栏包裹），不要任何解释性文字或前后缀。该对象至少包含以下字段（值需按语义填充，而非照抄占位）：
{format_json(CONTRACT_SCHEMA)}
要求：world_identity 的 title/setting/scope 由你从 DAG 的因果结构、节点语义（name/description/system/grain/variables）与所属领域，按第一性原理自行推断，不要期待外部给定，也不要用 DAG 文件名充数；节点是 canonical world 状态簇而非文件；建立统一实体 registry、时间轴和不变量；mechanisms 逐边覆盖且忠实还原每条边 note 描述的真实因果关系（方向 + 形态，可非线性/阈值/交互/滞后，edge 写成 "src->dst"）；先定义世界再定义观测系统和 datasets；观测系统应多样（多来源/多格式/多粒度），datasets 的 path 必须位于 env/ 下且不是脚本；数据的瑕疵必须来自现实观测过程而非人为噪声；causal_phenomena 如实记录这个世界里因 DAG 因果自然成立、且能从 env 数据复算出来的现象（不是设计给谁做的题、不设"误导"，只描述"数据里客观存在什么因果关系、如何从哪些文件复算验证"）；world 的规模/时间跨度/语言由领域自然决定，不要人为钉死；layer_contracts 与拓扑层一一对应且每节点恰好一次。"""


def layer(index: int, nodes: Any, edges: Any, parents: Any, earlier_results: Any,
          contract_path: Path, dag_path: Path, layer_dir: Path, result_path: Path,
          doc_path: Path, env_dir: Path, seed: int, last_error: str = "") -> str:
    """某个拓扑层的生成提示词。

    ``index`` 只用于 ``layer_result.json`` 的对账字段，不进正文叙述：层号是拓扑排序
    的产物，对"这批状态是什么"没有任何信息量，写进提示词只会让模型用"第 N 层"指代
    实际内容（后继层与审计者就得反查层号才知道那层是什么）。
    """
    retry = f"上次校验失败，必须修正：{last_error}" if last_error else ""
    labels = [node["label"] for node in nodes]
    # 句中提及用紧凑写法：format_json 的 indent=2 适合独立上下文块，嵌在句子里会
    # 把一句话拆成多行，读起来像是另一段。
    inline = "、".join(f"「{label}」" for label in labels)
    return SYSTEM + f"""

这一步要生成的是这批世界状态：{inline}。它们在同一个事务里联合生成，全部成功才提交。{retry}
合同={contract_path}；DAG={dag_path}；工作目录={layer_dir}；env={env_dir}（严禁写入）。
本批节点：{format_json(nodes)}
它们的入边：{format_json(edges)}
这些入边的来源节点：{format_json(parents)}
已建成的上游状态（这些节点的 canonical state 已经存在，是你的输入）：{format_json(earlier_results)}

先读合同，再按入边找到你真正依赖的上游状态去读——每份上游结果的 node_outputs 里写着它产出了哪些文件、粒度、canonical_key 和字段，那就是你要对接的接口；同名实体必须复用上游已有的 id、时间轴与单位，不要另起一套。上游状态一律只读。已建成的上游可能不止你的直接父节点，与你无关的那些不必逐字通读，但凡涉及共享实体、时间轴或背景事件就要对齐。

在工作目录写并运行确定性脚本（seed={seed}）产出 canonical state。规模要与该系统在真实世界运行相应时长后会积累的量级相称（一个真实组织跑这么久该有多少行就多少行，不要玩具级几十行，也不要凭空堆砌），并按节点 grain 采用相应粒度（事件级明细/按天周月的快照或聚合/维度表/SCD 缓慢变化维）。逐入边验证 note 描述的因果关系（方向/形态/阈值/交互）、时间、异质性；本批若没有入边，则验证分布与覆盖。

写 {result_path}，给后继阶段留下机器可读的账本：status="complete"，state_files（本层产出的 canonical state，必须存在、非空且不是脚本），node_outputs（**逐节点**写清产出文件、grain、canonical_key、字段清单与行数，下一层就靠它对接，写细一点），parent_files_read，edge_checks（逐入边的验证结论与可复核数值），invariants_checked。另外带上两个对账字段，照填即可：layer={index}，nodes={json.dumps(labels, ensure_ascii=False)}。结束前重新读取验证。不要写 env/、世界根目录的 WORLD.md、dag.json。

另写 {doc_path}，这是本批状态的自述。用连贯的自然段落讲清：这一步造出了什么 canonical state；每个节点的生成机制是什么、关键参数为什么取这个值；逐条入边的因果形态是怎么在数据里落实的（写机制本身——用什么函数形态、阈值定在哪、异质性从哪来，而不是复述 edge_checks 的 pass/fail，那份账本已经在 {result_path} 里了）；规模与粒度凭什么定在这一档；探索中试过又放弃的做法、被迫做的近似、留下的已知局限；后继层与独立审计者读这份 state 时最容易误解或踩坑的地方。这些是只有你这个执行者才知道的信息，不写下来就永久丢失。写你实际做过的判断，不要为凑长度复述已有的结构化字段。开头就点明这批状态实际是什么（哪些实体、什么机制），别只写层号——读者是靠内容而不是靠编号来找它的。"""


def projection(contract_path: Path, layer_results: Any, projection_dir: Path,
               doc_path: Path, env_dir: Path, last_error: str = "") -> str:
    retry = f"上次校验失败，必须修正：{last_error}" if last_error else ""
    return SYSTEM + f"""

执行统一 Observation Projection。{retry}
合同={contract_path}；层结果={format_json(layer_results)}；工作目录={projection_dir}；最终 env={env_dir}。
canonical world 只读；按 observation_systems/datasets 投影成像真实数据室的文件。要点：不同系统用不同文件格式（csv/tsv/jsonl/嵌套 json/.log/.sql dump/类 Excel 导出/kv 配置等），不同的命名风格、主键命名、日期格式、编码与粒度；跨系统映射源于统一 registry，但同一实体在不同系统里可以呈现不同 id 写法（大小写/前后缀/类型差异）。真实瑕疵——错配、缺失（多种缺失表示）、延迟/迟到、重复、单位不统一、神秘枚举码、schema 漂移、离群值——必须来自明确的观测机制，底层真值不变、因果仍可复算；不是人为撒噪声。文件之间靠不显式标注的外键互相钩稽，env 里不放数据字典（真实 dump 没有现成 schema）。脚本和真值只留工作目录，最终数据写 env。env 禁止 schema/manifest/答案/任务/报告/脚本/latent state。实际检查解析、非空、规模、时间、键关系和质量机制。写 projection_result.json，包含 status="complete", env_files（完整精确清单）, datasets, identity_checks, temporal_checks, quality_checks, source_state_files, scripts。

另写 {doc_path}，这是本次投影的自述。投影是全流程里自由度最高的一步——格式、命名风格、主键写法、日期编码、粒度、瑕疵全由你定，而这些决定的理由无法从产出的数据文件反推出来。用连贯的自然段落讲清：每个观测系统在这个世界里是什么角色、它为什么长成这样（它的格式与命名风格、主键写法、日期编码、粒度各自对应什么样的现实系统与录入习惯）；跨系统身份映射的骨架，同一实体在各系统里的 id 写法差异从何而来；逐条写出你注入的每个瑕疵及其背后的观测机制（谁在什么环节因为什么导致了它），并说明为什么它只改变可观测性、底层真值不变、因果仍可从 env 复算；确定性与种子是怎么处理的；设想过又放弃的方案、被迫做的近似、留下的已知局限；哪些文件靠哪些没有显式标注的外键互相钩稽。写机制与理由，不要复述 quality_checks 的 pass/fail，那份账本已经在 projection_result.json 里了。

这份自述连同脚本一律只留在 {projection_dir}，绝不写进 env——真实的数据 dump 不会附带自己的设计说明书，它出现在 env 就等于把观测机制和瑕疵成因直接告诉了下游分析者。"""


def audit(dag_path: Path, contract_path: Path, layers_dir: Path, projection_result: Path,
          env_dir: Path, world_md: Path, audit_dir: Path, last_error: str = "") -> str:
    """审计提示词。

    刻意不传 dag_sha256 / seed：它们是构建期簿记，权威副本已在 ``progress.json``
    与审计者自己写的 ``audit_result.json`` 里，没有任何下游从 WORLD.md 解析它们。
    让审计者把这些数字抄进说明书正文，只会污染一份本该是"世界说明书"的文档。
    """
    retry = f"上次校验失败，必须修正：{last_error}" if last_error else ""
    return SYSTEM + f"""

作为独立审计者复算最终世界。{retry}
DAG={dag_path}；合同={contract_path}；层={layers_dir}；投影={projection_result}；env={env_dir}。
写详细 {world_md} 和 {audit_dir / 'audit_result.json'}，脚本只进 {audit_dir}。

WORLD.md 是对这个世界的忠实说明书（不是答案册也不是题目），基于实测，包含世界/实体/时间、各簇世界状态的因果机制、系统文件与 join 关系、观测质量与瑕疵机制、每条 causal_phenomenon 的因果关系/涉及文件字段/复算方法/实测量化锚点、边与不变量检查、已知限制。预期不成立必须如实标失败。

写 WORLD.md 时，站在这个世界内部来写：它讲的是这家组织/这套系统本身，以及它的数据实际长什么样。构建流水线的簿记不属于这份文档——世界所在的目录名或编号、生成批次、DAG 文件的哈希、随机种子、"第几层"这类流水线内部编号，一律不要出现在正文里，标题更不要带。这些标识对读者理解这个世界毫无信息量，而且会把一份世界说明书变成构建日志；机器要查的话，它们已经在 audit_result.json 与 progress.json 里了。给这个世界起标题就用它在世界内部的真实名字（合同里的 world_identity.title）。提到层的时候，说清那一层实际是什么（哪些实体、什么机制），而不是只说层号。

audit_result 是机器要读的账本，包含 status="complete", env_files_checked, edge_checks, phenomenon_checks, invariant_checks, failures, warnings, scripts。不得修改 env 或 canonical state。"""
