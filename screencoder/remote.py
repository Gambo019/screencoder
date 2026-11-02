"""Remote control: see and drive the running window from a browser on another machine.

Built from the outside, like stealth.py and opacity.py: nothing in here knows what the app does. It mirrors the
Qt widgets themselves (every window, button, field, list and text view that exists), so whatever the app shows
or offers is there on the other side too, including anything added to the app later.

Why it is fast:
  * one WebSocket on one port (the page is served from the same port), Nagle off, no polling;
  * only what changed crosses the wire: a widget's changed properties, and for a long text just the part that
    differs, so an answer that is streaming costs a few bytes per update instead of the whole text;
  * the app is not scanned on a timer. An event filter marks the widgets Qt itself reports as changed and only
    those are looked at again, a few milliseconds later. A slow round-robin pass catches anything silent;
  * nothing runs at all while nobody is connected.
"""
from __future__ import annotations

import base64
import hmac
import html as html_lib
import json
import os
import re
import secrets
import shutil
import socket
import struct
import sys
import tempfile
import time
import traceback
import zlib
from bisect import bisect_left
from collections import deque
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import shiboken6
from PySide6.QtCore import (
    QBuffer, QCoreApplication, QEvent, QIODevice, QMimeData, QObject, QPoint, QPointF, QPersistentModelIndex, QRect,
    Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import QContextMenuEvent, QImage, QKeyEvent, QMouseEvent, QPalette, QPixmap, QTextDocument
from PySide6.QtNetwork import QAbstractSocket, QHostAddress, QNetworkInterface, QTcpServer
from PySide6.QtWebSockets import QWebSocketProtocol, QWebSocketServer
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QAbstractSlider, QApplication, QBoxLayout, QCheckBox, QComboBox, QDialog,
    QDoubleSpinBox, QFormLayout, QFrame, QGridLayout, QGroupBox, QLabel, QLineEdit, QMainWindow, QMenu,
    QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QRubberBand, QScrollArea, QScrollBar, QSizeGrip,
    QSpinBox, QSplitter, QSplitterHandle, QStackedLayout, QStatusBar, QTabWidget, QTableView, QTextBrowser,
    QTextEdit, QToolBar, QToolButton, QTreeView, QWidget,
)

from . import tunnel
from .config import APP_NAME, Settings
from .tunnel import Tunnel

DEFAULT_PORT = 8765
REACH_LAN, REACH_INTERNET = "lan", "internet"  # the `remote_reach` setting
WEB_DIR = Path(__file__).with_name("remote_web")
PAGES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
MAX_HEAD = 16384          # a request head larger than this is not a browser asking for our page
HEAD_TIMEOUT_MS = 10000   # a connection that has not sent its request by then is dropped
MAX_CLIENTS = 8
MAX_BACKLOG = 16 * 1024 * 1024   # a viewer this far behind is cut loose; it reconnects and starts afresh
MAX_UPLOAD = 32 * 1024 * 1024

FLUSH_MS = 4              # changes of one burst travel together, this long after the first of them
LATER_MS = 70             # when a costly widget (a huge text, a picture) is looked at again
SWEEP_MS = 250            # the slow pass: every so often a handful of widgets is checked regardless
SWEEP_BATCH = 30
CANVAS_GAP = 0.07         # seconds between two pictures of a self-painted widget
CANVAS_WAIT = 1.5         # the longest the next picture waits for viewers to say they have the last one
SNIP_WIDTH = 1920         # a region picker's screenshot is sent at most this wide (it is only there to aim at)
SNIP_QUALITY = 70         # as WebP: half the size of a JPEG that reads as well
SHOT_WIDTH = 960          # a monitor's picture on the map of the monitors: enough to tell the windows apart
SHOT_QUALITY = 60
CODE_GAP = 0.5            # seconds between two readings of a code view that only repainted
DELTA_FROM = 200          # texts longer than this are sent as "what differs", not whole
ROW_CAP = 2000            # rows of a list or tree that are mirrored
CODE_LINE_CAP = 8000
COMBO_CAP = 400
IMAGE_CAP = 160           # pictures kept for viewers that connect later
LOSSY_FROM = 250_000      # pixels; larger pictures travel as JPEG
QT_SIZE_MAX = 16777215

_E = QEvent.Type
_WATCHED = frozenset((
    _E.Paint, _E.Show, _E.Hide, _E.ShowToParent, _E.HideToParent, _E.ChildAdded, _E.ChildRemoved, _E.EnabledChange,
    _E.LayoutRequest, _E.StyleChange, _E.FontChange, _E.ToolTipChange, _E.WindowTitleChange, _E.ParentChange,
    _E.ReadOnlyChange, _E.Polish,
))
_PLACED = frozenset((_E.Move, _E.Resize, _E.WindowStateChange))  # of the main window only: where it is on its monitors
_SKIP = (QScrollBar, QSizeGrip, QRubberBand, QSplitterHandle)
_SKIP_TOPS = ("QTipLabel", "QComboBoxPrivateContainer")
_EXPAND, _GROW, _IGNORE = 2, 1, 8  # QSizePolicy flags
_TEXT_KEYS = ("html", "v")
_RICH = re.compile(r"<(?:[a-zA-Z!][^<>]*|/[a-zA-Z]+)>|&[a-zA-Z#0-9]+;")
_BODY = re.compile(r"<body([^>]*)>")
_STYLE_ATTR = re.compile(r'style="([^"]*)"')
_QT_NOISE = " margin-left:0px; margin-right:0px; -qt-block-indent:0; text-indent:0px;"
_MISSING = object()
_KEYS = {
    "Enter": Qt.Key.Key_Return, "Escape": Qt.Key.Key_Escape, "Tab": Qt.Key.Key_Tab, "Backspace": Qt.Key.Key_Backspace,
    "Delete": Qt.Key.Key_Delete, "ArrowLeft": Qt.Key.Key_Left, "ArrowRight": Qt.Key.Key_Right,
    "ArrowUp": Qt.Key.Key_Up, "ArrowDown": Qt.Key.Key_Down, "Home": Qt.Key.Key_Home, "End": Qt.Key.Key_End,
    "PageUp": Qt.Key.Key_PageUp, "PageDown": Qt.Key.Key_PageDown, " ": Qt.Key.Key_Space,
}
_BUTTONS = {0: Qt.MouseButton.LeftButton, 1: Qt.MouseButton.MiddleButton, 2: Qt.MouseButton.RightButton}
_MOUSE = {0: _E.MouseButtonPress, 1: _E.MouseMove, 2: _E.MouseButtonRelease, 3: _E.MouseButtonDblClick}
_DRAG = 4  # a whole drag in one message: pressed at one point, let go at another
# What a viewer may ask for. Those in _PASSIVE also work on a widget that is switched off.
_OPS = frozenset("c t k s e v r a d m x q n b i g y w h file post".split())
_PASSIVE = frozenset(("q", "a", "file", "y"))


def _ptr(obj) -> int:
    return shiboken6.getCppPointer(obj)[0]


def _plain_label(text: str) -> str:
    """Button and tab captions as they are displayed: '&File' -> 'File', 'A && B' -> 'A & B'."""
    return re.sub(r"&(.)", r"\1", text) if "&" in text else text


_CHAINS: dict[type, str] = {}


def _chain(widget) -> str:
    """Class names from the widget's own class up to (not including) QWidget: what style sheets select on."""
    cls = type(widget)
    chain = _CHAINS.get(cls)
    if chain is None:
        names = []
        for base in cls.__mro__:
            if base is QWidget:
                break
            names.append(base.__name__)
        chain = _CHAINS[cls] = " ".join(names)
    return chain


def _paints_itself(widget) -> bool:
    for base in type(widget).__mro__:
        if base.__module__.startswith("PySide6"):
            return False
        if "paintEvent" in vars(base):
            return True
    return False


def _child_widgets(widget) -> list:
    found = []
    for child in widget.children():
        if child.isWidgetType() and not child.isWindow() and not isinstance(child, _SKIP) \
                and not child.objectName().startswith("qt_") and not child.property("remote_skip"):
            found.append(child)
    return found


def _overflow_hidden(w) -> bool:
    """True when `w` is a toolbar item hidden only because the desktop toolbar is too narrow to fit it.

    When its toolbar runs out of width, Qt moves the overflow into a ">>" menu and hides the item's widget,
    but leaves the item's QAction visible. The remote window has its own width, so it should still show the
    item: this lets _describe keep it visible. An item the app itself hid (its QAction made invisible) has
    no visible action here, so it stays hidden on the remote too.
    """
    bar = w.parentWidget()
    if not isinstance(bar, QToolBar):
        return False
    action = w.defaultAction() if isinstance(w, QToolButton) else None
    if action is None:
        for candidate in bar.actions():
            if bar.widgetForAction(candidate) is w:
                action = candidate
                break
    return action is not None and action.isVisible()


def _kind_of(w) -> str:
    if w.property("remote_private"):
        # A private, un-synced input: the remote page renders its OWN box here (not a copy of the desktop's),
        # so the two sides never share text. Only a Send button sends anything (the "post" op). See _d_priv.
        return "priv"
    if isinstance(w, QMenu):
        return "menu"
    if isinstance(w, QAbstractButton):
        return "chk" if isinstance(w, (QCheckBox, QRadioButton)) else "btn"
    if isinstance(w, QLabel):
        return "lbl"
    if isinstance(w, QLineEdit):
        return "edit"
    if isinstance(w, QComboBox):
        return "combo"
    if isinstance(w, (QSpinBox, QDoubleSpinBox, QAbstractSlider)):
        return "num"
    if isinstance(w, QProgressBar):
        return "prog"
    if isinstance(w, (QTextEdit, QPlainTextEdit)):
        if not w.isReadOnly():
            return "text"
        return "rich" if isinstance(w, QTextEdit) else "code"
    if isinstance(w, QAbstractItemView):
        return "tree"
    if isinstance(w, QTabWidget):
        return "tabs"
    if isinstance(w, QSplitter):
        return "split"
    if isinstance(w, QScrollArea):
        return "scroll"
    if w.layout() is None and not _child_widgets(w):
        if type(w).__name__ == "Spinner":
            return "busy"
        if type(w).__name__ == "SnipOverlay" and isinstance(getattr(w, "_pix", None), QPixmap):
            return "snip"  # a frozen screenshot to pick a region on: sent once, the rectangle is drawn over there
        if _paints_itself(w):
            return "canvas"  # drawn by its own code: shown as a picture, the mouse is passed through
    return "box"


def _u16(text: str) -> int:
    """Length as the browser counts it (UTF-16 units)."""
    return len(text) if text.isascii() else len(text.encode("utf-16-le")) // 2


def _splice(old: str, new: str) -> tuple[int, int, str]:
    """(units kept at the start, units kept at the end, what goes in between) to turn `old` into `new`."""
    if new.startswith(old):
        keep, tail = len(old), 0
    else:
        lo, hi = 0, min(len(old), len(new))
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if old[lo:mid] == new[lo:mid]:
                lo = mid
            else:
                hi = mid - 1
        keep = lo
        lo, hi = 0, min(len(old), len(new)) - keep
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if old[len(old) - mid:len(old) - lo] == new[len(new) - mid:len(new) - lo]:
                lo = mid
            else:
                hi = mid - 1
        tail = lo
    return _u16(new[:keep]), _u16(new[len(new) - tail:]) if tail else 0, new[keep:len(new) - tail]


def _body(document_html: str) -> tuple[str, str]:
    """The inside of <body> of Qt's HTML export, slimmed, and the body's own style (font of the view)."""
    match = _BODY.search(document_html)
    if match is None:
        return document_html, ""
    style = _STYLE_ATTR.search(match.group(1))
    end = document_html.rfind("</body>")
    inner = document_html[match.end():end if end >= 0 else len(document_html)]
    return inner.replace(_QT_NOISE, "").strip("\n"), style.group(1).strip() if style else ""


def _format_css(fmt) -> str:
    css = []
    if fmt.foreground().style() != Qt.BrushStyle.NoBrush:
        css.append(f"color:{fmt.foreground().color().name()}")
    if fmt.background().style() != Qt.BrushStyle.NoBrush:
        css.append(f"background:{fmt.background().color().name()}")
    if fmt.fontWeight() >= 600:
        css.append("font-weight:700")
    if fmt.fontItalic():
        css.append("font-style:italic")
    return ";".join(css)


def _code_html(document) -> tuple[str, int]:
    """A plain-text document as HTML lines, with the colours its syntax highlighter gave it."""
    lines, block, escape = [], document.begin(), html_lib.escape
    while block.isValid() and len(lines) < CODE_LINE_CAP:
        text = block.text()
        ranges = block.layout().formats() if text.isascii() else []  # offsets are UTF-16: only trusted for ASCII
        if ranges:
            parts, at = [], 0
            for item in sorted(ranges, key=lambda r: r.start):
                start, end = max(item.start, at), min(item.start + item.length, len(text))
                if end <= start:
                    continue
                if start > at:
                    parts.append(escape(text[at:start], quote=False))
                css = _format_css(item.format)
                piece = escape(text[start:end], quote=False)
                parts.append(f'<span style="{css}">{piece}</span>' if css else piece)
                at = end
            parts.append(escape(text[at:], quote=False))
            lines.append("".join(parts))
        else:
            lines.append(escape(text, quote=False))
        block = block.next()
    return "\n".join(lines), len(lines)


def _unwritten(document) -> list[int]:
    """The blocks Qt's HTML export leaves out: the two empty ones marking where a table starts and ends."""
    gaps, frames = [], list(document.rootFrame().childFrames())
    while frames:
        frame = frames.pop()
        frames += frame.childFrames()
        gaps += [document.findBlock(frame.firstPosition()).blockNumber(),
                 document.findBlock(frame.lastPosition() + 1).blockNumber()]
    return sorted(gaps)


def _doc_spot(document, y: float, gaps=()) -> tuple[int, float]:
    """The block of a laid-out document at height `y`, counted among those the export writes (`gaps` are the
    ones it does not), and how far down it that is (0 its top, 1 its end)."""
    layout = document.documentLayout()
    spot = QPointF(document.documentMargin() + 8, y)  # a little way in: at the very edge, a table is not yet its cells
    block = document.findBlock(max(0, layout.hitTest(spot, Qt.HitTestAccuracy.FuzzyHit)))
    box = layout.blockBoundingRect(block)
    number = block.blockNumber()
    return number - bisect_left(gaps, number), round(min(1.0, max(0.0, (y - box.y()) / (box.height() or 1))), 4)


def _doc_block(document, index: int, gaps=()):
    """The block that _doc_spot calls `index`."""
    for gap in gaps:
        if gap > index:
            break
        index += 1
    return document.findBlockByNumber(index)


def _line_spot(document, line: int) -> tuple[int, float]:
    """The same for a plain-text view, whose scroll bar counts lines as displayed: a wrapped line is several."""
    block = document.findBlockByLineNumber(line)
    return block.blockNumber(), round((line - block.firstLineNumber()) / max(1, block.lineCount()), 4)


def _monitor_box(screen) -> list[int]:
    """A monitor in physical pixels. Qt keeps each monitor's corner where it physically is and scales what lies
    inside from there, so with monitors of different scaling only pixels add up to the real arrangement."""
    box, ratio = screen.geometry(), screen.devicePixelRatio()
    return [box.x(), box.y(), round(box.width() * ratio), round(box.height() * ratio)]


def _monitor_at(x: int, y: int, otherwise):
    """The monitor a physical point is on, or `otherwise` if it is on none."""
    for screen in QApplication.screens():
        left, top, width, height = _monitor_box(screen)
        if left <= x < left + width and top <= y < top + height:
            return screen
    return otherwise


def _data_url(image: QImage, quality: int = 82, lossy_kind: str = "jpeg") -> str:
    """A picture as a data URL: PNG while it is small or has transparency, else `lossy_kind` (jpeg or webp)."""
    lossy = image.width() * image.height() > LOSSY_FROM and not image.hasAlphaChannel()
    for kind in ((lossy_kind, "jpeg") if lossy else ("png",)):  # jpeg again if this Qt cannot write the other
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if image.save(buffer, kind.upper(), quality if lossy else 40):
            break
    return f"data:image/{kind};base64," + base64.b64encode(bytes(buffer.data())).decode("ascii")


def _align(flags: int, across_is_vertical: bool) -> str:
    """Where an item sits across its row/column: a(t the start), c(entre), e(nd) or '' for filling it."""
    if across_is_vertical:
        return "a" if flags & 0x20 else "e" if flags & 0x40 else "c" if flags & 0x80 else ""
    return "a" if flags & 0x1 else "e" if flags & 0x2 else "c" if flags & 0x4 else ""


def _flex(entries: list) -> list:
    """Qt's rule for sharing spare room along a row/column: stretch factors if any, else what wants to expand."""
    live = [entry for entry in entries if not entry[6]]
    if any(entry[2] > 0 for entry in live):
        mode = 0
    elif any(entry[3] & _EXPAND for entry in live):
        mode = 1
    else:
        mode = 2
    items = []
    for kind, payload, stretch, main, across, align, hidden in entries:
        if mode == 0:
            grow, zero = stretch, 1
        elif mode == 1:
            grow, zero = (1 if main & _EXPAND else 0), (1 if main & _IGNORE else 0)
        else:
            grow, zero = (1 if main & _GROW else 0), 0
        if kind == 0:
            if not align and not across & _GROW:
                align = "c"  # a widget that will not grow that way sits centred, as Qt places it
            items.append([0, payload, grow, align, zero if grow else 0])
        elif kind == 1:
            items.append([1, payload, grow, zero if grow else 0, 1 if hidden else 0])
        else:
            items.append([2, grow, payload])
    return items


class _Node:
    __slots__ = ("id", "widget", "ptr", "kind", "props", "kids", "parent", "cache")

    def __init__(self, node_id: int, widget, ptr: int, kind: str, parent: int):
        self.id, self.widget, self.ptr, self.kind, self.parent = node_id, widget, ptr, kind, parent
        self.props: dict = {}
        self.kids: list[int] = []
        self.cache: dict = {}


class Mirror(QObject):
    """Keeps a description of every widget and reports what changed in it.

    `send(message, only=None, skip=None)` delivers a message to the viewers; `viewers()` says who they are.
    """

    def __init__(self, app, window, send, viewers=lambda: ()):
        super().__init__(app)
        self._app, self._window, self._send, self.viewers = app, window, send, viewers
        self._nodes: dict[int, _Node] = {}
        self._by_ptr: dict[int, _Node] = {}
        self._wired: set[int] = set()
        self._wired_models: set[tuple[int, int]] = set()
        self._complained: set[str] = set()
        self._upload_dir = ""
        self._roots: list[int] = []
        self._next_id = 1
        self._dirty: set[int] = set()
        self._later: set[int] = set()
        self._fresh: list[_Node] = []
        self._kids_now: list[int] = []
        self._removed: list[int] = []
        self._images: dict[str, str] = {}
        self._new_images: list[str] = []
        self._sweep: list[int] = []
        self._queue: deque = deque()
        self._active = self._quiet = self._roots_dirty = self._roots_changed = False
        self._acting = 0
        self._native_before = False
        self._flush_timer = QTimer(self)
        self._flush_timer.setSingleShot(True)
        self._flush_timer.setInterval(FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush)
        self._later_timer = QTimer(self)
        self._later_timer.setSingleShot(True)
        self._later_timer.setInterval(LATER_MS)
        self._later_timer.timeout.connect(self._again)
        self._sweep_timer = QTimer(self)
        self._sweep_timer.setInterval(SWEEP_MS)
        self._sweep_timer.timeout.connect(self._sweep_some)

    # ---- life cycle

    @property
    def active(self) -> bool:
        return self._active

    def start(self) -> None:
        if self._active:
            return
        self._active = True
        self._app.installEventFilter(self)
        self._sweep_timer.start()
        self._roots_dirty = True
        self._flush()

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        self._app.removeEventFilter(self)
        for timer in (self._flush_timer, self._later_timer, self._sweep_timer):
            timer.stop()
        self._nodes.clear()
        self._by_ptr.clear()
        self._images.clear()
        self._roots, self._sweep, self._removed, self._new_images, self._fresh = [], [], [], [], []
        self._dirty.clear()
        self._later.clear()

    def snapshot(self) -> list:
        """Everything, for a viewer that just connected."""
        self._flush()
        ops = [[0, node.id, node.kind, node.props] for node in self._nodes.values()]
        used = set()
        for node in self._nodes.values():
            for key in ("ic", "img"):
                value = node.props.get(key)
                if value:
                    used.add(value)
        ops += [[5, key, self._images[key]] for key in used if key in self._images]
        ops.append([4, list(self._roots)])
        return ["S", self._meta(), ops]

    def _meta(self) -> dict:
        palette, font, role = self._app.palette(), self._app.font(), QPalette.ColorRole
        colors = {
            "window": role.Window, "text": role.WindowText, "base": role.Base, "button": role.Button,
            "btext": role.ButtonText, "hi": role.Highlight, "hitext": role.HighlightedText, "link": role.Link,
            "mid": role.PlaceholderText,
        }
        return {
            "title": APP_NAME,
            "font": [font.family(), round(font.pointSizeF(), 2)],
            "pal": {name: palette.color(which).name() for name, which in colors.items()},
            "css": self._app.styleSheet(),
        }

    # ---- noticing changes

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        kind = event.type()
        if kind in _WATCHED and not self._quiet:
            try:
                if obj.isWidgetType():
                    self._touch(obj, kind)
            except Exception:  # a widget half-way through being built or destroyed: the slow pass gets it
                pass
        elif kind in _PLACED and obj is self._window:
            self._mark_ptr(_ptr(obj))
        return False

    def _touch(self, widget, kind) -> None:
        if kind == _E.Polish:
            # A window opened because of a remote click must not grab the keyboard on this machine.
            if self._acting and widget.isWindow() and not isinstance(widget, QMenu):
                widget.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
            return
        if (kind == _E.Show or kind == _E.Hide) and widget.isWindow():
            self._roots_dirty = True
            self._kick()
        node = self._by_ptr.get(_ptr(widget))
        while node is None:
            widget = widget.parentWidget()
            if widget is None:
                return
            node = self._by_ptr.get(_ptr(widget))
        if kind == _E.Paint and (node.kind == "canvas" or node.kind == "code"):
            node.cache["stale"] = True
        self._dirty.add(node.id)
        self._kick()

    def _kick(self) -> None:
        if not self._flush_timer.isActive():
            self._flush_timer.start()

    def _mark_ptr(self, ptr: int) -> None:
        if not self._active:
            return
        node = self._by_ptr.get(ptr)
        if node is not None:
            if node.kind == "tree":
                node.cache["dirty"] = True
            self._dirty.add(node.id)
            self._kick()

    def _gone(self, ptr: int) -> None:
        self._wired.discard(ptr)
        node = self._by_ptr.get(ptr)
        if node is not None and self._active:
            if node.parent:
                self._dirty.add(node.parent)
            else:
                self._roots_dirty = True
            self._drop(node)
            self._kick()

    def _again(self) -> None:
        self._dirty |= self._later
        self._later.clear()
        self._kick()

    def _sweep_some(self) -> None:
        if not self._sweep:
            self._sweep = list(self._nodes)
            self._roots_dirty = True
            self._prune_images()
        take, self._sweep = self._sweep[-SWEEP_BATCH:], self._sweep[:-SWEEP_BATCH]
        for node_id in take:
            node = self._nodes.get(node_id)
            if node is not None:
                if node.kind == "tree":
                    node.cache["dirty"] = True
                self._dirty.add(node_id)
        self._kick()

    def _wire(self, node: _Node) -> None:
        """Connect the signals that announce a change Qt does not send an event for."""
        ptr = node.ptr
        if ptr in self._wired:
            return
        self._wired.add(ptr)
        w, kind = node.widget, node.kind

        def mark(*_args, p=ptr):
            self._mark_ptr(p)

        w.destroyed.connect(lambda *_args, p=ptr: self._gone(p))
        if kind in ("rich", "code", "text"):
            w.document().contentsChanged.connect(mark)
        elif kind == "edit":
            w.textChanged.connect(mark)
        elif kind in ("btn", "chk"):
            w.toggled.connect(mark)
        elif kind == "combo":
            w.currentIndexChanged.connect(mark)
            w.editTextChanged.connect(mark)
        elif kind == "num":
            w.valueChanged.connect(mark)
        elif kind == "tabs":
            w.currentChanged.connect(mark)
            corner = w.cornerWidget(Qt.Corner.TopRightCorner)
            if isinstance(corner, QCheckBox):
                corner.toggled.connect(mark)  # flip "Sync tab" -> re-describe so the web learns the new flag
        elif kind == "split":
            w.splitterMoved.connect(mark)
            if isinstance(self._window, QMainWindow) and w is self._window.centralWidget():
                w.splitterMoved.connect(lambda *_args: self._mark_ptr(_ptr(self._window)))  # the columns on the map
        elif kind == "tree" and isinstance(w, QTreeView):
            w.expanded.connect(mark)
            w.collapsed.connect(mark)
        if kind in ("rich", "code", "scroll"):
            bar = w.verticalScrollBar()
            bar.valueChanged.connect(mark)
            bar.rangeChanged.connect(mark)

    # ---- building and refreshing the description

    def _tops(self) -> list:
        tops = [self._window]
        for w in QApplication.topLevelWidgets():
            if w is self._window or not w.isVisible():
                continue
            if w.property("remote_skip"):
                continue  # a window kept off the remote on purpose (e.g. the local monitor-picker popup)
            if w.metaObject().className() in _SKIP_TOPS or w.windowType() == Qt.WindowType.ToolTip:
                continue
            if w.windowType() == Qt.WindowType.Popup and not isinstance(w, QMenu):
                continue
            tops.append(w)
        return tops

    def _sync_roots(self) -> None:
        ids = []
        for w in self._tops():
            node = self._by_ptr.get(_ptr(w))
            if node is None:
                node = self._new(w, 0)
            node.parent = 0
            ids.append(node.id)
        for old in self._roots:
            node = self._nodes.get(old)
            if old not in ids and node is not None and not node.parent:
                self._drop(node)
        ordered = [i for i in self._roots if i in ids] + [i for i in ids if i not in self._roots]
        if ordered != self._roots:
            self._roots, self._roots_changed = ordered, True

    def _new(self, widget, parent: int) -> _Node:
        ptr = _ptr(widget)
        node = _Node(self._next_id, widget, ptr, _kind_of(widget), parent)
        self._next_id += 1
        self._nodes[node.id] = node
        self._by_ptr[ptr] = node
        self._wire(node)
        self._fresh.append(node)
        return node

    def _ref(self, widget, parent: _Node) -> int:
        """The id of a child widget; one it has not met yet is queued to be described."""
        node = self._by_ptr.get(_ptr(widget))
        if node is None:
            node = self._new(widget, parent.id)
        else:
            node.parent = parent.id
        self._kids_now.append(node.id)
        return node.id

    def _drop(self, node: _Node) -> None:
        stack = [node]
        while stack:
            current = stack.pop()
            if self._nodes.pop(current.id, None) is None:
                continue
            if self._by_ptr.get(current.ptr) is current:
                del self._by_ptr[current.ptr]
            self._removed.append(current.id)
            self._dirty.discard(current.id)
            self._later.discard(current.id)
            if current.kind in ("canvas", "snip"):
                self._images.pop(current.cache.get("img"), None)  # its own picture: of no use to anyone after it
            for kid in current.kids:
                child = self._nodes.get(kid)
                if child is not None and child.parent == current.id:
                    stack.append(child)

    def _fill_fresh(self, ops: list) -> None:
        while self._fresh:
            node = self._fresh.pop()
            if node.id not in self._nodes:
                continue
            try:
                node.props, node.kids = self._describe(node)
            except Exception as problem:  # deleted under our hands, or a widget we cannot read: left out
                self._complain(node, problem)
                self._drop(node)
                continue
            ops.append([0, node.id, node.kind, node.props])

    def _complain(self, node: _Node, problem: Exception) -> None:
        if not isinstance(problem, RuntimeError) and node.kind not in self._complained:
            self._complained.add(node.kind)  # once per kind of widget: a note for whoever maintains this
            print(f"remote control: cannot describe a '{node.kind}' widget: {problem!r}", file=sys.stderr)

    def _rescan(self, node: _Node, ops: list) -> None:
        if not shiboken6.isValid(node.widget):
            self._drop(node)
            return
        try:
            props, kids = self._describe(node)
        except Exception as problem:
            self._complain(node, problem)
            self._drop(node)
            return
        old, change = node.props, {}
        for key, value in props.items():
            before = old.get(key, _MISSING)
            if before is _MISSING or before != value:
                if key in _TEXT_KEYS and isinstance(before, str) and isinstance(value, str) and len(before) > DELTA_FROM:
                    ops.append([2, node.id, key, *_splice(before, value)])
                else:
                    change[key] = value
        for key in old:
            if key not in props:
                change[key] = None
        if change:
            ops.append([1, node.id, change])
        node.props = props
        if kids != node.kids:
            lost = set(node.kids) - set(kids)
            node.kids = kids
            for kid in lost:
                child = self._nodes.get(kid)
                if child is not None and child.parent == node.id:
                    self._drop(child)

    def _flush(self) -> None:
        if not self._active:
            return
        try:
            self._flush_now()
        except Exception:  # never let one odd widget stop the stream: note it, start over from the windows
            traceback.print_exc(file=sys.stderr)
            self._fresh.clear()
            self._roots_dirty = True

    def _flush_now(self) -> None:
        ops: list = []
        if self._roots_dirty:
            self._roots_dirty = False
            self._sync_roots()
        dirty, self._dirty = self._dirty, set()
        for node_id in dirty:
            node = self._nodes.get(node_id)
            if node is not None:
                self._rescan(node, ops)
        self._fill_fresh(ops)
        for key in self._new_images:
            if key in self._images:
                ops.append([5, key, self._images[key]])
        self._new_images = []
        if self._removed:
            ops.append([3, self._removed])
            self._removed = []
        if self._roots_changed:
            self._roots_changed = False
            ops.append([4, list(self._roots)])
        if ops:
            self._send(["U", ops])
        if self._later and not self._later_timer.isActive():
            self._later_timer.start()
        if self._dirty:
            self._kick()

    def _describe(self, node: _Node) -> tuple[dict, list[int]]:
        self._kids_now = kids = []
        w, props = node.widget, {}
        top = w.isWindow()
        chain = _chain(w)
        if chain:
            props["c"] = chain
        name = w.objectName()
        if name and not name.startswith("qt_"):
            props["n"] = name
        if not top and w.isHidden() and not _overflow_hidden(w) and not w.property("remote_private"):
            props["h"] = 1  # a remote_private box is hidden on the desktop but still shown on the remote page
        if not w.isEnabled():
            props["e"] = 0
        tip = w.toolTip()
        if tip:
            props["tt"] = tip
        sheet = w.styleSheet()
        if sheet:
            props["ss"] = sheet
        parent = w.parentWidget()
        font, base = w.font(), (parent.font() if parent is not None and not top else self._app.font())
        if font.pixelSize() > 0:
            if font.pixelSize() != base.pixelSize():
                props["fp"] = font.pixelSize()
        elif font.pointSizeF() != base.pointSizeF():
            props["fs"] = round(font.pointSizeF(), 2)
        if font.weight() != base.weight():
            props["fw"] = int(font.weight().value)
        if font.italic() != base.italic():
            props["fi"] = 1 if font.italic() else 0
        if font.family() != base.family():
            props["ff"] = font.family()
        if top:
            props["top"] = 1
            props["wt"] = w.windowTitle()
            props["wsz"] = [w.width(), w.height()]
            if w.isModal():
                props["md"] = 1
            if w is self._window:
                props["geo"] = self._where(w)
        else:
            low_w, low_h, high_w, high_h = w.minimumWidth(), w.minimumHeight(), w.maximumWidth(), w.maximumHeight()
            if low_w or low_h:
                props["mn"] = [low_w, low_h]
            if high_w < QT_SIZE_MAX or high_h < QT_SIZE_MAX:
                props["mx"] = [high_w if high_w < QT_SIZE_MAX else 0, high_h if high_h < QT_SIZE_MAX else 0]
        if isinstance(w, QFrame):
            shape = w.frameShape()
            if shape == QFrame.Shape.HLine:
                props["sep"] = "h"
            elif shape == QFrame.Shape.VLine:
                props["sep"] = "v"
            elif shape != QFrame.Shape.NoFrame:
                props["fr"] = 1
        getattr(self, "_d_" + node.kind)(node, w, props)
        return props, kids

    def _where(self, w) -> dict:
        """Where the window is on this machine's monitors, in physical pixels and with its frame: for the map."""
        screen, frame, least = w.screen(), w.frameGeometry(), w.minimumSizeHint().expandedTo(w.minimumSize())
        ratio, corner, primary = screen.devicePixelRatio(), screen.geometry().topLeft(), QApplication.primaryScreen()
        normal = w.normalGeometry()
        return {
            "m": [[*_monitor_box(s), 1 if s == primary else 0] for s in QApplication.screens()],
            "r": [round(corner.x() + (frame.x() - corner.x()) * ratio), round(corner.y() + (frame.y() - corner.y()) * ratio),
                  round(frame.width() * ratio), round(frame.height() * ratio)],
            "min": [round((least.width() + frame.width() - w.width()) * ratio),
                    round((least.height() + frame.height() - w.height()) * ratio)],
            "st": "min" if w.isMinimized() else "full" if w.isFullScreen() else "max" if w.isMaximized() else "",
            "n": [round(normal.width() * ratio), round(normal.height() * ratio)],  # its size when it is not maximized
            "parts": self._columns(w, ratio),
        }

    def _columns(self, w, ratio: float) -> list:
        """The columns of a window that a splitter divides from left to right, for the map: [distance from the left
        of its frame, width, name] in physical pixels. A column is called what its `remote_map_name` property says,
        else after its first tab, else after its title."""
        split = w.centralWidget() if isinstance(w, QMainWindow) else None
        if not isinstance(split, QSplitter) or split.orientation() != Qt.Orientation.Horizontal:
            return []
        columns, margin = [], w.geometry().x() - w.frameGeometry().x()
        for i in range(split.count()):
            pane = split.widget(i)
            if pane.isHidden():
                continue
            tabs, name = pane.findChild(QTabWidget), pane.property("remote_map_name")
            if not name:
                name = _plain_label(tabs.tabText(0)) if tabs is not None else pane.title() if isinstance(pane, QGroupBox) else ""
            columns.append([round((pane.mapTo(w, QPoint(0, 0)).x() + margin) * ratio), round(pane.width() * ratio), name])
        return columns

    def _d_btn(self, node, w, props) -> None:
        props["t"] = _plain_label(w.text())
        if w.isCheckable():
            props["ck"] = 1
            if w.isChecked():
                props["on"] = 1
        icon = w.icon()
        if not icon.isNull():
            size = w.iconSize()
            key = f"i{icon.cacheKey()}x{size.width()}"
            if key not in self._images:
                self._keep_image(key, icon.pixmap(size).toImage())
            props["ic"] = key
            if isinstance(w, QToolButton) and w.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly:
                props["io"] = 1
        if isinstance(w, QToolButton):
            if w.autoRaise():
                props["fl"] = 1
        elif isinstance(w, QPushButton):
            if w.isFlat():
                props["fl"] = 1
            if w.isDefault():
                props["df"] = 1

    def _d_chk(self, node, w, props) -> None:
        props["t"] = _plain_label(w.text())
        if w.isChecked():
            props["on"] = 1
        if isinstance(w, QRadioButton):
            props["rd"] = 1

    def _d_lbl(self, node, w, props) -> None:
        pixmap = w.pixmap()
        if pixmap is not None and not pixmap.isNull():
            key = f"p{pixmap.cacheKey()}"
            if key not in self._images:
                self._keep_image(key, pixmap.toImage())
            ratio = pixmap.devicePixelRatio() or 1.0
            props["img"] = key
            props["isz"] = [round(pixmap.width() / ratio), round(pixmap.height() / ratio)]
            if w.hasScaledContents():
                props["sc"] = 1
        else:
            text, fmt = w.text(), w.textFormat()
            if fmt == Qt.TextFormat.MarkdownText:
                if node.cache.get("md") != text:  # Qt turns it into rich text to draw it: so do we, once
                    document = QTextDocument()
                    document.setMarkdown(text)
                    node.cache["md"], node.cache["md_html"] = text, _body(document.toHtml())[0]
                props["t"], props["r"] = node.cache["md_html"], 1
            else:
                # A caption of a field ("&Name:") marks its shortcut letter with "&", which is not displayed.
                props["t"] = _plain_label(text) if w.buddy() is not None else text
                if fmt == Qt.TextFormat.RichText or (fmt == Qt.TextFormat.AutoText and _RICH.search(text)):
                    props["r"] = 1
        if w.wordWrap():
            props["wr"] = 1
        flags = w.alignment().value
        if flags & 0x2:
            props["al"] = "r"
        elif flags & 0x4:
            props["al"] = "c"

    def _d_edit(self, node, w, props) -> None:
        props["v"] = w.text()
        hint = w.placeholderText()
        if hint:
            props["ph"] = hint
        if w.isReadOnly():
            props["ro"] = 1
        if w.echoMode() != QLineEdit.EchoMode.Normal:
            props["pw"] = 1

    def _d_text(self, node, w, props) -> None:
        props["v"] = w.toPlainText()
        hint = w.placeholderText()
        if hint:
            props["ph"] = hint
        if w.isReadOnly():
            props["ro"] = 1

    def _scroll_state(self, node, w, props) -> None:
        bar = w.verticalScrollBar()
        value, end = bar.value(), bar.maximum()
        if end <= 0:
            return
        if value >= end - (0 if isinstance(w, QPlainTextEdit) else 2):  # that bar counts lines, the others pixels
            props["end"] = 1  # this side follows the end of the text, so the viewer's does too
        if w.isVisible():  # a tab not open here says nothing: a viewer on it (sync off) scrolls it on their own
            props["sy"] = self._top_of(node, w, value)
            props["sm"] = self._top_of(node, w, end)  # how far this view goes: the page's must go as far (fit() there)

    def _top_of(self, node, w, value: int):
        """What the top of a scrolled view shows, by its content and not in pixels: the other side lays the text
        out in another width, where the same offset would show something else.

        0 is the very start. Otherwise [the widget it lies in, the block or line of its text (-1: it has none),
        how far down that is, from 0 to 1]. Viewers send the same back; _c_y reads it."""
        if not value:
            return 0
        if isinstance(w, QPlainTextEdit):
            return [node.id, *_line_spot(w.document(), value)]
        if isinstance(w, QTextEdit):
            return [node.id, *_doc_spot(w.document(), value, node.cache.get("gaps", ()))]
        spot, here, y, within = node, w.widget(), value, value  # a scroll area: down to what lies at that height
        while here is not None:
            known = self._by_ptr.get(_ptr(here))
            if known is not None:
                spot, within = known, y
                if isinstance(here, QTextEdit):
                    return [spot.id, *_doc_spot(here.document(), y, known.cache.get("gaps", ()))]
            kids = [kid for kid in _child_widgets(here) if kid.isVisible() and kid.y() + kid.height() > y]
            if not kids:
                break
            here = min(kids, key=lambda kid: kid.y())
            y -= here.y()
        return [spot.id, -1, round(min(1.0, max(0.0, within / (spot.widget.height() or 1))), 4)]

    def _d_rich(self, node, w, props) -> None:
        cache = node.cache
        document = w.document()
        revision = (document.revision(), document.defaultFont().key())  # a new text size changes it as well
        if cache.get("rev") != revision:
            now = time.perf_counter()
            if "html" in cache and now < cache.get("next", 0.0):
                self._later.add(node.id)  # a big document that keeps changing: not on every single change
            else:
                cache["html"], cache["bs"] = _body(w.toHtml())
                cache["gaps"] = _unwritten(document)
                took = time.perf_counter() - now
                cache["rev"], cache["next"] = revision, now + took + min(0.4, took * 8)
        props["html"] = cache.get("html", "")
        if cache.get("bs"):
            props["bs"] = cache["bs"]
        props["bc"] = document.blockCount() - len(cache.get("gaps", ()))
        if w.verticalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff:
            props["au"] = 1  # as tall as its text: the height is the browser's business
            props.pop("mn", None)
            props.pop("mx", None)
        else:
            self._scroll_state(node, w, props)

    def _d_code(self, node, w, props) -> None:
        cache = node.cache
        revision = w.document().revision()
        now = time.perf_counter()
        if cache.get("rev") != revision or "html" not in cache:
            fresh = True
        elif cache.get("stale"):  # repainted with the same text: its colours may have changed
            fresh = now - cache.get("at", 0.0) >= CODE_GAP
            if not fresh:
                self._later.add(node.id)
        else:
            fresh = False
        if fresh:
            cache["html"], cache["lines"] = _code_html(w.document())
            cache["rev"], cache["at"], cache["stale"] = revision, now, False
        props["html"] = cache["html"]
        if any("gutter" in type(child).__name__.lower() or "line" in type(child).__name__.lower()
               for child in _child_widgets(w)):
            props["ln"] = cache["lines"]
        if w.lineWrapMode() != QPlainTextEdit.LineWrapMode.NoWrap:
            props["wr"] = 1
        self._scroll_state(node, w, props)

    def _d_combo(self, node, w, props) -> None:
        props["it"] = [w.itemText(i) for i in range(min(w.count(), COMBO_CAP))]
        props["cur"] = w.currentIndex()
        if w.isEditable():
            props["ed"] = 1
            props["v"] = w.currentText()

    def _d_num(self, node, w, props) -> None:
        props["v"], props["lo"], props["hi"], props["st"] = w.value(), w.minimum(), w.maximum(), w.singleStep()
        if isinstance(w, QAbstractSlider):
            props["ty"] = "range"
            if w.orientation() == Qt.Orientation.Vertical:
                props["o"] = "v"
        elif isinstance(w, QDoubleSpinBox):
            props["dec"] = w.decimals()

    def _d_prog(self, node, w, props) -> None:
        props["v"], props["lo"], props["hi"] = w.value(), w.minimum(), w.maximum()
        if w.isTextVisible():
            props["t"] = w.text()

    def _d_busy(self, node, w, props) -> None:
        color = getattr(w, "_color", None)
        if color is not None and hasattr(color, "name"):
            props["col"] = color.name()

    def _d_priv(self, node, w, props) -> None:
        """A private, un-synced input (see _kind_of). It is a leaf here: none of the desktop box's own contents
        are described, so the local user's draft never reaches the remote page. The page renders its own box
        and sends only on a button press (the "post" op)."""
        props["t"] = "💻 Your message"

    def _d_snip(self, node, w, props) -> None:
        """A region picker. Its screenshot is frozen, so it goes over once; what moves while picking is the
        rectangle, and that is drawn on the other side. Nothing is sent while the viewer drags."""
        cache = node.cache
        if "img" not in cache and (cache.get("asked") or self._first_picker(w)):
            image = w._pix.toImage()
            wide = min(w.width(), SNIP_WIDTH)
            if 0 < wide != image.width():
                image = image.scaledToWidth(wide, Qt.TransformationMode.SmoothTransformation)
            cache["img"] = f"s{node.id}"
            self._images[cache["img"]] = _data_url(image, SNIP_QUALITY, "webp")
            self._new_images.append(cache["img"])
        if "img" in cache:
            props["img"] = cache["img"]
        props["isz"] = [w.width(), w.height()]
        props["geo"] = [w.x(), w.y()]  # where its monitor lies among the others
        ratio = w._pix.devicePixelRatio()
        if ratio != 1:
            props["dpr"] = round(ratio, 3)
        chosen = getattr(w, "_sel", None)  # a rectangle being dragged on this machine itself
        if isinstance(chosen, QRect) and not chosen.isEmpty():
            props["sel"] = [chosen.x(), chosen.y(), chosen.width(), chosen.height()]

    def _first_picker(self, w) -> bool:
        """Is this the picker of the first monitor (leftmost, then topmost)? Only that one's screenshot is sent
        unasked, so the picker is on the other side quickly; a viewer asks for the others' when it has that one."""
        spots = [(other.x(), other.y()) for other in QApplication.topLevelWidgets()
                 if type(other) is type(w) and other.isVisible()]
        return not spots or (w.x(), w.y()) <= min(spots)

    def _d_canvas(self, node, w, props) -> None:
        cache = node.cache
        if cache.get("stale", True):
            now = time.perf_counter()
            # The next picture waits until every viewer has the last one on screen. A slow link then gets fewer
            # pictures, each of them current, instead of a growing queue of old ones.
            behind = cache.get("unseen") and now < cache.get("until", 0.0) \
                and not cache["unseen"].isdisjoint(id(viewer) for viewer in self.viewers())
            if "img" in cache and (behind or now - cache.get("at", 0.0) < CANVAS_GAP):
                self._later.add(node.id)
            else:
                cache["stale"], cache["at"] = False, now
                self._quiet = True  # grabbing paints the widget: that must not count as a change
                try:
                    image = w.grab().toImage()
                finally:
                    self._quiet = False
                if image.width() != w.width() and w.width() > 0:
                    image = image.scaled(w.size(), Qt.AspectRatioMode.IgnoreAspectRatio,
                                         Qt.TransformationMode.SmoothTransformation)
                url = _data_url(image)
                key = f"c{node.id}_{zlib.crc32(url.encode('ascii')):x}"
                if key != cache.get("img"):
                    self._images.pop(cache.get("img"), None)
                    self._images[key] = url
                    self._new_images.append(key)
                    cache["img"] = key
                    cache["unseen"], cache["until"] = {id(viewer) for viewer in self.viewers()}, now + CANVAS_WAIT
        if cache.get("img"):
            props["img"] = cache["img"]
        props["isz"] = [w.width(), w.height()]

    def _d_menu(self, node, w, props) -> None:
        items, actions = [], []

        def walk(menu, depth: int) -> None:
            for action in menu.actions():
                if not action.isVisible():
                    continue
                if action.isSeparator():
                    items.append(0)
                    actions.append(None)
                    continue
                sub = action.menu()
                flags = (1 if action.isEnabled() else 0) | (2 if action.isCheckable() else 0) \
                    | (4 if action.isChecked() else 0) | (8 if sub is not None else 0)
                items.append([depth, flags, _plain_label(action.text()), action.shortcut().toString()])
                actions.append((menu, action))
                if sub is not None and depth < 4:
                    walk(sub, depth + 1)

        walk(w, 0)
        props["items"] = items
        node.cache["actions"] = actions

    def _d_tree(self, node, w, props) -> None:
        cache = node.cache
        model = w.model()
        if model is None:
            props["rows"] = []
            return
        if cache.get("model") != _ptr(model):
            cache["model"], cache["dirty"] = _ptr(model), True
            pair = (node.ptr, _ptr(model))
            if pair not in self._wired_models:
                self._wired_models.add(pair)

                def mark(*_args, p=node.ptr):
                    self._mark_ptr(p)

                for signal in (model.rowsInserted, model.rowsRemoved, model.dataChanged, model.modelReset,
                               model.layoutChanged):
                    signal.connect(mark)
                selection = w.selectionModel()
                if selection is not None:
                    selection.currentChanged.connect(mark)
        if cache.get("dirty", True):
            cache["dirty"] = False
            cache["rows"], cache["index"] = self._rows(w, model)
        props["rows"] = cache["rows"]
        header = w.header() if isinstance(w, QTreeView) else w.horizontalHeader() if isinstance(w, QTableView) else None
        if header is not None and not header.isHidden():
            props["hd"] = [str(model.headerData(c, Qt.Orientation.Horizontal) or "")
                           for c in range(model.columnCount()) if not header.isSectionHidden(c)]

    def _rows(self, view, model) -> tuple[list, list]:
        rows, index, tree = [], [], isinstance(view, QTreeView)
        root = view.rootIndex()
        try:
            column_count = model.columnCount(root)
        except TypeError:  # a QListWidget's model makes columnCount(parent) private (PySide6 6.12) - lists are 1 column
            column_count = 1
        columns = [c for c in range(column_count)
                   if not (isinstance(view, (QTreeView, QTableView)) and view.isColumnHidden(c))][:8] or [0]
        current, selection = view.currentIndex(), view.selectionModel()
        display, foreground = Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole

        def walk(parent, depth: int) -> None:
            for r in range(model.rowCount(parent)):
                if len(rows) >= ROW_CAP:
                    return
                first = model.index(r, columns[0], parent)
                if tree and view.isRowHidden(r, parent):
                    continue
                texts, colors = [], []
                for c in columns:
                    cell = first if c == columns[0] else model.index(r, c, parent)
                    value = model.data(cell, display)
                    texts.append("" if value is None else str(value))
                    brush = model.data(cell, foreground)
                    color = brush.color().name() if hasattr(brush, "color") else brush.name() if hasattr(brush, "name") else ""
                    colors.append(color)
                parent_of_more = tree and model.hasChildren(first)
                opened = parent_of_more and view.isExpanded(first)
                flags = (1 if parent_of_more else 0) | (2 if opened else 0)
                if first == current or (selection is not None and selection.isSelected(first)):
                    flags |= 4
                if not model.flags(first) & Qt.ItemFlag.ItemIsEnabled:
                    flags |= 8
                rows.append([depth, flags, texts, colors] if any(colors) else [depth, flags, texts])
                index.append(QPersistentModelIndex(first))
                if opened:
                    walk(first, depth + 1)

        walk(root, 0)
        return rows, index

    def _d_tabs(self, node, w, props) -> None:
        tabs, pages = [], []
        for i in range(w.count()):
            tabs.append([_plain_label(w.tabText(i)), 1 if w.isTabVisible(i) else 0, 1 if w.isTabEnabled(i) else 0,
                         w.tabToolTip(i)])
            pages.append(self._ref(w.widget(i), node))
        props["tabs"], props["cur"], props["ch"] = tabs, w.currentIndex(), pages
        corner = w.cornerWidget(Qt.Corner.TopRightCorner)  # the desktop's "Sync tab" box, when present
        props["sync"] = 0 if isinstance(corner, QCheckBox) and not corner.isChecked() else 1

    def _d_split(self, node, w, props) -> None:
        horizontal = w.orientation() == Qt.Orientation.Horizontal
        props["o"] = "h" if horizontal else "v"
        sizes = w.sizes()
        if not any(sizes):  # never laid out yet: what Qt would start from
            sizes = [(w.widget(i).sizeHint().width() if horizontal else w.widget(i).sizeHint().height())
                     for i in range(w.count())]
        props["sz"] = sizes
        props["ch"] = [self._ref(w.widget(i), node) for i in range(w.count())]

    def _d_scroll(self, node, w, props) -> None:
        inner = w.widget()
        props["ch"] = [self._ref(inner, node)] if inner is not None else []
        if w.widgetResizable():
            props["fit"] = 1
        self._scroll_state(node, w, props)

    def _d_box(self, node, w, props) -> None:
        if isinstance(w, QGroupBox):
            props["gt"] = _plain_label(w.title())
            if w.isCheckable():
                props["ck"] = 1
                if w.isChecked():
                    props["on"] = 1
        elif isinstance(w, QStatusBar):
            message = w.currentMessage()
            if message:
                props["msg"] = message
        if isinstance(w, QMainWindow):
            spec = self._main_window(node, w)
        elif isinstance(w, QToolBar):
            spec = self._toolbar(node, w)
        else:
            layout = w.layout()
            spec = self._lay(node, layout) if layout is not None else None
        if spec:
            props["lay"] = spec
        placed, loose = set(self._kids_now), []
        for child in _child_widgets(w):
            known = self._by_ptr.get(_ptr(child))
            if known is None or known.id not in placed:
                loose.append(self._ref(child, node))
        if loose:
            props["ex"] = loose

    # ---- layouts

    def _main_window(self, node, w) -> list:
        direct = Qt.FindChildOption.FindDirectChildrenOnly
        items = [[0, self._ref(bar, node), 0, "", 0] for bar in w.findChildren(QToolBar, options=direct)]
        central = w.centralWidget()
        if central is not None:
            items.append([0, self._ref(central, node), 1, "", 1])
        items += [[0, self._ref(bar, node), 0, "", 0] for bar in w.findChildren(QStatusBar, options=direct)]
        return ["v", 0, [0, 0, 0, 0], items]

    def _toolbar(self, node, w) -> list:
        items = []
        for action in w.actions():
            if action.isSeparator():
                if action.isVisible():
                    items.append([3])
                continue
            widget = w.widgetForAction(action)
            if widget is None:
                continue
            grow = 1 if widget.sizePolicy().horizontalPolicy().value & _EXPAND else 0
            items.append([0, self._ref(widget, node), grow, "c", 0])
        return ["h", 2, [4, 3, 4, 3], items]

    def _entry(self, node, item, stretch: int, horizontal: bool):
        widget = item.widget()
        if widget is not None:
            if isinstance(widget, _SKIP):
                return None
            policy = widget.sizePolicy()
            wide, tall = policy.horizontalPolicy().value, policy.verticalPolicy().value
            if not stretch:
                stretch = policy.horizontalStretch() if horizontal else policy.verticalStretch()
            main, across = (wide, tall) if horizontal else (tall, wide)
            return (0, self._ref(widget, node), stretch, main, across, _align(item.alignment().value, horizontal),
                    widget.isHidden() and not widget.property("remote_private"))
        sub = item.layout()
        if sub is not None:
            grows = sub.expandingDirections().value & (1 if horizontal else 2)
            return (1, self._lay(node, sub), stretch, 7 if grows else 5, 5, "", item.isEmpty())
        spacer = item.spacerItem()
        if spacer is not None:
            policy, size = spacer.sizePolicy(), spacer.sizeHint()
            main = (policy.horizontalPolicy() if horizontal else policy.verticalPolicy()).value
            return (2, size.width() if horizontal else size.height(), stretch, main, 0, "", False)
        return None

    def _cell(self, node, item):
        if item is None:
            return 0
        widget = item.widget()
        if widget is not None:
            return [0, self._ref(widget, node)]
        sub = item.layout()
        return [1, self._lay(node, sub)] if sub is not None else 0

    def _lay(self, node, layout) -> list:
        """A Qt layout as [type, spacing, margins, items, ...]: enough for the page to lay it out the same way."""
        margins = layout.contentsMargins()
        edge = [margins.left(), margins.top(), margins.right(), margins.bottom()]
        if isinstance(layout, QBoxLayout):
            direction, way = layout.direction(), QBoxLayout.Direction
            horizontal = direction in (way.LeftToRight, way.RightToLeft)
            entries = [self._entry(node, layout.itemAt(i), layout.stretch(i), horizontal) for i in range(layout.count())]
            items = _flex([entry for entry in entries if entry is not None])
            if direction in (way.RightToLeft, way.BottomToTop):
                items.reverse()
            return ["h" if horizontal else "v", max(layout.spacing(), 0), edge, items]
        if isinstance(layout, QFormLayout):
            role, rows = QFormLayout.ItemRole, []
            for r in range(layout.rowCount()):
                rows.append([self._cell(node, layout.itemAt(r, which))
                             for which in (role.LabelRole, role.FieldRole, role.SpanningRole)])
            gaps = [layout.horizontalSpacing(), layout.verticalSpacing()]
            return ["f", [gap if gap >= 0 else 6 for gap in gaps], edge, rows]
        if isinstance(layout, QGridLayout):
            columns, lines, cells = layout.columnCount(), layout.rowCount(), []
            wide = [layout.columnStretch(c) for c in range(columns)]
            tall = [layout.rowStretch(r) for r in range(lines)]
            wants_wide, wants_tall = [0] * columns, [0] * lines
            for i in range(layout.count()):
                item = layout.itemAt(i)
                r, c, row_span, column_span = layout.getItemPosition(i)
                widget = item.widget()
                if widget is not None:
                    if isinstance(widget, _SKIP):
                        continue
                    policy = widget.sizePolicy()
                    across, down = policy.horizontalPolicy().value, policy.verticalPolicy().value
                    if not widget.isHidden():
                        if column_span == 1 and across & _EXPAND:
                            wants_wide[c] = 1
                        if row_span == 1 and down & _EXPAND:
                            wants_tall[r] = 1
                    flags = item.alignment().value
                    cells.append([0, self._ref(widget, node), r, c, row_span, column_span,
                                  _align(flags, False) or ("" if across & _GROW else "a"),
                                  _align(flags, True) or ("" if down & _GROW else "c")])
                elif item.layout() is not None:
                    cells.append([1, self._lay(node, item.layout()), r, c, row_span, column_span])
            if not any(wide):
                wide = wants_wide if any(wants_wide) else [0] * (columns - 1) + [1]
            if not any(tall):
                tall = wants_tall
            gaps = [layout.horizontalSpacing(), layout.verticalSpacing()]
            return ["g", [gap if gap >= 0 else 6 for gap in gaps], edge, cells, [wide, tall]]
        if isinstance(layout, QStackedLayout):
            items = [[0, self._ref(layout.widget(i), node), 1, "", 1] for i in range(layout.count())]
            return ["k", 0, edge, items, layout.currentIndex()]
        # A layout of the app's own (rows that wrap): its widgets in order, wrapping.
        items = []
        for i in range(layout.count()):
            widget = layout.itemAt(i).widget()
            if widget is not None and not isinstance(widget, _SKIP):
                items.append([0, self._ref(widget, node), 0, "c", 0])
        spacing = layout.spacing()
        return ["w", spacing if spacing >= 0 else 6, edge, items]

    # ---- pictures

    def _keep_image(self, key: str, image: QImage) -> None:
        self._images[key] = _data_url(image)
        self._new_images.append(key)

    def _prune_images(self) -> None:
        if len(self._images) <= IMAGE_CAP:
            return
        used = set()
        for node in self._nodes.values():
            used.add(node.props.get("ic"))
            used.add(node.props.get("img"))
        for key in [key for key in self._images if key not in used]:
            del self._images[key]

    # ---- what a viewer does

    def command(self, client, message) -> None:
        """Queue a viewer's action. It runs from the event loop, so a dialog it opens blocks nothing here."""
        self._queue.append((client, message))
        QTimer.singleShot(0, self._drain)

    def _drain(self) -> None:
        while self._queue:
            client, message = self._queue.popleft()
            if not self._acting:
                self._native_before = QCoreApplication.testAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs)
            self._acting += 1
            # A file dialog the viewer opens has to be one made of widgets, or it cannot be seen from there.
            QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, True)
            try:
                self._run(client, message)
            except Exception:
                traceback.print_exc(file=sys.stderr)
            finally:
                self._acting -= 1
                if not self._acting:
                    QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs,
                                                  self._native_before)

    def _reachable(self, widget) -> bool:
        """A window behind a modal dialog cannot be used: the same holds from the other machine."""
        top = widget.window()
        if isinstance(top, QMenu):
            return True
        popup = QApplication.activePopupWidget()
        if popup is not None:
            if isinstance(popup, QMenu):
                return False  # an open menu comes first, as it does for a mouse on the machine itself
            popup.close()  # a suggestion list or the like: using something else dismisses it, as a click would
        modal = QApplication.activeModalWidget()
        return modal is None or _ptr(modal) == _ptr(top)

    def _run(self, client, message) -> None:
        if not self._active or not isinstance(message, list) or not message:
            return
        op = message[0]
        if op == "z":  # "where do things stand?": everything again, for a viewer that lost track
            self._send(self.snapshot(), only=client)
            return
        if len(message) < 2 or op not in _OPS or not isinstance(message[1], int):
            return
        node = self._nodes.get(message[1])
        if node is None or not shiboken6.isValid(node.widget):
            return
        w = node.widget
        if op == "i":  # "that picture is on my screen now": for this viewer the next one may come
            unseen = node.cache.get("unseen")
            if unseen and len(message) > 2 and message[2] == node.cache.get("img"):
                unseen.discard(id(client))
                self._dirty.add(node.id)
                self._kick()
            return
        if op == "h":  # looks at the screen, not at the app: a dialog open in the app is no reason to refuse
            self._shots(client)
            return
        if not self._reachable(w) or (op not in _PASSIVE and not w.isEnabled()):
            return
        getattr(self, "_c_" + op)(client, node, w, message)
        self._dirty.add(node.id)
        self._kick()

    def _c_c(self, client, node, w, message) -> None:
        if isinstance(w, QToolButton) and w.menu() is not None \
                and w.popupMode() == QToolButton.ToolButtonPopupMode.InstantPopup:
            w.showMenu()
        elif isinstance(w, QAbstractButton):
            w.click()
        elif isinstance(w, QGroupBox) and w.isCheckable():
            w.setChecked(not w.isChecked())
            w.clicked.emit(w.isChecked())

    def _c_t(self, client, node, w, message) -> None:
        value = str(message[2])
        caret = int(message[3]) if len(message) > 3 else len(value)
        if len(message) > 4:
            # "Got it, up to here": until the typist has this, anything we said about this field is older than
            # what they typed and they ignore it, so text arriving late can never undo their typing.
            self._send(["A", node.id, message[4]], only=client)
        if isinstance(w, QLineEdit):
            if w.isReadOnly():
                return
            if w.text() != value:
                w.setText(value)
                w.textEdited.emit(value)
            w.setCursorPosition(max(0, min(caret, len(value))))
        elif isinstance(w, (QPlainTextEdit, QTextEdit)):
            if w.isReadOnly():
                return
            if w.toPlainText() != value:
                w.setPlainText(value)
            cursor = w.textCursor()
            cursor.setPosition(max(0, min(caret, w.document().characterCount() - 1)))
            w.setTextCursor(cursor)
        else:
            return
        if node.props.get("v") != value:  # the others see it at once; the one who typed it already has it
            node.props["v"] = value
            self._send(["U", [[1, node.id, {"v": value}]]], skip=client)

    def _c_e(self, client, node, w, message) -> None:
        if len(message) > 3:
            self._send(["A", node.id, message[3]], only=client)
        if isinstance(w, QComboBox) and w.isEditable():
            w.setEditText(str(message[2]))

    def _key(self, w, name: str, mods: int) -> None:
        key, text = _KEYS.get(name), ""
        if key is None:
            if len(name) != 1:
                return
            code, text = ord(name.upper()), name
            key = Qt.Key(code) if 0x20 <= code <= 0x7E else Qt.Key.Key_unknown
        elif name == "Enter":
            text = "\r"
        elif name == " ":
            text = " "
        modifiers = Qt.KeyboardModifier.NoModifier
        for bit, flag in ((1, Qt.KeyboardModifier.ShiftModifier), (2, Qt.KeyboardModifier.ControlModifier),
                          (4, Qt.KeyboardModifier.AltModifier), (8, Qt.KeyboardModifier.MetaModifier)):
            if mods & bit:
                modifiers |= flag
        for kind in (_E.KeyPress, _E.KeyRelease):
            if not shiboken6.isValid(w):
                return
            QApplication.sendEvent(w, QKeyEvent(kind, key, modifiers, text))

    def _picked(self, client, node, w) -> None:
        """Tell the viewer whether what they just did ended the picking (1) or the picker is still up (0)."""
        if node.kind == "snip":
            self._send(["M", node.id, 0 if shiboken6.isValid(w) and w.isVisible() else 1], only=client)

    def _c_k(self, client, node, w, message) -> None:
        self._key(w, str(message[2]), int(message[3]) if len(message) > 3 else 0)
        self._picked(client, node, w)
        if not shiboken6.isValid(w):
            return
        # The widget decided what the key meant (Enter may submit, or break the line): tell the typist the outcome.
        if node.kind == "text":
            self._send(["V", node.id, w.toPlainText(), w.textCursor().position()], only=client)
        elif node.kind == "edit":
            self._send(["V", node.id, w.text(), w.cursorPosition()], only=client)

    def _c_s(self, client, node, w, message) -> None:
        index = int(message[2])
        if isinstance(w, QTabWidget):
            corner = w.cornerWidget(Qt.Corner.TopRightCorner)
            if isinstance(corner, QCheckBox) and not corner.isChecked():
                return  # tab sync is off: the remote browses on its own and must not move the app
            if 0 <= index < w.count() and w.isTabEnabled(index):
                w.setCurrentIndex(index)
        elif isinstance(w, QComboBox) and 0 <= index < w.count():
            w.setCurrentIndex(index)
            w.activated.emit(index)
            w.textActivated.emit(w.itemText(index))

    def _c_v(self, client, node, w, message) -> None:
        final = len(message) > 3 and bool(message[3])
        if isinstance(w, QAbstractSlider):
            w.setValue(int(float(message[2])))
            w.sliderMoved.emit(w.value())
            if final:
                w.sliderReleased.emit()
        elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
            w.setValue(float(message[2]) if isinstance(w, QDoubleSpinBox) else int(float(message[2])))
            if final:
                w.editingFinished.emit()

    def _c_b(self, client, node, w, message) -> None:
        if isinstance(w, QLineEdit) and not w.isReadOnly():
            w.editingFinished.emit()

    def _c_r(self, client, node, w, message) -> None:
        rows = node.cache.get("index") or []
        row, action = int(message[2]), int(message[3])
        if not isinstance(w, QAbstractItemView) or not 0 <= row < len(rows) or not rows[row].isValid():
            return
        kept = rows[row]
        index = w.model().index(kept.row(), kept.column(), kept.parent())
        node.cache["dirty"] = True
        if action == 2:
            if isinstance(w, QTreeView):
                w.setExpanded(index, not w.isExpanded(index))
            return
        w.setCurrentIndex(index)
        if action == 0:
            w.clicked.emit(index)
        elif action == 1:
            w.doubleClicked.emit(index)
            w.activated.emit(index)
        elif action == 3:
            w.scrollTo(index)
            spot, viewport = w.visualRect(index).center(), w.viewport()
            QApplication.sendEvent(viewport, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, spot,
                                                               viewport.mapToGlobal(spot)))

    def _c_a(self, client, node, w, message) -> None:
        target = str(message[2])
        if isinstance(w, QTextBrowser):
            w.anchorClicked.emit(QUrl(target))
        elif isinstance(w, QLabel):
            w.linkActivated.emit(target)

    def _mouse(self, w, kind, x: float, y: float, button, buttons, modifiers=Qt.KeyboardModifier.NoModifier) -> None:
        spot = QPointF(x, y)
        QApplication.sendEvent(w, QMouseEvent(kind, spot, QPointF(w.mapToGlobal(spot.toPoint())), button, buttons,
                                              modifiers))

    def _c_d(self, client, node, w, message) -> None:
        """A double click on something that is not a control of its own (a tile, a card)."""
        left = Qt.MouseButton.LeftButton
        self._mouse(w, _E.MouseButtonDblClick, w.width() / 2, w.height() / 2, left, left)

    def _c_m(self, client, node, w, message) -> None:
        if node.kind not in ("canvas", "snip"):
            return
        try:
            what, spots = int(message[2]), [float(value) for value in message[3:7]]
        except (IndexError, TypeError, ValueError):
            return
        none = Qt.MouseButton.NoButton
        if what == _DRAG and len(spots) == 4:
            # The drag was made on the other side, where it could be seen without delay. It arrives whole, so
            # this widget is never left half-way through one if the viewer goes away.
            left = Qt.MouseButton.LeftButton
            for kind, at, button, held in ((_E.MouseButtonPress, 0, left, left), (_E.MouseMove, 2, none, left),
                                           (_E.MouseButtonRelease, 2, left, none)):
                if shiboken6.isValid(w) and w.isVisible():
                    self._mouse(w, kind, spots[at], spots[at + 1], button, held)
        elif what in _MOUSE and len(spots) >= 2:
            kind = _MOUSE[what]
            button = _BUTTONS.get(int(spots[2]) if len(spots) > 2 else 0, Qt.MouseButton.LeftButton)
            self._mouse(w, kind, spots[0], spots[1], none if kind == _E.MouseMove else button,
                        none if kind == _E.MouseButtonRelease else button)
        else:
            return
        self._picked(client, node, w)

    def _c_y(self, client, node, w, message) -> None:
        """A viewer scrolled: show here what they show there. 0 is the very start, -1 the very end; anything else
        names a place the way _top_of does."""
        if node.kind not in ("rich", "code", "scroll") or not w.isVisible():
            return
        bar, target = w.verticalScrollBar(), int(message[2])
        if target <= 0:
            bar.setValue(bar.maximum() if target else 0)
            return
        spot = self._nodes.get(target)
        if spot is None or not shiboken6.isValid(spot.widget):
            return
        inner, index, part = spot.widget, int(message[3]), float(message[4])
        if isinstance(inner, QPlainTextEdit):
            block = inner.document().findBlockByNumber(index)
            bar.setValue(block.firstLineNumber() + round(part * block.lineCount()))
            return
        if index >= 0 and isinstance(inner, QTextEdit):
            document = inner.document()
            box = document.documentLayout().blockBoundingRect(_doc_block(document, index, spot.cache.get("gaps", ())))
            y = box.y() + part * box.height()
        else:
            y = part * inner.height()
        if inner is not w:
            area = w.widget()
            if area is None or not area.isAncestorOf(inner):
                return
            y += inner.mapTo(area, QPoint(0, 0)).y()
        bar.setValue(round(y))

    def _c_w(self, client, node, w, message) -> None:
        """A viewer moved or resized the app on the map of its monitors: physical pixels, frame included."""
        if w is not self._window:
            return
        x, y, width, height = [int(value) for value in message[2:6]]
        # Going by the middle of the window, as Qt does; off every monitor it goes on measuring by the last one it was on.
        screen = _monitor_at(x + width // 2, y + height // 2, w.screen())
        corner, ratio = screen.geometry().topLeft(), screen.devicePixelRatio()
        if w.isMaximized() or w.isFullScreen():
            w.showNormal()  # the map shows a normal window from here on; a minimized one stays down and comes back there
        frame, inner = w.frameGeometry(), w.geometry()
        w.setGeometry(round(corner.x() + (x - corner.x()) / ratio) + inner.x() - frame.x(),
                      round(corner.y() + (y - corner.y()) / ratio) + inner.y() - frame.y(),
                      round(width / ratio) - frame.width() + inner.width(),
                      round(height / ratio) - frame.height() + inner.height())

    def _shots(self, client) -> None:
        """One picture of each monitor, for the viewer who asked: to see the app among the other windows. A window
        in stealth mode is not in them, so they show what lies under it."""
        pictures = []
        for screen in QApplication.screens():
            image = screen.grabWindow(0).toImage()
            if image.width() > SHOT_WIDTH:
                image = image.scaledToWidth(SHOT_WIDTH, Qt.TransformationMode.SmoothTransformation)
            image = image.convertToFormat(QImage.Format.Format_RGB32)  # no alpha, or it would travel as a large PNG
            pictures.append("" if image.isNull() else _data_url(image, SHOT_QUALITY, "webp"))
        self._send(["H", pictures], only=client)

    def _c_g(self, client, node, w, message) -> None:
        """A viewer wants the screenshot of a picker that was not sent unasked (another monitor's)."""
        if node.kind == "snip":
            node.cache["asked"] = True

    def _c_x(self, client, node, w, message) -> None:
        spot = QPoint(w.width() // 2, w.height() // 2)
        QApplication.sendEvent(w, QContextMenuEvent(QContextMenuEvent.Reason.Mouse, spot, w.mapToGlobal(spot)))

    def _c_q(self, client, node, w, message) -> None:
        if w is self._window or not w.isWindow():
            return
        if isinstance(w, QDialog):
            w.reject()
        else:
            w.close()

    def _c_n(self, client, node, w, message) -> None:
        actions = node.cache.get("actions") or []
        index = int(message[2])
        if node.kind != "menu" or not 0 <= index < len(actions) or actions[index] is None:
            return
        menu, action = actions[index]
        if not action.isEnabled() or action.menu() is not None:
            return
        if menu is w:
            # Chosen the way a key press would, so the code waiting on menu.exec() gets this action back.
            w.setActiveAction(action)
            self._key(w, "Enter", 0)
        else:
            action.trigger()
            w.close()

    def _c_file(self, client, node, w, message) -> None:
        """A picture or file pasted or dropped on the other machine: handed to the input as if pasted here."""
        name, data = str(message[2]), message[3]
        sink = self._sink(node)
        if sink is None or not sink.widget.isEnabled() or not isinstance(data, bytes):
            return
        mime, image = QMimeData(), QImage.fromData(data)
        if not image.isNull():
            mime.setImageData(image)
        else:  # not a picture: it needs to exist as a file on this machine to be attached
            if not self._upload_dir:
                # The full path, not Windows' short form of it (C:\Users\NAME~1\...), which some tools refuse.
                self._upload_dir = os.path.realpath(tempfile.mkdtemp(prefix="screencoder-remote-"))
                self._app.aboutToQuit.connect(lambda: shutil.rmtree(self._upload_dir, ignore_errors=True))
            safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", os.path.basename(name.replace("\\", "/"))).strip(". ") or "file"
            path = os.path.join(self._upload_dir, safe)
            stem, ext = os.path.splitext(path)
            count = 1
            while os.path.exists(path):
                count += 1
                path = f"{stem} ({count}){ext}"
            with open(path, "wb") as handle:
                handle.write(data)
            mime.setUrls([QUrl.fromLocalFile(path)])
        sink.widget.insertFromMimeData(mime)
        self._dirty.add(sink.id)

    def _c_post(self, client, node, w, message) -> None:
        """A message from the remote page's own private box (see _d_priv): posted into the quick-answer panel.
        message = ["post", node_id, action, text]; action 0 = ask the AI developer, 1 = just show it to the
        local user. The box is the remote viewer's alone, so its text reaches here only on a button press."""
        if node.kind != "priv" or len(message) < 4:
            return
        action, text = int(message[2]), str(message[3])
        window = self._window
        if text.strip() and hasattr(window, "_post_message"):
            window._post_message(to_ai=(action == 0), sender="remote", text=text, attachments=[])

    def _sink(self, node: _Node):
        """The text box a pasted picture belongs to: the one pasted into, or the nearest one around it."""
        for _step in range(5):
            stack = [node]
            while stack:
                current = stack.pop()
                if current.kind == "text" and shiboken6.isValid(current.widget) and not current.widget.isReadOnly():
                    return current
                stack += [self._nodes[k] for k in current.kids if k in self._nodes]
            node = self._nodes.get(node.parent)
            if node is None:
                return None
        return None


def _home_network(address: str) -> bool:
    """An address of an ordinary local network (as opposed to, say, a VPN's)."""
    parts = address.split(".")
    return address.startswith(("192.168.", "10.")) or (parts[0] == "172" and parts[1].isdigit()
                                                        and 16 <= int(parts[1]) <= 31)


def local_addresses() -> list[str]:
    """This machine's addresses, the one a machine on the same network reaches first; VPN addresses after."""
    found = []
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("192.0.2.1", 9))  # nothing is sent: this only asks which interface would be used
            found.append(probe.getsockname()[0])
        finally:
            probe.close()
    except OSError:
        pass
    for address in QNetworkInterface.allAddresses():
        text = address.toString()
        if address.protocol() == QAbstractSocket.NetworkLayerProtocol.IPv4Protocol and not address.isLoopback() \
                and not text.startswith("169.254.") and text not in found:
            found.append(text)
    found = [a for a in found if a != "0.0.0.0"]
    return sorted(found, key=lambda a: not _home_network(a)) or ["127.0.0.1"]  # stable: keeps the order within


def new_key() -> str:
    return secrets.token_urlsafe(18)


def remote_link(port: int, key: str, host: str | None = None) -> str:
    return f"http://{host or local_addresses()[0]}:{port}/#k={key}"


class RemoteController(QObject):
    """The listening side: serves the page, accepts viewers that know the key, passes messages both ways."""

    changed = Signal()  # started, stopped, or a viewer came or went

    def __init__(self, app, window):
        super().__init__(app)
        self._tcp = QTcpServer(self)
        self._tcp.newConnection.connect(self._incoming)
        self._wss = QWebSocketServer(APP_NAME, QWebSocketServer.SslMode.NonSecureMode, self)
        self._wss.newConnection.connect(self._joined)
        self._clients: list = []
        self._waiting: dict = {}  # connections that have not sent their request yet -> the slot reading it
        self._key = ""
        self.error = ""
        self.internet = False  # reached through the internet link (tunnel.py) instead of this machine's network
        self.mirror = Mirror(app, window, self._broadcast, lambda: self._clients)
        self.tunnel = Tunnel(self)
        self.tunnel.changed.connect(self.changed)
        app.aboutToQuit.connect(self.stop)

    @property
    def listening(self) -> bool:
        return self._tcp.isListening()

    @property
    def port(self) -> int:
        return self._tcp.serverPort() if self._tcp.isListening() else 0

    @property
    def viewers(self) -> int:
        return len(self._clients)

    def apply(self, enabled: bool, port: int, key: str, internet: bool = False, download: bool = False) -> bool:
        """Switch remote control on or off. Returns False (and sets `error`) if the port cannot be opened.

        `internet`: reachable from anywhere through the internet link; `download` allows fetching the tunnel
        program for it if it is not on this machine yet."""
        self.error = ""
        if not enabled or not key:
            self.stop()
            return True
        if key != self._key:  # a new key shuts out whoever had the old one
            self._key = key
            for client in list(self._clients):
                client.close(QWebSocketProtocol.CloseCode.CloseCodePolicyViolated, "key")
        if internet:
            port = 0  # only the tunnel, on this machine, connects to it: any free port will do, so none can be "taken"
        if self._tcp.isListening() and ((port and self._tcp.serverPort() != port) or internet != self.internet):
            self._close()
        self.internet = internet
        if not self._tcp.isListening():
            # With the internet link the only one who has to reach the app is the tunnel, on this machine: so
            # nothing else can (and Windows' firewall has nothing to ask). Otherwise: this machine's network.
            where = QHostAddress.SpecialAddress.LocalHost if internet else QHostAddress.SpecialAddress.Any
            if not self._tcp.listen(QHostAddress(where), port):
                self.error = self._tcp.errorString()
                self.tunnel.stop()
                self.changed.emit()
                return False
        if internet:
            self.tunnel.start(self._tcp.serverPort(), download)
        else:
            self.tunnel.stop()
        self.changed.emit()
        return True

    def apply_settings(self, settings: Settings, download: bool = False) -> bool:
        return self.apply(bool(getattr(settings, "remote_enabled", False)),
                          int(getattr(settings, "remote_port", DEFAULT_PORT) or DEFAULT_PORT),
                          str(getattr(settings, "remote_key", "")),
                          getattr(settings, "remote_reach", REACH_LAN) == REACH_INTERNET, download)

    def _close(self) -> None:
        """Stop listening and let go of everyone connected."""
        self._tcp.close()
        for client in list(self._clients):
            client.close()
        self._clients.clear()
        for sock in list(self._waiting):
            if shiboken6.isValid(sock):
                sock.abort()
        self._waiting.clear()
        self.mirror.stop()

    def stop(self) -> None:
        self._close()
        self.tunnel.stop()
        self._key = ""
        self.changed.emit()

    def link(self, host: str | None = None) -> str:
        """The address to open on the other machine, or '' while there is none (off, or the link is not up yet)."""
        if not self.listening or not self._key:
            return ""
        if self.internet:
            return f"{self.tunnel.url}/#k={self._key}" if self.tunnel.url else ""
        return remote_link(self.port, self._key, host)

    def status(self) -> str:
        if self.error:
            return f"Could not start: {self.error}"
        if not self.listening:
            return "Off."
        count = self.viewers
        people = "nobody connected" if not count else f"{count} viewer{'s' if count != 1 else ''} connected"
        if not self.internet:
            return f"On, port {self.port}: {people}."
        link = self.tunnel
        if link.state == tunnel.DOWNLOADING:
            return f"Downloading Cloudflare's tunnel program ({tunnel.DOWNLOAD_MB} MB): {link.progress}%."
        if link.state == tunnel.ON:
            return f"On, reachable from the internet: {people}."
        if link.state == tunnel.FAILED:
            return f"The internet link is not up. {link.error}"
        return "Starting the internet link (a few seconds)…"

    # ---- plain HTTP: the page

    def _incoming(self) -> None:
        while self._tcp.hasPendingConnections():
            sock = self._tcp.nextPendingConnection()
            sock.setSocketOption(QAbstractSocket.SocketOption.LowDelayOption, 1)  # small messages leave at once

            def ready(s=sock):
                self._head(s)

            self._waiting[sock] = ready
            sock.readyRead.connect(ready)
            sock.disconnected.connect(lambda s=sock: self._waiting.pop(s, None))
            QTimer.singleShot(HEAD_TIMEOUT_MS, lambda s=sock: self._expire(s))
            if sock.bytesAvailable():
                self._head(sock)

    def _expire(self, sock) -> None:
        if sock in self._waiting:
            del self._waiting[sock]
            if shiboken6.isValid(sock):
                sock.abort()
                sock.deleteLater()

    def _head(self, sock) -> None:
        if sock not in self._waiting:
            return
        head = bytes(sock.peek(MAX_HEAD))
        end = head.find(b"\r\n\r\n")
        if end < 0:
            if len(head) >= MAX_HEAD:
                self._expire(sock)
            return
        sock.readyRead.disconnect(self._waiting.pop(sock))  # from here on someone else reads this socket
        lines = head[:end].decode("latin-1").split("\r\n")
        parts = lines[0].split(" ")
        headers = {}
        for line in lines[1:]:
            name, _, value = line.partition(":")
            headers[name.strip().lower()] = value.strip()
        if len(parts) < 2 or parts[0] != "GET":
            self._reply(sock, "405 Method Not Allowed", "text/plain", b"GET only")
            return
        if headers.get("upgrade", "").lower() == "websocket":
            if len(self._clients) >= MAX_CLIENTS:
                self._reply(sock, "503 Service Unavailable", "text/plain", b"too many viewers")
                return
            self._wss.handleConnection(sock)  # the socket is the WebSocket server's from here on
            return
        path = urlsplit(parts[1]).path
        if path == "/favicon.ico":
            self._reply(sock, "204 No Content", "image/x-icon", b"")
            return
        page = PAGES.get(path)
        if page is None:
            self._reply(sock, "404 Not Found", "text/plain", b"not found")
            return
        try:
            body = (WEB_DIR / page[0]).read_bytes()
        except OSError:
            self._reply(sock, "500 Internal Server Error", "text/plain", b"page missing")
            return
        host = headers.get("host", "")
        if not re.fullmatch(r"[\w.\-:\[\]]+", host):
            host = ""
        connect = f"'self' ws://{host} wss://{host}" if host else "'self'"
        policy = ("default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                  f"img-src 'self' data: blob:; connect-src {connect}; base-uri 'none'; form-action 'none'; "
                  "frame-ancestors 'none'")
        self._reply(sock, "200 OK", page[1], body, (f"Content-Security-Policy: {policy}",))

    def _reply(self, sock, status: str, content_type: str, body: bytes, extra: tuple = ()) -> None:
        sock.readAll()
        head = [f"HTTP/1.1 {status}", f"Content-Type: {content_type}", f"Content-Length: {len(body)}",
                "Connection: close", "Cache-Control: no-store", "X-Content-Type-Options: nosniff",
                "Referrer-Policy: no-referrer", *extra]
        sock.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + body)
        sock.disconnected.connect(sock.deleteLater)
        sock.disconnectFromHost()

    # ---- the WebSocket: viewers

    def _joined(self) -> None:
        while self._wss.hasPendingConnections():
            client = self._wss.nextPendingConnection()
            given = (parse_qs(urlsplit(client.requestUrl().toString()).query).get("k") or [""])[0]
            if not self._key or not hmac.compare_digest(given.encode("utf-8"), self._key.encode("utf-8")):
                client.disconnected.connect(client.deleteLater)
                client.close(QWebSocketProtocol.CloseCode.CloseCodePolicyViolated, "key")
                continue
            client.setMaxAllowedIncomingMessageSize(MAX_UPLOAD)
            client.textMessageReceived.connect(lambda text, c=client: self._text(c, text))
            client.binaryMessageReceived.connect(lambda data, c=client: self._binary(c, data))
            client.disconnected.connect(lambda c=client: self._left(c))
            self.mirror.start()
            # The whole picture first; only then does it take part in what the others get (the changes).
            client.sendTextMessage(json.dumps(self.mirror.snapshot(), separators=(",", ":"), ensure_ascii=False,
                                              default=str))
            client.flush()
            self._clients.append(client)
            self.changed.emit()

    def _left(self, client) -> None:
        if client in self._clients:
            self._clients.remove(client)
            client.deleteLater()
            if not self._clients:
                self.mirror.stop()  # nobody is looking: the app runs exactly as without this feature
            self.changed.emit()

    def _text(self, client, text: str) -> None:
        try:
            message = json.loads(text)
        except ValueError:
            return
        if not isinstance(message, list) or not message:
            return
        if message[0] == "p":  # a ping: answered at once, it measures the round trip
            client.sendTextMessage(json.dumps(["P", message[1] if len(message) > 1 else 0]))
            client.flush()
            return
        self.mirror.command(client, message)

    def _binary(self, client, data) -> None:
        """A pasted or dropped file: four bytes saying where, two for the length of its name, the name, the file."""
        raw = bytes(data)
        if len(raw) < 6:
            return
        node_id, name_length = struct.unpack(">IH", raw[:6])
        if len(raw) <= 6 + name_length:
            return
        name = raw[6:6 + name_length].decode("utf-8", "replace")
        self.mirror.command(client, ["file", node_id, name, raw[6 + name_length:]])

    def _broadcast(self, message, only=None, skip=None) -> None:
        text = json.dumps(message, separators=(",", ":"), ensure_ascii=False, default=str)
        for client in list(self._clients):
            if (only is not None and client is not only) or client is skip:
                continue
            if client.bytesToWrite() > MAX_BACKLOG:
                client.close(QWebSocketProtocol.CloseCode.CloseCodeGoingAway, "too slow")
                continue
            client.sendTextMessage(text)
            client.flush()


def _add_indicator(controller: RemoteController, window) -> None:
    """A note in the status bar while remote control is on, so it is never on without the user seeing it."""
    bar = window.statusBar() if isinstance(window, QMainWindow) else None
    if bar is None:
        return
    note = QPushButton()
    note.setFlat(True)
    note.setCursor(Qt.CursorShape.PointingHandCursor)
    note.setStyleSheet("QPushButton { color:#e3b341; padding:0 6px; border:none; }")
    bar.addPermanentWidget(note)
    announced = {"link": ""}

    def copy() -> None:
        link = controller.link()
        if link:
            QApplication.clipboard().setText(link)
            bar.showMessage("Remote link copied: open it in a browser on the other machine.", 6000)
        else:
            bar.showMessage(controller.status(), 6000)

    def show() -> None:
        count, link = controller.viewers, controller.tunnel
        if controller.error:  # switched on in Settings but the port could not be opened: say so, here
            text = f"🔗 Remote: not running ({controller.error})"
        elif controller.internet and link.state == tunnel.DOWNLOADING:
            text = f"🔗 Remote: downloading the internet link… {link.progress}%"
        elif controller.internet and link.state == tunnel.FAILED:
            text = "🔗 Remote: the internet link is not up"
        elif controller.internet and link.state != tunnel.ON:
            text = "🔗 Remote: starting the internet link…"
        else:
            where = " (internet)" if controller.internet else ""
            text = f"🔗 Remote{where}: {count} connected" if count else f"🔗 Remote{where}: on"
        note.setText(text)
        note.setToolTip(controller.status() + "\n" + ("Click to copy the link for the other machine."
                                                      if controller.link() else "Settings has the details."))
        note.setVisible(controller.listening or bool(controller.error))
        ready = controller.link() if controller.internet else ""
        if ready and ready != announced["link"]:  # the address is new every time: say when it is there
            bar.showMessage("The internet link is ready: click \"🔗 Remote\" (bottom right) to copy it.", 10000)
        announced["link"] = ready

    note.clicked.connect(copy)
    controller.changed.connect(show)
    show()


def install_remote(app, window) -> RemoteController:
    """Create the remote-control server for the running app + main window and apply the saved settings.

    The controller is stored on the app so the Settings dialog can switch it on and off live."""
    controller = RemoteController(app, window)
    app._remote_controller = controller
    _add_indicator(controller, window)
    settings = Settings.load()
    if getattr(settings, "remote_enabled", False):
        if not getattr(settings, "remote_key", ""):
            settings.remote_key = new_key()
            settings.save()
        # Once the window is up and the event loop runs. Nothing is downloaded here: that is only done from
        # Settings, where the user is asked.
        QTimer.singleShot(0, lambda: controller.apply_settings(Settings.load()))
    return controller
