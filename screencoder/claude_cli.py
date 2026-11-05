"""A long-lived `claude -p` process speaking stream-json on stdin/stdout, wrapped as a Qt object."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, Signal

# Variables that make a child CLI think it is nested inside another Claude Code / IDE session.
_SCRUBBED_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT")
_LOG_LINE_LIMIT = 100_000


def _fingerprint(content) -> str:
    """Identify a user message by its last text block (every message we send ends with one)."""
    if isinstance(content, str):
        return content[:500]
    texts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
    return texts[-1][:500] if texts else ""


def _kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


class ClaudeSession(QObject):
    """One Claude Code session. Send user messages with send(); events arrive as signals.

    A message sent while a turn is running is injected into that turn at the next tool boundary
    (or starts a new turn if the current one is already answering). The CLI echoes each message
    when it consumes it (--replay-user-messages), which is how `busy` knows whether more work is
    pending after a "result". interrupt() ends the current turn but keeps the session alive.

    With `resume` and `fork` it starts as a copy of that (saved) session: everything said and done in it so far,
    under a new id of its own, so the original goes on untouched.
    """

    initialized = Signal(dict)
    status_changed = Signal(str)
    permission_denied = Signal(dict)
    message_consumed = Signal(str)  # token returned by send(): the agent has just picked that message up
    text_started = Signal()
    text_delta = Signal(str)
    text_finished = Signal()
    thinking_finished = Signal(str)
    tool_used = Signal(dict, bool)  # tool_use block, came from a subagent
    tool_result = Signal(dict, bool)  # tool_result block, came from a subagent
    turn_finished = Signal(dict)  # the "result" event
    process_finished = Signal(int, str)  # exit code, stderr tail

    def __init__(
        self,
        *,
        claude_path: str,
        cwd: Path,
        model: str,
        effort: str | None = None,
        permission_mode: str | None = None,
        tools: str | None = None,
        add_dirs: list[Path] | None = None,
        append_system_prompt: str | None = None,
        name: str | None = None,
        persist: bool = True,
        log_path: Path | None = None,
        resume: str | None = None,
        fork: bool = False,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self.claude_path = claude_path
        self.cwd = Path(cwd)
        self.model = model
        self.effort = effort
        self.permission_mode = permission_mode
        self.tools = tools
        self.add_dirs = list(add_dirs or [])
        self.append_system_prompt = append_system_prompt
        self.name = name
        self.persist = persist
        self.log_path = log_path
        self.resume = resume
        self.fork = fork
        self.session_id = ""
        self.total_cost = 0.0
        self.entries = 0  # messages and tool results in the conversation so far: grows whenever it moves on
        self.thinking = True  # whether it thinks before answering (set_thinking)
        self._pending: deque[str] = deque()  # fingerprints of sent messages not yet consumed
        self._turn_active = False
        self._stopping = False
        self._buf = bytearray()
        self._stderr = bytearray()
        self._blocks: dict[int, dict] = {}  # stream content-block index -> {"type", "text"}
        self._proc = QProcess(self)
        self._proc.readyReadStandardOutput.connect(self._on_stdout)
        self._proc.readyReadStandardError.connect(self._on_stderr)
        self._proc.finished.connect(self._on_finished)
        self._proc.errorOccurred.connect(self._on_error)

    # ----------------------------------------------------------------- control
    def build_args(self) -> list[str]:
        args = [
            "-p",
            "--input-format", "stream-json",
            "--output-format", "stream-json",
            "--verbose",
            "--include-partial-messages",
            "--replay-user-messages",
            "--model", self.model,
            # Nobody can answer permission prompts in this app: anything that would prompt is denied.
            "--permission-prompts", "none",
        ]
        if self.effort:
            args += ["--effort", self.effort]
        if self.permission_mode:
            args += ["--permission-mode", self.permission_mode]
        if self.tools is not None:
            args += ["--tools", self.tools]
        if self.append_system_prompt:
            args += ["--append-system-prompt", self.append_system_prompt]
        if self.name:
            args += ["--name", self.name]
        if not self.persist:
            args += ["--no-session-persistence"]
        if self.resume:
            args += ["--resume", self.resume]
        if self.fork:
            args += ["--fork-session"]
        for directory in self.add_dirs:
            args += ["--add-dir", str(directory)]
        return args

    def start(self) -> None:
        env = QProcessEnvironment.systemEnvironment()
        for key in _SCRUBBED_ENV:
            env.remove(key)
        # Make sure `python` works in the agent's shell so it can run its checks: on many Windows PCs the
        # bare command only hits the Microsoft Store placeholder. Our own interpreter goes first on PATH.
        exe = "python.exe" if sys.platform == "win32" else "python3"
        own = [d for d in (sys.base_prefix, str(Path(sys.executable).parent)) if Path(d, exe).is_file()]
        if own:
            env.insert("PATH", os.pathsep.join(dict.fromkeys(own + [env.value("PATH")])))
        self._proc.setProcessEnvironment(env)
        self._proc.setWorkingDirectory(str(self.cwd))
        self._proc.setProgram(self.claude_path)
        self._proc.setArguments(self.build_args())
        self._proc.start()
        self.status_changed.emit("starting")

    @property
    def running(self) -> bool:
        return self._proc.state() != QProcess.ProcessState.NotRunning

    @property
    def busy(self) -> bool:
        """A turn is running, or a sent message has not been picked up yet."""
        return self.running and (self._turn_active or bool(self._pending))

    def send(self, content: list[dict]) -> str:
        """Queue a user message; returns a token that message_consumed emits when the agent picks it up."""
        token = _fingerprint(content)
        self._pending.append(token)
        self._write({"type": "user", "message": {"role": "user", "content": content}})
        return token

    def set_effort(self, effort: str | None) -> None:
        """Think at this effort from the next request on (the CLI applies it at once, between turns too). The
        conversation is cached once more at the new level, so it is worth it only for a change of phase."""
        if not effort or effort == self.effort:
            return
        self.effort = effort  # a copy of this session must use the same level to share its cache
        if self.running:
            self._write({
                "type": "control_request",
                "request_id": f"req_{uuid.uuid4().hex[:12]}",
                "request": {"subtype": "apply_flag_settings", "settings": {"effortLevel": effort}},
            })

    def set_thinking(self, on: bool) -> None:
        """Think before answering, or not (thinking budget 0), from the next request on; the CLI applies it at
        once. Measured: in effect for the very next request, and switched back by "on". Like an effort change,
        it reads the conversation into the cache once more."""
        if on == self.thinking:
            return
        self.thinking = on
        if self.running:
            self._write({
                "type": "control_request",
                "request_id": f"req_{uuid.uuid4().hex[:12]}",
                "request": {"subtype": "set_max_thinking_tokens", "max_thinking_tokens": None if on else 0},
            })

    def interrupt(self) -> None:
        if self.running:
            self._write({
                "type": "control_request",
                "request_id": f"req_{uuid.uuid4().hex[:12]}",
                "request": {"subtype": "interrupt"},
            })

    def stop(self) -> None:
        if not self.running:
            return
        self._stopping = True
        pid = self._proc.processId()
        if pid:
            _kill_tree(pid)
        self._proc.kill()

    def _write(self, obj: dict) -> None:
        if self.running:
            self._proc.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))

    # ----------------------------------------------------------------- output
    def _on_stderr(self) -> None:
        self._stderr += bytes(self._proc.readAllStandardError().data())
        del self._stderr[:-8000]

    def _on_stdout(self) -> None:
        self._buf += bytes(self._proc.readAllStandardOutput().data())
        while True:
            newline = self._buf.find(b"\n")
            if newline < 0:
                break
            line = bytes(self._buf[:newline]).strip()
            del self._buf[: newline + 1]
            if not line:
                continue
            self._log(line)
            try:
                event = json.loads(line)
            except ValueError:
                self._stderr += line + b"\n"
                continue
            try:
                self._handle(event)
            except Exception as exc:  # never let one odd event kill the reader
                print(f"[screencoder] failed to handle event: {exc!r}", file=sys.stderr)

    def _log(self, line: bytes) -> None:
        if self.log_path is None:
            return
        try:
            with open(self.log_path, "ab") as fh:
                fh.write(line[:_LOG_LINE_LIMIT] + b"\n")
        except OSError:
            pass

    def _handle(self, ev: dict) -> None:
        kind = ev.get("type")
        sub = ev.get("parent_tool_use_id") is not None
        if kind == "stream_event":
            if not sub:
                self._handle_stream(ev.get("event") or {})
        elif kind == "assistant":
            self.entries += 1
            for block in (ev.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    self.tool_used.emit(block, sub)
        elif kind == "user":
            self.entries += 1
            content = (ev.get("message") or {}).get("content")
            results = [b for b in content or [] if isinstance(b, dict) and b.get("type") == "tool_result"] \
                if isinstance(content, list) else []
            for block in results:
                self.tool_result.emit(block, sub)
            if not results and not sub:
                fingerprint = _fingerprint(content)
                if fingerprint in self._pending:  # echo of a message we sent: it is now being worked on
                    self._pending.remove(fingerprint)
                    self._turn_active = True
                    self.message_consumed.emit(fingerprint)
        elif kind == "result":
            if not self._turn_active and not ev.get("num_turns") and not ev.get("is_error"):
                return  # a copy first winds up a notice left in the session it copied: not an answer of ours
            self._turn_active = False
            self.total_cost = float(ev.get("total_cost_usd") or self.total_cost)
            self.status_changed.emit("idle")
            self.turn_finished.emit(ev)
        elif kind == "system":
            subtype = ev.get("subtype")
            if subtype == "init":
                self.session_id = ev.get("session_id", "")
                self.status_changed.emit("ready")
                self.initialized.emit(ev)
            elif subtype == "compact_boundary":
                self.status_changed.emit("compacted context")
            elif subtype == "permission_denied":
                self.permission_denied.emit(ev)
            elif subtype == "api_retry":
                self.status_changed.emit(
                    f"API error ({ev.get('error_status') or ev.get('error') or 'network'}), "
                    f"retrying {ev.get('attempt', '?')}/{ev.get('max_retries', '?')}"
                )
            elif subtype == "status" and ev.get("status"):
                self.status_changed.emit(str(ev.get("status")))

    def _handle_stream(self, event: dict) -> None:
        etype = event.get("type")
        if etype == "message_start":
            self._blocks.clear()
        elif etype == "content_block_start":
            block = event.get("content_block") or {}
            btype = block.get("type", "")
            self._blocks[event.get("index", 0)] = {"type": btype, "text": ""}
            if btype == "text":
                self.status_changed.emit("writing")
                self.text_started.emit()
            elif btype in ("thinking", "redacted_thinking"):
                self.status_changed.emit("thinking")
            elif btype == "tool_use":
                self.status_changed.emit(f"using {block.get('name', 'a tool')}")
        elif etype == "content_block_delta":
            delta = event.get("delta") or {}
            state = self._blocks.get(event.get("index", 0))
            if delta.get("type") == "text_delta":
                self.text_delta.emit(delta.get("text", ""))
            elif delta.get("type") == "thinking_delta" and state is not None:
                state["text"] += delta.get("thinking", "")
        elif etype == "content_block_stop":
            state = self._blocks.pop(event.get("index", 0), None)
            if state is None:
                return
            if state["type"] == "text":
                self.text_finished.emit()
            elif state["type"] == "thinking":
                self.thinking_finished.emit(state["text"])
        elif etype == "message_stop":
            self.status_changed.emit("working")

    def _on_error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart:
            self._pending.clear()
            self._turn_active = False
            self.process_finished.emit(-1, f"Could not start the Claude Code CLI at {self.claude_path!r}.")

    def _on_finished(self, code: int, _status) -> None:
        self._on_stdout()
        self._pending.clear()
        self._turn_active = False
        tail = "" if self._stopping else self._stderr.decode("utf-8", errors="replace").strip()
        self.process_finished.emit(code, tail)
