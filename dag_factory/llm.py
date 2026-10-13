"""轻量 LLM 客户端：基于 openai 库的 OpenAI 兼容单次调用。

与 data-world 的 ``data_world.sdk.LLMClient`` 保持一致的调用形态：
- 用 ``chat.completions.create`` 发一次请求；
- **不传 max_tokens**，不主动施加输出天花板；
- 检测 ``finish_reason == "length"`` 以识别端点侧默认上限导致的截断；
- 用 ``user`` 参数做渠道亲和（与反代/网关的会话复用配合）。

本模块只依赖 openai 库，不 import data-world 的 sdk，保持 DAG Factory 自洽。
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Optional, Tuple

import openai

# 与 ba-env/data-world 同源的瞬时错误判定：限流/过载/连接类错误值得退避重试。
_TRANSIENT_RE = re.compile(
    r"429|rate.?limit|限流|overloaded|529|503|502|504|"
    r"connection.?(closed|reset|refused|aborted)|timed?.?out|"
    r"temporarily|服务(暂时)?不可用|网络(异常|错误)",
    re.I,
)


class LlmError(Exception):
    """LLM 调用失败（含截断）——由上层决定是否作为一次生成失败处理。"""


class LlmClient:
    """OpenAI 兼容单次调用适配器。"""

    def __init__(
        self,
        *,
        model: str,
        endpoint: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout: int = 360,
        retries: int = 3,
        verbose: bool = False,
    ) -> None:
        # --endpoint 常指向 Anthropic Messages 协议地址（.../v1/anthropic），
        # openai 库走 OpenAI 兼容接口，需去掉该后缀。
        normalized = (endpoint or "").rstrip("/").removesuffix("/anthropic") or None
        self.model = model
        self.retries = retries
        self.verbose = verbose
        self.client = openai.OpenAI(base_url=normalized, api_key=auth_token or "dummy", timeout=timeout)

    def complete(self, prompt: str, *, system: Optional[str] = None) -> Tuple[bool, str]:
        """发一次请求，返回 (ok, text)。瞬时错误指数退避；截断视为失败。"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        call_id = uuid.uuid4().hex
        last = "unreachable"
        for attempt in range(self.retries + 1):
            try:
                # 流式接收：避免长生成时整段等待、被中间代理空闲超时切断。
                stream = self.client.chat.completions.create(
                    model=self.model, messages=messages, user=call_id,
                    stream=True, stream_options={"include_usage": True})
                parts: list = []
                finish_reason: Optional[str] = None
                for chunk in stream:
                    if not chunk.choices:  # include_usage 的末块只有 usage、无 choices
                        continue
                    choice = chunk.choices[0]
                    if choice.delta and choice.delta.content:
                        parts.append(choice.delta.content)
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                if finish_reason == "length":
                    return False, "LLM 输出因长度上限被截断，JSON 不完整"
                text = "".join(parts)
                if not text.strip():
                    last = "empty content"
                    if attempt < self.retries:
                        time.sleep(min(10 * (2 ** attempt), 600))
                        continue
                    return False, last
                return True, text.strip()
            except Exception as exc:  # noqa: BLE001 — 调用边界，统一转成 (ok, msg)
                last = f"{type(exc).__name__}: {exc}"
                if attempt >= self.retries or not _TRANSIENT_RE.search(last):
                    return False, last
                wait = min(10 * (2 ** attempt), 600)
                if self.verbose:
                    print(f"  [llm] 瞬时失败，{wait}s 后重试 {attempt + 1}/{self.retries}: {last[:120]}", flush=True)
                time.sleep(wait)
        return False, last
