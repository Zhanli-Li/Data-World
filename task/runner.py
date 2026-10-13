"""出题 Agent 的流式运行器：``ClaudeRunner`` 的流式替代。

出题是全流程里最长的一次 agent 会话（自由探索 + 写几十条 rubric），非流式请求
（``--output-format json``）有两个后果：

1. 整段会话生成完才出第一个字节，任何一次网关抖动都要靠整段重跑来补；
2. 长响应被网关截断时 CLI 会误报成功，只能靠事后识别 ``stop_reason`` 兜底
   （``data_world.sdk`` 里两处 "response likely truncated by gateway" 即为此）。

改用 ``--output-format stream-json``：事件逐条到达，超时语义变成"事件之间的空档"，
只要 agent 还在动就不算超时。于是出题提示词不再需要"输出长度纪律"那类为规避
超时而设的自我限制——rubric 该写多少条就写多少条。

复用 ``data_world.sdk.ClaudeRunner`` 的命令行构造与瞬时错误判定（同一套端点注入、
代理绕行与重试口径），只把"取结果"从"等进程结束读 stdout"换成"边读边解析事件流"。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from queue import Empty, Queue
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

from data_world.sdk import _TRANSIENT_RE, _build_claude_env, ClaudeRunner

#: 事件之间的最大空档（秒）。超过它才判卡死——不是整段会话的总时长上限。
IDLE_TIMEOUT_SEC = 900.0

#: 会话总时长的硬上限（秒）：即便一直有事件，也不允许无限跑下去。
HARD_TIMEOUT_SEC = 14_400.0

#: 视为正常收尾的 ``stop_reason``（与 ``data_world.sdk`` 保持一致）。
_OK_STOP_REASONS = ("end_turn", "max_turns", "pause_turn")


class StreamingClaudeRunner(ClaudeRunner):
    """与 ``ClaudeRunner`` 同接口，但以 ``stream-json`` 读取事件流。

    ``run()`` 的重试语义、返回三元组 ``(ok, message, raw)`` 均与父类一致，
    ``pipeline.py`` 因此可以直接替换而无需分支。
    """

    def __init__(self, *args: Any, idle_timeout: float = IDLE_TIMEOUT_SEC,
                 hard_timeout: float = HARD_TIMEOUT_SEC, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.idle_timeout = idle_timeout
        self.hard_timeout = hard_timeout

    def _build_command(self, prompt: str, cwd: Path, add_dirs: Sequence[Path],
                       max_turns: int, use_stdin: bool) -> List[str]:
        """与父类同构的命令行，只把输出格式换成 stream-json。

        ``--verbose`` 是 ``-p --output-format stream-json`` 的必需搭配（CLI 要求），
        ``--include-partial-messages`` 让文本增量也成为事件，空档判定因此更灵敏。
        """
        cmd = [self.executable, "-p"]
        if not use_stdin:
            cmd.append(prompt)
        cmd += ["--output-format", "stream-json", "--verbose",
                "--include-partial-messages",
                "--permission-mode", "bypassPermissions", "--model", self.model,
                "--max-turns", str(max_turns), "--allowedTools",
                "Read", "Write", "Edit", "Bash", "Glob", "Grep", "--add-dir", str(cwd)]
        for directory in add_dirs:
            cmd.extend(["--add-dir", str(directory)])
        cmd.extend([
            "--settings",
            json.dumps({"skipDangerousModePermissionPrompt": True}),
        ])
        return cmd

    def _run_once(self, prompt: str, cwd: Path, add_dirs: Sequence[Path],
                  max_turns: int) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        cwd.mkdir(parents=True, exist_ok=True)
        # 超长 prompt 会超过内核单参数 128KB 上限 (E2BIG)，改走 stdin（同父类）。
        use_stdin = len(prompt.encode("utf-8")) > 96 * 1024
        cmd = self._build_command(prompt, cwd, add_dirs, max_turns, use_stdin)
        env = self._child_env()
        started = time.time()
        if self.verbose:
            via = "stdin" if use_stdin else "argv"
            print(f"    [stream] model={self.model} cwd={cwd} "
                  f"max_turns={max_turns} prompt_via={via}", flush=True)
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(cwd), env=env, text=True, bufsize=1,
                stdin=subprocess.PIPE if use_stdin else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as exc:
            return False, f"cannot execute {self.executable!r}: {exc}", None

        try:
            return self._consume(proc, prompt if use_stdin else None, started)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=30)
            # Popen 不作为 context manager 使用时不会替我们关闭三条管道；批量重试下
            # 泄漏的 fd 会逐题累积，最终让后续 attempt 无法再创建 pipe。
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if stream is None:
                    continue
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass

    def _child_env(self) -> Dict[str, str]:
        return _build_claude_env(self.endpoint, self.auth_token)

    def _consume(self, proc: "subprocess.Popen[str]", stdin_text: Optional[str],
                 started: float) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
        """边读事件边推进空档计时；返回与父类同构的三元组。"""
        if stdin_text is not None:
            # 单独线程喂 stdin：prompt 很大时直接 write 可能因管道满而与读 stdout 互锁。
            threading.Thread(target=self._feed_stdin, args=(proc, stdin_text),
                             daemon=True).start()
        stderr_tail: List[str] = []
        threading.Thread(target=self._drain_stderr, args=(proc, stderr_tail),
                         daemon=True).start()

        result_event: Optional[Dict[str, Any]] = None
        assistant_text: List[str] = []
        events = 0
        for line in self._iter_lines(proc):
            if line is None:  # 空档超时
                idle = self.idle_timeout
                return False, (f"stream idle timeout after {idle:.0f}s "
                               f"(events={events}, elapsed={time.time() - started:.0f}s)"), None
            events += 1
            event = self._parse_line(line)
            if event is None:
                continue
            kind = event.get("type")
            if kind == "result":
                result_event = event
            elif kind == "assistant":
                assistant_text.append(self._assistant_text(event))
            if time.time() - started > self.hard_timeout:
                return False, f"hard timeout after {self.hard_timeout:.0f}s", None

        proc.wait(timeout=60)
        stderr = "".join(stderr_tail)[-1000:].strip()
        if result_event is None:
            return False, (f"stream ended without result event "
                           f"(exit={proc.returncode}, events={events}); stderr={stderr}"), None

        raw = result_event
        result = str(raw.get("result") or raw.get("text") or "")
        if raw.get("is_error", False) and not result.strip():
            # 新版 CLI 的 error_during_execution 结果常把诊断只放在 errors/subtype，
            # result 留空。若这里仍返回空字符串，父类看不到可重试信号，会把一次上游
            # 空尾响应立即固化成 0 分。保留诊断并复用现有的瞬时重试口径。
            errors = "; ".join(str(item) for item in (raw.get("errors") or []) if item)
            detail = errors or str(raw.get("subtype") or "agent execution error")
            # ``unexpected EOF`` 属于共享 runner 的瞬时错误模式，触发既有退避重试。
            result = f"{detail}; unexpected EOF (response likely truncated by gateway)"
        if not result.strip():
            # stream-json 的 result 事件偶尔不带 result 字段；用累积的 assistant 文本兜底。
            result = "\n".join(t for t in assistant_text if t).strip()
        ok = proc.returncode == 0 and not raw.get("is_error", False)
        # 流式下截断已由事件流本身暴露，但仍校验收尾原因，与父类口径一致。
        stop_reason = raw.get("stop_reason")
        if ok and stop_reason is not None and stop_reason not in _OK_STOP_REASONS:
            return False, (f"session ended abnormally: stop_reason={stop_reason} "
                           f"turns={raw.get('num_turns', '?')}"), raw
        if ok and not result.strip():
            return False, (f"empty result with stop_reason={stop_reason} "
                           f"turns={raw.get('num_turns', '?')}"), raw
        if self.verbose:
            print(f"    [stream] done {time.time() - started:.0f}s ok={ok} "
                  f"events={events} turns={raw.get('num_turns', '?')}", flush=True)
        if not ok and stderr:
            result = f"{result}\nstderr={stderr}"
        return ok, result, raw

    @staticmethod
    def _feed_stdin(proc: "subprocess.Popen[str]", text: str) -> None:
        try:
            assert proc.stdin is not None
            proc.stdin.write(text)
            proc.stdin.close()
        except (BrokenPipeError, ValueError, AssertionError):
            pass  # 子进程先退出；失败原因由 result/stderr 反映

    @staticmethod
    def _drain_stderr(proc: "subprocess.Popen[str]", sink: List[str]) -> None:
        try:
            assert proc.stderr is not None
            for line in proc.stderr:
                sink.append(line)
                del sink[:-50]  # 只留尾部，避免长会话把 stderr 全存进内存
        except (ValueError, AssertionError):
            pass

    def _iter_lines(self, proc: "subprocess.Popen[str]") -> Iterator[Optional[str]]:
        """逐行 yield stdout；空档超过 ``idle_timeout`` 时 yield 一个 None。

        读取放到线程里经队列取，才能在"读阻塞"上加超时——``for line in stdout``
        本身无法被超时打断。
        """
        queue: "Queue[Optional[str]]" = Queue()

        def pump() -> None:
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    queue.put(line)
            except (ValueError, AssertionError):
                pass
            finally:
                queue.put(None)

        threading.Thread(target=pump, daemon=True).start()
        while True:
            try:
                line = queue.get(timeout=self.idle_timeout)
            except Empty:
                yield None
                return
            if line is None:
                return
            yield line

    @staticmethod
    def _parse_line(line: str) -> Optional[Dict[str, Any]]:
        line = line.strip()
        if not line:
            return None
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return None
        return event if isinstance(event, dict) else None

    @staticmethod
    def _assistant_text(event: Dict[str, Any]) -> str:
        blocks = ((event.get("message") or {}).get("content")) or []
        return "".join(block.get("text", "") for block in blocks
                       if isinstance(block, dict) and block.get("type") == "text")


__all__ = ["StreamingClaudeRunner", "IDLE_TIMEOUT_SEC", "HARD_TIMEOUT_SEC", "_TRANSIENT_RE"]
