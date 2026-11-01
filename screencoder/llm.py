"""One streaming chat client for three back ends: Claude Code (the CLI), the OpenAI API and the Anthropic API.

Nothing here reads the rest of the app. ChatClient (a one-shot streaming request) backs the Settings "Test"
button; the shared helpers (endpoints, keys, model menus) are also used by api_agent.py, which runs the coding and
quick-answer sessions on these APIs. All of it talks straight to the APIs over Qt's own networking
(QNetworkAccessManager), so no extra Python package is needed.

A message is a plain dict:
    {"role": "user"|"assistant", "text": str, "images": [(base64, media_type), ...], "files": [(name, text), ...]}
The caller passes the whole conversation each turn; the HTTP providers are stateless and the Claude Code one is
given the conversation as one prompt, so all three behave the same way.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

from PySide6.QtCore import QByteArray, QObject, QProcess, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

CLAUDE_CODE, OPENAI, ANTHROPIC = "claude-code", "openai", "anthropic"
PROVIDERS = [CLAUDE_CODE, OPENAI, ANTHROPIC]
PROVIDER_LABELS = {CLAUDE_CODE: "Claude Code (CLI)", OPENAI: "OpenAI API (key)", ANTHROPIC: "Anthropic API (key)"}

# Default model menus. All of them are editable in Settings, so an exact API id can always be typed in.
OPENAI_MODELS = ["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4o"]
ANTHROPIC_MODELS = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5", "claude-opus-5-5",
                    "claude-sonnet-5-5"]
CLAUDE_CODE_MODELS = ["claude-opus-4-8", "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"]
ANTHROPIC_VERSION = "2023-06-01"


def models_for(provider: str, settings=None) -> list[str]:
    """The model menu for a provider. OpenAI's list is taken from Settings so the user can keep it current."""
    if provider == OPENAI:
        extra = list(getattr(settings, "openai_models", []) or []) if settings is not None else []
        return extra or OPENAI_MODELS
    if provider == ANTHROPIC:
        return ANTHROPIC_MODELS
    return CLAUDE_CODE_MODELS


def default_model(provider: str, settings=None) -> str:
    return models_for(provider, settings)[0]


def _endpoint(provider: str) -> str:
    """The API base URL, overridable by an env var (for tests, proxies or compatible endpoints)."""
    if provider == OPENAI:
        return os.environ.get("SCREENCODER_OPENAI_BASE", "https://api.openai.com/v1").rstrip("/")
    return os.environ.get("SCREENCODER_ANTHROPIC_BASE", "https://api.anthropic.com").rstrip("/")


def key_for(settings, provider: str) -> str:
    if provider == OPENAI:
        return (getattr(settings, "openai_key", "") or "").strip()
    if provider == ANTHROPIC:
        return (getattr(settings, "anthropic_key", "") or "").strip()
    return ""


def ready(settings, provider: str, claude_path: str | None = None) -> tuple[bool, str]:
    """Can this provider be used right now? Returns (ok, why-not)."""
    if provider in (OPENAI, ANTHROPIC):
        if key_for(settings, provider):
            return True, ""
        name = "OpenAI" if provider == OPENAI else "Anthropic"
        return False, f"No {name} API key - add one in Settings."
    if claude_path:
        return True, ""
    return False, "Claude Code CLI not found - set its path in Settings."


def _parts(message: dict) -> tuple[str, list, list]:
    return message.get("text", "") or "", message.get("images", []) or [], message.get("files", []) or []


def _files_as_text(files: list) -> str:
    out = []
    for name, text in files:
        if text is not None:
            out.append(f"\n\n--- {name} ---\n{text}")
    return "".join(out)


class ChatClient(QObject):
    """Sends one request and streams the reply. One request at a time; call send() again for the next turn."""

    started = Signal()
    delta = Signal(str)       # a piece of the answer text
    finished = Signal()       # the answer is complete
    failed = Signal(str)      # it could not be produced (network, auth, no key...)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._net = QNetworkAccessManager(self)
        self._reply: QNetworkReply | None = None
        self._proc: QProcess | None = None
        self._buf = bytearray()
        self._event = ""        # Anthropic names each SSE line's event on its own line
        self._got_any = False
        self._done = False
        self._parse_line = None

    # ------------------------------------------------------------------ control
    @property
    def busy(self) -> bool:
        return self._reply is not None or self._proc is not None

    def send(self, messages: list[dict], *, provider: str, model: str, api_key: str = "",
             system: str = "", claude_path: str = "", cwd: str = "", max_tokens: int = 4096) -> None:
        self.cancel()
        self._buf, self._event, self._got_any, self._done = bytearray(), "", False, False
        self.started.emit()
        try:
            if provider == OPENAI:
                self._http_openai(messages, model, api_key, system, max_tokens)
            elif provider == ANTHROPIC:
                self._http_anthropic(messages, model, api_key, system, max_tokens)
            else:
                self._claude_code(messages, model, system, claude_path, cwd)
        except Exception as exc:  # noqa: BLE001 - any setup error becomes a clean failure
            self._fail(f"{exc.__class__.__name__}: {exc}")

    def cancel(self) -> None:
        if self._reply is not None:
            reply, self._reply = self._reply, None
            try:
                reply.readyRead.disconnect()
                reply.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            reply.abort()
            reply.deleteLater()
        if self._proc is not None:
            proc, self._proc = self._proc, None
            try:
                proc.readyReadStandardOutput.disconnect()
                proc.finished.disconnect()
            except (RuntimeError, TypeError):
                pass
            proc.kill()
            proc.deleteLater()

    # ------------------------------------------------------------------ finishing
    def _emit(self, text: str) -> None:
        if text:
            self._got_any = True
            self.delta.emit(text)

    def _complete(self) -> None:
        if self._done:
            return
        self._done = True
        self.finished.emit()

    def _fail(self, why: str) -> None:
        if self._done:
            return
        self._done = True
        self.failed.emit(why)

    # ------------------------------------------------------------------ OpenAI
    def _http_openai(self, messages, model, api_key, system, max_tokens) -> None:
        if not api_key:
            raise RuntimeError("No OpenAI API key.")
        body: dict = {"model": model, "stream": True, "messages": []}
        if system:
            body["messages"].append({"role": "system", "content": system})
        for message in messages:
            text, imgs, files = _parts(message)
            text += _files_as_text(files)
            if imgs:
                content = [{"type": "text", "text": text}]
                for b64, media in imgs:
                    content.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}})
                body["messages"].append({"role": message["role"], "content": content})
            else:
                body["messages"].append({"role": message["role"], "content": text})
        request = QNetworkRequest(QUrl(f"{_endpoint(OPENAI)}/chat/completions"))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setRawHeader(b"Authorization", f"Bearer {api_key}".encode())
        request.setRawHeader(b"Accept", b"text/event-stream")
        self._post(request, body, self._parse_openai)

    def _parse_openai(self, line: str) -> None:
        if not line.startswith("data:"):
            return
        data = line[5:].strip()
        if data == "[DONE]":
            self._complete()
            return
        try:
            event = json.loads(data)
        except ValueError:
            return
        for choice in event.get("choices", []):
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                self._emit(piece)

    # ------------------------------------------------------------------ Anthropic
    def _http_anthropic(self, messages, model, api_key, system, max_tokens) -> None:
        if not api_key:
            raise RuntimeError("No Anthropic API key.")
        body: dict = {"model": model, "stream": True, "max_tokens": max_tokens, "messages": []}
        if system:
            body["system"] = system
        for message in messages:
            text, imgs, files = _parts(message)
            text += _files_as_text(files)
            content = []
            for b64, media in imgs:
                content.append({"type": "image", "source": {"type": "base64", "media_type": media, "data": b64}})
            content.append({"type": "text", "text": text})
            body["messages"].append({"role": message["role"], "content": content})
        request = QNetworkRequest(QUrl(f"{_endpoint(ANTHROPIC)}/v1/messages"))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        request.setRawHeader(b"x-api-key", api_key.encode())
        request.setRawHeader(b"anthropic-version", ANTHROPIC_VERSION.encode())
        request.setRawHeader(b"Accept", b"text/event-stream")
        self._post(request, body, self._parse_anthropic)

    def _parse_anthropic(self, line: str) -> None:
        if line.startswith("event:"):
            self._event = line[6:].strip()
            return
        if not line.startswith("data:"):
            return
        try:
            event = json.loads(line[5:].strip())
        except ValueError:
            return
        kind = event.get("type", self._event)
        if kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") in (None, "text_delta"):
                self._emit(delta.get("text", ""))
        elif kind == "message_stop":
            self._complete()
        elif kind == "error":
            self._fail((event.get("error") or {}).get("message", "API error"))

    # ------------------------------------------------------------------ shared HTTP
    def _post(self, request: QNetworkRequest, body: dict, parse) -> None:
        payload = QByteArray(json.dumps(body).encode("utf-8"))
        reply = self._net.post(request, payload)
        self._reply = reply
        self._parse_line = parse
        reply.readyRead.connect(lambda: self._on_bytes(reply))
        reply.finished.connect(lambda: self._on_http_finished(reply))

    def _on_bytes(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:
            return
        self._buf += bytes(reply.readAll())
        while b"\n" in self._buf:
            raw, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if line:
                self._parse_line(line)

    def _on_http_finished(self, reply: QNetworkReply) -> None:
        if reply is not self._reply:
            return
        self._reply = None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        error = reply.error()
        tail = bytes(reply.readAll())
        if tail:  # whatever was left unparsed
            self._buf += tail
            for raw in bytes(self._buf).split(b"\n"):
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._parse_line(line)
        reply.deleteLater()
        if self._done:
            return
        if error and error != QNetworkReply.NetworkError.OperationCanceledError and not self._got_any:
            self._fail(self._http_error(status, reply))
        elif self._got_any:
            self._complete()
        else:
            self._fail(f"No reply from the model (HTTP {status}).")

    @staticmethod
    def _http_error(status, reply: QNetworkReply) -> str:
        if status == 401:
            return "The API key was not accepted (HTTP 401). Check it in Settings."
        if status == 404:
            return "That model was not found (HTTP 404). Check the model name in Settings."
        if status == 429:
            return "Rate limited or out of quota (HTTP 429)."
        return f"Request failed: {reply.errorString()}" + (f" (HTTP {status})" if status else "")

    # ------------------------------------------------------------------ Claude Code
    def _claude_code(self, messages, model, system, claude_path, cwd) -> None:
        path = claude_path or shutil.which("claude")
        if not path:
            raise RuntimeError("Claude Code CLI not found.")
        prompt = self._as_prompt(messages, system)
        proc = QProcess(self)
        self._proc = proc
        proc.setProgram(path)
        proc.setArguments(["-p", prompt, "--model", model, "--output-format", "text"])
        if cwd:
            proc.setWorkingDirectory(cwd)
        proc.readyReadStandardOutput.connect(
            lambda: self._emit(bytes(proc.readAllStandardOutput()).decode("utf-8", "replace")))
        proc.finished.connect(lambda code, _status: self._on_proc_finished(code))
        if sys.platform == "win32":
            proc.setCreateProcessArgumentsModifier(_no_window)
        proc.start()

    def _on_proc_finished(self, code: int) -> None:
        self._proc = None
        if self._got_any:
            self._complete()
        else:
            self._fail(f"Claude Code produced no reply (exit {code}).")

    @staticmethod
    def _as_prompt(messages: list[dict], system: str) -> str:
        """The conversation as one prompt, for the stateless CLI call. Images cannot travel this way."""
        lines = []
        if system:
            lines += [system, ""]
        if len(messages) <= 1:
            text, _imgs, files = _parts(messages[-1]) if messages else ("", [], [])
            lines.append(f"{text}{_files_as_text(files)}")
            return "\n".join(lines).strip()
        for message in messages[:-1]:
            text, _imgs, files = _parts(message)
            who = "User" if message["role"] == "user" else "Assistant"
            lines.append(f"{who}: {text}{_files_as_text(files)}")
        text, _imgs, files = _parts(messages[-1])
        lines.append(f"\nReply to the user's last message:\nUser: {text}{_files_as_text(files)}")
        return "\n".join(lines).strip()


def _no_window(args):  # keep the CLI from flashing a console window on Windows
    args.flags |= 0x08000000  # CREATE_NO_WINDOW
