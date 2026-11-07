"""Settings persistence and Claude Code CLI discovery."""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

APP_NAME = "ScreenCoder"
HOME_DIR = Path(os.environ.get("SCREENCODER_HOME") or (Path.home() / "ScreenCoder"))
SETTINGS_FILE = HOME_DIR / "settings.json"

CODING_MODELS = ["claude-opus-4-8", "claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"]
SIDE_MODELS = ["claude-opus-4-8", "claude-sonnet-4-6", "claude-haiku-4-5", "claude-sonnet-5-5"]
EFFORT_LEVELS = ["low", "medium", "high", "xhigh", "max"]
# The kinds of session (the Mode box), each with its own flow, all in the language of the code at hand:
# "DSA" - a self-contained problem to solve: an algorithm, a SQL query, a function to fix;
# "Pair Programming" - an existing codebase, changed request by request while he talks it through;
# "Code Review" - he reviews given code, findings first, then fixes what he is asked to;
# "Vibe-Coding" - he writes no code himself: he directs a coding agent outside this app;
# "System Design" - no code: he designs a system (leading it step by step) or discusses one, with Mermaid diagrams.
# "Auto" (the default) - he judges which of the first four a task is, from what was said and shown.
MODE_DSA, MODE_PAIR, MODE_REVIEW, MODE_VIBE = "DSA", "Pair Programming", "Code Review", "Vibe-Coding"
MODE_DESIGN, MODE_AUTO = "System Design", "Auto"
MODES = {MODE_AUTO: "auto", MODE_DSA: "dsa", MODE_PAIR: "pair", MODE_REVIEW: "review", MODE_DESIGN: "design",
         MODE_VIBE: "vibe"}
# The code is written in the language the problem or the codebase is in (prompts.language_note).
LANG_OTHER = "Other"
# Where the code really is for the person he talks to (the "Code in" box): he speaks of it that way
# (prompts.place_note). His own working folder is never theirs to hear about.
PLACES = ["Local IDE", "Online IDE", "GitHub"]


def mode_of(label: str) -> str:
    """The kind of session a Mode choice means: "auto", "dsa", "pair", "review", "design" or "vibe"."""
    return MODES.get(label, "auto")


TEXT_SCALES = [1.0, 1.15, 1.25, 1.35, 1.5, 1.75, 2.0, 2.5]
PERMISSION_MODES = {
    "acceptEdits": "Edit files only - shell commands are denied",
    "auto": "Auto mode - a classifier approves safe commands",
    "bypassPermissions": "Full access - edit files and run any command",
}


def supports_effort(model: str) -> bool:
    """Haiku 4.5 rejects the effort parameter; every current Opus/Sonnet/Fable model accepts it."""
    return "haiku" not in model.lower()


def can_skip_thinking(model: str) -> bool:
    """Can the CLI switch thinking off for this model (set_max_thinking_tokens 0)? The 4.x models; on the 5.x
    and Fable models thinking cannot be disabled."""
    return model.startswith("claude-") and "-4-" in model


def can_run_commands(permission_mode: str) -> bool:
    return permission_mode in ("auto", "bypassPermissions")


@dataclass
class Settings:
    claude_path: str = ""
    coding_model: str = "claude-opus-4-8"
    # Vibe-Coding only. A coding task's agent picks its level itself: it judges each piece of work (main_window
    # COPY_EFFORT, _work_effort).
    coding_effort: str = "xhigh"
    side_model: str = "claude-opus-4-8"  # measured: first words ~1 s on a warm session, as quick as Sonnet
    side_effort: str = "medium"
    permission_mode: str = "acceptEdits"
    mode: str = MODE_AUTO  # the Mode box (a key of MODES)
    code_place: str = "Local IDE"  # the "Code in" box (one of PLACES)
    # When the person moves on to a new task ("next question", "now let's build it"), the session moves on to a
    # task of its own by itself. Off: "＋ Next task" does it.
    auto_next_task: bool = False
    # A name for the AI developer (coding agent + quick answers). The person talking to it (the interviewer)
    # may address it by this name; empty means no name. Woven into the system prompt (see prompts.name_note).
    dev_name: str = ""
    # Size of the plan, activity, transcript and chat text (1.0 = normal UI text). Named differently from the
    # first version of this setting ("text_scale", default 2.0) so that old saved value no longer applies.
    reading_scale: float = 1.5
    main_split: list = field(default_factory=lambda: [600, 900])  # widths of the Q&A panel and the code side
    show_thinking: bool = True
    # Screenshots that scroll through the same view are sent as only the lines each one adds (the "New part
    # only" box next to Screen). Off by default: anything else that changed in the overlap is not sent.
    new_part_only: bool = False
    hotkeys_enabled: bool = True
    stealth: bool = False  # hide the window from screen capture, taskbar and Alt+Tab (visible to the user)
    always_on_top: bool = False  # keep the window above all other apps
    # The mouse goes through the window to the apps under it (they get the clicks, scrolling and typing).
    click_through: bool = False
    opacity: float = 0.85  # window transparency, 0.2-1.0 (1.0 = fully opaque)
    # Remote control: the window can be seen and driven from a browser on another machine (see remote.py).
    remote_enabled: bool = False
    remote_reach: str = "lan"  # "lan": this machine's network; "internet": a public https link (see tunnel.py)
    remote_port: int = 8765
    remote_key: str = ""  # whoever has it can use the app while remote control is on; made on first use
    # The remote page may also click, type and scroll on this PC - its whole screen, not only the app. Off by
    # default: with it on, whoever has the link can use the PC (pc_control.py).
    remote_pc_control: bool = False
    listen_audio: bool = True
    speech_model: str = "kroko"
    runs_dir: str = str(HOME_DIR / "runs")
    workspace: str = ""
    # LLM providers. Coding and the quick-answer (Ask) part each pick a back end: "claude-code" (the CLI),
    # "openai" or "anthropic" (their APIs, with the keys below). See llm.py.
    openai_key: str = ""
    anthropic_key: str = ""
    openai_models: list = field(default_factory=lambda: ["gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4o"])
    coding_provider: str = "claude-code"
    side_provider: str = "claude-code"       # the quick-answer part (the Ask button)

    @classmethod
    def load(cls) -> "Settings":
        settings = cls()
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return settings
        known = {f.name for f in fields(cls)}
        for key, value in data.items():
            if key in known and isinstance(value, type(getattr(settings, key))):
                setattr(settings, key, value)
        if settings.mode not in MODES:
            settings.mode = MODE_AUTO
        if settings.code_place not in PLACES:
            settings.code_place = PLACES[0]
        if "mode" not in data:
            # Before the Mode box, the kind of session was picked among the programming languages ("language"):
            # a language meant a problem to solve.
            old = data.get("language")
            settings.mode = MODE_VIBE if old in ("Non-coding", MODE_VIBE) else old if old in MODES else MODE_DSA
        return settings

    def save(self) -> None:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def _extension_version(path: str) -> tuple[int, ...]:
    match = re.search(r"claude-code-(\d+(?:\.\d+)*)", path)
    return tuple(int(x) for x in match.group(1).split(".")) if match else ()


def find_claude_cli() -> str | None:
    """Locate the `claude` executable: PATH, the native installer, then editor extension bundles."""
    found = shutil.which("claude")
    if found:
        return found
    exe = "claude.exe" if sys.platform == "win32" else "claude"
    home = Path.home()
    for candidate in (
        home / ".local" / "bin" / exe,
        home / ".claude" / "local" / exe,
        Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd",
    ):
        if candidate.is_file():
            return str(candidate)
    # The VS Code / Cursor / Windsurf extensions ship a full CLI binary; pick the newest one.
    bundled: list[str] = []
    for editor in (".vscode", ".vscode-insiders", ".cursor", ".windsurf", ".vscode-server"):
        pattern = home / editor / "extensions" / "anthropic.claude-code-*" / "resources" / "native-binary" / exe
        bundled += [p for p in glob.glob(str(pattern)) if os.path.getsize(p) > 0]
    return max(bundled, key=_extension_version) if bundled else None


def resolve_claude_cli(settings: Settings) -> str | None:
    if settings.claude_path and Path(settings.claude_path).is_file():
        return settings.claude_path
    return find_claude_cli()
