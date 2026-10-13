from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

import openai

from . import WorldGenError

_TRANSIENT_RE = re.compile(
    r"429|rate.?limit|overloaded|529|50[234]|connection.?(closed|reset|refused|aborted)|"
    r"remote.?end.?closed|broken.?pipe|timed?.?out|unexpected.?eof|econnreset|epipe|"
    r"malformed.?tool.?use|tool.?call.*could not be parsed|"
    r"limit.?exceeded|调用(次数|量)已达上限|too many tokens|"
    r"issue with the selected model|"  # 网关模型列表抖动时 CLI 快速报错，值得退避重试
    r"temporarily|服务(暂时)?不可用|网络(异常|错误)|连接(已)?(关闭|重置|中断)",
    re.I,
)

_INFRASTRUCTURE_RE = re.compile(
    r"429|rate.?limit|overloaded|529|5\d\d|connection.?(closed|reset|refused|aborted)|"
    r"remote.?end.?closed|broken.?pipe|timed?.?out|unexpected.?eof|econnreset|epipe|"
    r"limit.?exceeded|调用(次数|量)已达上限|too many tokens|"
    r"server.?side issue|provider.?error|服务(暂时)?不可用|网络(异常|错误)|"
    r"连接(已)?(关闭|重置|中断)",
    re.I,
)

_CLAUDE_SESSION_ENV = {
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
}
_SENSITIVE_ENV_KEYS = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "CLOUDSDK_AUTH_CREDENTIAL_FILE_OVERRIDE",
    "DOCKER_CONFIG",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "HADOOP_TOKEN_FILE_LOCATION",
    "JUDGE_APP_ID",
    "KUBECONFIG",
    "NETRC",
    "NPM_CONFIG_USERCONFIG",
    "SSH_AUTH_SOCK",
}
_SENSITIVE_ENV_SUFFIXES = (
    "_ACCESS_KEY",
    "_API_KEY",
    "_AUTH_TOKEN",
    "_CREDENTIAL",
    "_CREDENTIALS",
    "_PASSWORD",
    "_PASSWD",
    "_PRIVATE_KEY",
    "_SECRET",
    "_TOKEN",
)


def _sanitized_process_env() -> Dict[str, str]:
    """复制进程环境，但剥离会授予其他系统访问权的变量。"""
    return {
        key: value for key, value in os.environ.items()
        if key not in _CLAUDE_SESSION_ENV
        and key.upper() not in _SENSITIVE_ENV_KEYS
        and not key.upper().endswith(_SENSITIVE_ENV_SUFFIXES)
    }


def _build_claude_env(endpoint: Optional[str], auth_token: Optional[str]) -> Dict[str, str]:
    """构造 Claude 子进程环境，只传本次调用需要的鉴权。

    模型可调用 Bash，因此不能把 judge、训练平台、云账户或 SSH agent 等无关凭据
    原样继承进去。目标 Anthropic token 在过滤前解析，再以环境变量注入；鉴权不再
    写入 ``--settings`` 命令行参数，避免被同机用户通过进程列表看到。
    """
    resolved_token = (auth_token or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                      or os.environ.get("ANTHROPIC_API_KEY"))
    env = _sanitized_process_env()
    if endpoint:
        env["ANTHROPIC_BASE_URL"] = endpoint
        env["CLAUDE_CODE_SKIP_BEDROCK_AUTH"] = "1"
        env["CLAUDE_CODE_SKIP_VERTEX_AUTH"] = "1"
    if resolved_token:
        env["ANTHROPIC_AUTH_TOKEN"] = resolved_token
    elif endpoint:
        env["ANTHROPIC_AUTH_TOKEN"] = "dummy"
    for key in ("no_proxy", "NO_PROXY"):
        hosts = [host.strip() for host in env.get(key, "").split(",") if host.strip()]
        for host in ("127.0.0.1", "localhost"):
            if host not in hosts:
                hosts.append(host)
        env[key] = ",".join(hosts)
    return env


def is_retryable_error(message: str, raw: Optional[Dict[str, Any]] = None) -> bool:
    """Return whether a failed agent call is worth retrying.

    Gateways do not preserve status codes consistently: a gateway can wrap a provider
    throttle in HTTP 500 with ``limit_exceeded`` in the body, while Claude CLI may
    expose the same failure only through ``terminal_reason=api_error``.  Inspect the
    structured result first, then fall back to the shared textual patterns.
    """
    raw = raw or {}
    status = raw.get("api_error_status")
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    if raw.get("terminal_reason") == "api_error":
        return True
    return bool(_TRANSIENT_RE.search(message or ""))


def is_infrastructure_error(message: str, raw: Optional[Dict[str, Any]] = None) -> bool:
    """Distinguish provider/transport failures from model capability failures."""
    raw = raw or {}
    status = raw.get("api_error_status")
    if isinstance(status, int) and (status == 429 or 500 <= status < 600):
        return True
    if raw.get("terminal_reason") == "api_error":
        return True
    if raw.get("subtype") == "error_during_execution" and raw.get("is_error"):
        return True
    return bool(_INFRASTRUCTURE_RE.search(message or ""))


class ClaudeRunner:
    """Claude Code CLI 适配器；只处理进程、端点注入和瞬时错误重试。"""

    def __init__(self, executable: str, model: str, endpoint: Optional[str],
                 auth_token: Optional[str], timeout: int, max_turns: int,
                 retries: int, verbose: bool, retry_base_delay: float = 20.0,
                 retry_max_delay: float = 600.0) -> None:
        self.executable = executable
        self.model = model
        # claude CLI 期望裸 host 形式的 BASE_URL（自行拼 /v1/...）；
        # 带 /v1/anthropic 后缀会拉不到模型清单，报 "issue with the selected model"。
        normalized = (endpoint or "").rstrip("/")
        for suffix in ("/v1/anthropic", "/anthropic", "/v1"):
            if normalized.endswith(suffix):
                normalized = normalized[:-len(suffix)]
                break
        self.endpoint = normalized or None
        self.auth_token = auth_token
        self.timeout = timeout
        self.max_turns = max_turns
        self.retries = retries
        self.verbose = verbose
        self.retry_base_delay = retry_base_delay
        self.retry_max_delay = retry_max_delay

    def retry_delay(self, attempt: int) -> float:
        """Exponential backoff delay before retry number ``attempt + 1``."""
        return min(self.retry_base_delay * (2 ** attempt), self.retry_max_delay)

    def run(self, prompt: str, cwd: Path, add_dirs: Sequence[Path],
            max_turns: Optional[int] = None) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        last: Tuple[bool, str, Optional[Dict[str, Any]]] = (False, "not started", None)
        for attempt in range(self.retries + 1):
            last = self._run_once(prompt, cwd, add_dirs, max_turns or self.max_turns)
            ok, message, raw = last
            if ok or attempt >= self.retries or not is_retryable_error(message, raw):
                return last
            wait = self.retry_delay(attempt)
            if self.verbose:
                print(f"    [sdk] 瞬时失败，{wait:g}s 后重试 {attempt + 1}/{self.retries}: {message[:160]}", flush=True)
            time.sleep(wait)
        return last

    def _run_once(self, prompt: str, cwd: Path, add_dirs: Sequence[Path],
                  max_turns: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        cwd.mkdir(parents=True, exist_ok=True)
        # 超长 prompt（如聚合全部层结果的 projection）会超过内核单参数 128KB 上限
        # (E2BIG)；超过阈值时改走 stdin 传 prompt（claude 无位置参数时读 stdin）。
        use_stdin = len(prompt.encode("utf-8")) > 96 * 1024
        cmd = [self.executable, "-p"]
        if not use_stdin:
            cmd.append(prompt)
        cmd += ["--output-format", "json",
                "--permission-mode", "bypassPermissions", "--model", self.model,
                "--max-turns", str(max_turns), "--allowedTools",
                "Read", "Write", "Edit", "Bash", "Glob", "Grep", "--add-dir", str(cwd)]
        for directory in add_dirs:
            cmd.extend(["--add-dir", str(directory)])
        cmd.extend([
            "--settings",
            json.dumps({"skipDangerousModePermissionPrompt": True}),
        ])
        env = _build_claude_env(self.endpoint, self.auth_token)
        started = time.time()
        if self.verbose:
            via = "stdin" if use_stdin else "argv"
            print(f"    [sdk] model={self.model} cwd={cwd} max_turns={max_turns} prompt_via={via}", flush=True)
        try:
            proc = subprocess.run(cmd, cwd=str(cwd), env=env, capture_output=True,
                                  text=True, timeout=self.timeout,
                                  input=prompt if use_stdin else None)
        except subprocess.TimeoutExpired:
            return False, f"timeout after {self.timeout}s", None
        except OSError as exc:
            return False, f"cannot execute {self.executable!r}: {exc}", None
        stdout, stderr = (proc.stdout or "").strip(), (proc.stderr or "").strip()
        if not stdout:
            return False, f"empty stdout exit={proc.returncode}; stderr={stderr[-1000:]}", None
        try:
            raw = json.loads(stdout)
        except json.JSONDecodeError:
            ok = proc.returncode == 0
            return ok, stdout if ok else f"{stdout[-1000:]}\nstderr={stderr[-1000:]}", None
        result = str(raw.get("result") or raw.get("text") or "")
        ok = proc.returncode == 0 and not raw.get("is_error", False)
        # 网关截断长响应时 CLI 会误报成功：stop_reason 缺失/为 tool_use、result 为空。
        # 识别为失败并给出可重试的错误信息，避免静默放过。
        if ok and raw.get("stop_reason") not in ("end_turn", "max_turns", "pause_turn"):
            detail = raw.get("stop_reason") or "missing"
            return False, f"session ended abnormally: stop_reason={detail} turns={raw.get('num_turns', '?')} (response likely truncated by gateway)", raw
        if ok and not result.strip():
            return False, f"empty result with stop_reason={raw.get('stop_reason')} turns={raw.get('num_turns', '?')} (response likely truncated by gateway)", raw
        if self.verbose:
            print(f"    [sdk] done {time.time() - started:.0f}s ok={ok} turns={raw.get('num_turns', '?')}", flush=True)
        if not ok and stderr:
            result = f"{result}\nstderr={stderr[-1000:]}"
        return ok, result, raw


class LLMClient:
    """OpenAI 兼容单次调用适配器；用于不需要工具/多轮的纯推理阶段（如 World Contract）。"""

    def __init__(self, base_url: Optional[str], api_key: Optional[str], model: str,
                 timeout: int, retries: int, verbose: bool,
                 affinity_key: Optional[str] = None) -> None:
        # --endpoint 指向 Anthropic Messages 协议地址（.../v1/anthropic），openai 库需要去掉该后缀。
        normalized = (base_url or "").rstrip("/").removesuffix("/anthropic") or None
        self.model = model
        self.retries = retries
        self.verbose = verbose
        # 会话亲和 key：给定则本 build 所有调用共用它（与工具阶段同渠道）；否则退回 per-call。
        self.affinity_key = affinity_key
        self.client = openai.OpenAI(base_url=normalized, api_key=api_key or "dummy", timeout=timeout)

    def complete(self, prompt: str) -> Dict[str, Any]:
        value, _ = self.complete_with_trace(prompt)
        return value

    def complete_with_trace(self, prompt: str) -> Tuple[Dict[str, Any], str]:
        """同 complete，但额外返回模型的原始 response 文本，供轨迹日志使用。"""
        message = ""
        # 亲和 id：命中代理侧 default 的 gjson:user，令请求粘到同一上游渠道。build 级
        # affinity_key 让 contract 与工具阶段同渠道；缺省则退回 per-call（含内部重试复用）。
        call_id = self.affinity_key or uuid.uuid4().hex
        for attempt in range(self.retries + 1):
            try:
                # 流式接收：合同 JSON 常超 3 万 token，非流式要整段等十几分钟——
                # 既看不到进度（无法分辨生成中还是卡死），也易被中间代理的空闲
                # 超时切断。流式下 finish_reason/usage 从末块取，语义不变。
                stream = self.client.chat.completions.create(
                    model=self.model, messages=[{"role": "user", "content": prompt}],
                    max_tokens=800000,  # 网关缺省仅 1024，不显式给足会被截断
                    user=call_id,
                    stream=True, stream_options={"include_usage": True})
                parts: list = []
                finish_reason: Optional[str] = None
                completion_tokens: Optional[int] = None
                think_chars = 0  # 推理模型的思考走 delta.reasoning_content，正文计数不含它
                t0 = time.time()
                last_log = t0
                for chunk in stream:
                    if not chunk.choices:  # include_usage 的末块只有 usage、无 choices
                        if getattr(chunk, "usage", None) is not None:
                            completion_tokens = chunk.usage.completion_tokens
                        continue
                    choice = chunk.choices[0]
                    delta = choice.delta
                    if delta and delta.content:
                        parts.append(delta.content)
                    elif delta is not None:
                        rc = getattr(delta, "reasoning_content", None)
                        if rc:
                            think_chars += len(rc)
                    if choice.finish_reason:
                        finish_reason = choice.finish_reason
                    if self.verbose and time.time() - last_log > 30:
                        last_log = time.time()
                        print(f"    [llm] 流式接收中 正文 {sum(len(p) for p in parts)} 字符"
                              f" / 思考 {think_chars} 字符…", flush=True)
                text = "".join(parts)
                if self.verbose:
                    print(f"    [llm] 流式结束 finish={finish_reason} "
                          f"completion_tokens={completion_tokens} 耗时={time.time() - t0:.0f}s",
                          flush=True)
                if finish_reason == "length":
                    raise WorldGenError(
                        "LLM 输出因长度上限被截断，合同 JSON 不完整"
                        f"（已收 {len(text)} 字符 / {completion_tokens} tokens，"
                        f"尾部: …{text[-200:]}）")
                try:
                    return _parse_json_object(text), text
                except (json.JSONDecodeError, WorldGenError) as exc:
                    if finish_reason is None:
                        # 网关静默截断/断流时不发 finish 块——把现场信息带进错误，
                        # 否则会退化成一句没有诊断价值的"未返回合法 JSON 对象"。
                        raise WorldGenError(
                            "LLM 未返回合法 JSON 对象，且流式响应无 finish_reason"
                            f"（疑似静默截断/断流：已收 {len(text)} 字符 / "
                            f"{completion_tokens} tokens，尾部: …{text[-200:]}）") from exc
                    raise
            except WorldGenError:
                raise
            except Exception as exc:  # noqa: BLE001 — 阶段级重试边界
                message = str(exc)
                if attempt >= self.retries or not _TRANSIENT_RE.search(message):
                    raise WorldGenError(f"LLM 调用失败: {message[-1000:]}") from exc
                wait = min(20 * (2 ** attempt), 600)
                if self.verbose:
                    print(f"    [llm] 瞬时失败，{wait}s 后重试 {attempt + 1}/{self.retries}: {message[:160]}", flush=True)
                time.sleep(wait)
        raise WorldGenError(f"LLM 调用失败: {message[-1000:]}")


def _parse_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    candidates = [text]
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S | re.I)
    if fenced:
        candidates.append(fenced.group(1).strip())
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise WorldGenError(f"LLM 未返回合法 JSON 对象: {text[:500]}")
