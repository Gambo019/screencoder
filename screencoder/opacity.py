"""A window-transparency control, embedded in the workspace toolbar between the
"Browse…" and "Open folder" buttons.

Built and inserted entirely from the outside: it uses only the main window's
public `browse_action` / `open_action` attributes to find the toolbar and slot
the slider in before "Open folder". It does not read or modify the window's own
code. The slider drives the whole window's opacity (QWidget.setWindowOpacity);
the value is saved so it persists across launches, and is re-applied after the
window is rebuilt by a stealth / always-on-top flag change. After each change the
stealth controller puts its click-through back on the window (see stealth.py):
at 100% Qt takes the window out of the layered state that click-through needs.
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QToolBar, QWidget

from .config import Settings

MIN_PCT = 20   # never let the window fade to the point it can't be seen or clicked
MAX_PCT = 100


def _clamp(op: float) -> float:
    return max(MIN_PCT / 100.0, min(1.0, float(op)))


class OpacityToolWidget(QWidget):
    """Label + slider + percent, sized to sit inline on a toolbar."""

    def __init__(self, window, stealth=None) -> None:
        super().__init__()
        self._window = window
        self._stealth = stealth  # the StealthController, whose click-through outlasts an opacity change
        self._opacity = _clamp(getattr(Settings.load(), "opacity", 0.85))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 0, 6, 0)
        lay.setSpacing(6)
        lay.addWidget(QLabel("Opacity"))
        self.slider = QSlider(Qt.Orientation.Horizontal)
        self.slider.setRange(MIN_PCT, MAX_PCT)
        self.slider.setFixedWidth(110)
        self.slider.setToolTip("Window transparency")
        self._pct = QLabel("100%")
        self._pct.setMinimumWidth(36)
        lay.addWidget(self.slider)
        lay.addWidget(self._pct)

        self.slider.valueChanged.connect(self._changed)
        self.slider.sliderReleased.connect(self._commit)
        self.slider.setValue(int(round(self._opacity * 100)))  # fires _changed -> applies
        self._window.setWindowOpacity(self._opacity)

        # Re-apply the opacity after a stealth / on-top rebuild re-shows the window.
        self._window.installEventFilter(self)

    def _changed(self, pct: int) -> None:
        self._pct.setText(f"{pct}%")
        self._opacity = _clamp(pct / 100.0)
        self._window.setWindowOpacity(self._opacity)
        if self._stealth is not None:
            # At 100% Qt takes the window out of the layered state that click-through needs: put it back.
            self._stealth.refresh_click_through()

    def _commit(self) -> None:
        try:
            s = Settings.load()
            s.opacity = self._opacity
            s.save()
        except Exception:
            pass

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt override)
        if obj is self._window and event.type() == QEvent.Type.Show:
            self._window.setWindowOpacity(self._opacity)
        return False


def install_opacity(app, window):
    """Insert the opacity slider into the toolbar, right before "Open folder"
    (i.e. between Browse… and Open folder). Returns the widget. Call after install_stealth, so an opacity
    change can put click-through back."""
    widget = OpacityToolWidget(window, getattr(app, "_stealth_controller", None))
    open_action = getattr(window, "open_action", None)
    browse_action = getattr(window, "browse_action", None)
    if open_action is not None:
        for tb in window.findChildren(QToolBar):
            actions = tb.actions()
            if open_action in actions and (browse_action is None or browse_action in actions):
                tb.insertWidget(open_action, widget)
                app._opacity_widget = widget
                return widget
    # Fallback: expected layout not found -> float it in the top-left corner so
    # the control still exists.
    widget.setParent(window)
    widget.move(12, 12)
    widget.raise_()
    widget.show()
    app._opacity_widget = widget
    return widget
