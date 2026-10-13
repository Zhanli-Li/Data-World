"""判分：LLM judge 按 rubric 逐条给 verdict，折算 [0,1] 小数 reward。

**评分语义的唯一实现处**。包内判分与 harbor 容器内 verifier（``harbor.py``
导出时把本文件拷进 ``tests/``）导入同一份代码——判分口径只有一个真值来源，
不允许两条路径各自演化。

判分约定（与出题纪律对应）：

- 正分项：``status=satisfied`` 得分，``not_satisfied`` 不得分；
- 负分项（``points<0``，"错误结论扣分"）：``status=triggered`` 时扣分；
- 报告没提到的结论不得分也不扣分，除非命中负分项描述的错误结论；
- ``reward = clamp(总分, 0, 正分上限) / 正分上限``：reward 恒落在 [0,1]——
  触发负分项最多把总分压到 0，不出现负 reward；超过正分上限同样截断（正常不会触发）。

容器内零依赖：本模块只用 stdlib，judge 的 HTTP 调用由 ``judge.py`` 负责。

命令行入口（harbor verifier 用）：``python3 grade.py``，路径全部可由环境变量覆盖，
缺省值遵循 harbor 约定。判分链路任何失败都写 reward 0 + ``judge_error``，绝不崩溃。
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

#: judge 连续失败的重试上限。
MAX_JUDGE_ATTEMPTS = 4

#: 报告过长时的截断窗口（judge 上下文有限；头部含结论与方法，尾部含局限与建议）。
REPORT_HEAD, REPORT_TAIL = 150_000, 20_000


# ── 独立运行（容器 verifier）与包内导入两种模式 ────────────────────────────────
# 容器里本文件被拷成 /tests/grade.py，没有 task 包可 import；此时从同目录的
# judge_prompt.py 取（``harbor.py`` 导出时由 prompts.JUDGE_PROMPT 生成，仍是同
# 一个真值来源）。两种模式共用下面同一段判分逻辑。
try:
    from .prompts import JUDGE_PROMPT
except ImportError:  # pragma: no cover - 容器内独立运行
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from judge_prompt import JUDGE_PROMPT  # type: ignore[no-redef]


def render_rubric(rubric: Dict[str, Any]) -> str:
    """把打平后的 criteria 渲染成 judge 可读的清单。"""
    return "\n".join(
        f"- id={c['id']} | section={c['section']} | points={c['points']}\n"
        f"  criterion: {c['criterion']}"
        for c in rubric["criteria"]
    )


def truncate_report(text: str, head: int = REPORT_HEAD, tail: int = REPORT_TAIL) -> str:
    if len(text) <= head + tail:
        return text
    return text[:head] + "\n\n...[报告中部过长已截断]...\n\n" + text[-tail:]


def score(rubric: Dict[str, Any], verdicts: List[Any]) -> Dict[str, Any]:
    """按 verdict 折算 reward；缺条记 ``missing`` 且不得分（由调用方决定是否重试）。"""
    by_id = {v["id"]: v for v in verdicts if isinstance(v, dict) and "id" in v}
    total_positive = sum(c["points"] for c in rubric["criteria"] if c["points"] > 0)
    raw_score = 0
    detail = []
    for criterion in rubric["criteria"]:
        verdict = by_id.get(criterion["id"], {})
        status = verdict.get("status", "")
        if criterion["points"] > 0:
            earned = criterion["points"] if status == "satisfied" else 0
        else:
            earned = criterion["points"] if status == "triggered" else 0
        raw_score += earned
        detail.append({
            "id": criterion["id"], "section": criterion["section"],
            "points": criterion["points"], "status": status or "missing",
            "earned": earned, "reason": verdict.get("reason", ""),
        })
    clamped = min(max(raw_score, 0), total_positive)
    return {
        "reward": round(clamped / total_positive, 4) if total_positive else 0.0,
        "raw_score": raw_score,
        "total_positive": total_positive,
        "missing_verdicts": [c["id"] for c in rubric["criteria"] if c["id"] not in by_id],
        "criteria": detail,
    }


# ── verdict 解析容错 ──────────────────────────────────────────────────────────
# judge 的两类典型坏输出：长推理导致 JSON 被截断（尾部对象残缺）、reason 里引用
# 报告原话时放了未转义的 ASCII 直引号（整体 json.loads 必挂）。verdict 对象是扁平
# 的（无嵌套花括号），可逐个正则捞出；坏对象再走字段级抢救——id/status 是枚举值
# 不含引号，reason 取到最后一个引号即可正确还原。
_VERDICT_OBJ_RE = re.compile(r'\{[^{}]*"id"[^{}]*\}', re.S)
_VERDICT_ID_RE = re.compile(r'"id"\s*:\s*"([^"]+)"')
_VERDICT_STATUS_RE = re.compile(r'"status"\s*:\s*"([^"]+)"')
_VERDICT_REASON_RE = re.compile(r'"reason"\s*:\s*"(.*)"\s*\}\s*$', re.S)

#: 打捞到的 verdict 少于 criteria 的这个比例即判失败重试（而非接受一份残缺判分）。
_SALVAGE_MIN_RATIO = 2 / 3


def _salvage_verdict(blob: str) -> Optional[Dict[str, Any]]:
    """解析单个 verdict 对象；严格解析失败时按字段正则抢救。"""
    try:
        obj = json.loads(blob)
        if isinstance(obj, dict) and "id" in obj and "status" in obj:
            return obj
    except json.JSONDecodeError:
        pass
    id_match = _VERDICT_ID_RE.search(blob)
    status_match = _VERDICT_STATUS_RE.search(blob)
    if not (id_match and status_match):
        return None
    reason_match = _VERDICT_REASON_RE.search(blob)
    return {"id": id_match.group(1), "status": status_match.group(1),
            "reason": reason_match.group(1) if reason_match else ""}


def parse_verdicts(text: str, n_criteria: int) -> List[Any]:
    """先严格解析 ``{"verdicts": [...]}``；失败则逐条打捞 verdict 对象。

    打捞数量不足 criteria 的 2/3 时抛 ValueError，交由调用方重试——宁可重判也
    不接受一份大面积缺条的结果（缺条会被 :func:`score` 静默记 0，污染统计）。
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:]
    start, end = stripped.find("{"), stripped.rfind("}")
    if 0 <= start < end:
        try:
            data = json.loads(stripped[start:end + 1])
            verdicts = data.get("verdicts")
            if isinstance(verdicts, list) and verdicts:
                return verdicts
        except (json.JSONDecodeError, AttributeError):
            pass
    verdicts = [obj for obj in (_salvage_verdict(m.group(0))
                                for m in _VERDICT_OBJ_RE.finditer(text))
                if obj is not None]
    if len(verdicts) < max(1, int(n_criteria * _SALVAGE_MIN_RATIO)):
        raise ValueError(f"verdicts 打捞不足: {len(verdicts)}/{n_criteria}")
    return verdicts


def judge_report(rubric: Dict[str, Any], report_text: str, call_judge: Any,
                 report_path: str = "报告", attempts: int = MAX_JUDGE_ATTEMPTS,
                 on_raw: Any = None) -> Dict[str, Any]:
    """判一份报告：构造 prompt → 调 judge → 容错解析 → 折算 reward。

    ``call_judge(prompt) -> str`` 由调用方注入（本机走 ``judge.py`` 的客户端，
    容器内同理），本模块因此不含任何网络代码。缺条且还有重试机会时重试。

    judge 连续失败返回 ``{"reward": None, "judge_error": ...}``——判分基础设施故障
    与真实 0 分是两件事，不能混为一谈污染统计与 RL 数据。
    """
    prompt = JUDGE_PROMPT.format(
        rubric_text=render_rubric(rubric),
        report=truncate_report(report_text),
        report_path=report_path,
    )
    n_criteria = len(rubric["criteria"])
    last_error: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            text = call_judge(prompt)
            if on_raw is not None:
                on_raw(text)
            verdicts = parse_verdicts(text, n_criteria)
            result = score(rubric, verdicts)
            # 缺条会被 score 静默记 0，不能当正常结果接受：还有重试机会就重试。
            if result["missing_verdicts"] and attempt < attempts - 1:
                raise ValueError(f"verdicts 缺条: {result['missing_verdicts']}")
            return result
        except Exception as exc:  # noqa: BLE001 — 任何 judge/解析异常都重试
            last_error = exc
            print(f"[grade] judge 第 {attempt + 1}/{attempts} 次失败: {exc}",
                  file=sys.stderr, flush=True)
            if attempt < attempts - 1:
                time.sleep(min(10 * (attempt + 1), 60))
    return {"reward": None, "judge_error": str(last_error)[:300], "criteria": []}


# ── harbor 容器 verifier 入口 ─────────────────────────────────────────────────

ANSWER_PATH = Path(os.environ.get("ANSWER_PATH") or "/app/answer.txt")
RUBRIC_PATH = Path(os.environ.get("RUBRIC_PATH") or "/tests/rubric.json")
REWARD_PATH = Path(os.environ.get("REWARD_PATH") or "/logs/verifier/reward.txt")
DETAILS_PATH = Path(os.environ.get("DETAILS_PATH") or "/logs/verifier/details.json")


def write_reward(result: Dict[str, Any]) -> None:
    REWARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    DETAILS_PATH.parent.mkdir(parents=True, exist_ok=True)
    REWARD_PATH.write_text(str(result["reward"]))
    DETAILS_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")


def write_failure(reason: str, extra: Optional[Dict[str, Any]] = None) -> None:
    """判分链路失败：reward 0 + details 带 ``judge_error``，绝不非零退出。

    harbor 把 verifier 的非零退出当作任务基础设施故障；此处统一兜底成 0 分 +
    诊断信息，让整批评测不因单题判分失败中断。
    """
    result: Dict[str, Any] = {"reward": 0, "judge_error": reason, "criteria": []}
    if extra:
        result.update(extra)
    write_reward(result)
    print(f"[grade] failure: {reason}", file=sys.stderr)


def _grade_container() -> None:
    if not ANSWER_PATH.is_file():
        write_failure(f"{ANSWER_PATH} missing", extra={"note": "answer missing"})
        return
    report_text = ANSWER_PATH.read_text(encoding="utf-8", errors="replace")
    if not report_text.strip():
        write_failure(f"{ANSWER_PATH} is empty", extra={"note": "answer empty"})
        return
    rubric = json.loads(RUBRIC_PATH.read_text(encoding="utf-8"))

    try:
        import judge  # type: ignore[import-not-found]
    except ImportError:
        from . import judge  # type: ignore[no-redef]
    try:
        call_judge = judge.make_judge()
    except judge.JudgeConfigError as exc:
        write_failure(f"judge 配置错误: {exc}", extra={"judge_config_error": True})
        return

    result = judge_report(rubric, report_text, call_judge, report_path=str(ANSWER_PATH))
    if result.get("reward") is None:
        write_failure(f"judge 连续 {MAX_JUDGE_ATTEMPTS} 次失败: {result.get('judge_error')}")
        return
    write_reward(result)
    print(f"[grade] reward={result['reward']} "
          f"raw={result['raw_score']}/{result['total_positive']}")


def main() -> int:
    try:
        _grade_container()
    except Exception:  # noqa: BLE001 — 兜底也不许崩溃
        write_failure(f"grader crashed: {traceback.format_exc()[-500:]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
