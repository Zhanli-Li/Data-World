"""Judge 客户端：给一段 prompt，返回模型的原始文本。

只负责"发一次请求、拿回文本"，判分语义全在 ``grade.py``——两者分开，才能让
容器与本机共用同一份评分口径而各用自己的网关。

**凭据只从环境变量读，代码里不落任何 token**。本机由 ``data_world.envfile``
从项目根 ``.env`` 注入（与 data_world/dag_factory 同一套凭据）；harbor 容器由
镜像构建或运行期注入（见 ``harbor.py``）。因此本模块不提供任何"零环境变量默认
可用"的内置密钥——那等于把密钥写进代码库。

两种网关形态（``JUDGE_API`` 选择）：

- ``openai``     ``POST {base}/v1/chat/completions``，鉴权 Bearer（项目内网关的形态）；
- ``anthropic``  ``POST {base}/v1/messages``，鉴权 ``x-api-key`` + Bearer。

base_url 末尾有无 ``/v1`` 都可以：:func:`normalize_base` 统一剥掉，各形态自己拼
完整路径。``.env`` 里的 ``ANTHROPIC_BASE_URL`` 带 ``/v1``，而 claude CLI 要求裸 host、
本模块要求带 ``/v1`` 的完整路径——归一化后两边共用同一个变量不会互相踩。

**一律流式（SSE）**。judge 的输出可以很长（rubric 几十条），非流式请求要等整段
生成完才回第一个字节，网关与客户端的超时都容易在此误杀一次本来会成功的判分。
流式下超时的语义变成"两个 chunk 之间的空档"，只要模型还在吐字就不会被判超时，
判分链路因此不需要靠"让模型少输出"来规避超时。

零第三方依赖（``urllib``），容器镜像不必装 requests。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Iterator, Optional

#: judge 输出上限。judge 常在 verdicts 前长篇推理，给小了会把 JSON 截断成非法输出；
#: 判分本身不贵，宁可给足。
MAX_TOKENS = 131_072

#: 流式读取的空档超时（秒）：两个 chunk 之间超过它才算卡死。
#:
#: 因为是流式，这里不是"整段生成的总时长"——模型持续吐字就一直不算超时，
#: 只有真正卡住（网关挂起、连接半开）才触发。
TIMEOUT_SEC = 180.0

#: 环境变量名：judge 专用（``JUDGE_*``）优先，否则回落到项目通用的 ``ANTHROPIC_*``。
_BASE_URL_KEYS = ("JUDGE_BASE_URL", "ANTHROPIC_BASE_URL")
_TOKEN_KEYS = ("JUDGE_API_KEY", "JUDGE_APP_ID", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
_MODEL_KEYS = ("JUDGE_MODEL", "ANTHROPIC_MODEL")


class JudgeConfigError(RuntimeError):
    """judge 凭据/配置缺失。``grade.py`` 捕获后写 reward 0 + 诊断信息。

    与"judge 调用失败"区分开：配置错误不该重试，重试只会重复失败。
    """


def _first_env(keys: tuple[str, ...]) -> Optional[str]:
    for key in keys:
        value = os.environ.get(key)
        if value:
            return value.strip()
    return None


def _require(keys: tuple[str, ...], what: str) -> str:
    value = _first_env(keys)
    if not value:
        raise JudgeConfigError(f"judge {what} 未配置，请设置 {' 或 '.join(keys)}")
    return value


def _iter_sse(url: str, headers: Dict[str, str], payload: Dict[str, Any],
              timeout: float) -> Iterator[Dict[str, Any]]:
    """POST 一个流式请求，逐个 yield SSE ``data:`` 里的 JSON 事件。

    ``timeout`` 传给 socket，因此它衡量的是单次读阻塞（chunk 间空档）而非总时长。
    显式禁用代理：网关多在内网，公网出口代理会让请求直接失败。
    """
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "accept": "text/event-stream",
                 **headers},
        method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=timeout) as response:
        for raw_line in response:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except json.JSONDecodeError:
                continue  # 心跳/注释等非 JSON 帧
            if isinstance(event, dict):
                yield event


def normalize_base(url: str) -> str:
    """剥掉 base_url 末尾的 ``/v1``、``/anthropic`` 等后缀，只留下裸 host 前缀。

    与 ``data_world.sdk.ClaudeRunner`` 的归一化同理：调用方给什么形态都能用，
    完整路径由各 API 形态自己拼。
    """
    normalized = (url or "").rstrip("/")
    for suffix in ("/v1/anthropic", "/anthropic", "/v1"):
        if normalized.endswith(suffix):
            return normalized[:-len(suffix)]
    return normalized


def _call_anthropic(base_url: str, token: str, model: str, prompt: str,
                    timeout: float) -> str:
    events = _iter_sse(
        f"{base_url}/v1/messages",
        {"anthropic-version": "2023-06-01", "x-api-key": token,
         "authorization": f"Bearer {token}"},
        {"model": model, "max_tokens": MAX_TOKENS, "temperature": 0, "stream": True,
         "messages": [{"role": "user", "content": prompt}]},
        timeout)
    chunks = []
    for event in events:
        if event.get("type") == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta":
                chunks.append(delta.get("text") or "")
        elif event.get("type") == "error":
            raise RuntimeError(f"judge 流式错误: {str(event.get('error'))[:200]}")
    return "".join(chunks)


def _call_openai(base_url: str, token: str, model: str, prompt: str,
                 timeout: float) -> str:
    # 不用 response_format=json_object：部分网关不支持严格模式会直接 400；
    # JSON 约束靠 prompt，解析端（grade.parse_verdicts）有容错打捞。
    events = _iter_sse(
        f"{base_url}/v1/chat/completions",
        {"authorization": f"Bearer {token}"},
        {"model": model, "max_tokens": MAX_TOKENS, "temperature": 0, "stream": True,
         "messages": [{"role": "user", "content": prompt}]},
        timeout)
    chunks = []
    for event in events:
        if event.get("error"):
            raise RuntimeError(f"judge 流式错误: {str(event['error'])[:200]}")
        for choice in event.get("choices") or []:
            content = (choice.get("delta") or {}).get("content")
            if content:
                chunks.append(content)
    return "".join(chunks)


_APIS: Dict[str, Any] = {"anthropic": _call_anthropic, "openai": _call_openai}

#: 默认 API 形态：项目内网关（``.env`` 里的 ``ANTHROPIC_BASE_URL``）是
#: OpenAI 兼容的 chat/completions，不是 Anthropic Messages。
DEFAULT_API = "openai"


def resolve_judge_identity(api: Optional[str] = None, base_url: Optional[str] = None,
                           model: Optional[str] = None,
                           timeout: float = TIMEOUT_SEC) -> Dict[str, Any]:
    """解析不含凭据的 judge 身份，供调用与评测缓存共同使用。"""
    api_name = (api or os.environ.get("JUDGE_API") or DEFAULT_API).strip().lower()
    if api_name not in _APIS:
        raise JudgeConfigError(
            f"未知 JUDGE_API={api_name!r}，可选 {'/'.join(sorted(_APIS))}")
    return {
        "api": api_name,
        "base_url": normalize_base(base_url or _require(_BASE_URL_KEYS, "base_url")),
        "model": model or _require(_MODEL_KEYS, "model"),
        "timeout": float(timeout),
    }


def make_judge(api: Optional[str] = None, base_url: Optional[str] = None,
               token: Optional[str] = None, model: Optional[str] = None,
               timeout: float = TIMEOUT_SEC) -> Callable[[str], str]:
    """构造 ``call_judge(prompt) -> str``；缺凭据时抛 :class:`JudgeConfigError`。

    显式入参优先于环境变量，便于 CLI 用 ``--judge-model`` 之类覆盖。
    """
    identity = resolve_judge_identity(api=api, base_url=base_url, model=model,
                                      timeout=timeout)
    resolved_token = token or _require(_TOKEN_KEYS, "api_key")
    call = _APIS[identity["api"]]

    def call_judge(prompt: str) -> str:
        return call(identity["base_url"], resolved_token, identity["model"], prompt,
                    identity["timeout"])

    return call_judge
