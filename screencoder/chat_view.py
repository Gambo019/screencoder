"""Chat-style message list for the quick-answer panel: one bubble per speaker, newest at the top."""
from __future__ import annotations

import math
from datetime import datetime

from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QTextBrowser, QVBoxLayout, QWidget,
)

from .widgets import code_point_size, scaled_font, style_code

# role -> (accent colour, bubble background)
ROLE_STYLE = {
    "heard": ("#58a6ff", "#10192a"),  # what was heard / asked
    "answer": ("#3fb950", "#0f1a13"),  # the developer's reply
    "agent": ("#e3b341", "#1f1a0d"),  # a change request handed to the coding session
    "error": ("#f85149", "#231113"),
}
MAX_BUBBLES = 200
TITLE_SCALE = 0.7  # bubble headers grow with the text, but stay clearly smaller than it
# Shown in an answer that is on its way, until its first words are there: nobody should stare at an empty box.
WAITING_TEXT = "Next answer is coming. Please check the current codebase while you wait."


class Spinner(QWidget):
    """A small turning arc: something is on its way. It only animates while it is visible."""

    def __init__(self, color: str, size: int = 18, parent=None):
        super().__init__(parent)
        self._color = QColor(color)
        self._angle = 0
        self.setFixedSize(size, size)
        self._timer = QTimer(self)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._turn)

    def _turn(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def showEvent(self, event) -> None:
        self._timer.start()
        super().showEvent(event)

    def hideEvent(self, event) -> None:
        self._timer.stop()
        super().hideEvent(event)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        ring = QRectF(2.5, 2.5, self.width() - 5, self.height() - 5)
        track = QColor(self._color)
        track.setAlpha(50)
        painter.setPen(QPen(track, 2.4))
        painter.drawEllipse(ring)  # the faint full circle the arc runs on
        pen = QPen(self._color, 2.4)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawArc(ring, -self._angle * 16, 100 * 16)


class AutoHeightBrowser(QTextBrowser):
    """A text browser that is always exactly as tall as its content (the chat scrolls, not the bubble)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setOpenExternalLinks(True)
        self.setStyleSheet("QTextBrowser { background: transparent; border: none; }")
        self.document().setDocumentMargin(2)
        self.document().documentLayout().documentSizeChanged.connect(self._fit)

    def _fit(self, *_args) -> None:
        height = math.ceil(self.document().size().height()) + 2 * self.frameWidth() + 2
        if height != self.height():
            self.setFixedHeight(max(18, height))

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._fit()

    def wheelEvent(self, event) -> None:
        event.ignore()  # let the chat's scroll area handle the wheel


class MessageBubble(QFrame):
    def __init__(self, role: str, title: str, markdown: str = "", scale: float = 1.0, parent=None):
        super().__init__(parent)
        self.role = role
        self.question: MessageBubble | None = None  # for a reply: the bubble it answers
        self._accent, background = ROLE_STYLE.get(role, ROLE_STYLE["answer"])
        self.setObjectName("bubble")
        self.setStyleSheet(
            f"QFrame#bubble {{ background: {background}; border: 1px solid #30363d; "
            f"border-left: 3px solid {self._accent}; border-radius: 6px; }}"
        )
        self._title = QLabel(title)
        self._status = QLabel()
        # A long status must never widen the bubble beyond the panel: it is clipped instead.
        self._status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._stamp = QLabel(datetime.now().strftime("%H:%M:%S"))
        head = QHBoxLayout()
        head.addWidget(self._title)
        head.addWidget(self._status, 1)
        head.addWidget(self._stamp)
        self.body = AutoHeightBrowser()
        # While an answer is being generated and none of it is there yet: a spinner and a line to read.
        self._wait_row = QWidget()
        self._wait_label = QLabel(WAITING_TEXT)
        self._wait_label.setWordWrap(True)
        wait = QHBoxLayout(self._wait_row)
        wait.setContentsMargins(0, 4, 0, 2)
        wait.setSpacing(8)
        wait.addWidget(Spinner(self._accent), 0, Qt.AlignmentFlag.AlignTop)
        wait.addWidget(self._wait_label, 1)
        self._wait_row.hide()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 8)
        layout.setSpacing(2)
        layout.addLayout(head)
        layout.addWidget(self._wait_row)
        layout.addWidget(self.body)
        self._markdown = ""
        self._scale = 1.0
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(60)
        self._timer.timeout.connect(self._render)
        self._markdown = markdown
        self.set_text_scale(scale)

    def set_text_scale(self, scale: float) -> None:
        self._scale = scale
        head_pt = scaled_font(self, max(1.0, scale * TITLE_SCALE)).pointSizeF()
        self._title.setStyleSheet(f"color: {self._accent}; font-weight: 600; font-size: {head_pt:.1f}pt;")
        self._status.setStyleSheet(f"color: #8b949e; font-style: italic; font-size: {head_pt:.1f}pt;")
        self._stamp.setStyleSheet(f"color: #6e7681; font-size: {max(8.0, head_pt * 0.8):.1f}pt;")
        self.body.setFont(scaled_font(self.body, scale))
        self._wait_label.setStyleSheet(
            f"color: #9aa4b2; font-style: italic; font-size: {scaled_font(self, scale).pointSizeF():.1f}pt;")
        self._render()

    def set_waiting(self, waiting: bool) -> None:
        """Show (or take away) the spinner and "next answer is coming" line. It goes by itself once there
        is text, and when the bubble is finished."""
        self._wait_row.setVisible(waiting and not self._markdown.strip())

    def waiting(self) -> bool:
        return not self._wait_row.isHidden()

    def markdown(self) -> str:
        return self._markdown

    def plain_text(self) -> str:
        return f"[{self._title.text()}] {self.body.toPlainText()}".strip()

    def set_markdown(self, markdown: str) -> None:
        self._markdown = markdown
        self._render()

    def append_text(self, text: str) -> None:
        """Streamed text: re-rendered at most every 60 ms."""
        self.update_text(self._markdown + text)

    def update_text(self, markdown: str) -> None:
        """Replace the (still streaming) text; re-rendered at most every 60 ms."""
        self._markdown = markdown
        if markdown.strip():
            self._wait_row.hide()
        if not self._timer.isActive():
            self._timer.start()

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def finish(self) -> None:
        self._timer.stop()
        self._render()
        self.set_status("")
        self._wait_row.hide()

    def _render(self) -> None:
        if self._markdown.strip():
            self._wait_row.hide()  # the first words are there: nothing to wait for any more
        self.body.setVisible(bool(self._markdown.strip()))
        self.body.setMarkdown(self._markdown)
        # No horizontal scrolling inside a bubble: long code lines wrap.
        style_code(self.body.document(), self.body.document().begin(), code_point_size(self._scale), wrap=True)


class ChatView(QScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setStyleSheet("QScrollArea { background: #0d1117; border: 1px solid #30363d; }")
        inner = QWidget()
        inner.setObjectName("chatInner")
        inner.setStyleSheet("QWidget#chatInner { background: #0d1117; }")
        self._layout = QVBoxLayout(inner)
        self._layout.setContentsMargins(6, 6, 6, 6)
        self._layout.setSpacing(8)
        self._placeholder = QLabel()
        self._placeholder.setWordWrap(True)
        self._placeholder.setTextFormat(Qt.TextFormat.MarkdownText)
        self._placeholder.setStyleSheet("color: #8b949e; padding: 6px;")
        self._layout.addWidget(self._placeholder)
        self._layout.addStretch(1)
        self.setWidget(inner)
        self._bubbles: list[MessageBubble] = []  # newest first, same order as on screen
        self._scale = 1.0

    def set_placeholder(self, markdown: str) -> None:
        self._placeholder.setText(markdown)

    def set_text_scale(self, scale: float) -> None:
        """Text size for every message, present and future, as a multiple of the normal size."""
        self._scale = scale
        for bubble in self._bubbles:
            bubble.set_text_scale(scale)

    def add(self, role: str, title: str, markdown: str = "") -> MessageBubble:
        """Add a message at the top and return its bubble (to stream into or update)."""
        self._placeholder.hide()
        bubble = MessageBubble(role, title, markdown, self._scale)
        self._layout.insertWidget(0, bubble)
        self._bubbles.insert(0, bubble)
        for old in self._bubbles[MAX_BUBBLES:]:
            old.setParent(None)
            old.deleteLater()
        del self._bubbles[MAX_BUBBLES:]
        QTimer.singleShot(0, lambda: self.verticalScrollBar().setValue(0))
        return bubble

    def clear(self) -> None:
        """Remove every message and show the placeholder again."""
        for bubble in self._bubbles:
            bubble.setParent(None)
            bubble.deleteLater()
        self._bubbles.clear()
        self._placeholder.show()

    def remove(self, bubble: MessageBubble) -> None:
        if bubble in self._bubbles:
            self._bubbles.remove(bubble)
            bubble.setParent(None)
            bubble.deleteLater()

    def bubbles(self) -> list[MessageBubble]:
        return list(self._bubbles)

    def plain_text(self) -> str:
        return "\n".join(bubble.plain_text() for bubble in self._bubbles)
