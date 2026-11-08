"""See-through: the main window's backgrounds - its panels, text views, tabs, bars, chat - become transparent, so the
desktop shows behind it, and only what is on it stays: text, borders, buttons, pictures. Off by default; a tick box
in the status bar and Settings switch it, and it is remembered.

How (measured on Windows 11, see the README): per-pixel transparency, which Windows gives a window only without
its native frame - so in this mode the window has a title bar of its own (drag to move; double-click or □ to
maximise; – and ×), and is resized from a thin rim along its edges. Over the backgrounds lies one even dark
tint, the Backdrop (the slider beside the tick box, 0-90%): never quite 0, so every pixel stays this window's and
the mouse never falls through a gap (it did with a colour key: the toolbar's >> popup closed on the way to its
buttons). Text gets a crisp dark outline (Outline), so it can be read over a white, grey or black desktop alike:
on each text view's viewport, on the bars as a whole, on each label, tick box and input box elsewhere - never on a
large part of the window at once (a blurred glow on the whole content cost 55 ms per update, measured), and never
one inside another. Soft blurred glows were the first try: too faint over white at 85% opacity.

Built from the outside, like stealth.py and opacity.py: style sheets of the main window's widgets are rewritten
while it is on (and put back after); the frameless flag goes through the stealth controller, which rebuilds the
window whenever flags change and would otherwise drop it."""
from __future__ import annotations

import re

import shiboken6
from PySide6.QtCore import QEvent, QObject, QPoint, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import (
    QAbstractScrollArea, QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QGraphicsEffect, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPushButton, QRadioButton, QSlider, QStatusBar, QToolBar, QWidget,
)

from .config import Settings

BACKDROP_RGB = (13, 17, 23)  # the app's darkest background
MIN_ALPHA = 3                # out of 255: looks clear, still catches the mouse
DEFAULT_BACKDROP = 0.35
MAX_BACKDROP = 0.9
# The outline: rings of (distance in px, darkness 0-255) - solid black right at the letters, softer just outside.
RINGS = ((2, 120), (1, 255))
EDGE = 5                     # px of rim along the window's edges, to resize it by
_CURSORS = {
    Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor, Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
    Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor, Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
}

# What keeps its own look: what moves or is pressed, picked or pointed at - buttons, ticks, scroll bars, selections.
_KEEP = re.compile(r":(hover|pressed|checked|selected|focus|disabled)|::(handle|indicator|tab|chunk|groove|add-|sub-)"
                   r"|QPushButton|QToolButton|QCheckBox|QRadioButton|QSlider|QScrollBar|QProgressBar|QHeaderView", re.I)
_BACKGROUND = re.compile(r"(?<![-\w])background(?:-color)?\s*:\s*[^;}]+")
_CLEAR = ("QTabWidget::pane { background: transparent; border: 1px solid #30363d; } "
          "QStatusBar, QToolBar, QTextBrowser, QPlainTextEdit, QTextEdit, QListWidget, QTreeWidget, QTreeView, "
          "QListView, QScrollArea, QFrame#approval { background: transparent; } QToolBar { border: none; } "
          # A group's title sits on its frame, where no outline goes (the group holds whole views): a dark chip.
          "QGroupBox::title { background: rgba(13, 17, 23, 200); border-radius: 3px; }")


def blank(sheet: str, colour: str = "transparent") -> str:
    """A style sheet with its backgrounds in `colour` - except those of what keeps its look."""
    rules = []
    for rule in sheet.split("}"):
        if "{" in rule:
            selector, body = rule.split("{", 1)
            if not _KEEP.search(selector):
                body = _BACKGROUND.sub(f"background: {colour}", body)
            rule = selector + "{" + body
        rules.append(rule)
    return "}".join(rules)


class Outline(QGraphicsEffect):
    """A dark outline around all a widget paints - its text above all: what it painted, in black, laid around it
    one and two pixels out, then the widget's own picture on top."""

    PAD = max(r for r, _ in RINGS)

    def boundingRectFor(self, rect: QRectF) -> QRectF:  # noqa: N802 (Qt override)
        return rect.adjusted(-self.PAD, -self.PAD, self.PAD, self.PAD)

    def draw(self, painter: QPainter) -> None:
        offset = QPoint()  # PySide fills it in: where the picture goes, in device pixels
        pixmap = self.sourcePixmap(Qt.CoordinateSystem.DeviceCoordinates, offset,
                                   QGraphicsEffect.PixmapPadMode.PadToEffectiveBoundingRect)
        if pixmap.isNull():
            return
        dark = QPixmap(pixmap.size())
        dark.setDevicePixelRatio(pixmap.devicePixelRatio())
        dark.fill(Qt.GlobalColor.transparent)
        p = QPainter(dark)
        p.drawPixmap(0, 0, pixmap)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        p.fillRect(dark.rect(), Qt.GlobalColor.black)
        p.end()
        world = painter.worldTransform()
        painter.setWorldTransform(QTransform())
        for radius, darkness in RINGS:
            painter.setOpacity(darkness / 255)
            for dx in (-radius, 0, radius):
                for dy in (-radius, 0, radius):
                    if dx or dy:
                        painter.drawPixmap(offset.x() + dx, offset.y() + dy, dark)
        painter.setOpacity(1.0)
        painter.drawPixmap(offset, pixmap)
        painter.setWorldTransform(world)


# What gets an outline of its own: the bars as a whole (small, seldom repainted), each text view's viewport, and
# each label, tick box and input box that is in none of those.
_BARS = (QToolBar, QStatusBar)
_SMALL = (QLabel, QCheckBox, QRadioButton, QLineEdit, QComboBox, QAbstractSpinBox)


class TitleBar(QWidget):
    """The window's own title bar while it has no native frame: drag it to move the window,
    double-click or □ to maximise, – and ×. Kept off the remote page (remote_skip)."""

    def __init__(self, window: QMainWindow) -> None:
        super().__init__()
        self._window = window
        self._normal = None  # the window's place before it was maximised here
        self.setProperty("remote_skip", True)
        self.setStyleSheet("TitleBar, QLabel { background: transparent; } QLabel { color: #c9d1d9; } "
                           "QPushButton { background: transparent; color: #c9d1d9; border: none; padding: 2px 12px; "
                           "font-size: 13px; } QPushButton:hover { background: #30363d; } "
                           "QPushButton#close:hover { background: #da3633; color: white; }")
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 2, 2, 2)
        row.setSpacing(0)
        self.title = QLabel(window.windowTitle())
        row.addWidget(self.title, 1)
        for text, tip, slot, name in (("–", "Minimise", window.showMinimized, ""),
                                      ("□", "Maximise / restore (or double-click the title bar)", self.toggle_max, ""),
                                      ("×", "Close", window.close, "close")):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.setObjectName(name)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.clicked.connect(slot)
            row.addWidget(button)
        window.windowTitleChanged.connect(self.title.setText)

    @property
    def maximised(self) -> bool:
        return self._normal is not None

    def toggle_max(self) -> None:
        """Maximised to the monitor's work area, so the taskbar stays in sight (a frameless window maximised by
        Windows would cover it)."""
        w = self._window
        if self._normal is not None:
            w.setGeometry(self._normal)
            self._normal = None
        else:
            self._normal = w.geometry()
            w.setGeometry(w.screen().availableGeometry())

    def mousePressEvent(self, event) -> None:  # noqa: N802 (Qt override)
        w = self._window
        if event.button() != Qt.MouseButton.LeftButton or w.windowHandle() is None:
            return
        if self._normal is not None:
            # Dragged off its maximised place: back to its normal size, under the mouse as it was over the bar.
            normal, self._normal = self._normal, None
            spot = event.globalPosition().toPoint()
            share = (spot.x() - w.x()) / max(1, w.width())
            normal.moveTo(spot.x() - round(share * normal.width()), spot.y() - min(event.position().y(), 20))
            w.setGeometry(normal)
        w.windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, _event) -> None:  # noqa: N802
        self.toggle_max()


class SeeThrough(QObject):
    changed = Signal()

    def __init__(self, app, window) -> None:
        super().__init__(window)
        self._app, self._window = app, window
        self._on = False
        settings = Settings.load()
        self.backdrop = min(MAX_BACKDROP, max(0.0, float(getattr(settings, "see_through_backdrop", DEFAULT_BACKDROP))))
        self._globals: dict = {}
        self._outlined: list = []  # widgets given an outline, to take it off again
        self.title_bar: TitleBar | None = None
        self._edges = Qt.Edge(0)
        self._cursor_set = False
        self._hover = QTimer(self)  # the resize cursor along the edges (most widgets track no mouse moves)
        self._hover.setInterval(80)
        self._hover.timeout.connect(self._edge_cursor)
        self._pending: list = []  # widgets made or restyled while it is on, looked at on the next turn
        self._margins = None
        self._no_background = False
        app.installEventFilter(self)
        # At the app's end the window is torn down while events still pass here: stop looking at them first.
        app.aboutToQuit.connect(lambda: app.removeEventFilter(self))

    @property
    def on(self) -> bool:
        return self._on

    def toggle(self) -> None:
        self.set(not self._on, persist=True)

    def set(self, on: bool, persist: bool = False) -> None:
        on = bool(on)
        if persist:
            self._save(see_through=on)
        if on == self._on:
            return
        self._on = on
        self._frame(on)
        self._colours(on)
        self._outlines(on)
        if on:
            self._hover.start()
        else:
            self._hover.stop()
            self._set_cursor(Qt.Edge(0))
        self.changed.emit()

    def set_backdrop(self, value: float, persist: bool = False) -> None:
        self.backdrop = min(MAX_BACKDROP, max(0.0, float(value)))
        if self._on:
            self._window.update()
        if persist:
            self._save(see_through_backdrop=self.backdrop)

    def _save(self, **values) -> None:
        try:
            settings = Settings.load()
            for key, value in values.items():
                setattr(settings, key, value)
            settings.save()
        except Exception:  # noqa: BLE001 - remembering it is a nicety
            pass

    # ---- the window: frameless and translucent, with a title bar of its own

    def _frame(self, on: bool) -> None:
        w = self._window
        frameless = int(Qt.WindowType.FramelessWindowHint)
        was_max = w.isMaximized()
        if was_max:
            w.showNormal()
        if on:
            self._no_background = w.testAttribute(Qt.WidgetAttribute.WA_NoSystemBackground)
        w.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, on)
        if not on:
            # Qt switched the window's own background off with the translucency, and does not switch it back on:
            # without it, the window drew over its old picture.
            w.setAttribute(Qt.WidgetAttribute.WA_NoSystemBackground, self._no_background)
        stealth = getattr(self._app, "_stealth_controller", None)
        if stealth is not None:
            # Stealth rebuilds the window from these flags whenever it switches anything: it has to know.
            stealth._base_flags = (stealth._base_flags | frameless) if on else (stealth._base_flags & ~frameless)
            stealth._apply()
        else:
            geometry = w.saveGeometry()
            flags = w.windowFlags()
            w.setWindowFlags((flags | Qt.WindowType.FramelessWindowHint) if on
                             else (flags & ~Qt.WindowType.FramelessWindowHint))
            w.restoreGeometry(geometry)
            w.show()
        from .stealth import reset_layering
        reset_layering(w)
        if stealth is not None:
            stealth.refresh_click_through()  # the layered bit was taken off and on: click-through's bit with it
        if on:
            # A rim of the window's own (tinted) pixels all round: what is pressed there resizes it, and the
            # scroll bars along the sides stay theirs.
            self._margins = w.contentsMargins()
            w.setContentsMargins(EDGE, 0, EDGE, EDGE)
            self.title_bar = TitleBar(w)
            w.setMenuWidget(self.title_bar)
            if was_max:
                QTimer.singleShot(0, self.title_bar.toggle_max)
        else:
            maximised = self.title_bar is not None and self.title_bar.maximised
            w.setMenuWidget(None)
            self.title_bar = None
            if self._margins is not None:
                w.setContentsMargins(self._margins)
                self._margins = None
            if maximised:
                w.showMaximized()

    # ---- the colours

    def _sheet(self) -> str:
        return f"QMainWindow {{ background: transparent; }} {_CLEAR}"

    def _tint(self) -> QColor:
        return QColor(*BACKDROP_RGB, max(MIN_ALPHA, round(self.backdrop * 255)))

    def _colours(self, on: bool) -> None:
        w = self._window
        from . import code_view, diagram_view  # views that paint their background themselves
        if on:
            w.setStyleSheet(self._sheet())
            self._globals = {(code_view, "EDITOR_BG"): code_view.EDITOR_BG,
                             (diagram_view, "BACKGROUND"): diagram_view.BACKGROUND}
            for (module, name) in self._globals:
                setattr(module, name, "transparent")
            for child in w.findChildren(QWidget):
                self._blank(child)
        else:
            w.setStyleSheet("")
            for (module, name), value in self._globals.items():
                setattr(module, name, value)
            self._globals = {}
            for child in w.findChildren(QWidget):
                self._restore(child)
        for child in [w, *w.findChildren(QWidget)]:
            child.update()

    def _blank(self, w: QWidget) -> None:
        if not shiboken6.isValid(w) or w is self._window or isinstance(w, TitleBar):
            return
        sheet = w.styleSheet()
        if not sheet or "background" not in sheet or sheet == w.property("_see_blank"):
            return
        new = blank(sheet)
        w.setProperty("_see_original", sheet)
        w.setProperty("_see_blank", new)
        if new != sheet:
            w.setStyleSheet(new)

    def _restore(self, w: QWidget) -> None:
        original = w.property("_see_original")
        if original is None:
            return
        if w.styleSheet() == w.property("_see_blank"):
            w.setStyleSheet(original)
        w.setProperty("_see_original", None)
        w.setProperty("_see_blank", None)

    # ---- the outline on the text

    def _outlines(self, on: bool) -> None:
        if on:
            for w in self._window.findChildren(QWidget):  # parents before their children
                self._outline(w)
        else:
            for widget in self._outlined:
                if shiboken6.isValid(widget) and isinstance(widget.graphicsEffect(), Outline):
                    widget.setGraphicsEffect(None)
            self._outlined = []

    def _outline(self, w: QWidget) -> None:
        """Give `w` (or, for a text view, its viewport) an outline - unless it is in something outlined already:
        one inside another would only cost twice."""
        if not shiboken6.isValid(w):
            return
        if isinstance(w, QAbstractScrollArea):
            target = w.viewport()
        elif isinstance(w, _BARS + _SMALL + (TitleBar,)):
            target = w
        else:
            return
        if target is None or target.graphicsEffect() is not None:
            return
        parent = w.parentWidget()
        while parent is not None and parent is not self._window:
            if isinstance(parent.graphicsEffect(), Outline):
                return
            parent = parent.parentWidget()
        target.setGraphicsEffect(Outline(target))
        self._outlined.append(target)

    # ---- events: new widgets, and resizing from the edges

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt override)
        if not self._on or not shiboken6.isValid(self._window):
            return False
        kind = event.type()
        if kind == QEvent.Type.Paint and obj is self._window:
            # The backdrop, under everything the window paints: a translucent window paints no background of its
            # own (nor its style sheet's), so it is laid here, exactly this alpha, before the window's own paint.
            painter = QPainter(obj)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            painter.fillRect(event.rect(), self._tint())
            painter.end()
        elif kind in (QEvent.Type.Polish, QEvent.Type.StyleChange) and isinstance(obj, QWidget) \
                and self._window.isAncestorOf(obj):
            # A widget made or restyled while it is on - a new chat bubble, a new task's views - follows. A burst
            # of these (a whole window restyled) is looked at once, on the next turn.
            if not self._pending:
                QTimer.singleShot(0, self._adopt_pending)
            self._pending.append(obj)
        elif kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton \
                and isinstance(obj, QWidget) and obj.window() is self._window:
            edges = self._edges_at(QCursor.pos())
            if edges and self._window.windowHandle() is not None:
                if self.title_bar is not None:
                    self.title_bar._normal = None
                self._window.windowHandle().startSystemResize(edges)
                return True
        return False

    def _adopt_pending(self) -> None:
        pending, self._pending = self._pending, []
        if not self._on:
            return
        seen = set()
        for w in pending:
            if not shiboken6.isValid(w) or id(w) in seen:
                continue
            seen.add(id(w))
            self._blank(w)
            self._outline(w)

    def _edges_at(self, pos: QPoint) -> Qt.Edge:
        w = self._window
        if (self.title_bar is not None and self.title_bar.maximised) or w.isMaximized() or not w.isVisible():
            return Qt.Edge(0)
        g = w.frameGeometry()
        if not g.adjusted(-1, -1, 1, 1).contains(pos):
            return Qt.Edge(0)
        edges = Qt.Edge(0)
        if pos.x() - g.left() < EDGE:
            edges |= Qt.Edge.LeftEdge
        elif g.right() - pos.x() < EDGE:
            edges |= Qt.Edge.RightEdge
        if pos.y() - g.top() < EDGE:
            edges |= Qt.Edge.TopEdge
        elif g.bottom() - pos.y() < EDGE:
            edges |= Qt.Edge.BottomEdge
        return edges

    def _edge_cursor(self) -> None:
        self._set_cursor(self._edges_at(QCursor.pos()) if QApplication.activePopupWidget() is None else Qt.Edge(0))

    def _set_cursor(self, edges: Qt.Edge) -> None:
        if edges == self._edges:
            return
        self._edges = edges
        if self._cursor_set:
            QApplication.restoreOverrideCursor()
            self._cursor_set = False
        if edges:
            if edges in (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.Edge.RightEdge | Qt.Edge.BottomEdge):
                shape = Qt.CursorShape.SizeFDiagCursor
            elif edges in (Qt.Edge.RightEdge | Qt.Edge.TopEdge, Qt.Edge.LeftEdge | Qt.Edge.BottomEdge):
                shape = Qt.CursorShape.SizeBDiagCursor
            else:
                shape = _CURSORS.get(edges, Qt.CursorShape.ArrowCursor)
            QApplication.setOverrideCursor(shape)
            self._cursor_set = True


def _add_controls(controller: SeeThrough, window) -> QCheckBox | None:
    """The See-through tick box in the status bar (on the remote page too), and while it is on, the Backdrop."""
    bar = window.statusBar() if isinstance(window, QMainWindow) else None
    if bar is None:
        return None
    box = QCheckBox("See-through")
    box.setToolTip("The window's backgrounds become transparent: the desktop shows behind it, and only the text, "
                   "borders, buttons and pictures stay. The text gets a dark edge, so it reads over a light desktop "
                   "too. Backdrop sets how dark the window still is. Also in Settings.")
    label = QLabel("Backdrop")
    label.setStyleSheet("color:#9aa4b2;")
    slider = QSlider(Qt.Orientation.Horizontal)
    slider.setRange(0, round(MAX_BACKDROP * 100))
    slider.setFixedWidth(90)
    slider.setValue(round(controller.backdrop * 100))
    slider.setToolTip("How dark the window's background still is in See-through: 0% shows the desktop as it is "
                      "(the text keeps its dark edge), higher is easier to read over a busy or light desktop.")
    slider.valueChanged.connect(lambda value: controller.set_backdrop(value / 100))
    slider.sliderReleased.connect(lambda: controller.set_backdrop(slider.value() / 100, persist=True))

    def follow() -> None:
        if box.isChecked() != controller.on:
            box.setChecked(controller.on)
        label.setVisible(controller.on)
        slider.setVisible(controller.on)

    box.clicked.connect(lambda checked: controller.set(checked, persist=True))
    controller.changed.connect(follow)
    bar.addPermanentWidget(box)
    bar.addPermanentWidget(label)
    bar.addPermanentWidget(slider)
    follow()
    controller.slider = slider
    return box


def install_see_through(app, window) -> SeeThrough:
    """Wire see-through onto the running app + main window. Call after install_stealth (its flags carry the
    frameless one) and install_opacity."""
    controller = SeeThrough(app, window)
    app._see_through = controller
    controller.box = _add_controls(controller, window)
    if getattr(Settings.load(), "see_through", False):
        QTimer.singleShot(0, lambda: controller.set(True))
    return controller
