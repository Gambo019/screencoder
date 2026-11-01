"""An agentic coding/answering session that runs on the OpenAI or Anthropic API, shaped as a drop-in for
ClaudeSession (claude_cli.py): the same Qt signals and the same `tool_use`/`tool_result` block shapes, so the rest
of the app does not know or care which back end is behind a session.

It runs a tool-use loop in the given working directory with the same tools the app already understands - Read,
Write, Edit, Glob, Grep, LS and Bash - executing them locally and feeding the results back to the model until it
stops. Requests go over Qt networking (no extra package). Each model step is a single (non-streaming) call, which
keeps the loop simple and robust; the step's whole text arrives as one text_delta. Bash runs in a child process so
a long command never freezes the window.

Only the machinery is exercised by this project's tests (against a mock model that drives the loop); how well a
real OpenAI/Anthropic model follows the app's coding protocol depends on the model and the key.
"""
from __future__ import annotations

import json
import re
import sys
import time
import uuid
from collections import deque
from pathlib import Path

from PySide6.QtCore import QByteArray, QObject, QProcess, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from . import llm

READ_CAP = 100_000       # characters returned by Read / a tool result
GREP_CAP = 200           # matching lines returned by Grep
GLOB_CAP = 400           # paths returned by Glob / LS
BASH_TIMEOUT_MS = 120_000
MAX_STEPS = 50           # tool rounds in one turn before giving up
ANTHROPIC_MAX_TOKENS = 8192


class ToolError(Exception):
    pass


# The tools the model may call. One neutral schema, rendered into each API's format below.
TOOLS = [
    ("Read", "Read a text file in the workspace.", {
        "file_path": ("string", "Path to the file, absolute or relative to the workspace.")}, ["file_path"]),
    ("Write", "Create or overwrite a file in the workspace with the given content.", {
        "file_path": ("string", "Path to write, relative to the workspace."),
        "content": ("string", "The full new contents of the file.")}, ["file_path", "content"]),
    ("Edit", "Replace an exact piece of text in a file.", {
        "file_path": ("string", "Path to the file."),
        "old_string": ("string", "The exact text to replace (must appear once unless replace_all)."),
        "new_string": ("string", "The replacement text."),
        "replace_all": ("boolean", "Replace every occurrence (default false).")},
     ["file_path", "old_string", "new_string"]),
    ("Glob", "List files matching a glob pattern (e.g. **/*.py).", {
        "pattern": ("string", "Glob pattern."),
        "path": ("string", "Directory to search in (default the workspace).")}, ["pattern"]),
    ("Grep", "Search file contents with a regular expression.", {
        "pattern": ("string", "Regular expression."),
        "path": ("string", "Directory or file to search (default the workspace)."),
        "glob": ("string", "Only search files matching this glob (optional).")}, ["pattern"]),
    ("LS", "List the entries of a directory.", {
        "path": ("string", "Directory (default the workspace).")}, []),
    ("Bash", "Run a shell command in the workspace. Use for building and running tests.", {
        "command": ("string", "The command line to run.")}, ["command"]),
]


def _tool_schema(props: dict, required: list) -> dict:
    return {"type": "object",
            "properties": {k: {"type": t, "description": d} for k, (t, d) in props.items()},
            "required": required}


def _anthropic_tools(allowed) -> list:
    return [{"name": n, "description": d, "input_schema": _tool_schema(p, r)}
            for n, d, p, r in TOOLS if n in allowed]


def _openai_tools(allowed) -> list:
    return [{"type": "function", "function": {"name": n, "description": d, "parameters": _tool_schema(p, r)}}
            for n, d, p, r in TOOLS if n in allowed]


def _within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def _fingerprint(content) -> str:
    if isinstance(content, str):
        return content[:500]
    texts = [b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
    return texts[-1][:500] if texts else ""


def _as_blocks(content) -> list:
    """A sent message (string or Anthropic-style blocks) as a block list for the history."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return list(content)


def _result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return ""


def _to_openai(history: list, system: str) -> list:
    """Translate the Anthropic-style history into OpenAI chat messages (text, images, tool calls, tool results)."""
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    for message in history:
        role, blocks = message["role"], message["content"]
        if role == "assistant":
            text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
            tool_calls = [{"id": b["id"], "type": "function",
                           "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})}}
                          for b in blocks if b.get("type") == "tool_use"]
            msg = {"role": "assistant", "content": text or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            messages.append(msg)
            continue
        results = [b for b in blocks if b.get("type") == "tool_result"]
        if results:
            for b in results:
                messages.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": _result_text(b)})
            continue
        parts = []
        for b in blocks:
            if b.get("type") == "text":
                parts.append({"type": "text", "text": b.get("text", "")})
            elif b.get("type") == "image":
                src = b.get("source") or {}
                parts.append({"type": "image_url",
                              "image_url": {"url": f"data:{src.get('media_type')};base64,{src.get('data')}"}})
        has_image = any(p["type"] == "image_url" for p in parts)
        messages.append({"role": "user", "content": parts if has_image else "".join(
            p["text"] for p in parts if p["type"] == "text")})
    return messages


class ApiAgentSession(QObject):
    """Drop-in for ClaudeSession backed by the OpenAI or Anthropic API. Same signals, same tool-block shapes."""

    initialized = Signal(dict)
    status_changed = Signal(str)
    permission_denied = Signal(dict)
    message_consumed = Signal(str)
    text_started = Signal()
    text_delta = Signal(str)
    text_finished = Signal()
    thinking_finished = Signal(str)
    tool_used = Signal(dict, bool)
    tool_result = Signal(dict, bool)
    turn_finished = Signal(dict)
    process_finished = Signal(int, str)

    def __init__(self, *, provider: str, api_key: str, cwd, model: str, tools: str | None = None,
                 add_dirs=None, append_system_prompt: str | None = None, permission_mode: str | None = None,
                 can_run: bool = False, log_path=None, name: str | None = None,
                 parent: QObject | None = None, **_ignored):
        super().__init__(parent)
        self.provider = provider
        self.api_key = api_key
        self.cwd = Path(cwd).resolve()
        self.model = model
        self.add_dirs = [Path(d).resolve() for d in (add_dirs or [])]
        self.system = append_system_prompt or ""
        self.permission_mode = permission_mode
        self.can_run = can_run
        self.log_path = log_path
        self.name = name
        self.session_id = uuid.uuid4().hex
        self.total_cost = 0.0
        allowed = {t.strip() for t in tools.split(",")} if tools else {n for n, *_ in TOOLS}
        self._allowed = {t[0] for t in TOOLS if t[0] in allowed}
        self._history: list[dict] = []   # Anthropic-style messages: {"role", "content": [blocks]}
        self._queue: deque = deque()
        self._running = False
        self._busy = False
        self._cancel = False
        self._n = 0
        self._results: list[dict] = []
        self._net = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._bash: QProcess | None = None

    # ----------------------------------------------------------------- control (ClaudeSession parity)
    def build_args(self) -> list:
        return []

    def start(self) -> None:
        self._running = True
        self.status_changed.emit("starting")
        QTimer.singleShot(0, self._ready)

    def _ready(self) -> None:
        if not self._running:
            return
        self.status_changed.emit("ready")
        self.initialized.emit({"session_id": self.session_id, "model": self.model, "subtype": "init"})
        self._pump()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def busy(self) -> bool:
        return self._running and (self._busy or bool(self._queue))

    def send(self, content) -> str:
        token = _fingerprint(content)
        self._queue.append((content, token))
        QTimer.singleShot(0, self._pump)
        return token

    def interrupt(self) -> None:
        # Deferred like ClaudeSession's async "result": lets the caller's handler run after this returns, and
        # avoids re-entering _pump from inside interrupt().
        if self._busy:
            self._cancel = True
            self._abort_reply()
            self._kill_bash()
            QTimer.singleShot(0, lambda: self._finish_turn(error=True, text="(interrupted)", interrupted=True))

    def stop(self) -> None:
        self._running = False
        self._cancel = True
        self._abort_reply()
        self._kill_bash()
        # Deferred so a caller that stops then clears its reference (e.g. _retire_side) is not re-entered here.
        QTimer.singleShot(0, lambda: self.process_finished.emit(0, ""))

    # ----------------------------------------------------------------- the turn loop
    def _pump(self) -> None:
        if self._busy or not self._queue or not self._running:
            return
        content, token = self._queue.popleft()
        self._busy = True
        self._cancel = False
        self._history.append({"role": "user", "content": _as_blocks(content)})
        self.message_consumed.emit(token)
        self.status_changed.emit("working")
        self._step(0)

    def _step(self, n: int) -> None:
        if self._cancel or not self._running:
            return
        if n >= MAX_STEPS:
            self._finish_turn(error=True, text="(stopped: too many tool steps)")
            return
        self._n = n
        self.status_changed.emit("thinking")
        try:
            self._request()
        except Exception as exc:  # noqa: BLE001
            self._finish_turn(error=True, text=f"{exc.__class__.__name__}: {exc}")

    # ----------------------------------------------------------------- HTTP
    def _request(self) -> None:
        if self.provider == llm.ANTHROPIC:
            url = f"{llm._endpoint(llm.ANTHROPIC)}/v1/messages"
            body = {"model": self.model, "max_tokens": ANTHROPIC_MAX_TOKENS,
                    "messages": self._history, "tools": _anthropic_tools(self._allowed)}
            if self.system:
                body["system"] = self.system
            request = QNetworkRequest(QUrl(url))
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            request.setRawHeader(b"x-api-key", self.api_key.encode())
            request.setRawHeader(b"anthropic-version", llm.ANTHROPIC_VERSION.encode())
        else:
            url = f"{llm._endpoint(llm.OPENAI)}/chat/completions"
            body = {"model": self.model, "messages": _to_openai(self._history, self.system),
                    "tools": _openai_tools(self._allowed)}
            request = QNetworkRequest(QUrl(url))
            request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
            request.setRawHeader(b"Authorization", f"Bearer {self.api_key}".encode())
        payload = QByteArray(json.dumps(body).encode("utf-8"))
        self._log("request", {"provider": self.provider, "step": self._n})
        reply = self._net.post(request, payload)
        self._reply = reply
        reply.finished.connect(lambda: self._on_reply(reply))

    def _on_reply(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:
            return
        self._reply = None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll())
        err = reply.error()
        reply.deleteLater()
        if self._cancel or not self._running:
            return
        if err and err != QNetworkReply.NetworkError.OperationCanceledError and not raw:
            self._finish_turn(error=True, text=f"Request failed: {reply.errorString()} (HTTP {status})")
            return
        try:
            data = json.loads(raw)
        except ValueError:
            self._finish_turn(error=True, text=f"Unexpected reply from the model (HTTP {status}).")
            return
        if isinstance(data, dict) and data.get("error"):
            detail = data["error"]
            msg = detail.get("message", "API error") if isinstance(detail, dict) else str(detail)
            self._finish_turn(error=True, text=f"{msg} (HTTP {status})")
            return
        self._log("reply", data)
        try:
            text, tool_calls, assistant_blocks = (self._parse_anthropic(data) if self.provider == llm.ANTHROPIC
                                                  else self._parse_openai(data))
        except Exception as exc:  # noqa: BLE001
            self._finish_turn(error=True, text=f"Could not read the model's reply: {exc}")
            return
        if text:
            self.status_changed.emit("writing")
            self.text_started.emit()
            self.text_delta.emit(text)
            self.text_finished.emit()
        self._history.append({"role": "assistant", "content": assistant_blocks})
        if not tool_calls:
            self._finish_turn(error=False, text=text)
            return
        self._results = []
        self._run_tools(tool_calls, 0)

    @staticmethod
    def _parse_anthropic(data: dict):
        text_parts, tool_calls, blocks = [], [], []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
                blocks.append({"type": "text", "text": block.get("text", "")})
            elif block.get("type") == "tool_use":
                call = {"id": block.get("id") or uuid.uuid4().hex, "name": block.get("name", ""),
                        "input": block.get("input") or {}}
                tool_calls.append(call)
                blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"], "input": call["input"]})
        return "".join(text_parts).strip(), tool_calls, blocks

    @staticmethod
    def _parse_openai(data: dict):
        message = (data.get("choices") or [{}])[0].get("message") or {}
        text = (message.get("content") or "").strip()
        blocks = []
        if text:
            blocks.append({"type": "text", "text": text})
        tool_calls = []
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                args = {}
            cid = call.get("id") or uuid.uuid4().hex
            tool_calls.append({"id": cid, "name": fn.get("name", ""), "input": args})
            blocks.append({"type": "tool_use", "id": cid, "name": fn.get("name", ""), "input": args})
        return text, tool_calls, blocks

    # ----------------------------------------------------------------- tools
    def _run_tools(self, calls: list, i: int) -> None:
        if self._cancel or not self._running:
            return
        if i >= len(calls):
            self._history.append({"role": "user", "content": self._results})
            QTimer.singleShot(0, lambda: self._step(self._n + 1))
            return
        call = calls[i]
        name, inp, cid = call["name"], call["input"], call["id"]
        self.tool_used.emit({"type": "tool_use", "id": cid, "name": name, "input": inp}, False)
        self.status_changed.emit(f"using {name}")

        def finish(text: str, is_error: bool) -> None:
            block = {"type": "tool_result", "tool_use_id": cid,
                     "content": [{"type": "text", "text": text[:READ_CAP]}], "is_error": is_error}
            self.tool_result.emit(block, False)
            self._results.append(block)
            QTimer.singleShot(0, lambda: self._run_tools(calls, i + 1))

        if name == "Bash":
            self._run_bash(inp.get("command", ""), finish)
            return
        try:
            finish(self._run_sync_tool(name, inp), False)
        except ToolError as exc:
            finish(str(exc), True)
        except Exception as exc:  # noqa: BLE001
            finish(f"{exc.__class__.__name__}: {exc}", True)

    def _run_sync_tool(self, name: str, inp: dict) -> str:
        if name not in self._allowed:
            raise ToolError(f"The {name} tool is not available in this session.")
        if name == "Read":
            path = self._resolve(inp.get("file_path", ""), write=False)
            if not path.is_file():
                raise ToolError(f"No such file: {inp.get('file_path')}")
            return path.read_text(encoding="utf-8", errors="replace")[:READ_CAP]
        if name == "Write":
            path = self._resolve(inp.get("file_path", ""), write=True)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(inp.get("content", ""), encoding="utf-8")
            return f"Wrote {path}"
        if name == "Edit":
            path = self._resolve(inp.get("file_path", ""), write=True)
            if not path.is_file():
                raise ToolError(f"No such file: {inp.get('file_path')}")
            old, new = inp.get("old_string", ""), inp.get("new_string", "")
            text = path.read_text(encoding="utf-8", errors="replace")
            count = text.count(old)
            if count == 0:
                raise ToolError("old_string not found in the file.")
            if count > 1 and not inp.get("replace_all"):
                raise ToolError(f"old_string appears {count} times; pass replace_all or make it unique.")
            path.write_text(text.replace(old, new), encoding="utf-8")
            return f"Edited {path}"
        if name == "Glob":
            base = self._resolve(inp.get("path") or ".", write=False)
            hits = sorted(str(p) for p in base.glob(inp.get("pattern", "*")) if p.is_file())
            return "\n".join(hits[:GLOB_CAP]) or "(no matches)"
        if name == "LS":
            base = self._resolve(inp.get("path") or ".", write=False)
            entries = sorted(p.name + ("/" if p.is_dir() else "") for p in base.iterdir())
            return "\n".join(entries[:GLOB_CAP]) or "(empty)"
        if name == "Grep":
            return self._grep(inp)
        raise ToolError(f"Unknown tool: {name}")

    def _grep(self, inp: dict) -> str:
        base = self._resolve(inp.get("path") or ".", write=False)
        try:
            pattern = re.compile(inp.get("pattern", ""))
        except re.error as exc:
            raise ToolError(f"Bad pattern: {exc}")
        globpat = inp.get("glob") or "**/*"
        files = [base] if base.is_file() else sorted(base.glob(globpat))
        out = []
        for file in files:
            if not file.is_file():
                continue
            try:
                for num, line in enumerate(file.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                    if pattern.search(line):
                        out.append(f"{file}:{num}: {line.strip()[:200]}")
                        if len(out) >= GREP_CAP:
                            return "\n".join(out) + "\n(more matches omitted)"
            except OSError:
                continue
        return "\n".join(out) or "(no matches)"

    def _run_bash(self, command: str, finish) -> None:
        if "Bash" not in self._allowed or not self.can_run:
            self.permission_denied.emit({"tool_name": "Bash"})
            finish("Shell commands are denied in this session (enable bypassPermissions to allow them).", True)
            return
        proc = QProcess(self)
        self._bash = proc
        proc.setWorkingDirectory(str(self.cwd))
        if sys.platform == "win32":
            proc.setProgram("cmd.exe")
            proc.setArguments(["/c", command])
        else:
            proc.setProgram("/bin/sh")
            proc.setArguments(["-c", command])
        out = bytearray()
        proc.readyReadStandardOutput.connect(lambda: out.extend(bytes(proc.readAllStandardOutput())))
        proc.readyReadStandardError.connect(lambda: out.extend(bytes(proc.readAllStandardError())))

        def done(code, _status):
            if proc is self._bash:
                self._bash = None
            text = bytes(out).decode("utf-8", "replace")
            finish(text + (f"\n(exit {code})" if code else ""), bool(code))

        proc.finished.connect(done)
        proc.start()
        QTimer.singleShot(BASH_TIMEOUT_MS, lambda: proc.kill() if proc is self._bash else None)

    def _kill_bash(self) -> None:
        if self._bash is not None:
            bash, self._bash = self._bash, None
            bash.kill()

    def _resolve(self, raw: str, write: bool) -> Path:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.cwd / raw
        candidate = candidate.resolve()
        roots = [self.cwd] if write else [self.cwd, *self.add_dirs]
        if not any(_within(candidate, root) for root in roots):
            raise ToolError(f"Path is outside the workspace: {raw}")
        return candidate

    # ----------------------------------------------------------------- finishing
    def _finish_turn(self, *, error: bool, text: str, interrupted: bool = False) -> None:
        if not self._busy:
            return
        self._busy = False
        self._abort_reply()
        subtype = "error_interrupted" if interrupted else ("error" if error else "success")
        result = {"type": "result", "subtype": subtype, "is_error": error, "result": text,
                  "total_cost_usd": self.total_cost}
        self.status_changed.emit("idle")
        self.turn_finished.emit(result)
        QTimer.singleShot(0, self._pump)

    def _abort_reply(self) -> None:
        if self._reply is not None:
            reply, self._reply = self._reply, None
            try:
                reply.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            reply.abort()
            reply.deleteLater()

    def _log(self, kind: str, obj) -> None:
        if self.log_path is None:
            return
        try:
            with open(self.log_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": time.time(), "kind": kind, "data": obj})[:100_000] + "\n")
        except (OSError, TypeError, ValueError):
            pass
