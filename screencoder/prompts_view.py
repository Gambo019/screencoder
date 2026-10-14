"""The Prompts tab: what the developer writes for the external coding agent, and what he wants run.

It is there to be read and typed off, newest first. Stretches of a prompt that already exist in the agent's
workspace are highlighted, each with the file and line it can be copied from. Every prompt has a small button
to click once it has been given to the agent: only then does the developer say why he sent it and what there
is to say while the agent works.
"""
from __future__ import annotations

import html
from dataclasses import dataclass, field
from datetime import datetime

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from .chat_view import AutoHeightBrowser
from .copy_marks import Mark
from .widgets import code_point_size, scaled_font

PROMPT_BG = "#10263f"
RUN_BG = "#161b22"
COPY_BG, COPY_FG = "#1c4a2e", "#b4f5c0"  # text that is already in the workspace
TAG_FG = "#8b949e"
LEGEND = ("<span style='background-color:{bg}; color:{fg};'>&nbsp;green&nbsp;</span> is already in the agent's "
          "workspace: copy it from the file and line shown after it instead of typing it.").format(bg=COPY_BG, fg=COPY_FG)
PLACEHOLDER = ("The developer's prompts for your coding agent appear here, newest at the top - to read and type "
               "off. What he says out loud is in the panel on the left.")
SENT_LABEL = "✔ Sent to the agent"
SENT_TIP = ("Click once you have given this prompt to your agent. The developer then says, in the panel on the "
            "left, why he sent it and what there is to say while the agent works.")
LIVE_TITLE = "✍️ Writing the next prompt…"


@dataclass
class _Entry:
    kind: str  # "prompt" | "run"
    number: int
    text: str
    marks: list[Mark] = field(default_factory=list)
    stamp: str = field(default_factory=lambda: datetime.now().strftime("%H:%M:%S"))
    sent: str = ""  # when the prompt was given to the agent (clock time), once it has been

    def title(self) -> tuple[str, str]:
        """(name, details) for the header line."""
        if self.kind == "run":
            return f"▶ Run {self.number}", f"{self.stamp} · run this in the agent's workspace and give him the output"
        parts = [self.stamp, f"{len(self.text.split())} words"]  # typed by hand: show how much there is to type
        if self.marks:
            parts.append(f"{len(self.marks)} part{'s' * (len(self.marks) != 1)} already in the workspace")
        return f"✉️ Prompt {self.number}", " · ".join(parts)


def _marked(text: str, marks: list[Mark]) -> str:
    """`text` as HTML, with the copyable stretches highlighted and followed by where they come from."""
    out, position = [], 0
    for mark in sorted(marks, key=lambda m: m.start):
        if mark.start < position:
            continue
        out.append(html.escape(text[position:mark.start]))
        out.append(f"<span style='background-color:{COPY_BG}; color:{COPY_FG};'>"
                   f"{html.escape(text[mark.start:mark.end])}</span>"
                   f"<span style='color:{TAG_FG}; font-size:small;'>&nbsp;⟨{html.escape(mark.where)}⟩</span>")
        position = mark.end
    out.append(html.escape(text[position:]))
    return "".join(out)


class _Card(QFrame):
    """One entry: a header line (name, details and - for a prompt - its "sent" button) over the text itself."""

    def __init__(self, name: str, details: str, accent: str, background: str, parent=None):
        super().__init__(parent)
        self.setObjectName("promptCard")
        self.setStyleSheet(f"QFrame#promptCard {{ background: {background}; border-radius: 5px; }}")
        self.accent = accent
        self.name = QLabel(name)
        self.details = QLabel(details)
        self.details.setStyleSheet(f"color: {TAG_FG};")
        self.details.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)  # clipped, never forcing width
        self.head = QHBoxLayout()
        self.head.addWidget(self.name)
        self.head.addWidget(self.details, 1)
        self.body = AutoHeightBrowser()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(3)
        layout.addLayout(self.head)
        layout.addWidget(self.body)
        self.button: QPushButton | None = None

    def set_body(self, body_html: str, mono_pt: float | None = None) -> None:
        font = f" font-family:Consolas,'Courier New',monospace; font-size:{mono_pt:.1f}pt;" if mono_pt else ""
        self.body.setHtml(f"<div style='white-space:pre-wrap;{font}'>{body_html}</div>")

    def plain_text(self) -> str:
        return f"{self.name.text()} · {self.details.text()}\n{self.body.toPlainText()}"


class PromptsView(QScrollArea):
    sent = Signal(int)  # the user has given prompt <number> to the agent

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea { background: #0d1117; border: 1px solid #30363d; }")
        inner = QWidget()
        inner.setObjectName("promptsInner")
        inner.setStyleSheet("QWidget#promptsInner { background: #0d1117; }")
        self._layout = QVBoxLayout(inner)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(10)
        self._note = QLabel(PLACEHOLDER)  # the placeholder while empty, the colour legend once there are prompts
        self._note.setWordWrap(True)
        self._note.setStyleSheet(f"color: {TAG_FG};")
        self._layout.addWidget(self._note)
        self._live = _Card(LIVE_TITLE, "", "#e3b341", PROMPT_BG)  # the prompt he is writing right now
        self._live.hide()
        self._layout.addWidget(self._live)
        self._layout.addStretch(1)
        self.setWidget(inner)
        self._entries: list[_Entry] = []
        self._cards: list[_Card] = []  # same order as _entries (oldest first); shown newest first
        self._scale = 1.0

    # ----------------------------------------------------------------- content
    def reset(self) -> None:
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._entries.clear()
        self._cards.clear()
        self._live.hide()
        self._note.setText(PLACEHOLDER)

    def set_text_scale(self, scale: float) -> None:
        self._scale = scale
        for card, entry in zip(self._cards, self._entries):
            self._style(card, entry)
        self._style(self._live, None)

    def add_prompt(self, number: int, text: str, marks: list[Mark]) -> None:
        for card, entry in zip(self._cards, self._entries):
            if card.button is not None and not entry.sent:
                card.button.setEnabled(False)  # a newer prompt has replaced it: only the latest can be sent
        self._live.hide()
        self._add(_Entry("prompt", number, text, marks))

    def add_run(self, number: int, text: str) -> None:
        self._add(_Entry("run", number, text))

    def set_live(self, text: str | None) -> None:
        """Show (or stop showing) the prompt the developer is writing right now."""
        if text is None:
            self._live.hide()
            return
        self._live.set_body(html.escape(text) or "&nbsp;")
        self._live.show()

    def mark_sent(self, number: int) -> None:
        """Prompt `number` has been given to the agent: its button has done its job."""
        for card, entry in zip(self._cards, self._entries):
            if entry.kind == "prompt" and entry.number == number and card.button is not None:
                entry.sent = datetime.now().strftime("%H:%M:%S")
                card.button.setEnabled(False)
                card.button.setText(f"✓ Sent {entry.sent}")

    def sent_button(self, number: int) -> QPushButton | None:
        """The "sent" button of prompt `number`, if that prompt is shown."""
        for card, entry in zip(self._cards, self._entries):
            if entry.kind == "prompt" and entry.number == number:
                return card.button
        return None

    def toPlainText(self) -> str:  # noqa: N802 - same name as the text widgets, for whoever reads the tab as text
        if not self._entries and self._live.isHidden():
            return PLACEHOLDER
        parts = ["green is already in the agent's workspace: copy it from the file and line shown after it instead "
                 "of typing it."]
        if not self._live.isHidden():
            parts.append(f"{LIVE_TITLE}\n{self._live.body.toPlainText()}")
        parts += [card.plain_text() for card in reversed(self._cards)]
        return "\n\n".join(parts)

    # ----------------------------------------------------------------- building
    def _add(self, entry: _Entry) -> None:
        name, details = entry.title()
        prompt = entry.kind == "prompt"
        card = _Card(name, details, "#58a6ff" if prompt else "#e3b341", PROMPT_BG if prompt else RUN_BG)
        if prompt:
            button = QPushButton(SENT_LABEL)
            button.setToolTip(SENT_TIP)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.clicked.connect(lambda _checked=False, number=entry.number: self.sent.emit(number))
            card.head.addWidget(button)
            card.button = button
        self._entries.append(entry)
        self._cards.append(card)
        self._style(card, entry)
        self._layout.insertWidget(2, card)  # under the note and the prompt being written: newest first
        self._note.setText(LEGEND)
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(0))

    def _style(self, card: _Card, entry: _Entry | None) -> None:
        """Fonts for the current text size, and the entry's text (again) in them."""
        small = max(8.0, scaled_font(self, self._scale * 0.7).pointSizeF())
        head = scaled_font(self, max(1.0, self._scale * 0.85)).pointSizeF()
        card.name.setStyleSheet(f"color: {card.accent}; font-weight: 600; font-size: {head:.1f}pt;")
        card.details.setStyleSheet(f"color: {TAG_FG}; font-size: {small:.1f}pt;")
        card.body.setFont(scaled_font(card.body, self._scale))
        self._note.setStyleSheet(f"color: {TAG_FG}; font-size: {small:.1f}pt;")
        if entry is None:
            return
        if entry.kind == "prompt":
            card.set_body(_marked(entry.text, entry.marks))
        else:
            card.set_body(html.escape(entry.text), code_point_size(self._scale))
