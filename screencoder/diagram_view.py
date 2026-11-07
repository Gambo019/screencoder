"""Mermaid diagrams (mode "System Design"), drawn by the real Mermaid library and shown as a picture.

Mermaid runs in a web page that never appears on screen: it turns the diagram text into a PNG, and
DiagramCanvas paints that picture itself. A widget that paints itself is mirrored to the remote page like any
other (remote.py shows it as a "canvas"), and no new window exists for screen capture or stealth mode to deal
with. The library is bundled (assets/mermaid, MIT licence), so nothing is fetched and no diagram leaves the PC."""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QSizePolicy, QSplitter, QToolButton, QVBoxLayout,
    QWidget,
)

ASSETS = Path(__file__).with_name("assets") / "mermaid"
BACKGROUND = "#0d1117"  # the app's text views (app.STYLE)
SCALE = 2  # drawn at twice its size: sharp on a large tab, when zoomed in, and on HiDPI screens
RENDER_TIMEOUT_MS = 20_000
# What he adds or changes in a turn is marked `:::new` (prompts.MERMAID_RULES): green, so it stands out.
NEW_CLASS = "classDef new fill:#12261a,stroke:#3fb950,stroke-width:3px,color:#e6edf3"
NEW_USED = re.compile(r":::\s*new\b")
FLOWCHART = re.compile(r"^\s*(flowchart|graph)\b", re.M)
FENCE = re.compile(r"^\s*```[\w-]*\s*\n(.*?)\n\s*```\s*$", re.S)
TITLE_LINE = re.compile(r"^\s*%%(?!\{)\s*(.+?)\s*$")

# GitHub's dark colours, as in the rest of the app.
THEME = {
    "darkMode": True, "background": BACKGROUND, "fontFamily": "Segoe UI, Arial, sans-serif", "fontSize": "15px",
    "primaryColor": "#161b22", "primaryTextColor": "#e6edf3", "primaryBorderColor": "#58a6ff",
    "secondaryColor": "#1c2333", "tertiaryColor": "#11161d", "lineColor": "#8b949e", "textColor": "#e6edf3",
    "mainBkg": "#161b22", "nodeBorder": "#58a6ff", "clusterBkg": "#11161d", "clusterBorder": "#30363d",
    "titleColor": "#c9d1d9", "edgeLabelBackground": "#21262d",
    "actorBkg": "#161b22", "actorBorder": "#58a6ff", "actorTextColor": "#e6edf3", "actorLineColor": "#6e7681",
    "signalColor": "#c9d1d9", "signalTextColor": "#e6edf3", "labelBoxBkgColor": "#21262d",
    "labelBoxBorderColor": "#58a6ff", "labelTextColor": "#e6edf3", "loopTextColor": "#e6edf3",
    "noteBkgColor": "#2d2a1a", "noteTextColor": "#e6edf3", "noteBorderColor": "#e3b341",
    "activationBkgColor": "#21262d", "activationBorderColor": "#58a6ff", "sequenceNumberColor": "#0d1117",
    "attributeBackgroundColorOdd": "#161b22", "attributeBackgroundColorEven": "#1c2333",
}

# Labels as SVG text, not HTML: a picture with HTML in it cannot be turned into a PNG (the canvas is "tainted").
PAGE = """<!doctype html><html><head><meta charset="utf-8"><script src="mermaid.min.js"></script></head><body>
<script>
mermaid.initialize({startOnLoad: false, securityLevel: 'strict', theme: 'base', themeVariables: %THEME%,
  htmlLabels: false, flowchart: {htmlLabels: false, curve: 'basis', padding: 12},
  sequence: {useMaxWidth: false}, er: {useMaxWidth: false}});
window.__out = {};
window.takeOut = function (id) {
  const r = window.__out[id];
  if (r === undefined) return null;
  delete window.__out[id];
  return JSON.stringify(r);
};
window.renderPng = async function (id, code, scale) {
  const name = 'scd' + id;
  try {
    await mermaid.parse(code);
    const {svg} = await mermaid.render(name, code);
    const holder = document.createElement('div');
    holder.innerHTML = svg;
    const el = holder.querySelector('svg');
    const box = el.viewBox.baseVal;
    const w = Math.max(1, Math.ceil(box.width)), h = Math.max(1, Math.ceil(box.height));
    el.setAttribute('width', w);
    el.setAttribute('height', h);
    el.style.maxWidth = 'none';
    const img = new Image();
    img.src = 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(new XMLSerializer().serializeToString(el));
    await img.decode();
    const pad = 16;
    const canvas = document.createElement('canvas');
    canvas.width = (w + 2 * pad) * scale;
    canvas.height = (h + 2 * pad) * scale;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '%BG%';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.scale(scale, scale);
    ctx.drawImage(img, pad, pad, w, h);
    window.__out[id] = {png: canvas.toDataURL('image/png')};
  } catch (e) {
    window.__out[id] = {error: String((e && e.message) || e)};
  } finally {
    const left = document.getElementById('d' + name) || document.getElementById(name);
    if (left) left.remove();  // what Mermaid leaves in the page after an error
  }
};
</script></body></html>""".replace("%THEME%", json.dumps(THEME)).replace("%BG%", BACKGROUND)


def prepare(source: str, settled: bool = False, fresh: set[str] | frozenset = frozenset()) -> str:
    """The diagram as Mermaid gets it: without a code fence around it, and with the boxes of `fresh` (what the
    latest drawing added or changed: fresh_nodes) in the "new" class. `settled`: nothing is new any more."""
    text = source.replace("\r\n", "\n").lstrip("﻿").strip()
    fenced = FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    text = _QUOTED.sub(lambda m: _plain_label(m.group(0)), text)
    if settled or fresh:
        text = NEW_USED.sub("", text)  # marks of his own (older prompts): the app says what is new
    if not settled and fresh and FLOWCHART.search(text):
        text += f"\n{NEW_CLASS}\nclass {','.join(sorted(fresh))} new"
    elif not settled and FLOWCHART.search(text) and NEW_USED.search(text) and "classDef new" not in text:
        text += "\n" + NEW_CLASS
    return text


# The boxes of a flowchart, to see what a drawing added: an id with a shape (`lb["Load balancer"]`), or one at
# either end of an arrow (`api --> db`). Statements that are not boxes are left out.
_NOT_BOXES = re.compile(r"^\s*(flowchart|graph|subgraph|end|classDef|class|style|linkStyle|click|direction)\b", re.I)
_QUOTED = re.compile(r'"[^"]*"')
_EDGE_LABEL = re.compile(r"\|[^|]*\|")
_SHAPE = re.compile(r"[\[\(\{>][^\n]*?[\]\)\}]+")
_ARROW = re.compile(r"<?[-=.~]{2,}[>xo]?")
_BOX_ID = re.compile(r"\b[A-Za-z_]\w*\b")
_LABEL = re.compile(r'\b([A-Za-z_]\w*)\s*[\[\(\{>]+\s*"([^"]*)"')


_BREAK = re.compile(r"<br\s*/?>", re.I)
_LABEL_SIGNS = (("<->", "↔"), ("->", "→"), ("<-", "←"), ("=>", "⇒"), (">", "›"), ("<", "‹"), ("&", "+"))


def _plain_label(quoted: str) -> str:
    """A quoted label as the picture shows it right: drawn as text, Mermaid prints `>`, `<` and `&` as `&gt;`,
    `&lt;` and `&amp;` - so "code -> URL" becomes "code → URL". Line breaks (<br/>) stay."""
    parts = _BREAK.split(quoted)
    for i, part in enumerate(parts):
        for sign, shown in _LABEL_SIGNS:
            part = part.replace(sign, shown)
        parts[i] = part
    return "<br/>".join(parts)


def boxes(source: str) -> dict[str, str]:
    """Box id -> its label ("" when it has none), of a flowchart."""
    found: dict[str, str] = {}
    if not FLOWCHART.search(source):
        return found
    for line in source.splitlines():
        line = line.split("%%", 1)[0]
        if not line.strip() or _NOT_BOXES.match(line):
            continue
        for box, label in _LABEL.findall(line):
            found[box] = label
        # Labels and arrows out first (an arrow's ">" would read as a shape), then the shapes: ids are left.
        bare = _ARROW.sub(" ", _QUOTED.sub('""', _EDGE_LABEL.sub(" ", NEW_USED.sub("", line))))
        for box in _BOX_ID.findall(_SHAPE.sub(" ", bare).replace("&", " ")):
            found.setdefault(box, "")
    return found


def fresh_nodes(before: str, after: str) -> set[str]:
    """The boxes a drawing added, or relabelled, compared with the version before it."""
    old, new = boxes(before), boxes(after)
    return {box for box, label in new.items() if box not in old or (label and label != old[box])}


def title_of(source: str, name: str) -> str:
    """A diagram's name: its first line, when that is a comment (`%% Architecture`), else its file name."""
    for line in source.splitlines():
        if not line.strip():
            continue
        found = TITLE_LINE.match(line)
        if found:
            return found.group(1)[:60]
        break
    stem = Path(name).stem.replace("-", " ").replace("_", " ").strip()
    return (stem[:1].upper() + stem[1:]) if stem else name


class MermaidRenderer(QObject):
    """Turns Mermaid text into pictures, one at a time, in an offscreen web page (started on first use)."""

    rendered = Signal(int, QImage)  # request id, the picture
    failed = Signal(int, str)  # request id, why (Mermaid's parse error, or that it could not run)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page = None
        self._ready = False
        self._broken = ""  # set when the page could not be made: every request fails with it
        self._queue: list[tuple[int, str]] = []
        self._current: tuple[int, int] | None = None  # (request id, polls so far)
        self._next = 0
        self._poll = QTimer(self)
        self._poll.setInterval(40)
        self._poll.timeout.connect(self._check)

    def warm(self) -> None:
        """Start the page now, so that the first diagram does not wait for it."""
        self._ensure_page()

    def render(self, source: str) -> int:
        self._next += 1
        self._queue.append((self._next, prepare(source)))
        self._ensure_page()
        self._pump()
        return self._next

    def _ensure_page(self) -> None:
        if self._page is not None or self._broken:
            return
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage  # Qt WebEngine (PySide6-Addons)
        except ImportError as exc:  # noqa: BLE001
            self._broken = f"diagrams need Qt WebEngine, which is not installed ({exc})"
            QTimer.singleShot(0, self._pump)
            return
        if not (ASSETS / "mermaid.min.js").is_file():
            self._broken = f"the Mermaid library is missing ({ASSETS / 'mermaid.min.js'})"
            QTimer.singleShot(0, self._pump)
            return
        self._page = QWebEnginePage(self)  # the default profile: off the record, nothing is stored
        self._page.loadFinished.connect(self._loaded)
        self._page.setHtml(PAGE, QUrl.fromLocalFile(str(ASSETS) + "/"))

    def _loaded(self, ok: bool) -> None:
        if ok:
            self._ready = True
        else:
            self._broken = "the diagram page did not load"
        self._pump()

    def _pump(self) -> None:
        if self._broken:
            while self._queue:
                self.failed.emit(self._queue.pop(0)[0], self._broken)
            return
        if not self._ready or self._current is not None or not self._queue:
            return
        rid, code = self._queue.pop(0)
        self._current = (rid, 0)
        self._page.runJavaScript(f"window.renderPng({rid}, {json.dumps(code)}, {SCALE})")
        self._poll.start()

    def _check(self) -> None:
        if self._current is None:
            self._poll.stop()
            return
        rid, polls = self._current
        if polls * self._poll.interval() > RENDER_TIMEOUT_MS:
            self._finish(rid, None, "drawing it took too long")
            return
        self._current = (rid, polls + 1)
        self._page.runJavaScript(f"window.takeOut({rid})", 0, lambda result, rid=rid: self._result(rid, result))

    def _result(self, rid: int, result) -> None:
        if self._current is None or self._current[0] != rid or not result:
            return
        try:
            data = json.loads(result)
        except (TypeError, ValueError):
            self._finish(rid, None, "no picture came back")
            return
        if "png" in data:
            image = QImage.fromData(base64.b64decode(data["png"].split(",", 1)[1]), "PNG")
            self._finish(rid, image if not image.isNull() else None, "" if not image.isNull() else "a broken picture")
        else:
            self._finish(rid, None, data.get("error") or "it could not be drawn")

    def _finish(self, rid: int, image: QImage | None, error: str) -> None:
        self._current = None
        self._poll.stop()
        if image is not None:
            self.rendered.emit(rid, image)
        else:
            self.failed.emit(rid, error)
        self._pump()


class DiagramCanvas(QWidget):
    """Paints one diagram: fitted to the space at first; the wheel zooms where the mouse is, dragging moves it,
    a double-click fits it again."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(160, 120)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(False)
        self._image: QImage | None = None
        self._message = ""
        self._zoom: float | None = None  # None: fitted
        self._offset = QPointF(0, 0)  # where the picture's top left is, when zoomed
        self._drag: QPointF | None = None

    def set_image(self, image: QImage | None, keep_view: bool = False) -> None:
        if not (keep_view and self._image is not None and image is not None):
            self._zoom = None
        self._image = image
        self.update()

    def set_message(self, text: str) -> None:
        self._message = text
        self.update()

    def _fit(self) -> float:
        img = self._image
        width, height = img.width() / SCALE, img.height() / SCALE
        margin = 8
        return min((self.width() - 2 * margin) / width, (self.height() - 2 * margin) / height, 1.5)

    def _rect(self) -> QRectF:
        img = self._image
        width, height = img.width() / SCALE, img.height() / SCALE
        if self._zoom is None:
            scale = self._fit()
            w, h = width * scale, height * scale
            return QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h)
        return QRectF(self._offset.x(), self._offset.y(), width * self._zoom, height * self._zoom)

    def paintEvent(self, _event) -> None:  # noqa: N802 (Qt override)
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(BACKGROUND))
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        if self._image is not None:
            painter.drawImage(self._rect(), self._image)
        if self._message:
            painter.setPen(QPen(QColor("#e3b341" if self._image is not None else "#8b949e")))
            if self._image is not None:  # a note over the picture: at the bottom, on a band of its own
                band = QRectF(0, self.height() - 34, self.width(), 34)
                painter.fillRect(band, QColor(13, 17, 23, 225))
                painter.drawText(band.adjusted(10, 0, -10, 0), Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextWordWrap,
                                 self._message)
            else:
                painter.drawText(QRectF(self.rect()).adjusted(24, 24, -24, -24),
                                 Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self._message)
        painter.end()

    def wheelEvent(self, event) -> None:  # noqa: N802
        if self._image is None:
            return
        rect = self._rect()
        current = rect.width() / (self._image.width() / SCALE)
        factor = 1.15 if event.angleDelta().y() > 0 else 1 / 1.15
        zoom = max(0.1, min(current * factor, 4.0))
        at = event.position()
        # The point under the mouse stays under the mouse.
        self._offset = QPointF(at.x() - (at.x() - rect.x()) * zoom / current,
                               at.y() - (at.y() - rect.y()) * zoom / current)
        self._zoom = zoom
        self.update()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if self._image is not None and event.button() == Qt.MouseButton.LeftButton:
            rect = self._rect()
            if self._zoom is None:
                self._zoom = rect.width() / (self._image.width() / SCALE)
                self._offset = rect.topLeft()
            self._drag = event.position()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag is not None:
            self._offset += event.position() - self._drag
            self._drag = event.position()
            self.update()

    def mouseReleaseEvent(self, _event) -> None:  # noqa: N802
        self._drag = None

    def mouseDoubleClickEvent(self, _event) -> None:  # noqa: N802
        self._zoom = None
        self.update()


@dataclass
class Diagram:
    name: str  # its file name
    source: str = ""
    title: str = ""
    image: QImage | None = None  # the last picture that could be drawn
    error: str = ""  # why the latest text could not be drawn ("" = it was)
    pending: int = 0  # the request for the latest text, while it is being drawn
    settled: bool = False  # drawn without its "new" marks: they belong to an earlier turn (settle_new)
    fresh: set = field(default_factory=set)  # the boxes the latest drawing added or changed (fresh_nodes)

    @property
    def marked(self) -> bool:
        """Is something on it shown as just added or changed?"""
        return not self.settled and (bool(self.fresh) or bool(NEW_USED.search(self.source)))


class DiagramPanel(QWidget):
    """The Diagram tab: which diagram, the picture, and its Mermaid text to copy or read."""

    updated = Signal(str)  # a diagram has a new picture (its file name)
    render_failed = Signal(str, str)  # a diagram's latest text cannot be drawn: file name, Mermaid's error

    def __init__(self, renderer: MermaidRenderer | None = None, parent=None):
        """`renderer`: one shared by several panels (one page draws for all of them)."""
        super().__init__(parent)
        self._diagrams: dict[str, Diagram] = {}
        self._requests: dict[int, str] = {}  # request id -> file name
        self._shown = ""  # the diagram on screen: a new version of it keeps the zoom
        self._quiet: set[int] = set()  # requests that only redraw a diagram without its marks (settle_new)
        self.renderer = renderer or MermaidRenderer(self)
        self.renderer.rendered.connect(self._rendered)  # a picture of another panel's is not in _requests
        self.renderer.failed.connect(self._failed)

        self.picker = QComboBox()
        self.picker.setToolTip("Which diagram is shown")
        self.picker.currentIndexChanged.connect(self._show_current)
        self.state = QLabel()
        self.state.setStyleSheet("color:#8b949e; font-size:11px;")
        self.state.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.source_btn = QToolButton()
        self.source_btn.setText("Mermaid")
        self.source_btn.setCheckable(True)
        self.source_btn.setToolTip("Show the diagram's Mermaid text under it")
        self.copy_btn = QPushButton("📋 Copy Mermaid")
        self.copy_btn.setToolTip("Copy the diagram as Mermaid text - Excalidraw, Miro, draw.io and mermaid.live can "
                                 "import it")
        self.copy_btn.clicked.connect(self._copy)
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self.picker)
        head.addWidget(self.state, 1)
        head.addWidget(self.source_btn)
        head.addWidget(self.copy_btn)
        self.canvas = DiagramCanvas()
        self.source = QPlainTextEdit()
        self.source.setReadOnly(True)
        self.source.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.source.hide()
        self.source_btn.toggled.connect(self.source.setVisible)
        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.canvas)
        split.addWidget(self.source)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(head)
        layout.addWidget(split, 1)
        self.clear()

    def warm(self) -> None:
        self.renderer.warm()

    def clear(self) -> None:
        self._diagrams.clear()
        self._requests.clear()
        self.picker.blockSignals(True)
        self.picker.clear()
        self.picker.blockSignals(False)
        self.picker.hide()
        self.canvas.set_image(None)
        self.canvas.set_message("No diagram yet - he draws here as the design takes shape.")
        self.source.clear()
        self.state.clear()
        self.copy_btn.setEnabled(False)
        self._shown = ""
        self._quiet.clear()

    def has_diagrams(self) -> bool:
        return any(d.image is not None for d in self._diagrams.values())

    def show_diagram(self, name: str, source: str) -> None:
        """A diagram file was written: draw it, and show it (the latest change is what matters)."""
        diagram = self._diagrams.get(name)
        if diagram is None:
            diagram = self._diagrams[name] = Diagram(name)
        if (prepare(source) == prepare(diagram.source) and not diagram.settled
                and (diagram.image is not None or diagram.pending)):
            return
        # Green: what this drawing added to the one before. A first version has nothing to stand out from.
        diagram.fresh = fresh_nodes(diagram.source, source) if diagram.source.strip() else set()
        diagram.source, diagram.title, diagram.settled = source, title_of(source, name), False
        self._draw(diagram)
        self._refresh_picker(select=name)

    def settle_new(self, keep: str = "") -> None:
        """What is marked new on the diagrams stays new only until he draws again: then it is drawn plain, and
        only that turn's changes stand out. `keep`: the diagram he is drawing now."""
        for diagram in self._diagrams.values():
            if diagram.name != keep and diagram.marked:
                diagram.settled = True
                self._draw(diagram)
                self._quiet.add(diagram.pending)  # the same diagram, only plainer: no news

    def _draw(self, diagram: Diagram) -> None:
        diagram.pending = self.renderer.render(prepare(diagram.source, diagram.settled, diagram.fresh))
        self._requests[diagram.pending] = diagram.name

    def errors(self) -> dict[str, str]:
        """The diagrams whose latest text cannot be drawn (none still being drawn)."""
        return {d.name: d.error for d in self._diagrams.values() if d.error and not d.pending}

    def current(self) -> Diagram | None:
        return self._diagrams.get(self.picker.currentData()) if self.picker.count() else None

    def _refresh_picker(self, select: str = "") -> None:
        names = list(self._diagrams)
        keep = select or self.picker.currentData()
        self.picker.blockSignals(True)
        self.picker.clear()
        for name in names:
            self.picker.addItem(self._diagrams[name].title, name)
        index = names.index(keep) if keep in names else 0
        self.picker.setCurrentIndex(index)
        self.picker.blockSignals(False)
        self.picker.setVisible(len(names) > 1)
        self._show_current()

    def _show_current(self, *_args) -> None:
        diagram = self.current()
        if diagram is None:
            return
        self.canvas.set_image(diagram.image, keep_view=diagram.name == self._shown)
        self._shown = diagram.name
        if diagram.error:
            self.canvas.set_message(f"⚠ This version does not draw - {diagram.error.splitlines()[0][:160]}"
                                    + (" (showing the last one that did)" if diagram.image is not None else ""))
        elif diagram.image is None:
            self.canvas.set_message("Drawing…")
        else:
            self.canvas.set_message("")
        if self.source.toPlainText() != diagram.source:
            self.source.setPlainText(diagram.source)
        new = "green = just added or changed · " if diagram.marked else ""
        self.state.setText(f"{diagram.title} · {new}wheel to zoom, drag to move, double-click to fit")
        self.copy_btn.setEnabled(bool(diagram.source.strip()))

    def _rendered(self, rid: int, image: QImage) -> None:
        name = self._requests.pop(rid, None)
        diagram = self._diagrams.get(name or "")
        if diagram is None or diagram.pending != rid:
            return  # an older version: a newer one is on its way
        diagram.image, diagram.error, diagram.pending = image, "", 0
        if self.picker.currentData() == name:
            self._show_current()
        if rid in self._quiet:
            self._quiet.discard(rid)
        else:
            self.updated.emit(name)

    def _failed(self, rid: int, error: str) -> None:
        self._quiet.discard(rid)
        name = self._requests.pop(rid, None)
        diagram = self._diagrams.get(name or "")
        if diagram is None or diagram.pending != rid:
            return
        diagram.error, diagram.pending = error or "it could not be drawn", 0
        if self.picker.currentData() == name:
            self._show_current()
        self.render_failed.emit(name, diagram.error)

    def _copy(self) -> None:
        diagram = self.current()
        if diagram is not None and diagram.source.strip():
            # Plain: the green of "just added" means nothing on anybody else's board.
            QGuiApplication.clipboard().setText(prepare(diagram.source, settled=True))
