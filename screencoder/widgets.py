"""Reusable widgets: the composer (text + screenshots + files) and a live Markdown view."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QMimeData, QPoint, QRect, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QFontDatabase, QGuiApplication, QImage, QPixmap, QTextBlockFormat,
    QTextCharFormat, QTextCursor, QTextFormat,
)
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLayout, QPlainTextEdit, QPushButton, QScrollArea,
    QSizePolicy, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from . import images
from .attachments import IMAGE_EXTS, Attachment
from .config import HOME_DIR

CODE_BLOCK_BG = QColor("#161b22")
PROMPT_BLOCK_BG = QColor("#10263f")  # a ```prompt block: text to hand to another agent, so it stands out
PROMPT_LANGUAGE = "prompt"
INLINE_CODE_BG = QColor("#262c36")  # `inline code` gets a soft chip, so code reads apart from the talk around it
# Invisible first paragraph: blocks inserted into a QTextDocument lose their first block's format
# (heading level, list membership), so every formatted entry starts with this throwaway paragraph.
_SPACER = chr(0x200B) + "\n\n"  # zero-width space paragraph


class FlowLayout(QLayout):
    """Lays widgets out left to right and wraps onto further rows when the width runs out.

    Lets button rows stay usable in a narrow panel: the minimum width is just the widest single widget.
    """

    def __init__(self, parent=None, spacing: int = 4):
        super().__init__(parent)
        self._items: list = []
        self._last_width = 0  # width of the last setGeometry, so minimumSize can report the real wrapped height
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        if rect.width() != self._last_width:
            # The number of rows (and so our minimum height) depends on the width; when it changes,
            # re-propagate so the parent reserves room for every wrapped row instead of just one.
            self._last_width = rect.width()
            self.invalidate()
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        return self.minimumSize()

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        if self._last_width > 0:
            # Reserve the height the buttons actually need at this width. Without it the layout keeps only
            # one row's height, and a wrapped row spills past the panel's edge where it can no longer be clicked.
            size.setHeight(max(size.height(), self.heightForWidth(self._last_width)))
        margins = self.contentsMargins()
        return size + QSize(margins.left() + margins.right(), margins.top() + margins.bottom())

    def _arrange(self, rect: QRect, apply: bool) -> int:
        margins = self.contentsMargins()
        area = rect.adjusted(margins.left(), margins.top(), -margins.right(), -margins.bottom())
        x, y, row_height, gap = area.x(), area.y(), 0, self.spacing()
        for item in self._items:
            if item.widget() is not None and item.widget().isHidden():
                continue
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:  # no room left on this row
                x, y, row_height = area.x(), y + row_height + gap, 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + gap
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + margins.bottom()


def _local_files(mime: QMimeData) -> list[str]:
    if not mime.hasUrls():
        return []
    return [u.toLocalFile() for u in mime.urls() if u.isLocalFile() and u.toLocalFile()]


def _mime_image(mime: QMimeData) -> QImage | None:
    if not mime.hasImage():
        return None
    data = mime.imageData()
    if isinstance(data, QPixmap):
        data = data.toImage()
    return data if isinstance(data, QImage) and not data.isNull() else None


class PasteTextEdit(QPlainTextEdit):
    """Plain-text editor that turns pasted/dropped images and files into attachments."""

    images_pasted = Signal(list)
    files_pasted = Signal(list)
    submit = Signal()
    submit_on_enter = False  # True: plain Enter submits (a one-line field); Shift+Enter still breaks the line

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        return source.hasImage() or bool(_local_files(source)) or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:
        files = _local_files(source)
        if files:
            self.files_pasted.emit(files)
            return
        img = _mime_image(source)
        text = source.text() if source.hasText() else ""
        # Office apps put an image next to real text; browsers/snipping tools put an image with no text
        # (or just its URL). Prefer the image unless there is genuine text.
        if img is not None and (not text.strip() or text.strip().startswith(("http://", "https://", "data:"))):
            self.images_pasted.emit([img])
            return
        super().insertFromMimeData(source)

    def keyPressEvent(self, event) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            modifiers = event.modifiers()
            plain = self.submit_on_enter and not modifiers & Qt.KeyboardModifier.ShiftModifier
            if modifiers & Qt.KeyboardModifier.ControlModifier or plain:
                self.submit.emit()
                return
        super().keyPressEvent(event)


class ImagePreview(QDialog):
    def __init__(self, img: QImage, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)  # shown once: gone when closed, not kept hidden
        label = QLabel()
        label.setPixmap(QPixmap.fromImage(img))
        scroll = QScrollArea()
        scroll.setWidget(label)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(scroll)
        avail = (self.screen() or QGuiApplication.primaryScreen()).availableGeometry()
        self.resize(min(img.width() + 30, int(avail.width() * 0.9)), min(img.height() + 30, int(avail.height() * 0.9)))


class AttachmentTile(QFrame):
    removed = Signal(int)

    def __init__(self, att: Attachment, index_label: str, parent=None):
        super().__init__(parent)
        self.att = att
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedSize(QSize(132, 104))
        self.setToolTip(self._tooltip())
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 4)
        layout.setSpacing(2)
        top = QHBoxLayout()
        caption = QLabel(index_label)
        caption.setStyleSheet("color:#9aa4b2; font-size:11px;")
        if att.near_dup_of:
            caption.setText(f"{index_label}  ~{att.near_dup_of}")
            caption.setStyleSheet("color:#e3b341; font-size:11px;")
        top.addWidget(caption)
        top.addStretch(1)
        close = QToolButton()
        close.setText("x")
        close.setAutoRaise(True)
        close.setToolTip("Remove")
        close.clicked.connect(lambda: self.removed.emit(att.id))
        top.addWidget(close)
        layout.addLayout(top)
        body = QLabel()
        body.setAlignment(Qt.AlignmentFlag.AlignCenter)
        if att.kind == "image" and att.image is not None:
            body.setPixmap(QPixmap.fromImage(att.image.scaled(
                120, 72, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)))
        else:
            body.setText(f"📄\n{att.name}")
            body.setWordWrap(True)
            body.setStyleSheet("font-size:11px;")
        layout.addWidget(body, 1)

    def _tooltip(self) -> str:
        if self.att.kind == "image":
            w, h = self.att.size
            tip = f"{self.att.name} - {w}x{h}px\nDouble-click to preview"
            if self.att.near_dup_of:
                tip += f"\nLooks nearly identical to {self.att.near_dup_of} - remove it if redundant"
            return tip
        return f"{self.att.path}\nDouble-click to open"

    def mouseDoubleClickEvent(self, _event) -> None:
        if self.att.kind == "image" and self.att.image is not None:
            ImagePreview(self.att.image, self.att.name, self.window()).exec()
        elif self.att.path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.att.path))


class Composer(QWidget):
    """Text + screenshots + files input. Ctrl+V pastes images/files, Ctrl+Enter submits.

    With text_input=False there is no text box (the question comes from elsewhere, e.g. the audio
    transcript): only attachments are collected, and pasted text becomes a text-file attachment.
    """

    submitted = Signal()
    capture_requested = Signal(str)  # "region" | "full"
    notice = Signal(str)
    changed = Signal()  # the text or the attachments changed (what can be sent is different now)

    def __init__(self, title: str, placeholder: str, min_text_height: int = 70, text_input: bool = True,
                 submit_on_enter: bool = False, parent=None):
        super().__init__(parent)
        self.title = title
        self.text_input = text_input
        self._atts: list[Attachment] = []
        self._shot_counter = 0
        self._snippet_counter = 0
        self.setAcceptDrops(True)

        self.edit = PasteTextEdit()
        self.edit.submit_on_enter = submit_on_enter
        self.edit.setPlaceholderText(placeholder)
        self.edit.setMinimumHeight(min_text_height)
        self.edit.images_pasted.connect(lambda imgs: [self.add_image(i) for i in imgs])
        self.edit.files_pasted.connect(self.add_files)
        self.edit.submit.connect(self.submitted)

        self._tray_inner = QWidget()
        self._tray_layout = QHBoxLayout(self._tray_inner)
        self._tray_layout.setContentsMargins(0, 0, 0, 0)
        self._tray_layout.setSpacing(6)
        self._tray_layout.addStretch(1)
        self.tray = QScrollArea()
        self.tray.setWidget(self._tray_inner)
        self.tray.setWidgetResizable(True)
        # Up to 124 px tall, but allowed to shrink when the panel is short. A fixed height here used to push
        # the Snip/Screen/Files buttons below it into the tray's own rectangle on a short window, where the
        # scroll area sat on top of them and swallowed the clicks. Shrinking the strip keeps the buttons below.
        self.tray.setMaximumHeight(124)
        self.tray.setMinimumHeight(0)
        _tray_policy = self.tray.sizePolicy()
        _tray_policy.setVerticalPolicy(QSizePolicy.Policy.Maximum)
        self.tray.setSizePolicy(_tray_policy)
        self.tray.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.tray.setFrameShape(QFrame.Shape.NoFrame)
        self.tray.hide()

        self.buttons = FlowLayout(spacing=4)  # wraps onto more rows when the panel is narrow
        for text, tip, slot in (
            ("✂ Snip", "Drag to capture a region on any monitor (the app turns invisible while you select)",
             lambda: self.capture_requested.emit("region")),
            ("🖥 Screen", "Capture a whole monitor (asks which one if there are several)",
             lambda: self.capture_requested.emit("full")),
            ("📎 Files", "Attach files from this PC (code, text, PDFs, images)", self.browse_files),
            ("📋 Paste", "Paste an image, files or text from the clipboard (Ctrl+V)" if text_input else
             "Paste an image or files from the clipboard (Ctrl+V); copied text is attached as a text file",
             self.paste_from_clipboard),
            ("Clear", "Remove the text and all attachments" if text_input else "Remove all attachments", self.clear),
        ):
            btn = QPushButton(text)
            btn.setToolTip(tip)
            btn.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            btn.clicked.connect(slot)
            self.buttons.addWidget(btn)
            if slot == self.paste_from_clipboard:
                self.paste_btn = btn  # enabled only while the clipboard holds something that can be pasted
            elif slot == self.clear:
                self.clear_btn = btn  # enabled only while there is something to remove
        self.count_label = QLabel()
        self.count_label.setStyleSheet("color:#9aa4b2;")
        self.buttons.addWidget(self.count_label)
        self.edit.textChanged.connect(self._content_changed)
        QGuiApplication.clipboard().dataChanged.connect(self._refresh_buttons)
        self._refresh_buttons()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        if text_input:
            layout.addWidget(self.edit, 1)
        else:
            self.edit.setParent(self)
            self.edit.hide()
        layout.addWidget(self.tray)
        layout.addLayout(self.buttons)

    # ----------------------------------------------------------------- content
    def _clipboard_usable(self) -> bool:
        """Does the clipboard hold something Paste would take: an image, files, or text?"""
        mime = QGuiApplication.clipboard().mimeData()
        if mime is None:
            return False
        return mime.hasImage() or bool(_local_files(mime)) or (mime.hasText() and bool(mime.text().strip()))

    def _refresh_buttons(self) -> None:
        self.paste_btn.setEnabled(self._clipboard_usable())
        self.clear_btn.setEnabled(not self.is_empty())

    def _content_changed(self) -> None:
        self._refresh_buttons()
        self.changed.emit()

    def text(self) -> str:
        return self.edit.toPlainText().strip()

    def attachments(self) -> list[Attachment]:
        return list(self._atts)

    def is_empty(self) -> bool:
        return not self.text() and not self._atts

    def clear(self) -> None:
        self.edit.clear()
        self._atts.clear()
        self._shot_counter = 0
        self._rebuild_tray()

    def take(self) -> tuple[str, list[Attachment]]:
        text, atts = self.text(), self.attachments()
        self.clear()
        return text, atts

    def add_image(self, img: QImage, name: str = "", path: str = "") -> bool:
        if img is None or img.isNull():
            return False
        sig = images.exact_signature(img)
        self._shot_counter += 1
        att = Attachment("image", name or f"Screenshot {self._shot_counter}", image=img, path=path,
                         exact_sig=sig, thumb=images.gray_thumbnail(img))
        # A deliberate capture or paste is always kept, even when it matches an earlier attachment.
        # Re-capturing an unchanged screen gives byte-identical pixels, and silently dropping that made the
        # Snip/Screen/Paste buttons look dead ("I clicked but nothing was captured"). We still point out the
        # resemblance - exact first, otherwise near - so an accidental double is easy to spot and remove.
        twin = next((e for e in self._atts if e.exact_sig == sig), None)
        if twin is not None:
            att.near_dup_of = twin.name
            self.notice.emit(f"{self.title}: {att.name} is identical to {twin.name}")
        else:
            near = next((e for e in self._atts if e.kind == "image"
                         and images.near_duplicate(att.size, att.thumb, e.size, e.thumb)), None)
            if near is not None:
                att.near_dup_of = near.name
                self.notice.emit(f"{self.title}: {att.name} looks nearly identical to {near.name}")
        self._atts.append(att)
        self._rebuild_tray()
        return True

    def add_files(self, paths: list[str]) -> None:
        for raw in paths:
            path = Path(raw)
            if path.is_dir():
                self.notice.emit(f"Folders can't be attached ({path.name}) - pick files, or set the folder as the Workspace")
                continue
            if not path.is_file():
                continue
            if path.suffix.lower() in IMAGE_EXTS:
                img = QImage(str(path))
                if not img.isNull():
                    self.add_image(img, name=path.name, path=str(path))
                    continue
            if any(a.kind == "file" and os.path.normcase(a.path) == os.path.normcase(str(path)) for a in self._atts):
                continue
            self._atts.append(Attachment("file", path.name, path=str(path)))
        self._rebuild_tray()

    def browse_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach files")
        if paths:
            self.add_files(paths)

    def paste_from_clipboard(self) -> None:
        mime = QGuiApplication.clipboard().mimeData()
        if mime is None:
            return
        files = _local_files(mime)
        if files:
            self.add_files(files)
            return
        img = _mime_image(mime)
        if img is not None:
            self.add_image(img)
        elif mime.hasText() and self.text_input:
            self.edit.insertPlainText(mime.text())
        elif mime.hasText():
            self.add_text_snippet(mime.text())
        if self.text_input:
            self.edit.setFocus()

    def add_text_snippet(self, text: str) -> None:
        """Attach copied text (code, an error message...) as a small text file."""
        if not text.strip():
            return
        folder = HOME_DIR / "scratch" / "pasted"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            self._snippet_counter += 1
            path = folder / f"pasted-text-{datetime.now():%H%M%S}-{self._snippet_counter}.txt"
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            self.notice.emit(f"Could not attach the pasted text: {exc}")
            return
        self.add_files([str(path)])
        self.notice.emit(f"{self.title}: pasted text attached ({len(text.splitlines())} lines)")

    def remove(self, att_id: int) -> None:
        self._atts = [a for a in self._atts if a.id != att_id]
        self._rebuild_tray()

    def _rebuild_tray(self) -> None:
        while self._tray_layout.count() > 1:
            item = self._tray_layout.takeAt(0)
            if item.widget():
                item.widget().setParent(None)  # hide now; deleteLater only runs on the next loop pass
                item.widget().deleteLater()
        for i, att in enumerate(self._atts):
            tile = AttachmentTile(att, f"#{i + 1}")
            tile.removed.connect(self.remove)
            self._tray_layout.insertWidget(i, tile)
        self.tray.setVisible(bool(self._atts))
        n_img = sum(a.kind == "image" for a in self._atts)
        n_file = len(self._atts) - n_img
        parts = [f"{n_img} screenshot{'s' * (n_img != 1)}"] if n_img else []
        parts += [f"{n_file} file{'s' * (n_file != 1)}"] if n_file else []
        self.count_label.setText("  ".join(parts))
        QTimer.singleShot(0, lambda: self.tray.horizontalScrollBar().setValue(self.tray.horizontalScrollBar().maximum()))
        self._content_changed()

    # ----------------------------------------------------------------- drag & drop
    def dragEnterEvent(self, event) -> None:
        if _local_files(event.mimeData()) or event.mimeData().hasImage():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        files = _local_files(event.mimeData())
        if files:
            self.add_files(files)
        else:
            img = _mime_image(event.mimeData())
            if img is not None:
                self.add_image(img)
        event.acceptProposedAction()


def scaled_font(widget: QWidget, scale: float) -> QFont:
    """The widget's font at `scale` times the application's normal text size."""
    font = widget.font()
    font.setPointSizeF(QGuiApplication.font().pointSizeF() * scale)
    return font


def code_point_size(scale: float) -> float:
    return QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont).pointSizeF() * scale


def style_code(document, first_block, point_size: float, wrap: bool = False) -> None:
    """Shade fenced code blocks and size all monospace text (fenced blocks and `inline` code).

    Rendered Markdown gives code the system fixed font at its own fixed size, so unlike normal text it
    does not follow the view's font size by itself. wrap=True lets long code lines wrap (chat bubbles).
    Blocks fenced as `prompt` hold prose for another agent: they always wrap and get their own background.
    """
    ranges: list[tuple[int, int, bool]] = []  # position, length, is inline code
    block = first_block
    while block.isValid():
        fmt = block.blockFormat()
        fenced = fmt.hasProperty(QTextFormat.Property.BlockCodeFence) or fmt.nonBreakableLines()
        if fenced:
            is_prompt = str(fmt.property(QTextFormat.Property.BlockCodeLanguage) or "").strip().lower() == PROMPT_LANGUAGE
            fmt.setBackground(PROMPT_BLOCK_BG if is_prompt else CODE_BLOCK_BG)
            if wrap or is_prompt:
                fmt.setNonBreakableLines(False)
            QTextCursor(block).setBlockFormat(fmt)
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.isValid() and (fenced or fragment.charFormat().fontFixedPitch()):
                ranges.append((fragment.position(), fragment.length(), not fenced))
            it += 1
        block = block.next()
    sized = QTextCharFormat()
    sized.setFontPointSize(point_size)
    chip = QTextCharFormat(sized)
    chip.setBackground(INLINE_CODE_BG)
    cursor = QTextCursor(document)
    for position, length, inline in ranges:  # applied afterwards: changing formats while iterating fragments is unsafe
        cursor.setPosition(position)
        cursor.setPosition(position + length, QTextCursor.MoveMode.KeepAnchor)
        cursor.mergeCharFormat(chip if inline else sized)


class LiveMarkdownView(QTextBrowser):
    """Append-only Markdown log whose last entry can be streamed in, re-rendered at most every 70 ms."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self._tail = 0  # document position where the live (still changing) entry starts
        self._live: str | None = None
        self._live_spaced = True
        self._scale = 1.0
        self.wrap_code = False  # True: long lines in code blocks wrap instead of scrolling sideways
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(70)
        self._timer.timeout.connect(self._render)

    def set_text_scale(self, scale: float) -> None:
        """Make all text (including what is already shown) `scale` times the normal size."""
        self._scale = scale
        self.setFont(scaled_font(self, scale))
        style_code(self.document(), self.document().begin(), code_point_size(scale), self.wrap_code)

    def reset(self) -> None:
        self._timer.stop()
        self.clear()
        self._tail = 0
        self._live = None

    def begin_live(self) -> None:
        self.commit_live()
        self._live = ""
        self._live_spaced = True

    def append_live(self, text: str) -> None:
        if self._live is None:
            self.begin_live()
        self._live += text
        if not self._timer.isActive():
            self._timer.start()

    def commit_live(self) -> str:
        if self._live is None:
            return ""
        self._timer.stop()
        self._render()
        text, self._live = self._live, None
        self._seal()
        return text

    def append_markdown(self, md: str, spaced: bool = False) -> None:
        """Add a finished entry. spaced=True for multi-paragraph Markdown (headings, lists, code)."""
        self.commit_live()
        self._live, self._live_spaced = md, spaced
        self._render()
        self._live = None
        self._seal()

    def _at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - 8

    def _render(self) -> None:
        stick = self._at_bottom()
        cur = QTextCursor(self.document())
        cur.setPosition(self._tail)
        cur.movePosition(QTextCursor.MoveOperation.End, QTextCursor.MoveMode.KeepAnchor)
        cur.removeSelectedText()
        if self._live:
            cur.insertMarkdown((_SPACER if self._live_spaced else "") + self._live)
            style_code(self.document(), self.document().findBlock(self._tail), code_point_size(self._scale),
                       self.wrap_code)
        if stick:
            self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _seal(self) -> None:
        cur = QTextCursor(self.document())
        cur.movePosition(QTextCursor.MoveOperation.End)
        if cur.position() > self._tail:
            cur.insertBlock(QTextBlockFormat(), QTextCharFormat())
        self._tail = cur.position()
