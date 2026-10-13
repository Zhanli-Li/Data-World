"""16 个【数据分析领域】（跨学科），用于驱动 DAG 生成的多样性。

category 只决定"世界"（有哪些实体/数据源/天然因果），分析方法与洞察机理
（混杂/滞后/mix-shift…）是横切的，不在这里。整体移植自 ba-env-factory。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

CATEGORIES: Dict[int, Tuple[str, List[str]]] = {
    # ---------------- A. 商业与经济 ----------------
    1: ("Commercial Growth & Acquisition / 商业增长与获客", [
        "新增/激活/留存/召回（AARRR）、北极星指标拆解",
        "渠道漏斗与多触点归因、CAC/ROAS/获客回本",
        "销售漏斗、赢单率、Pipeline 与 Forecast",
        "功能采用、用户行为路径、A/B 实验、留存曲线/Aha",
    ]),
    2: ("Customer, Pricing & Revenue / 客户·定价·收益", [
        "客户分群（RFM/价值分层/行为聚类）、健康分/NPS",
        "LTV/CLV、流失预测与归因、续费/续约",
        "价格弹性、套餐结构、折扣健康度",
        "动态定价与收益最大化",
    ]),
    3: ("Finance, FP&A & Markets / 财务·FP&A·金融市场", [
        "收入/成本/毛利、单位经济、预算 vs 实际、滚动预测",
        "现金流/回款/EBITDA/盈亏平衡、财务比率/情景分析",
        "资产价格、收益与风险、信用与利率",
        "投资组合、因子与归因",
    ]),
    4: ("Operations, Supply Chain & Manufacturing / 运营·供应链·制造", [
        "效率/产能/利用率/SLA 时效、流程挖掘与瓶颈",
        "质量/缺陷/良率、产能排程",
        "需求预测、安全库存与补货、库存周转/缺货",
        "履约与物流时效、供应商绩效与采购",
    ]),
    5: ("People / HR & Org / 人力与组织", [
        "招聘漏斗、人才留存与离职预测",
        "人效、薪酬公平性、绩效分布",
        "组织健康度、敬业度",
    ]),
    6: ("Risk, Fraud & Compliance / 风险·欺诈·合规", [
        "信用评分、违约预测",
        "欺诈检测、异常交易",
        "合规监控、审计分析、反洗钱（AML）",
    ]),

    # ---------------- B. 健康与生命科学 ----------------
    7: ("Clinical & Healthcare / 临床与医疗", [
        "EHR/电子病历、住院流程、诊疗路径",
        "再入院/并发症/不良事件",
        "医保理赔、费用与编码（ICD/DRG）",
        "检验结果、用药与疗效",
    ]),
    8: ("Epidemiology & Public Health / 流行病学与公共卫生", [
        "疾病监测、暴发追踪、传播动力学",
        "疫苗与公共卫生干预效果",
        "随机对照试验（RCT）、队列/病例对照研究",
        "健康风险因素与暴露",
    ]),
    9: ("Bioinformatics & Omics / 生物信息·组学", [
        "基因/表达/测序（RNA-seq/变异）",
        "实验 assay、化合物筛选、剂量反应",
        "批次效应、标准化与质控",
    ]),

    # ---------------- C. 科学与环境 ----------------
    10: ("Experimental & Physical Science / 实验与物理科学", [
        "仪器读数、传感器时序、重复实验与误差",
        "标定/校准、量纲与单位",
        "信号处理、异常剔除、质控",
    ]),
    11: ("Environment, Climate & Geospatial / 环境·气候·地理空间", [
        "气象/气候时序、排放/空气质量、水文",
        "遥感/栅格/地理空间、土地利用",
        "农业产量/物候、灾害事件",
    ]),
    12: ("Energy, Utilities & IoT / 能源·公用事业·IoT", [
        "电网、负荷与发电预测",
        "设备遥测、传感器网络、异常与停机预测",
        "计量、能耗、需求响应",
    ]),

    # ---------------- D. 社会与公共 ----------------
    13: ("Public Policy & Program Evaluation / 公共政策与项目评估", [
        "人口普查、行政记录、政府项目覆盖与公平",
        "犯罪/治安、城市运行",
        "因果评估（DiD/断点回归/工具变量/合成控制）",
    ]),
    14: ("Education & Learning Analytics / 教育与学习分析", [
        "LMS/学习行为、测评与成绩",
        "学生留存/辍学预测、学习路径",
        "教学/课程干预效果",
    ]),
    15: ("Social Science, Survey & Behavioral / 社会科学·调查·行为", [
        "问卷/面板/纵向调查、加权与抽样偏差",
        "民意/选举/态度测量",
        "社交媒体/文本/行为，实验与准实验",
    ]),
    16: ("Transportation & Mobility / 交通与移动性", [
        "出行需求/OD 矩阵、GPS 轨迹/速度",
        "车队/网约车/公共交通运营",
        "拥堵/事故/延误、路网与事件影响",
    ]),
}


def render_catalog() -> str:
    """把 16 类渲染成给 LLM 选择用的目录文本。"""
    lines: List[str] = []
    for key, (name, subs) in CATEGORIES.items():
        lines.append(f"[{key}] {name}")
        for sub in subs:
            lines.append(f"    - {sub}")
    return "\n".join(lines)


_KEYWORD_TABLE: Dict[int, List[str]] = {
    1: ["增长", "获客", "营销", "投放", "渠道", "广告", "归因", "销售", "pipeline", "forecast", "留存", "activation", "funnel", "growth", "marketing"],
    2: ["客户", "分群", "rfm", "ltv", "clv", "流失", "churn", "nps", "定价", "折扣", "价格", "pricing", "会员", "续费"],
    3: ["财务", "fp&a", "毛利", "预算", "现金", "成本", "ebitda", "金融", "股票", "交易", "投资", "portfolio", "信用", "利率", "market"],
    4: ["运营", "效率", "产能", "sla", "流程", "制造", "良率", "缺陷", "供应链", "库存", "补货", "缺货", "履约", "物流", "采购", "supply", "inventory", "manufactur"],
    5: ["hr", "招聘", "离职", "人效", "薪酬", "绩效", "组织", "敬业", "talent", "recruit", "attrition"],
    6: ["风控", "欺诈", "fraud", "合规", "compliance", "反洗钱", "aml", "审计", "异常交易", "违约", "风险"],
    7: ["临床", "医疗", "病历", "ehr", "住院", "诊疗", "再入院", "医保", "理赔", "icd", "drg", "patient", "hospital", "clinical"],
    8: ["流行病", "公共卫生", "疫情", "疫苗", "传播", "rct", "队列", "病例对照", "epidemi", "public health", "outbreak", "vaccine"],
    9: ["基因", "表达", "测序", "rna", "组学", "assay", "批次", "genom", "omic", "bioinformatic", "sequencing"],
    10: ["仪器", "传感器", "实验", "标定", "校准", "量纲", "信号", "物理", "physic", "sensor", "calibrat", "instrument"],
    11: ["气象", "气候", "环境", "排放", "空气", "水文", "遥感", "地理", "gis", "农业", "产量", "climate", "weather", "geospatial", "remote sensing", "emission"],
    12: ["能源", "电网", "负荷", "发电", "遥测", "iot", "停机", "计量", "能耗", "power grid", "utility", "telemetry"],
    13: ["政策", "政府", "普查", "行政", "犯罪", "治安", "城市", "项目评估", "did", "断点", "工具变量", "合成控制", "policy", "census", "program evaluation"],
    14: ["教育", "学习", "lms", "测评", "成绩", "学生", "辍学", "课程", "教学", "education", "learning", "student"],
    15: ["调查", "问卷", "面板", "民调", "选举", "态度", "抽样", "社交媒体", "社会", "survey", "poll", "social"],
    16: ["交通", "出行", "gps", "轨迹", "车队", "网约车", "公共交通", "拥堵", "路网", "mobility", "traffic", "transport", "trip"],
}


def keyword_category(description: str) -> int:
    """LLM 不可用时的关键词兜底：命中最多者胜，默认回退到 1。"""
    text = (description or "").lower()
    best, score = 1, 0
    for cid, keywords in _KEYWORD_TABLE.items():
        hit = sum(1 for kw in keywords if kw.lower() in text)
        if hit > score:
            best, score = cid, hit
    return best
