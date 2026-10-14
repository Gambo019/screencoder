"""Stealth mode: keep ScreenCoder visible to the user on their own monitor while
hiding it from screen capture (screenshots, Zoom/Teams/Meet screen sharing,
OBS), the taskbar and Alt+Tab. Toggled by a global hotkey (Ctrl+Alt+Shift+H)
and by a checkbox in the Settings dialog.

This module is self-contained: it attaches to the main window from the outside
(window flags, an application-wide event filter, a Win32 global hotkey) and
does not touch or depend on anything inside the window's own code.

  * Screen-capture exclusion -- SetWindowDisplayAffinity(hwnd,
    WDA_EXCLUDEFROMCAPTURE) on every top-level window, so dialogs and popups are
    covered too. Falls back to WDA_MONITOR before Windows 10 version 2004.
  * Taskbar / Alt+Tab -- the Qt.Tool flag (Win32 WS_EX_TOOLWINDOW) on the main
    window. Changing a window's flags recreates its native handle, so the
    capture affinity is re-applied to the new handle afterwards.
  * Global hotkey -- RegisterHotKey on the app thread, caught by a native event
    filter, so it fires whatever app has focus and survives the main window
    being recreated by the flag change.

Deliberately NOT attempted: hiding the process from Task Manager. That needs
rootkit-style kernel/hooking techniques, is blocked on modern Windows, and is
exactly what security software flags as malware -- out of scope for a
legitimate app. The window-level hiding above is what defeats shoulder-surfing
and screen sharing.
"""
from __future__ import annotations

import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QEvent, QObject, Qt
from PySide6.QtWidgets import QWidget

from .config import Settings

WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001            # black box in captures; since Windows 7
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # absent from captures; Windows 10 2004+

_IS_WIN = sys.platform == "win32"

# Global hotkeys: Ctrl+Alt+Shift+H toggles stealth, Ctrl+Alt+Shift+T toggles always-on-top.
_HOTKEY_ID = 0xB0B
_HOTKEY_TOP_ID = 0xB0C
_MOD_ALT, _MOD_CONTROL, _MOD_SHIFT, _MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
_VK_H = 0x48
_VK_T = 0x54
_WM_HOTKEY = 0x0312

if _IS_WIN:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]
    _user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
    _user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = wintypes.BOOL


def _exclude_supported() -> bool:
    if not _IS_WIN:
        return False
    v = sys.getwindowsversion()
    return (v.major, v.build) >= (10, 19041)


def _capture_affinity(on: bool) -> int:
    if not on or not _IS_WIN:
        return WDA_NONE
    return WDA_EXCLUDEFROMCAPTURE if _exclude_supported() else WDA_MONITOR


def _apply_affinity(hwnd: int, affinity: int) -> None:
    if not _IS_WIN or not hwnd:
        return
    ok = _user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), wintypes.DWORD(affinity))
    if not ok and affinity == WDA_EXCLUDEFROMCAPTURE:
        _user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), wintypes.DWORD(WDA_MONITOR))


class _HotkeyFilter(QAbstractNativeEventFilter):
    """Catches WM_HOTKEY off the Qt event loop and dispatches by hotkey id."""

    def __init__(self, handlers: dict) -> None:
        super().__init__()
        self._handlers = handlers  # {hotkey id: callback}

    def nativeEventFilter(self, event_type, message):  # noqa: N802 (Qt override)
        if _IS_WIN and bytes(event_type) == b"windows_generic_MSG":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == _WM_HOTKEY:
                handler = self._handlers.get(int(msg.wParam))
                if handler is not None:
                    handler()
        return False, 0


class StealthController(QObject):
    def __init__(self, app, window, settings: Settings) -> None:
        super().__init__(app)
        self._app = app
        self._window = window
        self._on = bool(getattr(settings, "stealth", False))
        self._on_top = bool(getattr(settings, "always_on_top", False))
        # The window's flags before we touch them, and the last set we applied.
        self._base_flags = int(window.windowFlags())
        self._applied_flags = None

        app.installEventFilter(self)  # re-stamp affinity on every window as it is shown

        self._hotkey_filter = _HotkeyFilter({
            _HOTKEY_ID: self.toggle,
            _HOTKEY_TOP_ID: self.toggle_on_top,
        })
        app.installNativeEventFilter(self._hotkey_filter)
        self._hotkey_ok = False
        self._hotkey_top_ok = False
        if _IS_WIN:
            mods = _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT | _MOD_NOREPEAT
            self._hotkey_ok = bool(_user32.RegisterHotKey(None, _HOTKEY_ID, mods, _VK_H))
            self._hotkey_top_ok = bool(_user32.RegisterHotKey(None, _HOTKEY_TOP_ID, mods, _VK_T))
            app.aboutToQuit.connect(self._cleanup)

        self._apply(initial=True)

    @property
    def enabled(self) -> bool:
        return self._on

    @property
    def on_top(self) -> bool:
        return self._on_top

    @property
    def hotkey_registered(self) -> bool:
        return self._hotkey_ok

    @property
    def top_hotkey_registered(self) -> bool:
        return self._hotkey_top_ok

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt override)
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            _apply_affinity(int(obj.winId()), _capture_affinity(self._on))
        return False

    def toggle(self) -> None:
        self.set_stealth(not self._on, persist=True)

    def set_stealth(self, on: bool, persist: bool = False) -> None:
        self._on = bool(on)
        self._apply()
        if persist:
            self._persist()

    def toggle_on_top(self) -> None:
        self.set_on_top(not self._on_top, persist=True)

    def set_on_top(self, on: bool, persist: bool = False) -> None:
        self._on_top = bool(on)
        self._apply()
        if persist:
            self._persist()

    def _apply(self, initial: bool = False) -> None:
        w = self._window
        # Compute the whole window-flag set from the untouched base so stealth
        # (Tool => WS_EX_TOOLWINDOW) and always-on-top (WindowStaysOnTopHint =>
        # WS_EX_TOPMOST) are applied in a single rebuild. The window TYPE is a
        # masked enum, so swap only those bits; the hint is a separate bit.
        wt = Qt.WindowType
        type_mask = int(wt.WindowType_Mask)
        flags = self._base_flags & ~type_mask
        flags |= int(wt.Tool) if self._on else int(wt.Window)
        if self._on_top:
            flags |= int(wt.WindowStaysOnTopHint)
        else:
            flags &= ~int(wt.WindowStaysOnTopHint)

        # Changing flags recreates the native window AND hides it, so capture
        # visibility first and re-show; the Show handler re-stamps the affinity.
        if flags != self._applied_flags:
            was_visible = w.isVisible()
            geo = w.saveGeometry()
            w.setWindowFlags(wt(flags))
            # Qt switches "closing this window ends the app" off for a Tool window and never back on. Without
            # this, once stealth had been on, the X closed the window but the app kept running, invisibly.
            w.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, True)
            w.restoreGeometry(geo)
            if was_visible:
                w.show()
                w.raise_()
                w.activateWindow()
            self._applied_flags = flags

        # Capture exclusion on the (possibly new) main-window handle and every
        # other open top-level window.
        # Only windows that are on screen. Asking a hidden widget for its winId() makes Qt create a native
        # window for it: a closed dialog would get a hidden, dead window of its own again (dozens of them per
        # call), and Windows can bring such a window back on screen blank. A window shown later is stamped by
        # the Show handler above.
        aff = _capture_affinity(self._on)
        for top in self._app.topLevelWidgets():
            if top.isWindow() and top.isVisible():
                _apply_affinity(int(top.winId()), aff)

    def _persist(self) -> None:
        # Write the flags we own, re-loading first so a concurrent change
        # elsewhere is not clobbered.
        try:
            s = Settings.load()
            s.stealth = self._on
            s.always_on_top = self._on_top
            s.save()
        except Exception:
            pass

    def _cleanup(self) -> None:
        if _IS_WIN and self._hotkey_ok:
            _user32.UnregisterHotKey(None, _HOTKEY_ID)
            self._hotkey_ok = False
        if _IS_WIN and self._hotkey_top_ok:
            _user32.UnregisterHotKey(None, _HOTKEY_TOP_ID)
            self._hotkey_top_ok = False


def install_stealth(app, window) -> StealthController:
    """Wire stealth mode onto the running app + main window. Call once, after
    the window exists. The controller is stored on the app so the Settings
    dialog can apply changes live."""
    controller = StealthController(app, window, Settings.load())
    app._stealth_controller = controller
    return controller
