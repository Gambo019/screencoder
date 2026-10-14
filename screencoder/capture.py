"""Screen capture: snipping-tool style region selection and full-screen grabs."""
from __future__ import annotations

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter, QPen, QPixmap, QRegion
from PySide6.QtWidgets import QApplication, QWidget

HINT = "Drag to select an area  -  Enter: whole screen  -  Esc / right-click: cancel"


class SnipOverlay(QWidget):
    """Frozen screenshot of one monitor; the user drags a rectangle to crop it."""

    selected = Signal(QImage)
    cancelled = Signal()

    def __init__(self, screen, pixmap: QPixmap):
        super().__init__(
            None,
            Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.Tool,
        )
        self._pix = pixmap
        self._screen = screen
        self._origin: QPoint | None = None
        self._sel = QRect()
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setMouseTracking(True)

    def show_on_screen(self) -> None:
        """Cover exactly this overlay's monitor.

        The position is forced explicitly, before and after showing: a monitor at (0, 0) looks like
        "no position set" to Qt, and Windows would then open the window on whichever monitor has the
        mouse - so two overlays ended up stacked on one display and the other could not be snipped.
        """
        geometry = self._screen.geometry()
        self.setScreen(self._screen)
        self.setGeometry(geometry)
        self.setAttribute(Qt.WidgetAttribute.WA_Moved, True)
        self.show()
        handle = self.windowHandle()
        if handle is not None:
            handle.setScreen(self._screen)
            handle.setGeometry(geometry)

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.drawPixmap(self.rect(), self._pix)
        shade = QColor(0, 0, 0, 120)
        sel = self._sel.normalized()
        if sel.isEmpty():
            p.fillRect(self.rect(), shade)
        else:
            p.setClipRegion(QRegion(self.rect()).subtracted(QRegion(sel)))
            p.fillRect(self.rect(), shade)
            p.setClipping(False)
            p.setPen(QPen(QColor(0, 170, 255), 2))
            p.drawRect(sel.adjusted(0, 0, -1, -1))
            dpr = self._pix.devicePixelRatio()
            label = f"{round(sel.width() * dpr)} x {round(sel.height() * dpr)}"
            p.setPen(QColor("white"))
            p.drawText(sel.left() + 4, max(14, sel.top() - 6), label)
        hint_rect = QRect(0, 12, self.width(), 30)
        p.fillRect(hint_rect.adjusted(self.width() // 2 - 260, 0, -(self.width() // 2 - 260), 0), QColor(0, 0, 0, 170))
        p.setPen(QColor("white"))
        p.drawText(hint_rect, Qt.AlignmentFlag.AlignCenter, HINT)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.RightButton:
            self.cancelled.emit()
        elif event.button() == Qt.MouseButton.LeftButton:
            self._origin = event.position().toPoint()
            self._sel = QRect(self._origin, self._origin)
            self.update()

    def mouseMoveEvent(self, event) -> None:
        if self._origin is not None:
            self._sel = QRect(self._origin, event.position().toPoint()).normalized()
            self.update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self._origin is None:
            return
        self._origin = None
        sel = self._sel.normalized()
        if sel.width() > 4 and sel.height() > 4:
            self._finish(sel)
        else:
            self._sel = QRect()
            self.update()

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.cancelled.emit()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._finish(self.rect())

    def _finish(self, sel: QRect) -> None:
        dpr = self._pix.devicePixelRatio()
        device = QRect(round(sel.x() * dpr), round(sel.y() * dpr), round(sel.width() * dpr), round(sel.height() * dpr))
        img = self._pix.copy(device.intersected(self._pix.rect())).toImage()
        img.setDevicePixelRatio(1.0)
        self.selected.emit(img)


class ScreenCapturer(QObject):
    """Makes the app's own windows invisible, grabs the screen(s), and emits the captured image."""

    captured = Signal(QImage, object)  # image, opaque target passed to start()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._busy = False
        self._target = None
        self._overlays: list[SnipOverlay] = []
        self._faded: list[QWidget] = []

    def start(self, mode: str, target, screen=None) -> None:
        """mode: "region" (drag to select on any monitor) or "full" (one whole monitor).

        For "full", `screen` picks the monitor; None means the one under the mouse.
        """
        if self._busy:
            return
        self._busy, self._target = True, target
        # Opacity 0 (instead of hide()) keeps window position, z-order and focus untouched.
        self._faded = [w for w in QApplication.topLevelWidgets() if w.isVisible() and not isinstance(w, SnipOverlay)]
        for w in self._faded:
            w.setWindowOpacity(0.0)
        QTimer.singleShot(200, lambda: self._grab(mode, screen))

    def _grab(self, mode: str, screen) -> None:
        if mode == "full":
            if screen not in QGuiApplication.screens():  # None, or a monitor unplugged meanwhile
                screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
            img = screen.grabWindow(0).toImage()
            img.setDevicePixelRatio(1.0)
            self._done(img)
            return
        under_mouse = QGuiApplication.screenAt(QCursor.pos())
        for screen in QGuiApplication.screens():
            overlay = SnipOverlay(screen, screen.grabWindow(0))
            overlay.selected.connect(self._done)
            overlay.cancelled.connect(lambda: self._done(None))
            self._overlays.append(overlay)
            overlay.show_on_screen()
            if screen is under_mouse:
                overlay.raise_()
                overlay.activateWindow()
                overlay.setFocus()

    def _done(self, img: QImage | None) -> None:
        for overlay in self._overlays:
            overlay.close()
            overlay.deleteLater()
        self._overlays = []
        for w in self._faded:
            w.setWindowOpacity(1.0)
        self._faded = []
        self._busy = False
        if img is not None and not img.isNull():
            self.captured.emit(img, self._target)
