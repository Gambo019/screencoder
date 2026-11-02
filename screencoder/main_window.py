"""Main window: task composer, plan / activity / files views, and the "ask while it codes" panel."""
from __future__ import annotations

import difflib
import enum
import hashlib
import html
import ast
import json
import os
import re
import shutil
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCursor, QDesktopServices, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QSizePolicy, QSplitter, QTabWidget, QTextBrowser,
    QToolButton, QVBoxLayout, QWidget,
)

from . import attachments as atts
from . import prompts
from .capture import ScreenCapturer
from .chat_view import ChatView, MessageBubble
from .claude_cli import ClaudeSession
from .api_agent import ApiAgentSession
from . import llm
from .code_view import CodeView
from .config import (
    APP_NAME, HOME_DIR, LANG_NON_CODING, LANGUAGES, Settings, can_run_commands, resolve_claude_cli, supports_effort,
)
from .copy_marks import Corpus, find_marks
from .file_tree import LEGEND_HTML, MODIFIED, NEW, ORIGINAL, FileTree
from .hotkeys import DEFAULT_HOTKEYS, GlobalHotkeys
from .listener import DEFAULT_SPEECH_MODEL, SPEECH_MODELS, SystemAudioListener
from .listener import supported as audio_supported
from .prompts_view import PromptsView
from .settings_dialog import SettingsDialog
from .speech_filter import is_filler
from .widgets import Composer, FlowLayout, LiveMarkdownView, scaled_font


class Phase(enum.Enum):
    IDLE = "Ready"
    ANALYZING = "Analyzing the inputs and planning"
    AWAITING = "Plan ready - waiting for your go-ahead"
    CODING = "Coding"
    # Language "Non-coding": the developer directs a coding agent outside this app, turn by turn.
    DIRECTING = "Working out the next step"
    WAITING = "Your move - the developer waits for the supervisor's answer or for what the coding agent did"
    DONE = "Finished"
    INTERRUPTED = "Interrupted - send the agent a message to continue"
    STOPPED = "Stopped"
    FAILED = "Failed"


ACTIVE = {Phase.ANALYZING, Phase.CODING, Phase.DIRECTING}
PHASE_COLORS = {
    Phase.ANALYZING: "#58a6ff", Phase.CODING: "#58a6ff", Phase.DIRECTING: "#58a6ff", Phase.AWAITING: "#e3b341",
    Phase.WAITING: "#e3b341", Phase.DONE: "#3fb950", Phase.FAILED: "#f85149", Phase.INTERRUPTED: "#e3b341",
}
EDIT_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".vscode", "dist", "build", "target",
    "bin", "obj", ".next", ".mypy_cache", ".pytest_cache", ".gradle",
}
# A coding task never touches the folder the user gives as Workspace: the developer gets a copy to work in.
CLONE_SKIP = ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache")  # caches are rebuilt, not copied
CLONE_ASK_FILES, CLONE_ASK_BYTES = 20_000, 400_000_000  # beyond this the user is asked before it is copied
DOING_CHARS, DOING_FILE_CHARS = 12_000, 3_500  # how much of his changes in progress the quick model is shown
SIDE_ACTIVITY_CHARS = 12_000
LEAD_LOG_CHARS = 40_000  # the same, when the log holds the developer's own turns (plans, prompts, code reviews)
WATCH_INTERVAL_MS = 3000  # how often the explorer looks for files written by an external coding agent
CHANGES_LISTED = 60  # at most this many changed workspace files are named to the developer per message
HEARD_CONTEXT_CHARS = 1_500  # earlier transcript sent along with a spoken question
TRANSCRIPT_LINES_SHOWN = 80
PARAGRAPH_SEP = chr(0x2029)  # QTextCursor.selectedText() uses this for line breaks
CUT_TIMEOUT_MS = 600  # how long Ask waits for the listener to close the sentence being spoken
MAX_SYNTAX_ROUNDS = 2  # how often unparseable code is automatically sent back to the agent
MIN_WINDOW_WIDTH = 806  # two thirds of the 1209 px the layout used to need at minimum
EXPLORER_CHARS = 20  # how long a file name fits in the explorer at its first width
EXPLORER_CHROME = 60  # px the file tree needs besides the name: indentation, icon, status column, frame
DEV_TITLE = "🤖 AI developer"  # quick answers and the coding session's replies speak as one developer
HEADING_LINE = re.compile(r"(?m)^#{1,6}\s")  # a chat reply ends where the agent's report/plan sections begin
NOTHING_TO_SEND = ("Nothing to send yet - no question in the new speech (filler like \"hmm\" or \"okay\" is skipped) "
                   "and no attachments.")
# Language "Non-coding": the developer directs a coding agent outside this app (see prompts.LEAD_RULES).
# After his first turn, what he says goes to the chat and what he writes for the agent to the Prompts tab.
# The two buttons that tell him where the external agent stands; before his first prompt there is no agent
# at work yet, and the second one lets him go ahead without (more) answers instead.
DONE_LABEL, WORKING_LABEL, GO_AHEAD_LABEL = "✅ Agent finished", "⏳ Agent still working", "▶ Go ahead"
WORKING_TIP = ("The agent is still working on the developer's latest prompt. He gets the screenshots and text added "
               "here and looks at the workspace, so that he knows how far it is. He speaks in the panel on the left "
               "only if that already gives him something worth saying; otherwise he just takes it in.")
GO_AHEAD_TIP = ("No (further) answers are coming: the developer goes ahead on his own assumptions - anything typed "
                "here is given to him as a note - then states his plan and writes the first prompt.")
# Text that is already complete (what he says once a prompt has been sent) is shown flowing in like a stream:
# the first few words at once, then a word about every REVEAL_MS. A bold label or a `code` span comes in one
# piece, so that no stray ** or ` flickers on the screen.
REVEAL_PIECE = re.compile(r"(?:\*\*[^*\n]+\*\*|`[^`\n]+`|\S+)\s*")
REVEAL_FIRST, REVEAL_MS = 4, 28
HOLD_SENTENCES = 2  # the instant first reaction before a hand-over is two short sentences: more is cut off
HOLD_LINE_CHARS = 320
LEAD_NO_REPLY = "*(nothing more to say to that)*"
LEAD_NO_WORDS = "*(nothing to say out loud this time - see the Prompts tab)*"
FENCE_LINE = re.compile(r"^[ \t]*(?P<fence>~{3,}|`{3,})[ \t]*(?P<info>[\w+-]*)[ \t]*$")
TURN_HEADING = re.compile(r"^#{1,6}[ \t]+(?P<title>.+?)[ \t#]*$")
# Headings of his own turn format: one of these inside a prompt means he forgot to close the prompt block.
SPOKEN_SECTIONS = ("while it works", prompts.RUN_SECTION.lower(), "reading the code", "what i caught")
# In the chat a heading is only a small label for the part that follows; "Prompt" there is what he says as he
# sends it, since the prompt itself is on the Prompts tab.
SPEECH_LABELS = {"prompt": "As I prompt the agent"}
# These parts of a turn are said once its prompt is with the agent - after the user has clicked "sent" on it.
AFTER_SEND_SECTIONS = ("prompt", "while it works")

INTRO_PLAN = """\
### How it works
1. **Add the task**: type a description, paste screenshots with **Ctrl+V**, use **Snip / Screen**, or attach files. \
Capture as many screenshots as you need to cover the code - overlaps and repeats are fine.
2. Press **Analyze & Solve**. The coding agent first explains its **understanding and plan** here, then writes the code \
(see *Coding activity* and *Files*).
3. While it works, use the panel on the left. Speech playing on this PC is transcribed live at the top - that is \
the question; add screenshots or files to go with it. **Ask** gets an instant answer from the fast model without \
disturbing the agent. **Send to agent** gives the same thing to the coding agent as a new instruction.

**Language: Vibe-Coding** - the developer writes no code. He asks his questions here, then directs your own \
coding agent: what he says is in the panel on the left, and the prompts he writes are on the **Prompts** tab, \
with the parts you can copy from the agent's files marked. Put the agent's folder in **Workspace** so he can \
read its files; you pass the prompts on and run things for him.

Global hotkeys (work while your IDE has focus): **Ctrl+Alt+Shift+S** snip a region, **Ctrl+Alt+Shift+F** whole screen."""


@dataclass
class Run:
    id: str
    dir: Path
    inputs: Path
    workspace: Path
    task: str
    inputs_md: str
    language: str = "Python"
    plan: str = ""
    activity: list[str] = field(default_factory=list)  # plain log handed to the quick-answer model
    touched: list[str] = field(default_factory=list)  # files the agent wrote or edited
    side_seen: int = 0
    side_plan_sent: bool = False
    messages: int = 0
    # Which files belong to the problem as presented (keys from file_key()), and what they looked like:
    existing: set[str] = field(default_factory=set)
    baseline: dict[str, str] = field(default_factory=dict)  # content hash of files already on disk at the start
    first_write: dict[str, str] = field(default_factory=dict)  # content hash of the agent's first Write per file
    edited: set[str] = field(default_factory=set)  # keys of files the agent wrote to or edited
    # The original text itself, to show which lines of a file the agent changed:
    baseline_text: dict[str, str] = field(default_factory=dict)  # files on disk at the start (small text files)
    first_write_text: dict[str, str] = field(default_factory=dict)  # what the agent first wrote per file
    # Language "Non-coding": the developer directs a coding agent outside this app and only reads its workspace.
    directing: bool = False
    shared: bool = False  # the user gave the external agent's workspace folder
    prompts: list[str] = field(default_factory=list)  # prompts written for the external agent, oldest first
    prompt_open: bool = False  # his last turn ended with a prompt, and nothing has come back for it yet
    run_open: bool = False  # the same for something he listed to run or edit by hand
    runs: int = 0  # how many such things he has listed so far
    first_done: bool = False  # his first turn (understanding and questions) is finished
    held: dict[int, str] = field(default_factory=dict)  # prompt number -> what he says once it has been sent
    sent: set[int] = field(default_factory=set)  # prompts the user has given to the external agent
    # A coding task started with a folder given by the user: that folder is never touched. `workspace` is then
    # the developer's own copy of it, and `given` the original - the only thing the person asking can see.
    given: Path | None = None
    turns: int = 0  # messages handed to the developer after the task itself
    seen: dict[str, str] = field(default_factory=dict)  # workspace file -> content hash when he last looked
    answered: list[tuple[str, str]] = field(default_factory=list)  # quick answers he has not been told about yet
    stat_cache: dict[str, tuple[int, int, str]] = field(default_factory=dict)  # file -> (mtime, size, hash)

    @property
    def manifest(self) -> Path:
        """Where the agent lists the files that already exist in the problem (see implement_prompt)."""
        return self.inputs / "existing_files.txt"

    def original_text(self, key: str) -> str | None:
        """The file as it was in the problem, if known."""
        return self.baseline_text.get(key, self.first_write_text.get(key))


REPLY_MARKS = (prompts.SKIP_MARK, prompts.AGENT_MARK)


def reply_mark(text: str) -> str | None:
    """The control marker a reply consists of ("nothing to answer", "pass to the coding session"), if any."""
    start = text.strip().strip("`*_ ").lower()
    return next((mark for mark in REPLY_MARKS if start.startswith(mark)), None)


def is_skip(text: str) -> bool:
    return reply_mark(text) == prompts.SKIP_MARK


def held_line(text: str) -> tuple[str, bool]:
    """What the quick model says right away while it passes a message on: the words after its "[agent]".

    Returns (those words, whether they are finished). They are meant to be two short sentences and nothing
    more: as soon as a new paragraph or a third sentence follows, or it gets too long, the rest is not wanted -
    the plan, the answer and the prompts are the working session's job.
    """
    at = text.lower().find(prompts.AGENT_MARK)
    if at < 0:
        return "", False
    rest = text[at + len(prompts.AGENT_MARK):].replace("\r", "").lstrip(" \t\n`*_-–—:")
    paragraph, new_paragraph, _more = rest.partition("\n\n")
    finished = bool(new_paragraph) and bool(paragraph.strip())
    line = " ".join(paragraph.split())  # a single line break inside it is just a space
    ends = [match.end() for match in re.finditer(r"[.!?…](?=\s+\S)", line)]  # sentence ends with more after them
    if len(ends) >= HOLD_SENTENCES:
        line, finished = line[:ends[HOLD_SENTENCES - 1]], True
    elif len(line) > HOLD_LINE_CHARS:
        line, finished = line[:HOLD_LINE_CHARS].rsplit(" ", 1)[0] + "…", True
    return line.strip(" \t`*_"), finished


def may_become_skip(text: str) -> bool:
    """While a reply is streaming in: could it still turn out to be only a control marker?"""
    start = text.strip().strip("`*_ ").lower()
    return any(mark.startswith(start) or start.startswith(mark) for mark in REPLY_MARKS)


@dataclass
class Turn:
    """A turn of the developer who directs an external agent, taken apart by where each part belongs."""
    speech: str = ""  # what he says out loud, as Markdown for the chat
    after_send: str = ""  # what he says once the prompt is with the agent: why he sent it, and while it works
    prompts: list[str] = field(default_factory=list)  # finished prompts for the agent
    writing: str | None = None  # a prompt he is still writing (the text ends inside its block)
    runs: list[str] = field(default_factory=list)  # commands and manual edits listed under "Running next"


def _spoken_markdown(spoken: list[tuple[bool, str, str]]) -> str:
    """Lines of a turn as chat Markdown: a section label is a small bold line - or, with nothing said under
    it (only a prompt was there), left out."""
    lines: list[str] = []
    for index, (is_label, line, _section) in enumerate(spoken):
        if not is_label:
            lines.append(line)
            continue
        rest = spoken[index + 1:]
        until = next((i for i, entry in enumerate(rest) if entry[0]), len(rest))
        if any(entry[1].strip() for entry in rest[:until]):
            lines += ["", f"**{line}**", ""]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_turn(text: str, final: bool) -> Turn:
    """Split a developer turn into his words, his prompts for the agent and what he wants run.

    His words come in two parts. What goes with a prompt - why he sends it, and what to say while the agent
    works - is only due once that prompt is with the agent, so it is kept apart as `after_send`.
    `final` is False while the turn is still streaming in: a prompt block that is not closed yet is then
    reported as `writing` rather than as a finished prompt.
    """
    turn = Turn()
    spoken: list[tuple[bool, str, str]] = []  # (is a section label, line, the section it is in)
    fence = kind = None  # inside a fenced block: its fence, and whether it is a "prompt", a "run" or plain "code"
    block: list[str] = []
    section = ""

    def close() -> None:
        nonlocal fence, kind, block
        body = "\n".join(block).strip()
        if body and kind in ("prompt", "run"):
            (turn.prompts if kind == "prompt" else turn.runs).append(body)
        fence, kind, block = None, None, []

    for line in text.split("\n"):
        fenced, heading = FENCE_LINE.match(line), TURN_HEADING.match(line)
        if fence is not None:
            closes = (fenced is not None and not fenced["info"] and fenced["fence"][0] == fence[0]
                      and len(fenced["fence"]) >= len(fence))
            # A heading of his own turn format inside a prompt: he forgot the closing fence.
            forgot = kind == "prompt" and heading is not None and heading["title"].lower() in SPOKEN_SECTIONS
            if not (closes or forgot):
                if kind == "code":
                    spoken.append((False, line, section))
                else:
                    block.append(line)
                continue
            if kind == "code":
                spoken.append((False, line, section))
            close()
            if closes:
                continue
        if heading is not None:
            section = heading["title"].lower()
            spoken.append((True, SPEECH_LABELS.get(section, heading["title"]), section))
        elif fenced is not None:
            fence = fenced["fence"]
            kind = ("prompt" if fenced["info"].lower() == prompts.PROMPT_TAG
                    else "run" if section == prompts.RUN_SECTION.lower() else "code")
            if kind == "code":
                spoken.append((False, line, section))
        else:
            spoken.append((False, line, section))
    if fence is not None:  # the text ends inside a block
        if kind == "prompt" and not final:
            turn.writing = "\n".join(block)
        elif final:
            close()
    # What belongs to the prompt is held back for when the prompt has been sent. A finished turn that turned
    # out to hold no prompt has nothing to wait for: those words then simply stay with the rest.
    waits = not final or bool(turn.prompts)
    turn.speech = _spoken_markdown([entry for entry in spoken if not (waits and entry[2] in AFTER_SEND_SECTIONS)])
    turn.after_send = _spoken_markdown([entry for entry in spoken if waits and entry[2] in AFTER_SEND_SECTIONS])
    return turn


def syntax_problems(root: Path, files: list[str]) -> tuple[list[str], int]:
    """Parse the given workspace files where we can (Python, JSON). Returns (problems, files checked)."""
    problems, checked = [], 0
    for rel in files:
        path = root / rel
        suffix = path.suffix.lower()
        if suffix not in (".py", ".json") or not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        checked += 1
        try:
            source = raw.decode("utf-8")
            if suffix == ".py":
                ast.parse(source, filename=rel)
            else:
                json.loads(source)
        except SyntaxError as exc:
            problems.append(f"`{rel}` line {exc.lineno}: {exc.msg}" + (f" -> `{exc.text.strip()}`" if exc.text else ""))
        except ValueError as exc:  # invalid JSON, or a file that is not valid UTF-8
            problems.append(f"`{rel}`: {exc}")
    return problems, checked


def unchanged_lines(original: str, current: str) -> set[int]:
    """0-based numbers of the lines in `current` that are identical to (and aligned with) `original`."""
    old, new = (t.replace("\r\n", "\n").replace("\r", "\n").split("\n") for t in (original, current))
    same: set[int] = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag == "equal":
            same.update(range(j1, j2))
    return same


def folder_weight(root: Path) -> tuple[int, int]:
    """Files and bytes a copy of `root` would take (caches left out). Stops counting once it is clearly large."""
    files = size = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in CLONE_SKIP]
        for name in filenames:
            files += 1
            try:
                size += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
        if files > CLONE_ASK_FILES or size > CLONE_ASK_BYTES:
            break
    return files, size


def content_hash(data: bytes | str) -> str:
    """Hash that ignores line-ending differences (the Write tool may store CRLF or LF)."""
    if isinstance(data, str):
        data = data.encode("utf-8", errors="replace")
    return hashlib.sha1(data.replace(b"\r\n", b"\n")).hexdigest()


def file_key(path: str | Path, root: Path) -> str:
    """Canonical key for a workspace file: relative to `root`, OS-normalised, case-folded on Windows."""
    path = Path(str(path).strip())
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(root)
        except (ValueError, OSError):
            pass
    return os.path.normcase(os.path.normpath(str(path)))


# ----------------------------------------------------------------------------- formatting helpers
def shorten(text: str, n: int) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def code_span(text: str) -> str:
    text = " ".join(str(text).split())
    ticks = "``" if "`" in text else "`"
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def quote(text: str) -> str:
    return "\n".join("> " + line for line in text.splitlines())


def fmt_secs(secs: float) -> str:
    secs = int(secs)
    return f"{secs // 60}m {secs % 60:02d}s" if secs >= 60 else f"{secs}s"


def rel_path(path: str, root: Path) -> str:
    try:
        return str(Path(path).resolve().relative_to(root))
    except (ValueError, OSError):
        return str(path)


def tool_summary(name: str, inp: dict, root: Path) -> tuple[str, bool]:
    """One-line Markdown for a tool call; returns (markdown, needs_spacing)."""
    path = inp.get("file_path") or inp.get("notebook_path") or inp.get("path") or ""
    shown = code_span(rel_path(path, root)) if path else ""
    if name == "Read":
        return f"📖 Read {shown}", False
    if name == "Write":
        lines = str(inp.get("content", "")).count("\n") + 1
        return f"📝 Write {shown} ({lines} lines)", False
    if name in ("Edit", "MultiEdit", "NotebookEdit"):
        return f"✏️ Edit {shown}", False
    if name in SHELL_TOOLS:
        desc = inp.get("description")
        return f"▶ {code_span(shorten(str(inp.get('command', '')), 200))}" + (f" - {desc}" if desc else ""), False
    if name in ("Glob", "Grep"):
        return f"🔎 {name} {code_span(inp.get('pattern', ''))}" + (f" in {shown}" if shown else ""), False
    if name == "TodoWrite":
        marks = {"completed": "☑", "in_progress": "◐"}
        items = [f"- {marks.get(t.get('status'), '☐')} {t.get('content', '')}" for t in inp.get("todos", [])]
        return "📋 **To-do**\n\n" + "\n".join(items), True
    if name in ("Task", "Agent"):
        return f"🤖 Subagent: {inp.get('description') or shorten(str(inp.get('prompt', '')), 120)}", False
    if name == "WebSearch":
        return f"🌐 Search {code_span(inp.get('query', ''))}", False
    if name == "WebFetch":
        return f"🌐 Fetch {inp.get('url', '')}", False
    detail = next((str(v) for v in inp.values() if isinstance(v, str) and v.strip()), "")
    return f"🔧 {name}" + (f" {code_span(shorten(detail, 120))}" if detail else ""), False


def result_text(block: dict) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
    return ""


# ----------------------------------------------------------------------------- main window
class MainWindow(QMainWindow):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.settings = Settings.load()
        self.run: Run | None = None
        self.main: ClaudeSession | None = None
        self.side: ClaudeSession | None = None
        self.phase = Phase.IDLE
        self._main_status = ""
        self._live_view: LiveMarkdownView | None = None
        self._turn_text = ""  # text streamed during the current analysis turn (= the plan)
        self._tool_names: dict[str, str] = {}
        self._interrupt_requested = False
        self._syntax_rounds = 0  # automatic "your code does not parse" round trips in the current task
        self._side_first = True
        self._side_cost_done = 0.0
        self._questions = 0
        self._run_clock = QElapsedTimer()
        self._run_secs = 0.0
        self.listener: SystemAudioListener | None = None
        self._tx: list[list] = []  # transcript lines: [text, already_asked]
        self._tx_partial = ""
        self._pending_action = None  # Ask/Send waiting for the listener to close the sentence being spoken
        self._fillers_skipped = 0
        self._audio_status_text = ""
        self._answers: deque[MessageBubble] = deque()  # answer bubbles waiting for / receiving their reply
        # Replies of the coding session to "Send to agent" messages, shown in the chat:
        self._agent_replies: dict[str, MessageBubble] = {}  # message token -> bubble, until the agent picks it up
        self._replies_ready: deque[MessageBubble] = deque()  # picked up; the agent's next text is the reply
        self._reply_live: MessageBubble | None = None  # bubble mirroring the text block being streamed
        self._reply_text = ""
        # Language "Non-coding", after the developer's first turn: what he says is shown in the chat.
        self._lead_live: MessageBubble | None = None  # the bubble his current turn is spoken into
        self._speaking = False  # the text block being streamed belongs to that bubble
        self._block_start = 0  # where that block starts in _turn_text
        self._turn_prompts = 0  # prompts of the current turn that are already on the Prompts tab
        self._lead_shown = False  # his first turn has reached its first heading and is being shown
        self._intro = ""  # what he wrote in that turn before the heading (not shown)
        # Prompts marked as sent while his turn was still being written: prompt number -> the bubble that
        # receives what he says after sending, as it comes in.
        self._after_bubbles: dict[int, MessageBubble] = {}
        self._shown_file: tuple[str, str, str] | None = None  # (relative path, content hash, status) on screen

        self.capturer = ScreenCapturer(self)
        self.capturer.captured.connect(self._on_captured)
        self.hotkeys = GlobalHotkeys(app, self)
        self.hotkeys.triggered.connect(lambda mode: self.capture(mode))

        self._build_ui()
        self._apply_settings()
        self._set_phase(Phase.IDLE)
        self._tick_timer = QTimer(self)
        self._tick_timer.timeout.connect(self._tick)
        self._tick_timer.start(1000)
        self._files_timer = QTimer(self)
        self._files_timer.setSingleShot(True)
        self._files_timer.setInterval(700)
        self._files_timer.timeout.connect(self.refresh_files)
        # While an external coding agent writes into the workspace nothing tells us, so the explorer polls.
        self._watched_state: dict = {}
        self._watch_timer = QTimer(self)
        self._watch_timer.setInterval(WATCH_INTERVAL_MS)
        self._watch_timer.timeout.connect(self._watch_workspace)
        self._lead_timer = QTimer(self)  # re-renders the developer's streaming turn (chat + Prompts tab)
        self._lead_timer.setSingleShot(True)
        self._lead_timer.setInterval(90)
        self._lead_timer.timeout.connect(self._render_lead)
        self._tx_timer = QTimer(self)
        self._tx_timer.setSingleShot(True)
        self._tx_timer.setInterval(80)
        self._tx_timer.timeout.connect(self._render_transcript)
        self._render_transcript()
        if self.settings.listen_audio and audio_supported():
            QTimer.singleShot(300, lambda: self.listen_btn.setChecked(True))
        # Start the quick-answer CLI in the background so the first Ask doesn't pay its startup time.
        QTimer.singleShot(1500, lambda: self.side is None and resolve_claude_cli(self.settings) and self._ensure_side())

    # ------------------------------------------------------------------ UI construction
    def _action(self, text: str, slot, tip: str = "") -> QAction:
        action = QAction(text, self)
        action.setToolTip(tip or text)
        action.triggered.connect(slot)
        return action

    def _build_ui(self) -> None:
        self.setWindowTitle(f"{APP_NAME} - screenshots in, code out")
        self.resize(1500, 940)

        # Toolbar: new task + workspace + models + settings
        tb = self.addToolBar("Main")
        tb.setMovable(False)
        # Which of these (and of all the other buttons) can be used at a given moment: see _update_buttons.
        self.new_task_action = self._action(
            "＋ New task", self._new_task,
            "Start over for another problem: after you confirm, the current task is stopped and "
            "the plan, prompts, activity, files, Q&A panel and workspace path are cleared")
        tb.addAction(self.new_task_action)
        tb.addSeparator()
        tb.addWidget(QLabel(" Workspace: "))
        self.ws_edit = QLineEdit(self.settings.workspace)
        self.ws_edit.setPlaceholderText("(a new empty folder for each task)")
        self.ws_edit.setMinimumWidth(340)
        self.ws_edit.setToolTip("Leave empty for a fresh folder per task, or pick your project folder if you have the "
                                "code locally.\nYour folder is never changed: the developer works on a copy of it "
                                "(edits and runs happen only there), you bring his changes over yourself, and quick "
                                "answers are given from your folder as it is at that moment.\n"
                                "With Language \"Vibe-Coding\": the folder your own coding agent works in - "
                                "the developer only reads it.")
        tb.addWidget(self.ws_edit)
        self.browse_action = self._action("Browse…", self._browse_workspace, "Choose the workspace folder")
        self.open_action = self._action("Open folder", self._open_workspace,
                                        "Open the folder the developer works in (his copy, if you gave a workspace) "
                                        "in Explorer")
        tb.addAction(self.browse_action)
        tb.addAction(self.open_action)
        self.ws_edit.textChanged.connect(self._update_buttons)
        tb.addSeparator()
        self.models_label = QLabel()
        self.models_label.setStyleSheet("color:#9aa4b2; padding:0 8px;")
        tb.addWidget(self.models_label)
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        tb.addWidget(spacer)
        tb.addAction(self._action("⚙ Settings", self._open_settings))

        # Left: task composer + result tabs
        self.task = Composer(
            "Task",
            "Describe the task (optional if the screenshots show it), e.g. \"Fix the failing test\" or "
            "\"Solve this in Python\".\nCtrl+V pastes screenshots or files  ·  Ctrl+Enter starts",
            80,
        )
        self.task.submitted.connect(self.start_task)
        self.task.capture_requested.connect(lambda mode: self.capture(mode, self.task))
        self.task.notice.connect(self._notice)
        self.task.changed.connect(self._update_buttons)
        self.review_check = QCheckBox("Review the plan first")
        self.review_check.setToolTip("Pause after the plan so you can approve it, add feedback, or ask for a "
                                     "revised plan before coding starts")
        self.review_check.setChecked(self.settings.review_plan)
        self.lang_combo = QComboBox()
        self.lang_combo.addItems(LANGUAGES)
        self.lang_combo.setCurrentText(self.settings.language if self.settings.language in LANGUAGES else "Python")
        self.lang_combo.setToolTip("Language for the solution. \"Other\" follows the language of the problem.\n"
                                   "\"Vibe-Coding\": the developer writes no code - he plans, writes the prompts "
                                   "for your own coding agent and reviews what it produces.")
        self.start_btn = QPushButton("▶  Analyze && Solve")
        self.start_btn.setObjectName("primary")
        self.start_btn.setToolTip("Ctrl+Enter")
        self.start_btn.clicked.connect(self.start_task)
        row = FlowLayout(spacing=8)  # wraps when the code side is narrow
        row.addWidget(QLabel("Language:"))
        row.addWidget(self.lang_combo)
        row.addWidget(self.review_check)
        row.addWidget(self.start_btn)
        self.mode_hint = QLabel("Vibe-Coding: the developer writes no code. He asks his questions, plans, writes the "
                                "prompts for your own coding agent and reviews what it produces. Put that agent's "
                                "folder in Workspace (toolbar) so he can read its files.")
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setStyleSheet("color:#e3b341; font-size:11px;")
        task_box = QGroupBox("Task - text, screenshots and files")
        self.task_box = task_box  # hidden once a task starts; "＋ New task" brings it back
        box_layout = QVBoxLayout(task_box)
        box_layout.addWidget(self.task, 1)
        box_layout.addLayout(row)
        box_layout.addWidget(self.mode_hint)
        self.lang_combo.currentTextChanged.connect(self._language_changed)
        self._language_changed(self.lang_combo.currentText())

        self.plan_view = LiveMarkdownView()
        self.plan_view.append_markdown(INTRO_PLAN, spaced=True)
        self.feedback_edit = QLineEdit()
        self.feedback_edit.setPlaceholderText("Optional feedback, e.g. \"keep the original function name\", \"use Java 8\"…")
        self.feedback_edit.returnPressed.connect(lambda: self._approve(self.feedback_edit.text()))
        go = QPushButton("▶ Start coding")
        go.setObjectName("primary")
        go.clicked.connect(lambda: self._approve(self.feedback_edit.text()))
        revise = QPushButton("↻ Revise plan")
        revise.setToolTip("Ask for a new plan that takes your feedback into account (type the feedback first)")
        revise.clicked.connect(self._revise)
        self.revise_btn = revise
        self.feedback_edit.textChanged.connect(self._update_buttons)
        self.approval_bar = QFrame()
        self.approval_bar.setObjectName("approval")
        bar = QHBoxLayout(self.approval_bar)
        bar.addWidget(QLabel("Plan ready."))
        bar.addWidget(self.feedback_edit, 1)
        bar.addWidget(revise)
        bar.addWidget(go)
        # Language "Non-coding": the user carries things between the developer and an external coding agent.
        # Here they show him how that agent is doing: screenshots of its latest activity (its chat, its action
        # history, a terminal) and, if they like, a line of text ("pytest: 3 passed"). Two buttons say where the
        # agent stands - finished with his prompt, or still working on it. He takes it all as plain fact: the
        # user never appears in what he says or writes.
        self.status = Composer(
            "Status",
            "Screenshots of the agent's latest activity (Snip, Screen or Ctrl+V) and/or a line of status, e.g. "
            "\"pytest: 3 passed\" - then say below where the agent stands",
            34,
        )
        self.status.edit.setMaximumHeight(64)
        self.status.capture_requested.connect(lambda mode: self.capture(mode, self.status))
        self.status.notice.connect(self._notice)
        self.status.changed.connect(self._update_buttons)
        self.done_btn = QPushButton(DONE_LABEL)
        self.done_btn.setObjectName("primary")
        self.done_btn.setToolTip("The agent has completed its turn for the developer's latest prompt. He gets the "
                                 "screenshots and text added here, reads what the agent wrote in the workspace, and "
                                 "reviews it in the panel on the left. Nothing needs to be added: the click is enough.")
        self.done_btn.clicked.connect(lambda: self._send_status("done"))
        self.working_btn = QPushButton(WORKING_LABEL)  # before his first prompt it reads "Go ahead": _update_buttons
        self.working_btn.clicked.connect(lambda: self._send_status("working"))
        self.status.buttons.addWidget(self.done_btn)
        self.status.buttons.addWidget(self.working_btn)
        self.lead_bar = QFrame()  # sits under the tabs, so it is there whichever tab is open
        lead = QVBoxLayout(self.lead_bar)
        lead.setContentsMargins(0, 0, 0, 0)
        lead.addWidget(self.status)
        plan_tab = QWidget()
        self.plan_tab = plan_tab
        plan_layout = QVBoxLayout(plan_tab)
        plan_layout.setContentsMargins(0, 0, 0, 0)
        plan_layout.addWidget(self.plan_view, 1)
        plan_layout.addWidget(self.approval_bar)
        # Language "Non-coding": the prompts the developer writes for the external agent - to read and type off.
        self.prompts_view = PromptsView()
        self.prompts_view.sent.connect(self._prompt_sent)

        self.activity_view = LiveMarkdownView()
        # Files tab: explorer tree on the left (names coloured by status), code viewer on the right
        self.explorer_title = QLabel("EXPLORER")
        self.explorer_title.setStyleSheet("color:#bbbbbb; font-size:11px; font-weight:600; padding:4px 2px;")
        self.file_tree = FileTree()
        self.file_tree.file_selected.connect(self._show_file)
        legend = QLabel(LEGEND_HTML)
        legend.setStyleSheet("font-size:11px; padding:2px;")
        legend.setWordWrap(True)
        explorer = QWidget()
        explorer_layout = QVBoxLayout(explorer)
        explorer_layout.setContentsMargins(0, 0, 0, 0)
        explorer_layout.setSpacing(2)
        explorer_layout.addWidget(self.explorer_title)
        explorer_layout.addWidget(self.file_tree, 1)
        explorer_layout.addWidget(legend)
        self.file_label = QLabel()  # breadcrumb of the open file; the full path is in its tooltip
        self.file_label.setStyleSheet("color:#9aa4b2; padding:4px 2px;")
        self.file_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        # Like an IDE's side bar: the explorer can be folded away to give the code the full width.
        self.explorer_toggle = QToolButton()
        self.explorer_toggle.setCheckable(True)
        self.explorer_toggle.setChecked(True)
        self.explorer_toggle.setText("◀ Explorer")
        self.explorer_toggle.setToolTip("Hide or show the file explorer")
        self.explorer_toggle.toggled.connect(self._toggle_explorer)
        self.explorer_pane = explorer
        crumb_row = QHBoxLayout()
        crumb_row.setContentsMargins(0, 0, 0, 0)
        crumb_row.addWidget(self.explorer_toggle)
        crumb_row.addWidget(self.file_label, 1)
        self.code_view = CodeView()
        file_buttons = QHBoxLayout()
        self.refresh_btn = QPushButton("↻ Refresh")
        self.refresh_btn.setToolTip("Re-read the workspace of the current task")
        self.refresh_btn.clicked.connect(self.refresh_files)
        self.copy_code_btn = QPushButton("📋 Copy code")
        self.copy_code_btn.setToolTip("Copy the file that is open here")
        self.copy_code_btn.clicked.connect(self._copy_file)
        file_buttons.addWidget(self.refresh_btn)
        file_buttons.addWidget(self.copy_code_btn)
        file_buttons.addStretch(1)
        lines_legend = QLabel("<span style='color:#d4d4d4'>bright</span> = added or edited by the agent"
                              " &nbsp;·&nbsp; <span style='color:#6b6b6b'>dim</span> = original, unchanged")
        lines_legend.setStyleSheet("color:#8b949e; font-size:11px;")
        lines_legend.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # may be clipped
        lines_legend.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        file_buttons.addWidget(lines_legend, 1)
        viewer = QWidget()
        viewer_layout = QVBoxLayout(viewer)
        viewer_layout.setContentsMargins(0, 0, 0, 0)
        viewer_layout.setSpacing(2)
        viewer_layout.addLayout(crumb_row)
        viewer_layout.addWidget(self.code_view, 1)
        viewer_layout.addLayout(file_buttons)
        explorer.setMinimumWidth(120)
        files_tab = QSplitter(Qt.Orientation.Horizontal)
        files_tab.addWidget(explorer)
        files_tab.addWidget(viewer)
        files_tab.setStretchFactor(0, 0)  # extra width goes to the code, like an IDE
        files_tab.setStretchFactor(1, 1)
        files_tab.setSizes([self.file_tree.fontMetrics().averageCharWidth() * EXPLORER_CHARS + EXPLORER_CHROME, 840])

        self.tabs = QTabWidget()  # names, and whether the Prompts tab is there at all: see _apply_mode
        self.tabs.addTab(plan_tab, "Understanding && plan")
        self.tabs.addTab(self.prompts_view, "Prompts")
        self.tabs.addTab(self.activity_view, "Coding activity")
        self.tabs.addTab(files_tab, "Output Code")
        # At the end of the tab row, and on the remote page beside the last tab: when on, the open tab stays in step
        # with everyone viewing from the web (and their tab clicks move it here); when off, each side browses the
        # tabs on its own. Either side can switch it, and it is one box: the two always show the same.
        self.tab_sync = QCheckBox("Sync tab")
        self.tab_sync.setChecked(True)
        self.tab_sync.setToolTip("Keep the open tab in sync with the web viewers. Turn off to browse tabs separately.")
        self.tabs.setCornerWidget(self.tab_sync, Qt.Corner.TopRightCorner)
        code_side = QWidget()
        code_layout = QVBoxLayout(code_side)
        code_layout.setContentsMargins(0, 0, 0, 0)
        code_layout.setSpacing(4)
        code_layout.addWidget(self.tabs, 1)
        code_layout.addWidget(self.lead_bar)

        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(task_box)
        left.addWidget(code_side)
        left.setSizes([300, 640])

        # Right: quick questions / steering
        self.side_label = QLabel()
        self.side_label.setStyleSheet("color:#9aa4b2;")
        self.side_label.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # never forces width
        self.chat = ChatView()  # answers as chat bubbles, newest at the top
        # No typed question: the question is what was heard (transcript); this only collects attachments.
        self.ask = Composer("Question", "", text_input=False)
        self.ask.capture_requested.connect(lambda mode: self.capture(mode, self.ask))
        self.ask.notice.connect(self._notice)
        self.ask.changed.connect(self._update_buttons)
        self.ask_btn = QPushButton("⚡ Ask")
        self.ask_btn.setObjectName("primary")
        self.ask_btn.clicked.connect(self.ask_side)
        self.send_btn = QPushButton("↪ Send to agent")  # text and tooltips: see _apply_mode
        self.send_btn.clicked.connect(lambda: self.send_to_agent(False))
        self.redirect_btn = QPushButton("✋ Interrupt && send")
        self.redirect_btn.setToolTip("Like Send to agent, but stops the agent's current step first")
        self.redirect_btn.clicked.connect(lambda: self.send_to_agent(True))
        self.stop_answer_btn = QPushButton("■ Stop answer")
        self.stop_answer_btn.clicked.connect(lambda: self.side and self.side.interrupt())
        self.stop_answer_btn.setEnabled(False)
        ask_row = FlowLayout(spacing=4)  # wraps when the Q&A panel is narrow
        for btn in (self.ask_btn, self.send_btn, self.redirect_btn, self.stop_answer_btn):
            ask_row.addWidget(btn)
        # Live transcript of the PC's audio output (feeds the Ask button)
        self.listen_btn = QPushButton("🎧 PC audio: off")
        self.listen_btn.setCheckable(True)
        self.listen_btn.setToolTip("Transcribe everything this PC plays, live and offline. Ask sends the new speech.")
        self.listen_btn.toggled.connect(self._toggle_listening)
        self.level_bar = QProgressBar()
        self.level_bar.setRange(0, 100)
        self.level_bar.setTextVisible(False)
        self.level_bar.setFixedSize(70, 8)
        self.audio_status = QLabel()
        self.audio_status.setStyleSheet("color:#9aa4b2;")
        self.audio_status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        audio_head = QHBoxLayout()
        audio_head.addWidget(self.listen_btn)
        audio_head.addWidget(self.level_bar)
        audio_head.addWidget(self.audio_status, 1)
        self.tx_view = QTextBrowser()
        self.tx_view.setToolTip("White = new speech (sent by the buttons below), grey = already sent, "
                                "blue = being spoken.\nSelect text to send just that part.")
        self.tx_view.selectionChanged.connect(self._update_buttons)  # a selection is something to send
        # Top block reads top to bottom: what was heard -> attachments that go with it -> where to send it.
        question_box = QWidget()
        question_layout = QVBoxLayout(question_box)
        question_layout.setContentsMargins(0, 0, 0, 0)
        question_layout.addLayout(audio_head)
        question_layout.addWidget(self.tx_view, 1)
        question_layout.addWidget(self.ask)
        question_layout.addLayout(ask_row)
        if not audio_supported():
            self.listen_btn.setEnabled(False)
            self.audio_status.setText("PC audio capture needs Windows")

        # A private message box for the person on the remote page only: they can drop a message into this panel
        # ("Send to AI" asks the developer, answered here; "Send to local user" just posts it so you see it).
        # The box is drawn entirely on the page (see remote.py _kind_of/_d_priv and remote_web); the local user
        # has no such box, so this is just an invisible marker telling the mirror where to place the remote one.
        self.msg = QWidget()
        self.msg.setProperty("remote_private", True)
        self.msg.hide()  # never shown on the desktop; the mirror keeps it visible on the remote page

        right = QGroupBox("Ask while it codes")
        self.right_panel = right
        right.setProperty("remote_map_name", "Quick answers")  # what the remote page's monitor map calls this side
        right_layout = QVBoxLayout(right)
        right_layout.addWidget(self.side_label)
        right_split = QSplitter(Qt.Orientation.Vertical)
        right_split.addWidget(question_box)
        right_split.addWidget(self.chat)
        right_split.setSizes([360, 520])
        right_layout.addWidget(right_split, 1)
        right_layout.addWidget(self.msg)  # hidden here; placeholder for the remote page's private message box

        # Questions and answers on the left, the code side (plan / activity / files) on the right. Drag the
        # divider between them to give either side more room; the position is remembered.
        main_split = QSplitter(Qt.Orientation.Horizontal)
        main_split.setObjectName("mainSplit")
        main_split.addWidget(right)
        main_split.addWidget(left)
        main_split.setHandleWidth(9)
        main_split.setChildrenCollapsible(False)
        main_split.handle(1).setToolTip("Drag to resize the Q&A panel and the code side")
        saved = self.settings.main_split
        valid = isinstance(saved, list) and len(saved) == 2 and all(isinstance(v, int) and v > 0 for v in saved)
        main_split.setSizes(saved if valid else [600, 900])
        self.main_split = main_split
        self.setCentralWidget(main_split)
        self.setMinimumWidth(MIN_WINDOW_WIDTH)

        # Status bar
        self.phase_label = QLabel()
        self.elapsed_label = QLabel()
        self.cost_label = QLabel()
        self.cost_label.setToolTip("API-equivalent cost reported by Claude Code (subscription users are not billed this)")
        self.hotkey_label = QLabel()
        self.hotkey_label.setStyleSheet("color:#9aa4b2;")
        self.interrupt_btn = QPushButton("✋ Interrupt")
        self.interrupt_btn.setToolTip("Stop the agent's current step but keep the session (send it a message to continue)")
        self.interrupt_btn.clicked.connect(self.interrupt_main)
        self.stop_btn = QPushButton("■ Stop")
        self.stop_btn.setToolTip("Kill the coding agent")
        self.stop_btn.clicked.connect(self.stop_main)
        status = self.statusBar()
        status.addWidget(self.phase_label, 1)
        status.addPermanentWidget(self.hotkey_label)
        status.addPermanentWidget(self.elapsed_label)
        status.addPermanentWidget(self.cost_label)
        status.addPermanentWidget(self.interrupt_btn)
        status.addPermanentWidget(self.stop_btn)

        # Ctrl+V anywhere in the window (text boxes handle it themselves first)
        paste = QShortcut(QKeySequence(QKeySequence.StandardKey.Paste), self)
        paste.setContext(Qt.ShortcutContext.WindowShortcut)
        paste.activated.connect(self._paste_anywhere)

        self.chat.set_placeholder(
            "Messages appear here, newest at the top. The question is what was heard: the new speech in the "
            "transcript above (white text), together with any screenshots or files you attach.\n\n"
            "**⚡ Ask** - the fast model answers here in seconds, using the task, the plan and the agent's "
            "progress. The coding agent is not disturbed.\n\n"
            "**↪ Send to agent** - the coding agent gets it as a new instruction and adjusts the code "
            "(**✋ Interrupt & send** stops its current step first)."
        )

    def _apply_settings(self) -> None:
        s = self.settings
        effort = f" · {s.coding_effort}" if supports_effort(s.coding_model) else ""
        side_effort = f" · {s.side_effort}" if supports_effort(s.side_model) else ""
        self.models_label.setText(f"Coding: {s.coding_model}{effort}   |   Quick answers: {s.side_model}{side_effort}"
                                  f"   |   {s.permission_mode}")
        self.side_label.setText(f"⚡ {s.side_model}{side_effort} - instant answers, read-only")
        self._apply_mode()
        # Reading text (plan, activity, transcript, chat) at the chosen size; buttons and menus stay normal.
        scale = min(4.0, max(0.75, s.reading_scale))
        self.plan_view.set_text_scale(scale)
        self.prompts_view.set_text_scale(scale)
        self.activity_view.set_text_scale(scale)
        self.chat.set_text_scale(scale)
        self.tx_view.setFont(scaled_font(self.tx_view, scale))
        self.hotkeys.unregister_all()
        if s.hotkeys_enabled and self.hotkeys.supported:
            failed = self.hotkeys.register_defaults()
            if failed:
                self._notice(f"Could not register hotkeys (in use by another app?): {', '.join(failed)}")
        self._refresh_hotkey_label()

    def _refresh_hotkey_label(self) -> None:
        if not (self.settings.hotkeys_enabled and self.hotkeys.supported):
            self.hotkey_label.setText("")
            return
        keys = " / ".join(label for _m, _v, label in DEFAULT_HOTKEYS.values())
        self.hotkey_label.setText(f"{keys} → {self._default_target().title}  ")

    # ------------------------------------------------------------------ small helpers
    def _notice(self, text: str, ms: int = 6000) -> None:
        self.statusBar().showMessage(text, ms)

    def _claude(self) -> str | None:
        path = resolve_claude_cli(self.settings)
        if not path:
            QMessageBox.warning(
                self, APP_NAME,
                "Could not find the Claude Code CLI (`claude`).\n\nInstall it (https://claude.com/claude-code) "
                "and log in once by running `claude` in a terminal, or set its path in Settings.",
            )
        return path

    def _default_target(self) -> Composer:
        """Where hotkey captures and stray pastes go: the task box while it is shown; after that the question -
        or, when the developer directs an external agent, his status field (a capture then shows that agent)."""
        if not self.task_box.isHidden():
            return self.task
        return self.status if self._directing() else self.ask

    def _new_task(self) -> None:
        """Start over for the next problem - after a confirmation, since everything on screen is cleared."""
        running = self.main is not None and self.main.running and self.phase in ACTIVE
        question = ("Start a new task?\n\nThis clears the plan, the prompts, the activity log, the files view and the "
                    "Q&A panel (transcript, attachments and chat), and empties the workspace path.\n"
                    "Nothing on disk is deleted.")
        if running:
            question += "\n\nThe current task is still running and will be stopped."
        if QMessageBox.question(self, APP_NAME, question) != QMessageBox.StandardButton.Yes:
            return
        self._clear_everything()
        self.task.edit.setFocus()
        self._notice("Ready for a new task.")

    def _clear_everything(self) -> None:
        """Back to how the window is when the app starts: no task, no history, an empty workspace path."""
        self._stop_sessions()
        for timer in (self._files_timer, self._watch_timer, self._lead_timer):
            timer.stop()
        self.run = None
        self._set_phase(Phase.IDLE)
        self._turn_text, self._intro = "", ""
        self._live_view, self._pending_action = None, None
        self._speaking, self._lead_shown, self._interrupt_requested = False, False, False
        self._turn_prompts = self._syntax_rounds = self._questions = 0
        self._run_secs = self._side_cost_done = 0.0
        self._tool_names.clear()
        # The code side: plan, prompts, activity, files
        self.plan_view.wrap_code = False
        self.plan_view.reset()
        self.plan_view.append_markdown(INTRO_PLAN, spaced=True)
        self.prompts_view.reset()
        self.tabs.setTabText(self.tabs.indexOf(self.prompts_view), "Prompts")
        self.activity_view.reset()
        self.file_tree.clear()
        self.code_view.set_code("")
        self.file_label.clear()
        self._shown_file = None
        self.explorer_title.setText("EXPLORER")
        self.feedback_edit.clear()
        self.status.clear()
        self.elapsed_label.setText("")
        self.cost_label.setText("")
        # The Q&A panel: what was heard, the attachments, the chat
        self._tx.clear()
        self._tx_partial = ""
        self._fillers_skipped = 0
        self._show_audio_status()
        self._render_transcript()
        self.ask.clear()
        self.chat.clear()
        # The workspace path, now and for the next start of the app
        self.ws_edit.clear()
        self.settings.workspace = ""
        self.settings.save()
        self.task.clear()
        self.task_box.show()
        self._apply_mode()
        self.tabs.setCurrentIndex(0)
        self._update_buttons()
        self._refresh_hotkey_label()
        if resolve_claude_cli(self.settings):
            self._ensure_side()  # warm up the quick-answer CLI again, as at start-up

    def _language_changed(self, language: str) -> None:
        """Language "Non-coding" turns the task into directing an external coding agent: show what that means."""
        directing = language == LANG_NON_CODING
        self.mode_hint.setVisible(directing)
        self.review_check.setVisible(not directing)  # he always waits for answers before he plans
        self.ws_edit.setPlaceholderText("(the folder your coding agent works in)" if directing
                                        else "(a new empty folder for each task)")

    def _directing(self) -> bool:
        """Is the current task one where the developer directs an external coding agent?"""
        return self.run is not None and self.run.directing

    def _apply_mode(self) -> None:
        """Name the tabs and buttons for what the developer does in this task: code, or direct a coding agent."""
        directing = self._directing()
        tab = self.tabs.indexOf
        # Directing: the first tab holds only his understanding and his questions; what he says after that is
        # in the chat, and what he writes for the agent on the Prompts tab.
        self.tabs.setTabText(tab(self.plan_tab), "Understanding && questions" if directing else "Understanding && plan")
        self.tabs.setTabText(tab(self.activity_view), "Activity" if directing else "Coding activity")
        self.tabs.setTabVisible(tab(self.prompts_view), directing)
        self.right_panel.setTitle("Supervisor - questions && answers" if directing else "Ask while it codes")
        self.send_btn.setText("↪ Send to developer" if directing else "↪ Send to agent")
        self.send_btn.setToolTip(
            "Gives the developer the new speech plus the attachments (what your coding agent answered and did); "
            "he reviews it and takes his next step." if directing else
            "New instruction for the coding agent: sends the new speech plus the attachments "
            "to it; it picks them up after its current step and adjusts the code.")
        self.ask_btn.setToolTip(
            "Sends the new speech (white lines) plus the attachments. A question from the supervisor is answered "
            "below in seconds; anything that moves the work goes on to the developer's working session.\n"
            "Attachments alone go straight to the developer." if directing else
            "Quick answer: sends the new speech (white lines) plus the attachments to the fast "
            "model, which replies below in seconds. The coding agent is not disturbed.")

    def _set_phase(self, phase: Phase) -> None:
        if phase in ACTIVE and self.phase not in ACTIVE:
            self._run_clock.start()
        elif phase not in ACTIVE and self.phase in ACTIVE and self._run_clock.isValid():
            self._run_secs += self._run_clock.elapsed() / 1000
            self._run_clock.invalidate()
        self.phase = phase
        self.approval_bar.setVisible(phase == Phase.AWAITING)
        self.lead_bar.setVisible(self._directing())
        self._update_buttons()
        self._refresh_phase_label()
        self._refresh_hotkey_label()

    def _something_to_send(self) -> bool:
        """Is there anything Ask / Send would send: new speech, a selected part of the transcript, attachments?"""
        if not self.ask.is_empty() or self.tx_view.textCursor().hasSelection():
            return True
        if any(not asked for _text, asked in self._tx):
            return True
        return bool(self._tx_partial) and not is_filler(self._tx_partial)

    def _update_buttons(self) -> None:
        """Enable every button exactly while what it does is possible; called whenever that may have changed."""
        if not hasattr(self, "stop_btn"):
            return  # the window is still being built
        run = self.run
        alive = self.main is not None and self.main.running
        working = alive and self.phase in ACTIVE
        setup = not self.task_box.isHidden()  # the task box is shown: no task has been started yet
        to_send = self._something_to_send()
        typed_folder = self.ws_edit.text().strip()
        # Toolbar
        self.new_task_action.setEnabled(run is not None or bool(self._tx) or bool(self.chat.bubbles()))
        self.ws_edit.setEnabled(setup)  # the folder is fixed once a task has started
        self.browse_action.setEnabled(setup)
        self.open_action.setEnabled(run is not None or (bool(typed_folder) and Path(typed_folder).expanduser().is_dir()))
        # Starting a task, and the plan review
        self.start_btn.setEnabled(setup and not self.task.is_empty())
        self.revise_btn.setEnabled(self.phase == Phase.AWAITING and bool(self.feedback_edit.text().strip()))
        # The question panel
        self.ask_btn.setEnabled(to_send)
        self.send_btn.setEnabled(alive and to_send)
        self.redirect_btn.setEnabled(working and to_send)
        # Where the external agent stands (Non-coding): only once the developer has given it a prompt
        directing = run is not None and run.directing
        prompted = directing and bool(run.prompts)
        self.done_btn.setEnabled(alive and prompted)
        self.working_btn.setText(WORKING_LABEL if prompted or not directing else GO_AHEAD_LABEL)
        self.working_btn.setToolTip(WORKING_TIP if prompted or not directing else GO_AHEAD_TIP)
        self.working_btn.setEnabled(alive and directing and (prompted or run.first_done))
        # Files tab
        self.refresh_btn.setEnabled(run is not None)
        self.copy_code_btn.setEnabled(self._shown_file is not None)
        # The running session
        self.interrupt_btn.setEnabled(working)
        self.stop_btn.setEnabled(alive)

    def _refresh_phase_label(self) -> None:
        color = PHASE_COLORS.get(self.phase, "#9aa4b2")
        detail = f" - {self._main_status}" if self.phase in ACTIVE and self._main_status else ""
        self.phase_label.setText(f"<span style='color:{color}'>●</span> {self.phase.value}{detail}")

    def _tick(self) -> None:
        self._update_buttons()  # a safety net: whatever changed without telling is picked up within a second
        secs = self._run_secs + (self._run_clock.elapsed() / 1000 if self._run_clock.isValid() else 0)
        self.elapsed_label.setText(f"⏱ {fmt_secs(secs)}  " if self.run else "")
        main_cost = self.main.total_cost if self.main else 0.0
        side_cost = self._side_cost_done + (self.side.total_cost if self.side else 0.0)
        if main_cost or side_cost:
            self.cost_label.setText(f"Coding ${main_cost:.2f} · Q&A ${side_cost:.2f}  ")

    def _guard(self, session: ClaudeSession, fn):
        """Ignore late signals from sessions that have been replaced."""
        def handler(*args):
            if session is self.main:
                fn(*args)
        return handler

    # ------------------------------------------------------------------ capture & paste
    def capture(self, mode: str, target: Composer | None = None) -> None:
        """Start a capture. From a button (`target` given) a whole-screen capture asks which monitor;
        from the global hotkey it takes the monitor the mouse is on."""
        from_button = target is not None
        target = target or self._default_target()
        screens = QGuiApplication.screens()
        if mode == "full" and from_button and len(screens) > 1:
            # Clicking the button puts the mouse on this window's monitor, so "the monitor under the
            # mouse" would always be the same one - let the user pick instead.
            menu = QMenu(self)
            menu.setProperty("remote_skip", True)  # the monitor picker is a local popup; keep it off the remote page
            choices = {}
            for number, screen in enumerate(screens, 1):
                size = screen.geometry().size() * screen.devicePixelRatio()
                here = "  (this window is here)" if screen is self.screen() else ""
                choices[menu.addAction(f"Monitor {number} - {size.width()} × {size.height()}{here}")] = screen
            chosen = menu.exec(QCursor.pos())
            menu.deleteLater()
            if chosen is not None:
                self.capturer.start("full", target, choices[chosen])
            return
        self.capturer.start(mode, target)

    def _on_captured(self, img, target: Composer) -> None:
        if target.add_image(img):
            self._notice(f"Screenshot added to {target.title} ({img.width()}x{img.height()})")

    def _paste_anywhere(self) -> None:
        focus = QApplication.focusWidget()
        if focus is not None and self.right_panel.isAncestorOf(focus):
            self.ask.paste_from_clipboard()
        elif focus is not None and self.task.isAncestorOf(focus):
            self.task.paste_from_clipboard()
        elif focus is not None and self.lead_bar.isAncestorOf(focus):
            self.status.paste_from_clipboard()
        else:
            self._default_target().paste_from_clipboard()

    # ------------------------------------------------------------------ main task
    def start_task(self) -> None:
        if self.task.is_empty():
            self._notice("Add a description, screenshots or files first.")
            return
        coding_provider = self.settings.coding_provider
        if coding_provider == llm.CLAUDE_CODE:
            claude = self._claude()
            if not claude:
                return
        else:
            claude = resolve_claude_cli(self.settings)  # may be unused for an API provider
            ok, why = llm.ready(self.settings, coding_provider)
            if not ok:
                QMessageBox.warning(self, APP_NAME, why)
                return
        if self.main is not None and self.main.running and self.phase in ACTIVE | {Phase.AWAITING}:
            answer = QMessageBox.question(self, APP_NAME, "The coding agent is still working on the current task.\n"
                                                          "Stop it and start the new task?")
            if answer != QMessageBox.StandardButton.Yes:
                return
        language = self.lang_combo.currentText()
        directing = language == LANG_NON_CODING  # the developer directs a coding agent outside this app
        ws_text = self.ws_edit.text().strip()
        if ws_text and not Path(ws_text).expanduser().is_dir():
            QMessageBox.warning(self, APP_NAME, f"The workspace folder does not exist:\n{ws_text}\n\n"
                                + ("Pick the folder your coding agent works in, or leave Workspace empty." if directing
                                   else "Pick your project folder, or leave Workspace empty for a fresh folder."))
            return
        # A coding task with a folder given: that folder is the user's and stays exactly as it is. The developer
        # gets a copy of it to work in; the user brings his changes over by hand.
        given = Path(ws_text).expanduser().resolve() if ws_text and not directing else None
        if given is not None:
            runs_root = Path(self.settings.runs_dir).expanduser().resolve()
            if runs_root == given or given in runs_root.parents:
                QMessageBox.warning(self, APP_NAME, "The runs folder (Settings) lies inside this workspace, so the "
                                                    "workspace cannot be copied into it. Choose another one.")
                return
            count, size = folder_weight(given)
            if count > CLONE_ASK_FILES or size > CLONE_ASK_BYTES:
                answer = QMessageBox.question(
                    self, APP_NAME, f"The developer works on a copy of the workspace, and this one is large (more "
                                    f"than {min(count, CLONE_ASK_FILES):,} files or {CLONE_ASK_BYTES // 1_000_000} MB)."
                                    "\n\nCopy it now? That can take a while.")
                if answer != QMessageBox.StandardButton.Yes:
                    return
        self._stop_sessions()

        text, attachments = self.task.text(), self.task.attachments()
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        run_dir = (Path(self.settings.runs_dir).expanduser() / run_id).resolve()
        try:
            workspace = (Path(ws_text).expanduser() if ws_text and given is None else run_dir / "workspace")
            if given is not None:
                QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
                try:
                    shutil.copytree(given, workspace, ignore=shutil.ignore_patterns(*CLONE_SKIP), dirs_exist_ok=True)
                finally:
                    QApplication.restoreOverrideCursor()
            workspace.mkdir(parents=True, exist_ok=True)
            workspace = workspace.resolve()  # long path names: Claude Code distrusts 8.3 short paths
            inputs = run_dir / "inputs"
            inputs.mkdir(parents=True, exist_ok=True)
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                imgs, files = atts.prepare(attachments, inputs)
            finally:
                QApplication.restoreOverrideCursor()
            (run_dir / "task.md").write_text(text, encoding="utf-8")
        except OSError as exc:  # includes shutil.Error: files of the workspace that could not be copied
            QMessageBox.critical(self, APP_NAME, f"Could not prepare the run folder:\n{shorten(str(exc), 1200)}")
            return

        self.settings.workspace = ws_text
        self.settings.review_plan = self.review_check.isChecked()
        self.settings.language = language
        self.settings.save()
        inputs_md = atts.describe_inputs(imgs, files)
        self.run = Run(run_id, run_dir, inputs, workspace, text, inputs_md, language=language,
                       directing=directing, shared=directing and bool(ws_text), given=given)
        workspace_empty = not any(workspace.iterdir())
        self._snapshot_existing()  # whatever is already in the workspace belongs to the problem
        if directing:
            self._workspace_changes()  # from here on, what changes in the workspace is the coding agent's doing
            self._watched_state = self._workspace_state()
            self._watch_timer.start()
        else:
            self._watch_timer.stop()
        self._apply_mode()
        self.plan_view.wrap_code = directing
        self.prompts_view.reset()
        self.tabs.setTabText(self.tabs.indexOf(self.prompts_view), "Prompts")
        self._lead_live, self._speaking, self._turn_prompts = None, False, 0
        self._lead_shown, self._intro = False, ""
        self.status.clear()
        self._run_secs = 0.0
        self._turn_text = ""
        self._tool_names.clear()
        self._interrupt_requested = False
        self._syntax_rounds = 0
        self.plan_view.reset()
        self.activity_view.reset()
        self.file_tree.clear()
        self.code_view.set_code("")
        self.file_label.clear()
        self._shown_file = None
        self.explorer_title.setText(f"EXPLORER  ·  {workspace.name.upper()}" if given is None else
                                    f"EXPLORER  ·  {given.name.upper()}  (THE DEVELOPER'S COPY)")
        self.explorer_title.setToolTip("" if given is None else
                                       f"The developer's own copy: {workspace}\nYour folder, untouched: {given}")
        self.activity_view.append_markdown(
            f"**Task {run_id}** · {len(imgs)} screenshot(s), {len(files)} file(s) · workspace {code_span(workspace)}"
        )
        if given is not None:
            self.activity_view.append_markdown(
                f"📁 **Working on a copy** of {code_span(given)} ({len(self._workspace_files())} files). That folder "
                "itself is never changed: the developer edits and runs only in the copy above, and quick answers "
                "are given from your folder as it is at that moment. Bring his changes over yourself - the Files "
                "tab shows what he edited (M) and added (U), and *Open folder* opens the copy.")

        s = self.settings
        # Directing: read-only tools, so there is nothing to write with and nothing to run.
        tools = "Read,Glob,Grep" if directing else None
        permission_mode = None if directing else s.permission_mode
        system = (prompts.LEAD_SYSTEM if directing else prompts.MAIN_SYSTEM) + prompts.name_note(s.dev_name)
        if s.coding_provider == llm.CLAUDE_CODE:
            session = ClaudeSession(
                claude_path=claude,
                cwd=workspace,
                model=s.coding_model,
                effort=s.coding_effort if supports_effort(s.coding_model) else None,
                permission_mode=permission_mode,
                tools=tools,
                add_dirs=[inputs],
                append_system_prompt=system,
                name=f"ScreenCoder {run_id}",
                log_path=run_dir / "events.jsonl",
                parent=self,
            )
        else:
            session = ApiAgentSession(
                provider=s.coding_provider,
                api_key=llm.key_for(s, s.coding_provider),
                cwd=workspace,
                model=s.coding_model,
                tools=tools,
                add_dirs=[inputs],
                append_system_prompt=system,
                permission_mode=permission_mode,
                can_run=(not directing) and can_run_commands(s.permission_mode),
                name=f"ScreenCoder {run_id}",
                log_path=run_dir / "events.jsonl",
                parent=self,
            )
        self.main = session
        g = lambda fn: self._guard(session, fn)  # noqa: E731
        session.initialized.connect(g(self._main_init))
        session.status_changed.connect(g(self._main_status_changed))
        session.text_started.connect(g(self._main_text_started))
        session.text_delta.connect(g(self._main_text_delta))
        session.text_finished.connect(g(self._main_text_finished))
        session.thinking_finished.connect(g(self._main_thinking))
        session.tool_used.connect(g(self._main_tool))
        session.tool_result.connect(g(self._main_tool_result))
        session.permission_denied.connect(g(self._main_denied))
        session.message_consumed.connect(g(self._main_message_consumed))
        session.turn_finished.connect(g(self._main_turn))
        session.process_finished.connect(g(self._main_exit))
        session.start()
        if directing:
            body = prompts.lead_start_prompt(text, inputs_md, str(workspace), self.run.shared, workspace_empty)
        else:
            body = prompts.analysis_prompt(text, inputs_md, str(workspace), workspace_empty, language)
        session.send(atts.content_blocks(imgs, files, body))
        self._set_phase(Phase.ANALYZING)
        self.tabs.setCurrentIndex(0)
        self.plan_view.append_markdown(f"*{s.coding_model} is reading the problem…*")
        self._restart_side()  # fresh quick-answer context for the new task (and pre-warm the CLI)
        self.task_box.hide()  # not needed again for this task; "＋ New task" brings it back
        self._update_buttons()
        self._refresh_hotkey_label()
        self.refresh_files()

    def _workspace_files(self) -> list[str]:
        """Relative paths of the files in the workspace (build/VCS folders skipped, capped at 2000)."""
        found: list[str] = []
        if self.run is None:
            return found
        root = self.run.workspace
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                found.append(os.path.relpath(os.path.join(dirpath, name), root))
            if len(found) > 2000:
                break
        return found

    def _snapshot_existing(self) -> None:
        """Remember the files that are in the workspace before the agent starts, and their content."""
        run = self.run
        text_budget = 30_000_000  # keep at most this much original text in memory for line comparison
        for rel in self._workspace_files():
            key = file_key(rel, run.workspace)
            run.existing.add(key)
            try:
                path = run.workspace / rel
                if path.stat().st_size > 2_000_000:
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            run.baseline[key] = content_hash(raw)
            if len(raw) <= 300_000 and len(raw) <= text_budget and b"\x00" not in raw[:8192]:
                run.baseline_text[key] = raw.decode("utf-8", errors="replace")
                text_budget -= len(raw)

    def _read_manifest(self, content: str) -> None:
        """The agent's list of files that already exist in the problem (one relative path per line)."""
        for line in content.splitlines():
            line = line.strip().strip("`\"'").lstrip("-* ").strip()
            if line and not line.startswith("#"):
                self.run.existing.add(file_key(line, self.run.workspace))
        self._files_timer.start()

    def _file_status(self, rel: str) -> str:
        """ORIGINAL (from the problem, untouched), MODIFIED (from the problem, edited) or NEW (agent-made)."""
        run = self.run
        key = file_key(rel, run.workspace)
        if key not in run.existing:
            return NEW
        if run.directing:  # an external agent writes the files: only their content tells what it touched
            original, current = run.baseline.get(key), self._current_hash(rel)
            return ORIGINAL if original is None or current is None or current == original else MODIFIED
        original = run.baseline.get(key) or run.first_write.get(key)
        if original is None or key not in run.first_write and key not in run.edited:
            return ORIGINAL  # the agent never wrote to it
        try:
            current = content_hash((run.workspace / rel).read_bytes())
        except OSError:
            return ORIGINAL
        return ORIGINAL if current == original else MODIFIED

    def _current_hash(self, rel: str) -> str | None:
        """Content hash of a workspace file, re-read only when its size or modification time changed."""
        run = self.run
        path = run.workspace / rel
        try:
            stat = path.stat()
            cached = run.stat_cache.get(rel)
            if cached is not None and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                return cached[2]
            # Too big to hash on every look: its size and time stand in for the content.
            digest = (content_hash(path.read_bytes()) if stat.st_size <= 2_000_000
                      else f"large:{stat.st_mtime_ns}:{stat.st_size}")
        except OSError:
            return None
        run.stat_cache[rel] = (stat.st_mtime_ns, stat.st_size, digest)
        return digest

    def _workspace_changes(self) -> str:
        """Markdown list of what appeared, changed or vanished in the workspace since the last call."""
        run = self.run
        now = {rel: digest for rel in self._workspace_files() if (digest := self._current_hash(rel)) is not None}
        lines = []
        for rel, digest in now.items():
            before = run.seen.get(rel)
            if before is None:
                lines.append(f"- new: `{rel}`")
            elif before != digest:
                lines.append(f"- changed: `{rel}`")
        lines += [f"- deleted: `{rel}`" for rel in run.seen if rel not in now]
        run.seen = now
        if len(lines) > CHANGES_LISTED:
            lines = lines[:CHANGES_LISTED] + [f"- ...and {len(lines) - CHANGES_LISTED} more"]
        return "\n".join(lines)

    def _project_changes(self) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
        """For a task on a copy of the user's folder: the developer's changes that are not in that folder yet,
        and those that are - each as (relative path, "edited" | "new" | "removed").

        He speaks as the owner of the user's folder, so this - not what happened in his copy - is what decides
        whether his work counts as done.
        """
        run = self.run
        pending: list[tuple[str, str]] = []
        landed: list[tuple[str, str]] = []
        if run is None or run.given is None:
            return pending, landed
        present = set()
        for rel in self._workspace_files():
            key = file_key(rel, run.workspace)
            present.add(key)
            mine = self._current_hash(rel)
            if mine is None or mine.startswith("large:") or run.baseline.get(key) == mine:
                continue  # as it was when the task started (or too large to compare): not a change of his
            try:
                theirs = content_hash((run.given / rel).read_bytes())
            except OSError:
                theirs = None  # not in the user's folder (yet)
            (landed if theirs == mine else pending).append((rel, "edited" if key in run.baseline else "new"))
        for key in sorted(run.baseline):  # files he removed, where the user's folder still has them
            if key not in present:
                (pending if (run.given / key).exists() else landed).append((key, "removed"))
        return pending, landed

    def _project_state(self) -> tuple[list[str], list[str]]:
        """The same in the words he is briefed with. What is not in the user's folder is something he is doing
        ("editing `a.py`"), what is there is something he has done ("`a.py` (edited)") - with no hint of where
        the former comes from: his own copy is never mentioned to him or by him."""
        doing = {"edited": "editing", "new": "adding", "removed": "removing"}
        done = {"edited": "edited", "new": "added", "removed": "removed"}
        pending, landed = self._project_changes()
        return ([f"{doing[kind]} `{rel.replace(os.sep, '/')}`" for rel, kind in pending],
                [f"`{rel.replace(os.sep, '/')}` ({done[kind]})" for rel, kind in landed])

    def _changes_in_progress(self) -> str:
        """The changes that are not in the user's folder yet, spelled out: what each file there looks like now
        against what it is becoming. This is how he knows exactly what he "is changing right now"."""
        run = self.run
        parts, budget = [], DOING_CHARS
        pending = self._project_changes()[0]
        for rel, kind in pending:
            shown = rel.replace(os.sep, "/")
            if kind == "removed":
                parts.append(f"### `{shown}` - you are removing this file")
                continue
            try:
                new = (run.workspace / rel).read_bytes()
                old = (run.given / rel).read_bytes() if kind == "edited" else b""
            except OSError:
                continue
            if b"\x00" in new[:8192] or len(new) > 300_000:
                parts.append(f"### `{shown}` - {'you are changing' if kind == 'edited' else 'you are adding'} this file")
                continue
            new_lines = new.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")
            if kind == "new":
                title, body, lang = f"### `{shown}` - a file you are adding", "\n".join(new_lines), ""
            else:
                old_lines = old.decode("utf-8", errors="replace").replace("\r\n", "\n").split("\n")
                diff = list(difflib.unified_diff(old_lines, new_lines, lineterm="", n=2))[2:]  # without the file header
                title = f"### `{shown}` - lines with - are as they are now, lines with + as you are making them"
                body, lang = "\n".join(diff), "diff"
            body = shorten(body, min(DOING_FILE_CHARS, max(200, budget)))
            budget -= len(body)
            fence = atts.fence_for(body)
            parts.append(f"{title}\n{fence}{lang}\n{body}\n{fence}")
            if budget <= 0:
                parts.append(f"(…and {len(pending) - len(parts)} more file(s) named above)")
                break
        return "\n\n".join(parts)

    def _workspace_state(self) -> dict[str, tuple[int, int]]:
        """Modification time and size of every workspace file: cheap to compare, enough to notice a change."""
        state = {}
        for rel in self._workspace_files():
            try:
                stat = (self.run.workspace / rel).stat()
            except OSError:
                continue
            state[rel] = (stat.st_mtime_ns, stat.st_size)
        return state

    def _watch_workspace(self) -> None:
        """Refresh the explorer when the external coding agent has written, changed or removed a file."""
        if not self._directing():
            self._watch_timer.stop()
            return
        state = self._workspace_state()
        if state != self._watched_state:
            self._watched_state = state
            self.refresh_files()

    def _main_init(self, ev: dict) -> None:
        sid = ev.get("session_id", "")
        sid_file = self.run.dir / "session.txt" if self.run else None
        if sid_file is not None and sid and not sid_file.exists():  # init is re-sent on every turn
            sid_file.write_text(sid, encoding="utf-8")
            self.activity_view.append_markdown(
                f"Session {code_span(sid)} · model {code_span(ev.get('model', ''))} · "
                f"resume later in a terminal with {code_span('claude --resume ' + sid)}"
            )

    def _main_status_changed(self, status: str) -> None:
        self._main_status = "" if status in ("idle", "ready", "starting") else status
        self._refresh_phase_label()

    def _main_message_consumed(self, token: str) -> None:
        """The agent picked up a "Send to agent" message: its next piece of text is the reply for the chat."""
        bubble = self._agent_replies.pop(token, None)
        if bubble is not None:
            if not getattr(bubble, "status_only", False):  # for a status he first reads, and may then say nothing
                bubble.set_status("replying…")
            self._replies_ready.append(bubble)

    def _next_reply_bubble(self) -> MessageBubble | None:
        """The chat bubble waiting for the agent's reply to a message it has just picked up, if any."""
        if self._reply_live is not None or not self._replies_ready:
            return None
        bubble = self._replies_ready.popleft()
        while self._replies_ready:  # several messages picked up at once get one combined reply
            extra = self._replies_ready.popleft()
            extra.set_markdown("*I answered this together with the message below.*")
            extra.finish()
        return bubble

    def _main_text_started(self) -> None:
        if self._directing():
            # His first turn - understanding and questions - is written on the first tab. From then on
            # what he says goes into the chat, in one bubble per turn (see _render_lead).
            if self._lead_live is None and self.run.first_done:
                self._lead_live = self._next_reply_bubble() or self.chat.add("answer", DEV_TITLE)
            if self._lead_live is not None:
                self._speaking, self._block_start = True, len(self._turn_text)
                return
            self._live_view = self.plan_view
            if self._lead_shown:
                self.plan_view.begin_live()
            else:
                self._intro = ""  # shown from its first heading on - see _main_text_delta
            return
        self._live_view = self.plan_view if self.phase == Phase.ANALYZING else self.activity_view
        self._live_view.begin_live()
        bubble = self._next_reply_bubble()
        if bubble is not None:
            self._reply_live, self._reply_text = bubble, ""

    def _render_lead(self) -> None:
        """Show the developer's turn as far as it is written: his words in the chat, his prompt on its tab."""
        bubble = self._lead_live
        if bubble is None or self.run is None:
            return
        turn = parse_turn(self._turn_text, final=False)
        if not may_become_skip(self._turn_text):  # hold it back while it could still be just "[skip]"
            bubble.set_status("")
            bubble.update_text(turn.speech)
        self._show_prompts(turn.prompts)
        self.prompts_view.set_live(turn.writing)
        # A prompt of this turn that was already marked as sent: what goes with it is shown as it is written.
        after = self._after_bubbles.get(len(self.run.prompts)) if self._turn_prompts else None
        if after is not None and turn.after_send:
            after.set_status("")
            after.update_text(turn.after_send)

    def _main_text_delta(self, text: str) -> None:
        if self._live_view is None and not self._speaking:
            self._main_text_started()
        if self._speaking:
            self._turn_text += text
            if not self._lead_timer.isActive():
                self._lead_timer.start()
            return
        if self._directing() and not self._lead_shown:
            # The first tab holds his understanding and his questions and nothing else: a remark he makes
            # before he has read the task ("let me look at...") is left out, as is the "is reading" line.
            self._intro += text
            heading = HEADING_LINE.search(self._intro)
            if heading is None:
                return
            text, self._lead_shown = self._intro[heading.start():], True
            self.plan_view.reset()
            self.plan_view.begin_live()
        self._live_view.append_live(text)
        if self._live_view is self.plan_view:
            self._turn_text += text
        if self._reply_live is not None:
            self._reply_text += text
            cut = HEADING_LINE.search(self._reply_text)  # only the direct reply, not the sections after it
            shown = self._reply_text[: cut.start()] if cut else self._reply_text
            if not may_become_skip(shown):  # hold it back while it could still be just "[skip]"
                self._reply_live.set_status("")
                self._reply_live.update_text(shown)

    def _main_text_finished(self) -> None:
        if self._speaking:
            self._speaking = False
            said = self._turn_text[self._block_start:].strip()
            self._turn_text += "\n\n"
            if is_skip(said):  # he took a status in and had nothing to say about it
                self.run.activity.append("STATUS: you took that in and said nothing.")
            elif said:  # the quick-answer model has to know what the developer has said and asked for
                self.run.activity.append(f"YOU SAID:\n{said}")
            self._lead_timer.stop()
            self._render_lead()
            return
        if self._live_view is None:
            return
        text = self._live_view.commit_live().strip()
        if self._live_view is self.plan_view:
            self._turn_text += "\n\n"
            if text and self._directing():  # the quick-answer model has to know what the developer has said
                self.run.activity.append(f"YOU SAID:\n{text}")
        elif text and self.run:
            self.run.activity.append(text)
        self._live_view = None
        if self._reply_live is not None:
            bubble, self._reply_live = self._reply_live, None
            cut = HEADING_LINE.search(self._reply_text)
            reply = (self._reply_text[: cut.start()] if cut else self._reply_text).strip()
            if is_skip(reply):
                self._drop_skipped(bubble, "· nothing to change - no reply needed")
            else:
                # An empty reply means the agent went straight to work without answering in words.
                bubble.set_markdown(reply or (LEAD_NO_REPLY if self._directing() else
                                              "I've reworked my plan - have a look at the Understanding & plan tab."
                                              if self.phase == Phase.ANALYZING else
                                              "I'm on it - you can follow what I'm doing in the Coding activity tab."))
                bubble.finish()

    def _close_agent_replies(self, note: str) -> None:
        """Settle chat bubbles that are still waiting for a reply from the coding session."""
        waiting = list(self._agent_replies.values()) + list(self._replies_ready)
        if self._reply_live is not None:
            self._reply_live.finish()
        if self._lead_live is not None:
            self._lead_live.finish()
            self._lead_live = None
        for bubble in self._after_bubbles.values():
            bubble.finish()
        self._after_bubbles.clear()
        for bubble in waiting:
            bubble.set_markdown(note)
            bubble.finish()
        self._agent_replies.clear()
        self._replies_ready.clear()
        self._reply_live = None

    def _main_thinking(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        if self.settings.show_thinking:
            self.activity_view.append_markdown(quote("💭 " + shorten(text, 700)), spaced=True)
        if self._directing():
            return  # his reply is what he says out loud, and that is always written as normal text
        # Opus writes its short between-steps notes as progress updates rather than normal text, so the
        # direct reply to a message it has just picked up usually arrives here.
        bubble = self._next_reply_bubble()
        if bubble is not None:
            cut = HEADING_LINE.search(text)
            reply = (text[: cut.start()] if cut else text).strip()
            if is_skip(reply):
                self._drop_skipped(bubble, "· nothing to change - no reply needed")
                return
            bubble.set_markdown(shorten(reply, 900))
            bubble.finish()
            if self.run:
                self.run.activity.append(text)

    def _main_tool(self, block: dict, from_subagent: bool) -> None:
        if self.run is None:
            return
        name, inp = block.get("name", "?"), block.get("input") or {}
        self._tool_names[block.get("id", "")] = name
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        if name == "Write" and path and Path(path).name == self.run.manifest.name and self.run.inputs.name in Path(path).parts:
            self._read_manifest(str(inp.get("content", "")))  # bookkeeping only: not shown in the log
            return
        md, spaced = tool_summary(name, inp, self.run.workspace)
        if from_subagent:
            md = "↳ " + md
        self.activity_view.append_markdown(md, spaced)
        self.run.activity.append(md)
        if name in EDIT_TOOLS and path:
            key = file_key(path, self.run.workspace)
            self.run.edited.add(key)
            if name == "Write":  # the first version the agent writes is the reference for "did it change later?"
                self.run.first_write.setdefault(key, content_hash(str(inp.get("content", ""))))
                self.run.first_write_text.setdefault(key, str(inp.get("content", "")))
            shown = rel_path(path, self.run.workspace)
            if shown not in self.run.touched:
                self.run.touched.append(shown)
            self._files_timer.start()

    def _main_tool_result(self, block: dict, from_subagent: bool) -> None:
        if self.run is None:
            return
        name = self._tool_names.get(block.get("tool_use_id", ""), "tool")
        text = result_text(block).strip()
        if block.get("is_error"):
            first = shorten(text.splitlines()[0] if text else "error", 300)
            self.activity_view.append_markdown(f"⚠️ {name}: {code_span(first)}")
            self.run.activity.append(f"{name} failed: {first}")
        elif name in SHELL_TOOLS and text and not from_subagent:
            lines = text.splitlines()
            tail = "\n".join(lines[-15:])
            fence = atts.fence_for(tail)
            more = f"*… last 15 of {len(lines)} lines*\n\n" if len(lines) > 15 else ""
            self.activity_view.append_markdown(f"{more}{fence}text\n{tail}\n{fence}", spaced=True)
            self.run.activity.append("Output:\n" + shorten(tail, 1500))

    def _main_denied(self, ev: dict) -> None:
        tool = ev.get("tool_name", "tool")
        hint = " (enable bypassPermissions in Settings to let it run commands)" if tool in SHELL_TOOLS else ""
        self.activity_view.append_markdown(f"🚫 {tool} was denied{hint}")

    def _main_turn(self, ev: dict) -> None:
        if self._live_view is not None or self._speaking:
            self._main_text_finished()
        if self.run is None or self.main is None:
            return
        more = self.main.busy  # messages sent meanwhile will start another turn
        failed = bool(ev.get("is_error"))
        interrupted = self._interrupt_requested
        self._interrupt_requested = False

        if self.run.directing:
            self._lead_turn(ev, more, failed, interrupted)
            return

        if self.phase == Phase.ANALYZING and not failed:
            self.run.plan = self._turn_text.strip() or str(ev.get("result") or "")
            self.run.side_plan_sent = False
            self._turn_text = ""
            (self.run.dir / "plan.md").write_text(self.run.plan, encoding="utf-8")
            if more:
                return
            self._close_agent_replies("I've reworked my plan - have a look at the Understanding & plan tab.")
            QApplication.alert(self)
            if self.review_check.isChecked():
                self._set_phase(Phase.AWAITING)
                self.feedback_edit.setFocus()
            else:
                self._begin_coding("")
            return

        # The view deliberately stays on whatever tab the user is reading; status lines go to both logs.
        self._turn_text = ""
        if more:
            return
        # The agent is idle now: a chat message it picked up but never answered in words gets a closing line.
        self._close_agent_replies("I've taken that into account - the Coding activity tab shows what I did.")
        if failed and interrupted:
            self.activity_view.append_markdown("✋ **Interrupted.** Send the agent a message (left panel) to continue.")
            self._set_phase(Phase.INTERRUPTED)
        elif failed:
            msg = ev.get("result") or "; ".join(map(str, ev.get("errors") or [])) or ev.get("subtype", "error")
            self.activity_view.append_markdown(f"❌ **The agent stopped with an error:** {msg}")
            self.plan_view.append_markdown("❌ *The agent stopped with an error - see the Coding activity tab.*")
            self._set_phase(Phase.FAILED)
        else:
            # Safety net before calling it done: the files the agent wrote must at least parse.
            problems, checked = syntax_problems(self.run.workspace, self.run.touched)
            if problems and self._syntax_rounds < MAX_SYNTAX_ROUNDS:
                self._syntax_rounds += 1
                listing = "\n".join(f"- {p}" for p in problems)
                self.activity_view.append_markdown(
                    f"🧪 **Syntax check failed - sent back to the agent to fix:**\n\n{listing}", spaced=True)
                self.main.send([{"type": "text", "text": prompts.syntax_fix_prompt(problems)}])
                self.refresh_files()
                return  # still coding
            secs = (ev.get("duration_ms") or 0) / 1000
            if problems:
                check_note = "⚠️ **Syntax errors remain** (" + "; ".join(problems) + ")"
            elif checked:
                check_note = f"🧪 syntax check passed for {checked} file(s)"
            else:
                check_note = "no automatic syntax check for this language"
            self.activity_view.append_markdown(
                f"✅ **Done** - {fmt_secs(secs)} this turn · {len(self.run.touched)} file(s) written or edited · "
                f"{check_note}. See the *Files* tab, or send follow-ups from the panel on the left."
                + (f" The changes are in the developer's copy only - {code_span(self.run.given)} is untouched until "
                   "you bring them over." if self.run.given else "")
            )
            if self.run.given:
                # Nothing about his own copy on his tab: for whoever reads it he is still at work until the
                # changes are in the user's folder. The user is told here and in the Coding activity tab.
                self._notice("The developer has finished in his own folder - bring the changes over (Files tab). "
                             "Until then he answers as still working on it.", 20000)
            else:
                self.plan_view.append_markdown(
                    "⚠️ *The code still has syntax errors - see the Coding activity tab.*" if problems
                    else "✅ *The code is ready - see the Files tab.*")
            self._set_phase(Phase.DONE)
            self.refresh_files()
            QApplication.alert(self)

    # ------------------------------------------------------------------ directing an external coding agent
    def _lead_turn(self, ev: dict, more: bool, failed: bool, interrupted: bool) -> None:
        """The developer who directs an external coding agent has finished a turn: now it is the user's move."""
        run = self.run
        text, self._turn_text = self._turn_text.strip(), ""
        bubble, self._lead_live = self._lead_live, None
        self._lead_timer.stop()
        skipped = is_skip(text)
        first, run.first_done = not run.first_done, True
        if first and not self._lead_shown and self._intro.strip():
            # He wrote his first turn without any heading: show it as it is rather than nothing.
            text = self._intro.strip()
            self.plan_view.reset()
            self.plan_view.append_markdown(text, spaced=True)
            run.activity.append(f"YOU SAID:\n{text}")
        if text:
            try:
                if first:
                    run.plan = text
                    (run.dir / "plan.md").write_text(text, encoding="utf-8")
                with open(run.dir / "developer.md", "a", encoding="utf-8") as log:
                    log.write(text + "\n\n---\n\n")
            except OSError:
                pass
        turn = parse_turn(text, final=True)
        if text and not skipped:  # a turn that was only "nothing for me" leaves what was open, open
            self._show_prompts(turn.prompts)
            run.prompt_open, run.run_open = bool(turn.prompts), bool(turn.runs)
            if turn.prompts:
                # What he says once the prompt is with the agent waits for the user's click on "sent".
                number = len(run.prompts)
                run.held[number] = turn.after_send
                early = self._after_bubbles.pop(number, None)  # clicked while he was still writing
                if early is not None and turn.after_send:
                    early.set_markdown(turn.after_send)
                    early.finish()
                elif early is not None:
                    self.chat.remove(early)
            for command in turn.runs:
                self._show_run(command)
        self._turn_prompts = 0
        self.prompts_view.set_live(None)
        if bubble is not None:  # what he said in this turn, in the chat
            if skipped and getattr(bubble, "status_only", False):
                # A status that gave him nothing to say: he has read it and looked at the workspace, no more.
                self._drop_skipped(bubble, "· taken in - nothing to say yet")
                self.activity_view.append_markdown("🤫 *The developer took that in - nothing worth saying yet.*")
                self._notice("The developer took the status in - nothing to say yet.", 8000)
            elif skipped:
                self._drop_skipped(bubble, "· nothing in that for me - no reply needed")
            else:
                bubble.set_markdown(turn.speech or (LEAD_NO_WORDS if text else LEAD_NO_REPLY))
                bubble.finish()
        if more:
            return  # a message that arrived meanwhile starts his next turn right away
        self._close_agent_replies(LEAD_NO_REPLY)
        if failed and interrupted:
            self.activity_view.append_markdown("✋ **Interrupted.** Send the developer a message to continue.")
            self._set_phase(Phase.INTERRUPTED)
        elif failed:
            msg = ev.get("result") or "; ".join(map(str, ev.get("errors") or [])) or ev.get("subtype", "error")
            self.activity_view.append_markdown(f"❌ **The developer stopped with an error:** {msg}")
            self.plan_view.append_markdown("❌ *I stopped with an error - see the Activity tab.*")
            self._set_phase(Phase.FAILED)
        else:
            self._set_phase(Phase.WAITING)
            self.refresh_files()
            QApplication.alert(self)

    def _show_prompts(self, found: list[str]) -> None:
        """Put the prompts of the current turn that are not shown yet on the Prompts tab (and on disk)."""
        run = self.run
        for prompt in found[self._turn_prompts:]:
            run.prompts.append(prompt)
            number = len(run.prompts)
            try:
                folder = run.dir / "prompts"
                folder.mkdir(exist_ok=True)
                (folder / f"{number:02d}.txt").write_text(prompt, encoding="utf-8")
                (folder / "latest.txt").write_text(prompt, encoding="utf-8")
            except OSError as exc:
                self._notice(f"Could not save the prompt: {exc}")
            # The prompt is typed in by hand: mark what can be copied from the agent's own files instead.
            marks = find_marks(prompt, Corpus(run.workspace, self._workspace_files())) if run.shared else []
            self.prompts_view.add_prompt(number, prompt, marks)
            self.activity_view.append_markdown(
                f"✉️ **Prompt {number} for the coding agent** is on the Prompts tab"
                + (f" - {len(marks)} part(s) of it can be copied from the workspace" if marks else ""))
            self._point_at_prompts(f"Prompt {number} for your coding agent is on the Prompts tab.")
        self._turn_prompts = max(self._turn_prompts, len(found))
        self._update_buttons()  # with a prompt out, the agent's progress can be reported

    def _show_run(self, command: str) -> None:
        """Something the developer "runs next": he cannot, so the user is pointed at it on the Prompts tab."""
        run = self.run
        run.runs += 1
        self.prompts_view.add_run(run.runs, command)
        self.activity_view.append_markdown(
            "▶ **Something to run** - it is on the Prompts tab: run it in the agent's workspace and give him the "
            "output in the status field under the tabs (a snip or a line of text), then click *Agent finished*.")
        self._point_at_prompts("The developer wants something run - see the Prompts tab.")

    def _prompt_sent(self, number: int) -> None:
        """The user has given prompt `number` to the external agent (the small button on that prompt).

        Only now is it time for what the developer says with it: why he sent it, and what there is to say
        while the agent works. It was written with the rest of his turn and has been waiting for this.
        """
        run = self.run
        if run is None or number in run.sent:
            return
        run.sent.add(number)
        self.prompts_view.mark_sent(number)
        self.activity_view.append_markdown(f"✔ **Prompt {number} has been given to the coding agent**")
        run.activity.append(f"STATUS: prompt {number} has been given to the agent, which is now working on it.")
        words = run.held.get(number)
        if words is None:  # his turn is still being written: show what is there, the rest follows as it comes
            bubble = self.chat.add("answer", DEV_TITLE)
            bubble.set_status("still writing…")
            bubble.set_waiting(True)
            self._after_bubbles[number] = bubble
            self._render_lead()
        elif words:
            # It is all written already, but it is shown the way he would say it: flowing in, from the first
            # word at once, rather than dropped on the screen in one piece.
            self._reveal(self.chat.add("answer", DEV_TITLE), words)
        else:
            self._notice(f"Prompt {number} is marked as sent - he has nothing more to say about it.")

    def _reveal(self, bubble: MessageBubble, markdown: str) -> None:
        """Show text that is already complete the way a reply comes in: flowing, a word at a time.

        The first words are there immediately - nobody waits for anything - and the rest follows at about the
        pace of a model's stream.
        """
        pieces = [match.group(0) for match in REVEAL_PIECE.finditer(markdown.strip())]
        shown = min(REVEAL_FIRST, len(pieces))
        bubble.set_markdown("".join(pieces[:shown]))  # rendered now, not on the next timer tick
        if shown >= len(pieces):
            bubble.finish()
            return
        timer = QTimer(bubble)  # belongs to the bubble: it stops when the bubble goes

        def step() -> None:
            nonlocal shown
            shown += 1
            if shown >= len(pieces):
                timer.stop()
                bubble.set_markdown(markdown.strip())
                bubble.finish()
            else:
                bubble.update_text("".join(pieces[:shown]))

        timer.timeout.connect(step)
        timer.start(REVEAL_MS)

    def _point_at_prompts(self, notice: str) -> None:
        """Something new is on the Prompts tab: say so, and open it unless the user is reading another tab."""
        index = self.tabs.indexOf(self.prompts_view)
        self.tabs.setTabText(index, f"Prompts ({len(self.run.prompts) + self.run.runs})")
        if self.tabs.currentWidget() is self.plan_tab:  # nothing new comes in there after the first turn
            self.tabs.setCurrentIndex(index)
        self._notice(notice, 15000)

    def _send_status(self, agent: str) -> None:
        """Tell the developer where the external agent stands: "done" with his prompt, or still "working" on it.

        The screenshots (its latest activity) and the text in the status field go with it. Before he has given
        any prompt there is no agent at work, and this lets him go ahead on his own assumptions instead.
        """
        if not (self.main and self.main.running and self._directing()):
            self._notice("No developer session is running - start a Vibe-Coding task first.")
            return
        note, attachments = self.status.take()
        latest = len(self.run.prompts)
        if latest and latest not in self.run.sent:
            # The agent is at work on it, so it has evidently been sent; what went with sending is past.
            self.run.sent.add(latest)
            self.prompts_view.mark_sent(latest)
        self._send_to_agent("", False, note=note, attachments=attachments,
                            agent=agent if self.run.prompts else "")

    def _main_exit(self, code: int, stderr: str) -> None:
        if self._live_view is not None or self._speaking:
            self._main_text_finished()
        self._close_agent_replies("*I couldn't reply - my coding session has ended.*")
        if self.phase == Phase.STOPPED:
            self._set_phase(Phase.STOPPED)  # refresh buttons now that the process is gone
            return
        detail = f"\n\n```text\n{shorten(stderr, 3000)}\n```" if stderr else ""
        self.activity_view.append_markdown(f"❌ **The Claude CLI exited** (code {code}).{detail}", spaced=True)
        self.plan_view.append_markdown("❌ The coding agent exited - see *Coding activity* for details.")
        self._set_phase(Phase.FAILED)

    def _begin_coding(self, feedback: str) -> None:
        if not (self.main and self.main.running and self.run):
            return
        self.feedback_edit.clear()
        self._set_phase(Phase.CODING)
        # No tab switch: the user keeps reading the plan while the agent codes.
        self.plan_view.append_markdown("*Coding now - progress is in the Coding activity tab, the code in Files.*")
        self.activity_view.append_markdown("---\n\n**Plan accepted - coding starts.**", spaced=True)
        can_run = can_run_commands(self.settings.permission_mode)
        body = prompts.implement_prompt(str(self.run.workspace), can_run, self.run.language,
                                        str(self.run.manifest), feedback)
        self.main.send([{"type": "text", "text": body}])

    def _approve(self, feedback: str) -> None:
        if self.phase == Phase.AWAITING:
            self._begin_coding(feedback.strip())

    def _revise(self) -> None:
        feedback = self.feedback_edit.text().strip()
        if not feedback:
            self._notice("Type what should change in the plan first.")
            self.feedback_edit.setFocus()
            return
        if self.phase != Phase.AWAITING or not self.main:
            return
        self.feedback_edit.clear()
        self.plan_view.append_markdown(f"---\n\n**You:** {feedback}\n\n### Revised plan", spaced=True)
        self.main.send([{"type": "text", "text": prompts.revise_prompt(feedback)}])
        self._set_phase(Phase.ANALYZING)
        self.tabs.setCurrentIndex(0)

    def interrupt_main(self) -> None:
        if self.main and self.main.busy:
            self._interrupt_requested = True
            self.main.interrupt()

    def stop_main(self) -> None:
        if self.main and self.main.running:
            self._set_phase(Phase.STOPPED)  # set first so the exit handler knows it was on purpose
            self.main.stop()
            self.activity_view.append_markdown("■ **Stopped.** The session is saved; start a new task when ready.")

    def send_to_agent(self, interrupt_first: bool) -> None:
        """Give the coding agent what was heard (plus the attachments) as a new instruction."""
        if not (self.main and self.main.running and self.run):
            self._notice("No coding agent is running - start a task with Analyze & Solve first.")
            return
        self._with_heard(lambda heard, _context: self._send_to_agent(heard, interrupt_first))

    def _send_to_agent(self, heard: str, interrupt_first: bool, note: str | None = None,
                       attachments: list | None = None, agent: str = "") -> None:
        """`note` and `attachments` are a status from the user for a developer who directs an external agent
        ("" and nothing = just carry on), `agent` where that agent stands ("done", "working" or unsaid).
        Without them the attachments of the question panel are sent."""
        if not (self.main and self.main.running and self.run):
            return
        if not heard and self.ask.is_empty() and note is None:
            self._notice(NOTHING_TO_SEND)
            return
        if attachments is None:
            _typed, attachments = self.ask.take()
        self.run.messages += 1
        try:
            # Saved under the inputs folder: both sessions are allowed to read files there.
            imgs, files = atts.prepare(attachments, self.run.inputs / "messages" / f"m{self.run.messages:02d}", "Image")
        except OSError as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not save the attachments:\n{exc}")
            return
        self._deliver_to_agent(heard, imgs, files, interrupt_first, note=note or "", agent=agent)

    def _deliver_to_developer(self, heard: str, imgs: list, files: list, interrupt_first: bool,
                              heard_bubble: MessageBubble | None, note: str, said: str = "", agent: str = "") -> None:
        """Start the next turn of the developer who directs an external coding agent.

        He gets what the supervisor said (`heard`) and/or news from the coding agent's side: where the agent
        stands (`agent`: "done" with his prompt, still "working" on it, or unsaid), the user's note and
        screenshots of its latest activity, and which workspace files changed since he last looked.
        """
        run = self.run
        count = len(imgs) + len(files)
        changes = self._workspace_changes() if run.shared else ""
        answered, run.answered = run.answered, []
        run.turns += 1
        body = prompts.lead_turn_prompt(
            number=run.turns, heard=heard, note=note, changes=changes, said=said, agent=agent,
            attachments=atts.describe_inputs(imgs, files) if count else "",
            answered="\n\n".join(f"Q: {q}\nA: {a}" for q, a in answered),
        )
        mid_turn = self.main.busy and not interrupt_first  # he picks it up after the step he is on
        if interrupt_first and self.main.busy:
            self.main.interrupt()
        stands = {"done": "The agent has finished its turn.", "working": "The agent is still working."}.get(agent, "")
        extras = [f"📎 *{count} attachment(s) from the agent's side*"] if count else []
        extras += [f"📂 *{len(changes.splitlines())} file(s) changed in the workspace*"] if changes else []
        lines = [f"🎧 **Supervisor:** {heard}"] if heard else []
        lines += [f"{'✅' if agent == 'done' else '⏳'} **{stands}**"] if stands else []
        # The user's own note is shown as a neutral status: in the developer's world there is nobody but him,
        # the person he talks to ("you") and the agent.
        lines += [f"📝 **Status:** {note}"] if note else []
        shown = "\n\n".join(lines + extras) or "▶ *Go ahead*"
        token = self.main.send(atts.content_blocks(imgs, files, body))
        self.activity_view.append_markdown(shown.replace("\n\n", " · "))
        if heard_bubble is None:
            # The bubble's title already says who is speaking, so its text is just what was said.
            title = ("🎧 Supervisor → developer" if heard else "✅ Agent finished → developer" if agent == "done"
                     else "⏳ Agent working → developer" if agent == "working" else "📝 Status → developer")
            words = [part for part in (heard, note) if part] or ([] if extras or stands else ["Go ahead."])
            request = self.chat.add("agent", title + (" (interrupting)" if interrupt_first else ""),
                                    "\n\n".join(words + extras) or stands)
        else:
            request = heard_bubble
            request.set_status("· moves the work on - next step coming")
        reply = self.chat.add("answer", DEV_TITLE)
        reply.question = request
        reply.status_only = not heard  # nothing was said to him: he may take it in without saying anything
        reply.set_status("will reply right after the current step…" if mid_turn else
                         "thinking…" if heard else "reading it and checking the workspace…")
        reply.set_waiting(True)
        reply.setToolTip(f"What the developer says next ({self.settings.coding_model})")
        self._agent_replies[token] = reply
        # The same in the words the quick-answer model is briefed with:
        log = [f"SAID TO YOU (passed to your working session): {heard}"] if heard else []
        log += [f"STATUS: {stands.replace('its turn', 'its turn for your latest prompt')}"] if stands else []
        log += [f"STATUS: {note}"] if note else []
        log += [f"STATUS: the agent's chat or activity, or a run's output, came back ({count} item(s))"] if count else []
        log += [f"STATUS: files changed in the agent's workspace:\n{changes}"] if changes else []
        run.activity.append("\n".join(log) or "STATUS: nothing new was said; you carried on.")
        self._set_phase(Phase.DIRECTING)

    def _deliver_to_agent(self, heard: str, imgs: list, files: list, interrupt_first: bool,
                          heard_bubble: MessageBubble | None = None, note: str = "", said: str = "",
                          agent: str = "") -> None:
        """Hand a message to the coding session and set up the chat bubble its reply will appear in.

        `said` is the short first reaction the developer has already given in the chat (Ask shows it at once,
        because this session takes a while): the session is told, so that it does not acknowledge again.

        `heard_bubble` is given when Ask routed the message here: what was heard is already in the chat.
        """
        if self.run.directing:
            self._deliver_to_developer(heard, imgs, files, interrupt_first, heard_bubble, note, said, agent)
            return
        count = len(imgs) + len(files)
        message = prompts.heard_text(heard)
        extra = f"\n\nAttached:\n{atts.describe_inputs(imgs, files)}" if count else ""
        planning = self.phase in (Phase.ANALYZING, Phase.AWAITING)
        # Working on a copy of the user's folder: what he has changed there is not in front of the person he
        # answers until the user has brought it over, so he must not speak of it as done.
        unseen = self._project_state()[0]
        body = (prompts.revise_prompt(message + extra, chat_reply_first=True, said=said) if planning
                else prompts.steer_prompt(message, said, unseen) + extra)
        if interrupt_first and self.main.busy:
            self.main.interrupt()
        shown = heard or "(see attachments)"
        if count:
            shown += f" *+ {count} attachment(s)*"
        if planning:
            self._turn_text = ""
            self.plan_view.append_markdown(f"---\n\n🎧 **Heard:** {shown}\n\n### Revised plan", spaced=True)
        token = self.main.send(atts.content_blocks(imgs, files, body))
        label = "Heard → agent (interrupting)" if interrupt_first else "Heard → agent"
        self.activity_view.append_markdown(f"🗨️ **{label}:** {shown}")
        # In the chat: what was heard, then (above it) the developer's own reply once the agent picks it up.
        if heard_bubble is None:
            request = self.chat.add("agent", "🎧 Heard · change request" + (" (interrupting)" if interrupt_first else ""), shown)
        else:
            request = heard_bubble
            request.set_status("· work request - passed to the coding session")
        reply = self.chat.add("answer", DEV_TITLE)
        reply.question = request
        reply.set_status("replying…" if interrupt_first else "will reply right after the current step…")
        reply.set_waiting(True)
        reply.setToolTip(f"Reply from the coding session ({self.settings.coding_model})")
        self._agent_replies[token] = reply
        self.run.activity.append(f"THE USER ASKED YOU, WHILE YOU WERE CODING: {heard or '(see the attachments)'}")
        self._set_phase(Phase.ANALYZING if planning else Phase.CODING)

    # ------------------------------------------------------------------ quick answers
    def _retire_side(self) -> None:
        if self.side is not None:
            self._side_cost_done += self.side.total_cost
            self.side.stop()
            self.side.deleteLater()
            self.side = None
        while self._answers:  # answers that will never arrive now
            self._answers.popleft().finish()
        self._side_first = True
        self.stop_answer_btn.setEnabled(False)

    def _restart_side(self) -> None:
        self._retire_side()
        self._ensure_side()

    def _ensure_side(self):
        s = self.settings
        provider = s.side_provider
        if (self.side is not None and self.side.running and self.side.model == s.side_model
                and getattr(self.side, "provider", llm.CLAUDE_CODE) == provider):
            return self.side
        self._retire_side()
        claude = resolve_claude_cli(s)
        if provider == llm.CLAUDE_CODE and not claude:
            return None
        if provider != llm.CLAUDE_CODE and not llm.ready(s, provider)[0]:
            return None  # the chosen API has no key; ask_side falls back to telling the user
        if self.run:
            # With a folder given by the user, questions are answered from that folder - it is what the person
            # asking sees - and not from the developer's copy, which may be ahead of it.
            cwd, add_dirs = self.run.given or self.run.workspace, [self.run.inputs]
        else:
            cwd, add_dirs = (Path(s.runs_dir).expanduser() / "scratch"), []
            cwd.mkdir(parents=True, exist_ok=True)
            cwd = cwd.resolve()
        system = (prompts.LEAD_SIDE_SYSTEM if self._directing() else
                  prompts.SIDE_SYSTEM_GIVEN if self.run and self.run.given else prompts.SIDE_SYSTEM)
        system += prompts.name_note(s.dev_name)
        log_path = self.run.dir / "quick_answers.jsonl" if self.run else None
        if provider == llm.CLAUDE_CODE:
            side = ClaudeSession(
                claude_path=claude,
                cwd=cwd,
                model=s.side_model,
                effort=s.side_effort if supports_effort(s.side_model) else None,
                tools="Read,Glob,Grep",
                add_dirs=add_dirs,
                append_system_prompt=system,
                log_path=log_path,  # what it was sent and said
                persist=False,
                parent=self,
            )
        else:
            side = ApiAgentSession(
                provider=provider,
                api_key=llm.key_for(s, provider),
                cwd=cwd,
                model=s.side_model,
                tools="Read,Glob,Grep",  # read-only: the quick answer can look at the workspace, not change it
                add_dirs=add_dirs,
                append_system_prompt=system,
                log_path=log_path,
                parent=self,
            )

        def guarded(fn):
            def handler(*args):
                if side is self.side:
                    fn(*args)
            return handler

        side.text_started.connect(guarded(self._side_text_started))
        side.text_delta.connect(guarded(self._side_text_delta))
        side.tool_used.connect(guarded(self._side_tool))
        side.turn_finished.connect(guarded(self._side_turn))
        side.process_finished.connect(guarded(self._side_exit))
        side.start()
        self.side = side
        return side

    def _with_heard(self, action) -> None:
        """Collect what to send, then call action(heard, earlier_context).

        "Heard" is the selected transcript text if there is a selection, otherwise all new speech -
        including the sentence being spoken right now, which the listener is asked to close first.
        """
        selection = self.tx_view.textCursor().selectedText().replace(PARAGRAPH_SEP, "\n").strip()
        if selection:  # the user picked exactly which part of the transcript to send
            context = self._transcript_context()
            cursor = self.tx_view.textCursor()
            cursor.clearSelection()
            self.tx_view.setTextCursor(cursor)
            self._mark_transcript_asked()
            action(selection, context)
            return
        if self._pending_action is not None:
            return  # already waiting for the listener to close the current sentence
        speaking = bool(self._tx_partial) and not is_filler(self._tx_partial)  # "hmm…" isn't worth waiting for
        if speaking and self.listener is not None and self.listener.running:
            self._pending_action = action
            self.listener.cut()
            QTimer.singleShot(CUT_TIMEOUT_MS, self._finish_pending)
            return
        action(*self._take_heard())

    def _finish_pending(self) -> None:
        action, self._pending_action = self._pending_action, None
        if action is not None:
            action(*self._take_heard())

    def ask_side(self) -> None:
        """Quick answer from the fast model to what was heard (plus the attachments)."""
        self._with_heard(self._ask_side)

    def _ask_side(self, heard: str, heard_context: str) -> None:
        if not heard and self.ask.is_empty():
            self._notice(NOTHING_TO_SEND)
            return
        if not heard and self._directing() and self.main and self.main.running:
            # Nothing was said: attachments on their own are news from the coding agent's side, and reading
            # that is the developer's job, not a question for the quick model.
            self._send_to_agent("", False)
            return
        side = self._ensure_side()
        if side is None:
            if self.settings.side_provider != llm.CLAUDE_CODE:
                self._notice(llm.ready(self.settings, self.settings.side_provider)[1])
            else:
                self._claude()
            return
        text, attachments = self.ask.take()
        self._questions += 1
        base = self.run.inputs if self.run else HOME_DIR / "scratch"  # inputs: readable by both sessions
        try:
            imgs, files = atts.prepare(attachments, base / "questions" / f"q{self._questions:02d}", "Image")
        except OSError as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not save the attachments:\n{exc}")
            return
        body = self._side_body(question=text, imgs=imgs, files=files, heard=heard, heard_context=heard_context)
        self._side_first = False
        # Chat order is newest first: the question bubble goes in, then the (still empty) answer above it.
        question = heard or "*(the question is in the attachments)*"
        if attachments:
            question += f"\n\n*+ {len(attachments)} attachment(s)*"
        heard_title = "🎧 Supervisor" if self._directing() else "🎧 Heard"
        heard_bubble = self.chat.add("heard", heard_title if heard else "📎 Question", question)
        answer = self.chat.add("answer", DEV_TITLE)
        answer.question = heard_bubble
        answer.raw = ""  # everything the model has said so far (shown once it is clearly not a control marker)
        answer.forward = (heard, imgs, files)  # what to hand to the coding session if this is a work request
        answer.set_status("thinking…")
        answer.set_waiting(True)  # a spinner and "next answer is coming" until the first words are there
        answer.setToolTip(f"Quick answer ({self.settings.side_model})")
        self._answers.append(answer)
        side.send(atts.content_blocks(imgs, files, body))
        self.stop_answer_btn.setEnabled(True)

    def _side_body(self, *, question: str, imgs: list, files: list, heard: str, heard_context: str) -> str:
        """Build the message for the quick-answer session: the task, plan, work state and the question itself.
        Shared by the audio Ask and the typed message box (see _post_message)."""
        run = self.run
        # A request may be handed on to the coding session - only possible while one is alive.
        can_forward = bool(self.main and self.main.running and run)
        att_desc = atts.describe_inputs(imgs, files) if (imgs or files) else ""
        if run is not None and run.directing:
            fresh = run.activity[run.side_seen:]
            run.side_seen = len(run.activity)
            log = "\n\n".join(fresh)
            if len(log) > LEAD_LOG_CHARS:
                log = "(…earlier part omitted…)\n" + log[-LEAD_LOG_CHARS:]
            return prompts.lead_side_prompt(
                question=question, attachments=att_desc,
                first=self._side_first, task=run.task, inputs=run.inputs_md, workspace=str(run.workspace),
                shared=run.shared, working=self.phase in ACTIVE, prompt_open=run.prompt_open,
                run_open=run.run_open, log=log, heard=heard, heard_context=heard_context,
                can_forward=can_forward,
            )
        if run is not None:
            # Working on a copy of the user's folder: which of his changes the person asking can see. While
            # some are missing there he is "in the middle of" them, and his work log - which reads as finished
            # work, done somewhere they cannot see - is kept from him until everything is in the folder.
            pending, landed = self._project_state()
            activity = ""
            if not pending:
                fresh = run.activity[run.side_seen:]
                run.side_seen = len(run.activity)
                activity = "\n".join(fresh)
                if len(activity) > SIDE_ACTIVITY_CHARS:
                    activity = "(…earlier activity omitted…)\n" + activity[-SIDE_ACTIVITY_CHARS:]
            plan = None
            if run.plan and not run.side_plan_sent:
                plan, run.side_plan_sent = run.plan, True
            return prompts.side_prompt(
                question=question, attachments=att_desc,
                first=self._side_first, task=run.task, inputs=run.inputs_md,
                workspace=str(run.given or run.workspace), given=run.given is not None,
                pending=pending, landed=landed, doing=self._changes_in_progress() if pending else "",
                phase=self.phase.value, plan=plan if (plan or not self._side_first) else "(still being written)",
                activity=activity, touched=run.touched, heard=heard, heard_context=heard_context,
                can_forward=can_forward,
            )
        return prompts.side_prompt(
            question=question, attachments=att_desc,
            first=self._side_first, task=None, inputs="", workspace="", phase=self.phase.value,
            plan=None, activity="", touched=[], heard=heard, heard_context=heard_context,
            can_forward=False,
        )

    def _post_message(self, to_ai: bool, sender: str = "remote", text: str | None = None,
                      attachments: list | None = None) -> None:
        """A message typed on the remote page drops into the quick-answer panel. `to_ai`: ask the AI developer
        (answered in the panel, labeled as a teammate); otherwise just post it so the local user sees it (the
        chat is mirrored to the remote page). The remote page passes its own text/attachments explicitly."""
        text = text or ""
        attachments = attachments or []
        if not text and not attachments:
            self._notice("Type a message or attach something first.")
            return
        role, title = ("local", "💻 Local") if sender == "local" else ("remote", "🖥 Remote assistant")
        shown = text or "*(see the attachments)*"
        if attachments:
            shown += f"\n\n*+ {len(attachments)} attachment(s)*"
        posted = self.chat.add(role, title, shown)
        posted.finish()
        if not to_ai:
            return
        side = self._ensure_side()
        if side is None:
            if self.settings.side_provider != llm.CLAUDE_CODE:
                self._notice(llm.ready(self.settings, self.settings.side_provider)[1])
            else:
                self._claude()
            return
        self._questions += 1
        base = self.run.inputs if self.run else HOME_DIR / "scratch"
        try:
            imgs, files = atts.prepare(attachments, base / "questions" / f"q{self._questions:02d}", "Image")
        except OSError as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not save the attachments:\n{exc}")
            return
        body = self._side_body(question=prompts.teammate_prefix(sender) + text, imgs=imgs, files=files,
                               heard="", heard_context="")
        self._side_first = False
        answer = self.chat.add("answer", DEV_TITLE)
        answer.question = posted
        answer.raw = ""
        answer.forward = (text, imgs, files)  # a work request is handed to the coding session like any other
        answer.set_status("thinking…")
        answer.set_waiting(True)
        answer.setToolTip(f"Quick answer ({self.settings.side_model})")
        self._answers.append(answer)
        side.send(atts.content_blocks(imgs, files, body))
        self.stop_answer_btn.setEnabled(True)

    def _answer_bubble(self) -> MessageBubble:
        """The bubble the quick model is currently answering into (oldest unanswered question first)."""
        if not self._answers:  # text nobody asked for (should not happen): still show it
            bubble = self.chat.add("answer", DEV_TITLE)
            bubble.raw = ""
            self._answers.append(bubble)
        return self._answers[0]

    def _side_tool(self, block: dict, _from_subagent: bool) -> None:
        """The quick model is looking something up for its answer."""
        bubble = self._answer_bubble()
        bubble.looked_last = True  # until it says something again: an answer must not end on a look-up
        name = Path(str((block.get("input") or {}).get("file_path") or "")).name
        bubble.set_status(f"looking at {name or 'the code'}…")

    def _side_text_started(self) -> None:
        bubble = self._answer_bubble()
        bubble.looked_last = False
        if bubble.raw.strip():
            bubble.raw += "\n\n"  # a second text block of the same answer (e.g. after it read a file)

    def _side_text_delta(self, text: str) -> None:
        bubble = self._answer_bubble()
        if getattr(bubble, "passed_on", False):
            return  # its first words are said and the message is with the working session: the rest is not wanted
        bubble.raw += text
        if reply_mark(bubble.raw) == prompts.AGENT_MARK:
            # It is being passed on to the working session, which takes a while: what the developer says
            # in the meantime ("Understood - let me think that through.") is shown as it comes in.
            said, finished = held_line(bubble.raw)
            if finished and self._can_pass_on(bubble):
                self._pass_on(bubble, said)
                if self.side is not None and self.side.busy:
                    self.side.interrupt()  # those first words were all that was asked of it
            elif said:
                bubble.set_status("")
                bubble.update_text(said)
        elif not may_become_skip(bubble.raw):  # hold the text back while it could still be just "[skip]"
            bubble.set_status("")
            bubble.update_text(bubble.raw)

    def _can_pass_on(self, bubble: MessageBubble) -> bool:
        """Is there a working session to hand this question to?"""
        return bool(self.main and self.main.running and self.run and hasattr(bubble, "forward"))

    def _pass_on(self, bubble: MessageBubble, said: str) -> None:
        """The quick model judged that this is for the working session: hand it over.

        `said`, its short first reaction, stays in the chat; the session's full reply appears above it.
        """
        bubble.passed_on = True
        heard, imgs, files = bubble.forward
        if said:
            bubble.set_markdown(said)
            bubble.finish()
        else:
            self.chat.remove(bubble)
        self._deliver_to_agent(heard, imgs, files, False, heard_bubble=bubble.question, said=said)

    def _drop_skipped(self, bubble: MessageBubble, note: str) -> None:
        """The model judged there was nothing to answer: no reply bubble, just a note on what was heard."""
        question = bubble.question
        self.chat.remove(bubble)
        if question is not None:
            question.set_status(note)

    def _side_turn(self, ev: dict) -> None:
        if self._answers:
            bubble = self._answers.popleft()
            failed = bool(ev.get("is_error"))
            mark = None if failed else reply_mark(bubble.raw)
            unfinished = getattr(bubble, "looked_last", False) and not getattr(bubble, "passed_on", False)
            if unfinished and not failed and not getattr(bubble, "nudged", False) and self.side is not None:
                # It read a file and stopped without answering ("Let me check..." and nothing more): ask once
                # for the answer itself, into the same bubble.
                bubble.nudged, bubble.looked_last, bubble.raw = True, False, ""
                bubble.update_text("")
                bubble.set_status("finishing the answer…")
                bubble.set_waiting(True)
                self._answers.appendleft(bubble)
                self.side.send([{"type": "text", "text": prompts.FINISH_ANSWER}])
                return
            if getattr(bubble, "passed_on", False):
                pass  # already handed to the working session while its first line was coming in
            elif mark == prompts.SKIP_MARK:
                self._drop_skipped(bubble, "· not a question - no answer needed")
            elif mark == prompts.AGENT_MARK and self._can_pass_on(bubble):
                # A work request, not a question: the quick model passes it on to the coding session.
                self._pass_on(bubble, held_line(bubble.raw)[0])
            elif mark == prompts.AGENT_MARK:
                bubble.set_markdown("I can't change the code right now - no coding session is running. "
                                    "Start a task first and ask me again.")
                bubble.finish()
            else:
                bubble.set_markdown(bubble.raw)
                bubble.finish()
                if failed and ev.get("subtype") != "error_during_execution":
                    bubble.set_markdown(f"{bubble.raw}\n\n❌ {ev.get('result') or ev.get('subtype')}")
                elif not bubble.raw.strip():
                    bubble.set_markdown("*(stopped)*" if failed else "*(no answer)*")
                elif not failed and self._directing() and getattr(bubble, "forward", ("",))[0]:
                    # The developer's working session is told what was answered on his behalf, with his
                    # next message, so that he does not contradict it later.
                    self.run.answered.append((bubble.forward[0], bubble.raw.strip()))
        if self.side is None or not self.side.busy:
            # Questions asked while an answer was running were folded into that answer by the CLI.
            while self._answers:
                leftover = self._answers.popleft()
                leftover.set_markdown("*Answered together with the message below.*")
                leftover.finish()
            self.stop_answer_btn.setEnabled(False)

    def _side_exit(self, code: int, stderr: str) -> None:
        while self._answers:
            self._answers.popleft().finish()
        self.stop_answer_btn.setEnabled(False)
        if stderr:
            self.chat.add("error", "❌ Quick-answer model stopped",
                          f"The CLI exited (code {code}): {code_span(shorten(stderr, 500))}")
        self._retire_side()  # the next question starts a fresh one

    # ------------------------------------------------------------------ PC audio transcript
    def _toggle_listening(self, on: bool) -> None:
        self.listen_btn.setText("🎧 PC audio: on" if on else "🎧 PC audio: off")
        if on:
            self._start_listening()
        else:
            self._stop_listening()
        if self.settings.listen_audio != on:
            self.settings.listen_audio = on
            self.settings.save()

    def _start_listening(self) -> None:
        if self.listener is not None and self.listener.running:
            return
        model = SPEECH_MODELS.get(self.settings.speech_model) or SPEECH_MODELS[DEFAULT_SPEECH_MODEL]
        listener = SystemAudioListener(model, parent=self)

        def guarded(fn):
            def handler(*args):
                if listener is self.listener:
                    fn(*args)
            return handler

        listener.partial.connect(guarded(self._on_heard_partial))
        listener.final.connect(guarded(self._on_heard_final))
        listener.level.connect(guarded(lambda value: self.level_bar.setValue(int(value * 100))))
        listener.status.connect(guarded(self._on_audio_status))
        listener.failed.connect(guarded(self._on_listen_failed))
        self.listener = listener
        self.audio_status.setStyleSheet("color:#9aa4b2;")
        listener.start()

    def _stop_listening(self) -> None:
        listener, self.listener = self.listener, None
        if listener is not None:
            listener.stop()
        self.level_bar.setValue(0)
        if self._tx_partial and not is_filler(self._tx_partial):  # keep the words heard so far
            self._tx.append([self._tx_partial, False])
            self._tx_partial = ""
        self.audio_status.setText("Not listening")
        self._schedule_transcript()
        self._finish_pending()

    def _on_audio_status(self, text: str) -> None:
        self._audio_status_text = text
        self._show_audio_status()

    def _show_audio_status(self) -> None:
        text = self._audio_status_text
        if self._fillers_skipped and text.startswith("Listening"):
            text += f"  ·  {self._fillers_skipped} filler line{'s' * (self._fillers_skipped != 1)} skipped"
        self.audio_status.setText(text)
        self.audio_status.setToolTip(text + "\nLines with nothing to answer (\"hmm\", \"okay\", \"thank you\"…) "
                                            "are left out of the transcript and never sent.")

    def _on_listen_failed(self, message: str) -> None:
        self.listener = None
        self.level_bar.setValue(0)
        self.audio_status.setStyleSheet("color:#f85149;")
        self.audio_status.setText(f"PC audio stopped: {message}")
        self.audio_status.setToolTip(message)
        self.listen_btn.blockSignals(True)  # keep the saved preference: it is retried on the next start
        self.listen_btn.setChecked(False)
        self.listen_btn.setText("🎧 PC audio: off")
        self.listen_btn.blockSignals(False)
        self._finish_pending()

    def _on_heard_partial(self, text: str) -> None:
        self._tx_partial = text
        self._schedule_transcript()

    def _on_heard_final(self, text: str, cut: bool) -> None:
        self._tx_partial = ""
        lead = len(text) - len(text.lstrip("?.!,;:"))
        if lead and self._tx:  # a sentence's closing "?" sometimes lands at the start of the next one
            self._tx[-1][0] += text[:lead]
            text = text[lead:].strip()
        if text and is_filler(text):  # "hmm", "okay", "thank you"…: nothing to answer, keep it out
            text = ""
            self._fillers_skipped += 1
            self._show_audio_status()
        if text:
            # A sentence closed for an Ask/Send that already timed out was included in that message.
            self._tx.append([text, cut and self._pending_action is None])
        self._schedule_transcript()
        if cut:
            self._finish_pending()

    def _transcript_context(self) -> str:
        return "\n".join(text for text, asked in self._tx if asked)[-HEARD_CONTEXT_CHARS:]

    def _mark_transcript_asked(self) -> None:
        for line in self._tx:
            line[1] = True
        self._schedule_transcript()

    def _take_heard(self) -> tuple[str, str]:
        """New (not yet asked) speech, plus earlier speech as context; marks it all as asked."""
        context = self._transcript_context()
        lines = [text for text, asked in self._tx if not asked]
        if self._tx_partial and not is_filler(self._tx_partial):  # sentence not closed in time: use what we have
            lines.append(self._tx_partial)
        self._mark_transcript_asked()
        return "\n".join(lines).strip(), context

    def _schedule_transcript(self) -> None:
        if not self._tx_timer.isActive():
            self._tx_timer.start(80)

    def _render_transcript(self) -> None:
        self._update_buttons()  # new speech is something to send; speech already sent is not
        if self.tx_view.textCursor().hasSelection():
            self._tx_timer.start(300)  # don't wipe the user's selection; refresh once it is gone
            return
        del self._tx[:-400]
        rows = []
        for text, asked in self._tx[-TRANSCRIPT_LINES_SHOWN:]:
            color = "#6e7681" if asked else "#e6edf3"
            rows.append(f"<p style='margin:0 0 4px 0; color:{color}'>{html.escape(text)}</p>")
        if self._tx_partial:
            rows.append(f"<p style='margin:0; color:#79c0ff'><i>{html.escape(self._tx_partial)} …</i></p>")
        if not rows:
            rows.append("<p style='color:#6e7681'>Speech playing on this PC appears here live (transcribed on this "
                        "PC, nothing is uploaded). This is the question: add screenshots or files below if "
                        "needed, then click ⚡ Ask for an instant answer.</p>")
        bar = self.tx_view.verticalScrollBar()
        stick = bar.value() >= bar.maximum() - 8
        self.tx_view.setHtml("".join(rows))
        if stick:
            bar.setValue(bar.maximum())

    # ------------------------------------------------------------------ files tab
    def refresh_files(self) -> None:
        """Rebuild the explorer (with each file's status colour) and refresh the open file."""
        if self.run is None:
            return
        files = self._workspace_files()
        statuses = {rel: self._file_status(rel) for rel in files}
        self.file_tree.populate(self.run.workspace, files, statuses)
        current = self.file_tree.current_file()
        if current is not None:
            self._show_file(current)
        elif files:
            # Open the most interesting file first: one the agent edited, else one it created.
            first = next((r for r in files if statuses[r] == MODIFIED), None) \
                or next((r for r in files if statuses[r] == NEW), files[0])
            self.file_tree.select(first)  # emits file_selected -> _show_file

    def _show_file(self, rel: str) -> None:
        if not rel or self.run is None:
            return
        path = self.run.workspace / rel
        self.file_label.setText("  ›  ".join(Path(rel).parts))
        self.file_label.setToolTip(str(path))
        try:
            too_large = path.stat().st_size > 2_000_000
            raw = b"" if too_large else path.read_bytes()
        except OSError as exc:
            self.code_view.set_code(f"(cannot read: {exc})")
            self._shown_file = None
            self._update_buttons()
            return
        if too_large or b"\x00" in raw[:8192]:
            self.code_view.set_code("(file too large to preview)" if too_large else "(binary file)")
            self._shown_file = None
            self._update_buttons()
            return
        status = self._file_status(rel)
        shown = (rel, content_hash(raw), status)
        if self._shown_file == shown:
            return  # unchanged: keep the scroll position and selection
        same_file = self._shown_file is not None and self._shown_file[0] == rel
        scroll = self.code_view.verticalScrollBar().value()
        text = raw.decode("utf-8", errors="replace")
        # Fade what was already there, keep what the agent added or edited at full brightness.
        untouched: set[int] | None = None  # None = nothing to compare with (a new file): everything normal
        if status == ORIGINAL:
            untouched = set(range(text.count("\n") + 1))
        elif status == MODIFIED:
            original = self.run.original_text(file_key(rel, self.run.workspace))
            if original is not None and text.count("\n") <= 8000:
                untouched = unchanged_lines(original, text)
        self.code_view.set_code(text, path.name, untouched)
        if same_file:  # the agent edited the file being viewed: stay where the user was reading
            self.code_view.verticalScrollBar().setValue(scroll)
        self._shown_file = shown
        self._update_buttons()

    def _copy_file(self) -> None:
        QGuiApplication.clipboard().setText(self.code_view.toPlainText())
        self._notice("Copied to the clipboard.")

    def _toggle_explorer(self, shown: bool) -> None:
        self.explorer_pane.setVisible(shown)
        self.explorer_toggle.setText("◀ Explorer" if shown else "▶ Explorer")

    # ------------------------------------------------------------------ workspace & settings
    def _current_workspace(self) -> Path:
        if self.run is not None:
            return self.run.workspace
        if self.ws_edit.text().strip():
            return Path(self.ws_edit.text().strip()).expanduser()
        return Path(self.settings.runs_dir).expanduser()

    def _browse_workspace(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Workspace folder", self.ws_edit.text() or str(Path.home()))
        if folder:
            self.ws_edit.setText(folder)

    def _open_workspace(self) -> None:
        path = self._current_workspace()
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self.settings, self)
        try:
            if dialog.exec():
                old_side = (self.settings.side_model, self.settings.side_effort)
                old_speech = self.settings.speech_model
                dialog.apply()
                self._apply_settings()
                if old_side != (self.settings.side_model, self.settings.side_effort) and self.side is not None:
                    self._restart_side()
                if audio_supported():
                    if self.settings.speech_model != old_speech and self.listen_btn.isChecked():
                        self._stop_listening()
                        self._start_listening()
                    self.listen_btn.setChecked(self.settings.listen_audio)
                self._notice("Settings saved - coding model changes apply to the next task.")
        finally:
            dialog.deleteLater()  # a closed dialog must not live on, hidden: one more is made for every opening

    # ------------------------------------------------------------------ shutdown
    def _stop_sessions(self) -> None:
        if self.main is not None:
            self.main.stop()
            self.main.deleteLater()
            self.main = None
        self._close_agent_replies("*I couldn't reply - that task was closed.*")
        self._retire_side()

    def closeEvent(self, event) -> None:
        if self.main is not None and self.main.busy:
            answer = QMessageBox.question(self, APP_NAME, "The coding agent is still working. Stop it and quit?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        self.settings.workspace = self.ws_edit.text().strip()
        self.settings.review_plan = self.review_check.isChecked()
        self.settings.language = self.lang_combo.currentText()
        self.settings.main_split = self.main_split.sizes()
        self.settings.save()
        self.hotkeys.unregister_all()
        if self.listener is not None:
            self.listener.stop()
            self.listener = None
        self._stop_sessions()
        event.accept()
