from __future__ import annotations

import copy
import json
import os
import re
import socket
import sqlite3
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence

from . import WorldGenError
from .sdk import _sanitized_process_env

# ==========================================================================
# CCR (claude-code-router) 生命周期管理
#
# 让 claude -p 走一个 OpenAI 兼容代理，代理全量记录每次 request/response，
# 供事后重组为可 SFT 的多轮对话。CCR 通过 HOME 定位配置与日志，这里用
# 隔离 home，既避免污染用户全局配置，也让 v3 的一次性 JSON -> SQLite 迁移安全可控。
# ==========================================================================

# v3 已内建 Anthropic <-> OpenAI 协议转换；供应商 extraBody 负责给转换后的
# OpenAI 请求注入亲和字段并强制上游非流式。这里只保留一个受支持的路由脚本，
# 精确移除 Claude Code 注入的本机上下文 reminder，不改题面、工具或其他消息。
_STRIP_CLI_CONTEXT_ROUTE = r"""const rewrites = [];
const marker = /<system-reminder>\s*As you answer the user's questions, you can use the following context:[\s\S]*?<\/system-reminder>\s*/g;
for (let i = 0; i < (input.body.messages || []).length; i += 1) {
  const content = input.body.messages[i] && input.body.messages[i].content;
  if (typeof content === "string") {
    const clean = content.replace(marker, "");
    if (clean !== content) rewrites.push({ key: `request.body.messages.${i}.content`, operation: "set", value: clean });
    continue;
  }
  if (!Array.isArray(content)) continue;
  for (let j = 0; j < content.length; j += 1) {
    const text = content[j] && content[j].type === "text" && content[j].text;
    if (typeof text !== "string") continue;
    const clean = text.replace(marker, "");
    if (clean !== text) rewrites.push({ key: `request.body.messages.${i}.content.${j}.text`, operation: "set", value: clean });
  }
}
return rewrites.length ? { rewrites } : null;
"""


class CCRProxy:
    def __init__(self, ccr_bin: str, openai_base_url: str, api_key: Optional[str],
                 model: str, home_dir: Path, port: int, verbose: bool,
                 affinity_key: Optional[str] = None) -> None:
        self.ccr_bin = ccr_bin
        # CCR v3 provider 需要 OpenAI base（如 .../v1），具体 endpoint 由协议适配器拼接。
        self.openai_base_url = (openai_base_url.rstrip("/")
                                .removesuffix("/chat/completions")
                                .removesuffix("/anthropic"))
        self.api_key = api_key or "dummy"
        self.model = model
        self.home = home_dir
        self.port = port
        self.verbose = verbose
        # 本次 build 的固定亲和 key，经 extraBody 注入转换后的 OpenAI 请求。
        self.affinity_key = affinity_key or uuid.uuid4().hex
        # ccr_bin 可能是共享 bootstrap wrapper；明确把“工具安装 home”和 CCR 的
        # 隔离 HOME 分开，避免每题在临时目录重复 npm install 100+ 个包。
        self.runtime_home = os.environ.get("DATA_WORLD_RUNTIME_HOME", str(Path.home()))
        self.management_port: Optional[int] = None
        self._proc: Optional[subprocess.Popen] = None
        self._startup_log: Optional[Any] = None
        self._startup_log_path = self.home / "ccr-startup.log"

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def logs_dir(self) -> Path:
        """CCR 2.x 兼容日志位置；v3 走 request_logs_db。"""
        return self.home / ".claude-code-router" / "logs"

    @property
    def request_logs_db(self) -> Path:
        return self.home / ".claude-code-router" / "app-data" / "request-logs.sqlite"

    @property
    def _route_script_path(self) -> Path:
        return self.home / ".claude-code-router" / "strip_cli_context.js"

    def _env(self) -> Dict[str, str]:
        env = _sanitized_process_env()
        env["HOME"] = str(self.home)
        env["DATA_WORLD_RUNTIME_HOME"] = self.runtime_home
        # 本地代理端点必须绕过 http(s)_proxy，否则请求被外发代理拦截、CCR 收不到
        no_proxy = "127.0.0.1,localhost"
        env["no_proxy"] = no_proxy
        env["NO_PROXY"] = no_proxy
        return env

    def _write_config(self) -> None:
        config_dir = self.home / ".claude-code-router"
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        config_dir.chmod(0o700)
        self._route_script_path.write_text(_STRIP_CLI_CONTEXT_ROUTE, encoding="utf-8")
        self._route_script_path.chmod(0o600)
        try:
            retry_count = max(0, min(20, int(os.environ.get("CCR_RETRY_COUNT", "4"))))
        except ValueError:
            retry_count = 4
        config = {
            "HOST": "127.0.0.1",
            "PORT": self.port,
            # ClaudeRunner 仍携带原上游 token，请它同时作为隔离网关的 client key；
            # HOME 每次临时创建，不会把该 key 写入用户全局配置。
            "APIKEY": self.api_key,
            "Providers": [
                {
                    "name": "friday",
                    "type": "openai_chat_completions",
                    "api_base_url": self.openai_base_url,
                    "api_key": self.api_key,
                    "models": [self.model],
                    # v3 先做协议转换，再深合并 extraBody：让一次 build 的所有
                    # 请求命中同一上游亲和键。不再强制 stream:false——长生成非流式
                    # 会撞上游空闲超时（2026-07-31 LLMClient 改流式同款教训），
                    # 且 v3 适配器已能正确处理 SSE block index。
                    "extraBody": {
                        "user": self.affinity_key,
                        "prompt_cache_key": self.affinity_key,
                    },
                }
            ],
            "Router": {
                "builtInRules": {
                    "claude-code": {"enabled": True},
                    "codex": {"enabled": True},
                },
                "fallback": {"mode": "retry", "models": [], "retryCount": retry_count},
                "rules": [{
                    "id": "strip-claude-cli-context",
                    "name": "Strip Claude CLI injected local context",
                    "enabled": True,
                    "type": "script",
                    "script": {
                        "apiVersion": 1,
                        "file": str(self._route_script_path),
                        "language": "javascript",
                        "timeoutMs": 2000,
                    },
                }],
            },
            "observability": {
                "agentAnalysis": False,
                "requestLogs": True,
                "requestLogBodyCapture": "all",
                "requestLogMaxBodyBytes": 50 * 1024 * 1024,
                "requestLogSuccessSampleRate": 1,
            },
        }
        config_path = config_dir / "config.json"
        config_path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        config_path.chmod(0o600)

    def _port_is_open(self, port: Optional[int] = None) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", port or self.port), timeout=1):
                return True
        except OSError:
            return False

    def _pick_management_port(self) -> int:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    def _startup_diagnostic(self) -> str:
        details: List[str] = []
        if self._proc is not None and self._proc.poll() is not None:
            details.append(f"ccr serve 已退出，退出码 {self._proc.returncode}")
        if self._startup_log is not None:
            self._startup_log.flush()
        if self._startup_log_path.exists():
            lines = self._startup_log_path.read_text(
                encoding="utf-8", errors="replace").strip().splitlines()
            if lines:
                details.append("启动日志末尾：\n" + "\n".join(lines[-20:]))
        return "\n".join(details) or "CCR 未输出可用诊断信息"

    def start(self, timeout: int = 60) -> None:
        self._write_config()
        env = self._env()
        if self._port_is_open():
            raise WorldGenError(
                f"CCR 端口 {self.port} 已被其他进程占用；请指定其他 --ccr-port，"
                "并发脚本应为每个任务分配独立端口")
        self.management_port = self._pick_management_port()
        # v3 的 `ccr start` 会派生后台进程后立即退出；用 serve 才能可靠跟踪并回收
        # 本题的完整进程树。实际 gateway TCP 监听仍是最终就绪判据。
        self._startup_log_path.parent.mkdir(parents=True, exist_ok=True)
        self._startup_log = self._startup_log_path.open("w", encoding="utf-8")
        try:
            self._proc = subprocess.Popen(
                [self.ccr_bin, "serve", "--host", "127.0.0.1", "--port",
                 str(self.management_port), "--no-open"],
                env=env, cwd=str(self.home),
                stdout=self._startup_log, stderr=subprocess.STDOUT,
                start_new_session=True)
        except OSError as exc:
            self._startup_log.close()
            self._startup_log = None
            raise WorldGenError(f"无法启动 CCR（{self.ccr_bin}）：{exc}") from exc
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._port_is_open():
                if self.verbose:
                    print(f"    [ccr] 已启动 {self.base_url} HOME={self.home}", flush=True)
                return
            if self._proc.poll() is not None:
                break
            time.sleep(0.5)
        diagnostic = self._startup_diagnostic()
        self.stop()
        raise WorldGenError(f"CCR 代理启动失败（端口 {self.port}）：\n{diagnostic}")

    def stop(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=5)
        if self._startup_log is not None:
            self._startup_log.close()
            self._startup_log = None

    def log_snapshot(self) -> Dict[str, int]:
        """记录 v3 SQLite 最大行号；若是旧 CCR，则退回 JSONL 行数。"""
        snapshot: Dict[str, int] = {}
        marker = f"sqlite:{self.request_logs_db}"
        v3_config = self.home / ".claude-code-router" / "config.sqlite"
        if self.request_logs_db.is_file() or v3_config.is_file():
            snapshot[marker] = 0
            try:
                with sqlite3.connect(self.request_logs_db, timeout=10) as db:
                    row = db.execute("SELECT COALESCE(MAX(id), 0) FROM request_logs").fetchone()
                snapshot[marker] = int(row[0] if row else 0)
            except sqlite3.Error:
                pass
            return snapshot
        if not self.logs_dir.is_dir():
            return snapshot
        for path in self.logs_dir.glob("ccr-*.log"):
            with path.open(encoding="utf-8", errors="replace") as handle:
                snapshot[str(path)] = sum(1 for _ in handle)
        return snapshot

    def lines_since(self, snapshot: Dict[str, int]) -> List[str]:
        """返回快照后的请求记录，统一封装成安全的 JSON 行供轨迹解析。"""
        lines: List[str] = []
        marker = f"sqlite:{self.request_logs_db}"
        if marker in snapshot and not self.request_logs_db.is_file():
            for _ in range(30):
                if self.request_logs_db.is_file():
                    break
                time.sleep(0.1)
        if self.request_logs_db.is_file():
            start = snapshot.get(marker, 0)
            rows: List[Any] = []
            # 请求日志数据库、schema 和正式记录分三步异步出现；任一步尚未完成都
            # 继续等，避免刚创建空 DB 时因 no such table 提前返回。
            for attempt in range(50):
                try:
                    with sqlite3.connect(self.request_logs_db, timeout=10) as db:
                        rows = db.execute(
                            """
                            SELECT id, request_id, status_code, ok, request_body_text,
                                   request_body_encoding, request_body_truncated,
                                   response_body_text, response_body_encoding,
                                   response_body_truncated
                            FROM request_logs
                            WHERE id > ? AND path = '/v1/messages'
                            ORDER BY id
                            """,
                            (start,),
                        ).fetchall()
                except sqlite3.Error:
                    rows = []
                if rows and all(row[7] for row in rows):
                    break
                if attempt < 49:
                    time.sleep(0.1)
            for row in rows:
                (row_id, request_id, status_code, ok, request_text, request_encoding,
                 request_truncated, response_text, response_encoding,
                 response_truncated) = row
                if request_encoding != "utf8" or response_encoding != "utf8":
                    continue
                if request_truncated or response_truncated:
                    continue
                lines.append(json.dumps({
                    "_ccr_format": "v3-sqlite",
                    "id": row_id,
                    "request_id": request_id,
                    "status_code": status_code,
                    "ok": bool(ok),
                    "request_body": request_text,
                    "response_body": response_text,
                }, ensure_ascii=False))
            return lines
        if not self.logs_dir.is_dir():
            return lines
        for path in sorted(self.logs_dir.glob("ccr-*.log")):
            start = snapshot.get(str(path), 0)
            with path.open(encoding="utf-8", errors="replace") as handle:
                for index, line in enumerate(handle):
                    if index >= start:
                        lines.append(line)
        return lines


# ==========================================================================
# CCR debug log -> OpenAI 对话（移植自 terminal-bench parse_cc.py 的核心）
#
# 安全：CCR 日志为外部进程产物，这里仅当作数据解析，不执行其中任何内容。
# ==========================================================================


def format_final_data(request_message: Dict[str, Any], unique_id: str, source: str,
                      agent_name: str, model_name: str) -> Optional[Dict[str, Any]]:
    msgs = copy.deepcopy(request_message)
    if "messages" not in msgs:
        return None

    for msg in msgs["messages"]:
        if msg.get("content") is None:
            msg["content"] = ""
        if msg["role"] == "system":
            content = msg.get("content", "")
            if content and isinstance(content, str):
                content = re.sub(r"Today's date: \d{4}-\d{2}-\d{2}\n?", "", content)
                content = re.sub(
                    r"You are powered by the model named .*?\. The exact model ID is .*?\.\n+",
                    "", content)
                content = re.sub(r"<claude_background_info>.*?</claude_background_info>\n*",
                                 "", content, flags=re.DOTALL)
                msg["content"] = content
        if msg["role"] == "assistant":
            if "reasoning_content" not in msg or msg["reasoning_content"] is None:
                msg["reasoning_content"] = ""
        if msg["role"] == "assistant" and msg.get("tool_calls"):
            for tool_call in msg["tool_calls"]:
                func = tool_call.get("function", {})
                args = func.get("arguments")
                if isinstance(args, str):
                    try:
                        func["arguments"] = json.loads(args)
                    except json.JSONDecodeError:
                        pass

    if msgs.get("tools"):
        for tool in msgs["tools"]:
            if "function" in tool:
                params = tool["function"].get("parameters", {})
                params.pop("title", None)
                for key in list(params.get("properties", {}).keys()):
                    params["properties"][key].pop("title", None)

    return {
        "messages": msgs["messages"],
        "tools": msgs.get("tools", []),
        "special_ability": "code_agent",
        "source": source,
        "id": unique_id,
        "synthetic_model": model_name,
        "tag": {"code_agent": agent_name},
    }


def _content_text(content: Any) -> str:
    """把 Anthropic text/tool_result 内容转成稳定文本，不执行或解释外部内容。"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else json.dumps(content, ensure_ascii=False)
    parts: List[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
        elif isinstance(block, dict) and "content" in block:
            parts.append(_content_text(block.get("content")))
        elif block is not None:
            parts.append(json.dumps(block, ensure_ascii=False))
    return "\n".join(part for part in parts if part)


def _anthropic_user_messages(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    if not isinstance(content, list):
        return [{"role": "user", "content": _content_text(content)}]

    messages: List[Dict[str, Any]] = []
    text_parts: List[str] = []

    def flush_text() -> None:
        if text_parts:
            messages.append({"role": "user", "content": "\n".join(text_parts)})
            text_parts.clear()

    for block in content:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            flush_text()
            messages.append({
                "role": "tool",
                "tool_call_id": str(block.get("tool_use_id") or ""),
                "content": _content_text(block.get("content")),
            })
        elif isinstance(block, dict) and block.get("type") == "text":
            text_parts.append(str(block.get("text") or ""))
        elif isinstance(block, str):
            text_parts.append(block)
        elif block is not None:
            # 图片/文档等少见输入不静默丢弃；以 JSON 文本保留原始语义。
            text_parts.append(json.dumps(block, ensure_ascii=False))
    flush_text()
    return messages


def _anthropic_assistant_message(message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    content = message.get("content")
    if isinstance(content, str):
        return {"role": "assistant", "content": content,
                "reasoning_content": "", "tool_calls": None}
    if not isinstance(content, list):
        return None

    text_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text" and block.get("text"):
            text_parts.append(str(block["text"]))
        elif block_type == "thinking" and block.get("thinking"):
            reasoning_parts.append(str(block["thinking"]))
        elif block_type == "tool_use":
            arguments = block.get("input", {})
            tool_calls.append({
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(block.get("name") or ""),
                    "arguments": json.dumps(arguments, ensure_ascii=False),
                },
            })
    if not text_parts and not reasoning_parts and not tool_calls:
        return None
    return {
        "role": "assistant",
        "content": "\n".join(text_parts) or None,
        "reasoning_content": "\n".join(reasoning_parts) or None,
        "tool_calls": tool_calls or None,
    }


def _anthropic_request_to_openai(request: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = []
    system = _content_text(request.get("system"))
    if system:
        messages.append({"role": "system", "content": system})
    for message in request.get("messages") or []:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            messages.extend(_anthropic_user_messages(message.get("content")))
        elif message.get("role") == "assistant":
            converted = _anthropic_assistant_message(message)
            if converted:
                messages.append(converted)
    if not messages:
        return None

    tools: List[Dict[str, Any]] = []
    for tool in request.get("tools") or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        function: Dict[str, Any] = {
            "name": str(tool["name"]),
            "parameters": copy.deepcopy(tool.get("input_schema") or {
                "type": "object", "properties": {}}),
        }
        if tool.get("description") is not None:
            function["description"] = str(tool["description"])
        tools.append({"type": "function", "function": function})
    return {"messages": messages, "tools": tools}


def _anthropic_sse_message(text: str) -> Optional[Dict[str, Any]]:
    message: Dict[str, Any] = {"role": "assistant", "content": []}
    blocks: Dict[int, Dict[str, Any]] = {}
    input_fragments: Dict[int, List[str]] = {}
    saw_event = False
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        saw_event = True
        event_type = event.get("type")
        if event_type == "message_start" and isinstance(event.get("message"), dict):
            initial = event["message"]
            message.update({key: value for key, value in initial.items() if key != "content"})
            for index, block in enumerate(initial.get("content") or []):
                if isinstance(block, dict):
                    blocks[index] = copy.deepcopy(block)
        elif event_type == "content_block_start":
            index = int(event.get("index", len(blocks)))
            block = event.get("content_block")
            if isinstance(block, dict):
                blocks[index] = copy.deepcopy(block)
        elif event_type == "content_block_delta" and isinstance(event.get("delta"), dict):
            index = int(event.get("index", 0))
            delta = event["delta"]
            block = blocks.setdefault(index, {})
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                block["type"] = block.get("type") or "text"
                block["text"] = str(block.get("text") or "") + str(delta.get("text") or "")
            elif delta_type == "thinking_delta":
                block["type"] = block.get("type") or "thinking"
                block["thinking"] = (str(block.get("thinking") or "") +
                                     str(delta.get("thinking") or ""))
            elif delta_type == "input_json_delta":
                input_fragments.setdefault(index, []).append(str(delta.get("partial_json") or ""))
            elif delta_type == "signature_delta" and delta.get("signature"):
                block["signature"] = str(block.get("signature") or "") + str(delta["signature"])
        elif event_type == "message_delta" and isinstance(event.get("delta"), dict):
            message.update(event["delta"])

    if not saw_event:
        return None
    for index, fragments in input_fragments.items():
        raw = "".join(fragments)
        try:
            blocks.setdefault(index, {})["input"] = json.loads(raw)
        except json.JSONDecodeError:
            blocks.setdefault(index, {})["input"] = raw
    message["content"] = [blocks[index] for index in sorted(blocks)]
    return message


def _anthropic_response_message(text: str) -> Optional[Dict[str, Any]]:
    stripped = text.strip()
    if not stripped:
        return None
    if stripped.startswith("{"):
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            return None
        if not isinstance(payload, dict) or payload.get("error"):
            return None
        return _anthropic_assistant_message(payload)
    parsed = _anthropic_sse_message(stripped)
    return _anthropic_assistant_message(parsed) if parsed else None


def _parse_lines(lines: Iterable[str], response_map: Dict[str, Any],
                 request_map: Dict[str, Any]) -> None:
    for line in lines:
        try:
            sample = json.loads(line)
        except json.JSONDecodeError:
            continue

        if sample.get("_ccr_format") == "v3-sqlite":
            try:
                request = json.loads(sample.get("request_body") or "")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(request, dict):
                continue
            body = _anthropic_request_to_openai(request)
            if not body or not body.get("messages") or not body.get("tools"):
                continue
            key = f"v3_{sample.get('id')}"
            request_map[key] = {"body": body, "key": key}
            response = _anthropic_response_message(sample.get("response_body") or "")
            if response:
                response_map[key] = response
            continue

        pid = sample.get("pid")
        req_id = sample.get("reqId")

        if "response" in sample and pid is not None and req_id is not None:
            key = f"{pid}_{req_id}"
            choices = sample["response"].get("choices", [])
            if not choices:
                continue
            choice = choices[0]
            if "delta" in choice:
                delta = choice["delta"]
                if not delta:
                    continue
                agg = response_map.setdefault(key, {
                    "role": "assistant", "content": "", "reasoning_content": "",
                    "tool_calls": [], "_tc_map": {},
                })
                # 不同模型的推理字段名不一：OpenAI 系为 reasoning_content，GLM 等为 reasoning
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning:
                    agg["reasoning_content"] += reasoning
                if delta.get("content"):
                    agg["content"] += delta["content"]
                for tc in delta.get("tool_calls") or []:
                    idx = tc.get("index", 0)
                    entry = agg["_tc_map"].setdefault(idx, {
                        "id": tc.get("id", ""), "type": "function",
                        "function": {"name": tc.get("function", {}).get("name", ""),
                                     "arguments": ""},
                    })
                    if tc.get("id"):
                        entry["id"] = tc["id"]
                    if tc.get("function", {}).get("name"):
                        entry["function"]["name"] = tc["function"]["name"]
                    if tc.get("function", {}).get("arguments"):
                        entry["function"]["arguments"] += tc["function"]["arguments"]
            elif "message" in choice:
                msg_obj = choice["message"]
                msg_obj.setdefault("role", "assistant")
                # 归一非流式响应的推理字段名
                if not msg_obj.get("reasoning_content") and msg_obj.get("reasoning"):
                    msg_obj["reasoning_content"] = msg_obj.pop("reasoning")
                response_map[key] = msg_obj

        elif "request" in sample and sample.get("msg") == "final request":
            body_str = sample["request"].get("body")
            try:
                body = json.loads(body_str)
            except (json.JSONDecodeError, TypeError):
                continue
            messages = body.get("messages", [])
            if ("tools" not in body) or (not messages):
                continue

            # One record per real LLM call, keyed by pid_reqId. Reasoning lives
            # only in the live streamed response (never in request-body history),
            # so collapsing calls that share a first user turn would discard it.
            # If the same reqId issues several final requests (retransmit), keep
            # the last (most complete) body.
            key = f"{pid}_{req_id}"
            request_map[key] = {"body": body, "key": key}


def pack_messages_from_lines(lines: Sequence[str]) -> Iterator[Dict[str, Any]]:
    response_map: Dict[str, Any] = {}
    request_map: Dict[str, Any] = {}
    _parse_lines(lines, response_map, request_map)

    records: List[Dict[str, Any]] = []
    for item in request_map.values():
        request_message = item["body"]
        key = item["key"]

        has_response = False
        if key in response_map:
            response_message = response_map[key]
            if isinstance(response_message, dict) and "_tc_map" in response_message:
                tc_map = response_message.pop("_tc_map")
                response_message["tool_calls"] = (
                    [tc_map[i] for i in sorted(tc_map.keys())] if tc_map else None)
                if not response_message.get("content"):
                    response_message["content"] = None
                if not response_message.get("reasoning_content"):
                    response_message["reasoning_content"] = None
            if response_message:
                request_message["messages"].append(response_message)
                has_response = True

        id2name: Dict[str, str] = {}
        for turn in request_message["messages"]:
            if turn.get("role") == "assistant":
                for tool in turn.get("tool_calls") or []:
                    id2name[tool["id"]] = tool["function"]["name"]
            elif turn.get("role") == "tool":
                tool_call_id = turn.get("tool_call_id")
                if tool_call_id in id2name:
                    turn["name"] = id2name[tool_call_id]

        # body_key 只含请求体（历史消息），不含本次追加的响应。
        body = (request_message["messages"][:-1] if has_response
                else request_message["messages"])
        records.append({
            "message": request_message,
            "has_response": has_response,
            "body_key": json.dumps(body, sort_keys=True, ensure_ascii=False),
        })

    # 过滤重试残渣。CLI/CCR 超时重试会把同一逻辑请求（请求体逐字节相同）以
    # 不同 reqId 各记一条 "final request"，同 reqId 的重发已在 _parse_lines
    # 按 key 覆盖，这里处理跨 reqId 的情况。按请求体分组后：
    #   1) 组内只保留日志顺序最后一条带响应的记录 —— 线性会话里同一请求体被
    #      重发只会是重试，被 CLI 接受（并据此推进历史）的是最后一次成功尝试；
    #      早次尝试的响应要么没记到（客户端已断开），要么是断开前的截断残流，
    #      若按"先到先得"保留会把截断响应当成 SFT 目标轮；
    #   2) 整组都没有响应则丢弃 —— 该次调用从未成功，记录里没有任何新的
    #      assistant/CoT 内容（CoT 只存在于实时 response 中），留着则是
    #      "以 tool/user 结尾"的畸形 SFT 样本。
    # 组顺序按首次出现排列，与被接受响应推进历史的逻辑时序一致；
    # 判定只依据请求体与"是否追加到响应"，不动 messages 内部任何一轮。
    groups: Dict[str, List[Dict[str, Any]]] = {}
    group_order: List[str] = []
    for rec in records:
        if rec["body_key"] not in groups:
            groups[rec["body_key"]] = []
            group_order.append(rec["body_key"])
        groups[rec["body_key"]].append(rec)

    for body_key in group_order:
        chosen: Optional[Dict[str, Any]] = None
        for rec in groups[body_key]:
            if rec["has_response"]:
                chosen = rec  # 覆盖式保留最后一次成功尝试
        if chosen is not None:
            yield chosen["message"]


def write_stage_jsonl(lines: Sequence[str], out_path: Path, stage: str,
                      model: str, source: str = "data_world") -> int:
    """把某阶段的 CCR 日志行重组成对话，写入 out_path，返回写出的记录数。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_path.open("w", encoding="utf-8") as handle:
        for request_message in pack_messages_from_lines(lines):
            if not request_message.get("messages"):
                continue
            record = format_final_data(
                request_message, f"{stage}_{count}", source, "claude-code", model)
            if record:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
    return count


def write_single_turn_jsonl(prompt: str, response_text: str, out_path: Path,
                            stage: str, model: str, source: str = "data_world") -> None:
    """把一次无工具的单轮调用（如 contract）写成与 write_stage_jsonl 同构的 SFT 记录。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "messages": [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": response_text,
             "reasoning_content": "", "tool_calls": None},
        ],
        "tools": [],
        "special_ability": "code_agent",
        "source": source,
        "id": f"{stage}_0",
        "synthetic_model": model,
        "tag": {"code_agent": "llm-client"},
    }
    with out_path.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
