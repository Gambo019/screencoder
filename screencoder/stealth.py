"""Stealth mode: keep ScreenCoder visible to the user on their own monitor while
hiding it from screen capture (screenshots, Zoom/Teams/Meet screen sharing,
OBS), the taskbar and Alt+Tab. Toggled by a global hotkey (Ctrl+Alt+Shift+H)
and by a checkbox in the Settings dialog.

This module is self-contained: it attaches to the main window from the outside
(window flags, an application-wide event filter, a Win32 global hotkey) and
does not touch or depend on anything inside the window's own code.

  * Screen-capture exclusion -- SetWindowDisplayAffinity(hwnd,
    WDA_EXCLUDEFROMCAPTURE). A Qt window is stamped the moment it is shown (an
    app-wide event filter); native windows Qt does not wrap -- a combo popup's
    drop-shadow window, a native dialog, a transient frame on a flag change --
    are caught by a short repeating sweep over the process's own windows while
    stealth is on. File/colour dialogs are forced to Qt's own (non-native) ones
    while stealth is on, so they are stamped instantly instead of leaking.
    Falls back to WDA_MONITOR before Windows 10 version 2004.
  * Taskbar / Alt+Tab -- the Qt.Tool flag (Win32 WS_EX_TOOLWINDOW) on the main
    window. Changing a window's flags recreates its native handle, so the
    capture affinity is re-applied to the new handle afterwards.
  * Always-on-top -- a pure Win32 z-order change (SetWindowPos HWND_TOPMOST /
    HWND_NOTOPMOST), NOT the Qt WindowStaysOnTopHint flag: toggling that flag
    goes through setWindowFlags, which hides and re-shows the same native
    window, and a capture-excluded window that is hidden then re-shown returns
    as a solid rectangle in a screen share (a stale compositor surface). A
    z-order change leaves the live surface alone. Open dialogs are moved into
    the same band so Settings stays above the main window, not adrift.
  * Global hotkey -- RegisterHotKey on the app thread, caught by a native event
    filter, so it fires whatever app has focus and survives the main window
    being recreated by the flag change.
  * Dialogs stay owned by the main window even while it is a Qt.Tool window, which
    Qt 6.12 stopped doing itself: they stay above it and off the taskbar.
  * Click-through -- the main window lets the mouse through to whatever is under
    it, so you click, scroll and (once that app has the focus a click gives it)
    type in the app below while this window stays on screen over it. Win32
    WS_EX_LAYERED | WS_EX_TRANSPARENT on the main window only: its dialogs stay
    clickable. Flipped as style bits on the live window, NOT through the Qt
    WindowTransparentForInput flag, which would go through setWindowFlags and hide
    and re-show the window (the screen-share rectangle above). Qt rewrites the
    extended style when the window's flags change and drops the layered bit at
    100% opacity, so the bits are put back after each of those. Toggled by
    Ctrl+Alt+Shift+C, Settings, and a Click-through tick box in the status bar,
    which the remote page shows and ticks like any other box. While it is on, the
    window cannot be clicked, so the hotkey or the remote page turns it off.

Deliberately NOT attempted: hiding the process from Task Manager. That needs
rootkit-style kernel/hooking techniques, is blocked on modern Windows, and is
exactly what security software flags as malware -- out of scope for a
legitimate app. The window-level hiding above is what defeats shoulder-surfing
and screen sharing.
"""
from __future__ import annotations

import os
import sys

import shiboken6
from PySide6.QtCore import QAbstractNativeEventFilter, QCoreApplication, QEvent, QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import QCheckBox, QMainWindow, QWidget

from .config import Settings

WDA_NONE = 0x00000000
WDA_MONITOR = 0x00000001            # black box in captures; since Windows 7
WDA_EXCLUDEFROMCAPTURE = 0x00000011  # absent from captures; Windows 10 2004+

_IS_WIN = sys.platform == "win32"

# Global hotkeys: Ctrl+Alt+Shift+H toggles stealth, Ctrl+Alt+Shift+T always-on-top, Ctrl+Alt+Shift+C click-through.
_HOTKEY_ID = 0xB0B
_HOTKEY_TOP_ID = 0xB0C
_HOTKEY_CLICK_ID = 0xB0D
_MOD_ALT, _MOD_CONTROL, _MOD_SHIFT, _MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x4000
_VK_H = 0x48
_VK_T = 0x54
_VK_C = 0x43
_WM_HOTKEY = 0x0312
CLICK_THROUGH_HOTKEY = "Ctrl+Alt+Shift+C"

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
    _user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
    _user32.GetWindow.restype = wintypes.HWND
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
    _user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    _user32.SetWindowPos.argtypes = [wintypes.HWND, ctypes.c_ssize_t, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                     ctypes.c_int, wintypes.UINT]
    _user32.SetWindowPos.restype = wintypes.BOOL
    _user32.EnumWindows.argtypes = [ctypes.c_void_p, wintypes.LPARAM]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.SetWinEventHook.restype = wintypes.HANDLE
    _user32.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, ctypes.c_void_p,
                                        wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    _user32.UnhookWinEvent.argtypes = [wintypes.HANDLE]
    _user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.DWORD, wintypes.BYTE, wintypes.DWORD]
    _user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
    _user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    _user32.RedrawWindow.restype = wintypes.BOOL
    _WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                       wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
    _EVENT_OBJECT_SHOW = 0x8002
    _WINEVENT_OUTOFCONTEXT = 0x0000
    _OBJID_WINDOW = 0

# Window ownership (see _keep_owned).
_GW_OWNER, _GWLP_HWNDPARENT, _GWL_EXSTYLE, _WS_EX_TOPMOST = 4, -8, -20, 0x8
_HWND_TOP, _HWND_TOPMOST, _HWND_NOTOPMOST = 0, -1, -2
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE = 0x1, 0x2, 0x10
# Click-through (see _set_click_through).
_WS_EX_LAYERED, _WS_EX_TRANSPARENT, _LWA_ALPHA = 0x00080000, 0x00000020, 0x2
_RDW_INVALIDATE, _RDW_ERASE, _RDW_FRAME, _RDW_ALLCHILDREN = 0x1, 0x4, 0x400, 0x80


def _set_click_through(hwnd: int, on: bool, qt_layered: bool) -> None:
    """Let the mouse through the window `hwnd` to whatever is under it, or stop doing so.

    Windows skips a window that is both layered and transparent when it decides what the mouse is over, so the
    click lands on the window below. The transparent bit alone does not do it. A window below 100% opacity is
    layered already (Qt makes it so for the opacity); at 100% it is not, and the layered bit is added with
    full opacity, without which a layered window would not be drawn at all. `qt_layered` says whether Qt
    wants the window layered for its own sake (its opacity): only a layered bit Qt did not want is removed
    again when click-through ends."""
    if not _IS_WIN or not hwnd:
        return
    ex = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
    if on:
        want = ex | _WS_EX_LAYERED | _WS_EX_TRANSPARENT
    else:
        want = ex & ~_WS_EX_TRANSPARENT
        if not qt_layered:
            want &= ~_WS_EX_LAYERED
    if want == ex:
        return
    _user32.SetWindowLongPtrW(hwnd, _GWL_EXSTYLE, want)
    if want & _WS_EX_LAYERED and not ex & _WS_EX_LAYERED:
        _user32.SetLayeredWindowAttributes(hwnd, 0, 255, _LWA_ALPHA)
    elif ex & _WS_EX_LAYERED and not want & _WS_EX_LAYERED:
        # No longer layered: Windows asks the window to paint itself again, make sure all of it is.
        _user32.RedrawWindow(hwnd, None, None, _RDW_INVALIDATE | _RDW_ERASE | _RDW_FRAME | _RDW_ALLCHILDREN)


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
    # No WDA_MONITOR fallback on Windows that supports WDA_EXCLUDEFROMCAPTURE: that fallback turned a momentary
    # failure (e.g. stamping a popup the instant it is shown) into a persistent BLACK BOX in captures. If the
    # EXCLUDE call does not take this time, the repeating sweep and the show-hook retry it until it does, which
    # leaves at worst a brief flash instead of a black rectangle. WDA_MONITOR is used only as the chosen
    # affinity itself on pre-2004 Windows, where EXCLUDE is unavailable (see _capture_affinity).
    _user32.SetWindowDisplayAffinity(wintypes.HWND(hwnd), wintypes.DWORD(affinity))


def _process_windows() -> list:
    """Every visible top-level window owned by this process, by raw HWND. Includes native windows Qt does not
    wrap (a popup's drop-shadow window, a native file dialog), which is why we enumerate instead of asking Qt."""
    if not _IS_WIN:
        return []
    mine, found = os.getpid(), []

    def visit(hwnd, _lparam):
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value == mine and _user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    _user32.EnumWindows(_ENUMPROC(visit), 0)
    return found


def _keep_owned(hwnd: int, owner: int) -> None:
    """Make `owner` the owner of the dialog window `hwnd`, if Qt left it without one.

    Windows keeps an owned window above its owner (and topmost while the owner is), and gives it no taskbar
    button. Qt 6.12 no longer makes a Qt.Tool window, which stealth mode turns the main window into, the
    owner of its dialogs: they came up unowned, under an always-on-top main window that their being open
    had locked, so nothing could be clicked. Qt 6.11 did this itself; a window it already owns is left alone.
    """
    if not _IS_WIN or not hwnd or not owner or hwnd == owner or _user32.GetWindow(hwnd, _GW_OWNER):
        return
    _user32.SetWindowLongPtrW(hwnd, _GWLP_HWNDPARENT, owner)
    topmost = _user32.GetWindowLongW(owner, _GWL_EXSTYLE) & _WS_EX_TOPMOST
    _user32.SetWindowPos(hwnd, _HWND_TOPMOST if topmost else _HWND_TOP, 0, 0, 0, 0,
                         _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)


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
    changed = Signal()  # stealth, always-on-top or click-through was toggled (so open controls can follow)

    def __init__(self, app, window, settings: Settings) -> None:
        super().__init__(app)
        self._app = app
        self._window = window
        self._on = bool(getattr(settings, "stealth", False))
        self._on_top = bool(getattr(settings, "always_on_top", False))
        self._click_through = bool(getattr(settings, "click_through", False))
        self.click_box = None  # the status bar's Click-through box (install_stealth adds it)
        # The window's flags before we touch them, and the last set we applied.
        self._base_flags = int(window.windowFlags())
        self._applied_flags = None

        app.installEventFilter(self)  # re-stamp affinity on every window as it is shown
        # A short repeating sweep stamps native windows the Qt event filter never sees (a popup's drop shadow,
        # a native dialog, a transient frame while flags change). It runs only while stealth is on.
        self._sweep = QTimer(self)
        self._sweep.setInterval(150)
        self._sweep.timeout.connect(self._sweep_native)
        # The fast path: Windows tells us the moment ANY of our windows is shown (before it is composited), so a
        # popup, its native drop shadow, or a dialog is stamped with no visible flash - even on the 2nd open, when
        # the Qt-level Show event may not fire. The periodic sweep above is just a backstop.
        self._winhook = None
        self._winhook_cb = None
        if _IS_WIN:
            self._winhook_cb = _WINEVENTPROC(self._on_window_shown)
            self._winhook = _user32.SetWinEventHook(_EVENT_OBJECT_SHOW, _EVENT_OBJECT_SHOW, None,
                                                    self._winhook_cb, os.getpid(), 0, _WINEVENT_OUTOFCONTEXT)

        self._hotkey_filter = _HotkeyFilter({
            _HOTKEY_ID: self.toggle,
            _HOTKEY_TOP_ID: self.toggle_on_top,
            _HOTKEY_CLICK_ID: self.toggle_click_through,
        })
        app.installNativeEventFilter(self._hotkey_filter)
        self._hotkey_ok = False
        self._hotkey_top_ok = False
        self._hotkey_click_ok = False
        if _IS_WIN:
            mods = _MOD_CONTROL | _MOD_ALT | _MOD_SHIFT | _MOD_NOREPEAT
            self._hotkey_ok = bool(_user32.RegisterHotKey(None, _HOTKEY_ID, mods, _VK_H))
            self._hotkey_top_ok = bool(_user32.RegisterHotKey(None, _HOTKEY_TOP_ID, mods, _VK_T))
            self._hotkey_click_ok = bool(_user32.RegisterHotKey(None, _HOTKEY_CLICK_ID, mods, _VK_C))
            app.aboutToQuit.connect(self._cleanup)
        if self._click_through and not self._hotkey_click_ok:
            # A window that ignores the mouse, with no hotkey to undo it: start without it rather than leave a
            # window nothing on this machine can reach. (Settings or the remote page can still switch it on.)
            self._click_through = False

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

    @property
    def click_through(self) -> bool:
        return self._click_through

    @property
    def click_hotkey_registered(self) -> bool:
        return self._hotkey_click_ok

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt override)
        if event.type() == QEvent.Type.Show and isinstance(obj, QWidget) and obj.isWindow():
            _apply_affinity(int(obj.winId()), _capture_affinity(self._on))
            self._own(obj)
            if obj is self._window and self._click_through:
                # Showing can come with a fresh extended style (a flag change, the opacity put back by its own
                # Show handler, which runs after this one): set the click-through bits once all that is done.
                QTimer.singleShot(0, self.refresh_click_through)
            if self._on:
                # A Qt window often brings a native drop-shadow window with it a tick later; stamp the whole
                # process's windows on the next turn so the shadow never shows in a capture.
                QTimer.singleShot(0, self._sweep_native)
        elif (event.type() == QEvent.Type.Hide and self._on and isinstance(obj, QWidget) and obj.isWindow()
              and obj is not self._window and obj.windowType() == Qt.WindowType.Popup):
            # A hidden-then-reshown popup (combo list, menu) keeps the SAME native window, whose DWM surface a
            # screen capture renders BLACK on the 2nd open even though it is excluded. Destroying the native
            # window on hide makes Qt build a fresh one next time, which captures correctly (like the 1st open).
            def kill(w=obj):
                if shiboken6.isValid(w) and not w.isVisible():
                    w.destroy()
            QTimer.singleShot(0, kill)
        return False

    def _own(self, window: QWidget) -> None:
        """A dialog of the app stays owned by the window it belongs to (see _keep_owned)."""
        parent = window.parentWidget()
        if window is self._window or parent is None:
            return  # the main window, or a window meant to stand on its own
        if window.windowType() in (Qt.WindowType.Popup, Qt.WindowType.ToolTip, Qt.WindowType.SplashScreen):
            return  # menus and tips: Qt keeps those on top itself
        owner = parent.window()
        if not owner.isVisible():
            return  # never ask a hidden window for its handle (see _apply)

        def own() -> None:
            if shiboken6.isValid(window) and shiboken6.isValid(owner) and window.isVisible() and owner.isVisible():
                _keep_owned(int(window.winId()), int(owner.winId()))

        own()                       # the Show event comes just before the window is put on screen
        QTimer.singleShot(0, own)   # and again just after, in case showing it reset the owner

    def toggle(self) -> None:
        self.set_stealth(not self._on, persist=True)

    def set_stealth(self, on: bool, persist: bool = False) -> None:
        self._on = bool(on)
        self._apply()
        if persist:
            self._persist()
        self.changed.emit()

    def toggle_on_top(self) -> None:
        self.set_on_top(not self._on_top, persist=True)

    def set_on_top(self, on: bool, persist: bool = False) -> None:
        self._on_top = bool(on)
        self._apply()
        if persist:
            self._persist()
        self.changed.emit()

    def toggle_click_through(self) -> None:
        self.set_click_through(not self._click_through, persist=True)

    def set_click_through(self, on: bool, persist: bool = False) -> None:
        """Let the mouse through the main window to the apps under it (on), or take it back (off). Only style
        bits change on the window that is on screen: nothing is hidden or rebuilt, so it is safe at any moment,
        inside an open dialog's loop too, and a screen share sees no change."""
        on = bool(on)
        if on == self._click_through:
            if persist:
                self._persist()
            return
        self._click_through = on
        self.refresh_click_through()
        if persist:
            self._persist()
        self.changed.emit()

    def refresh_click_through(self) -> None:
        """Put the click-through bits on the main window as they should be. Qt rewrites the window's extended
        style when its flags change and drops the layered bit when the opacity reaches 100%, so this is called
        after each of those (the opacity control calls it after every change)."""
        w = self._window
        if not _IS_WIN or not shiboken6.isValid(w) or not w.isVisible():
            return  # never ask a hidden window for its handle; the Show handler comes back here
        # Qt keeps a window layered for its opacity, and always for a see-through background (see_through.py).
        layered = w.windowOpacity() < 1.0 or w.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        _set_click_through(int(w.winId()), self._click_through, qt_layered=layered)

    def _apply(self, initial: bool = False) -> None:
        w = self._window
        # Compute the window-flag set from the untouched base. Only the window TYPE changes here (Tool =>
        # WS_EX_TOOLWINDOW for stealth); it is a masked enum, so swap just those bits. Always-on-top is applied
        # separately, as a Win32 z-order change in _apply_on_top (see just below).
        wt = Qt.WindowType
        type_mask = int(wt.WindowType_Mask)
        flags = self._base_flags & ~type_mask
        flags |= int(wt.Tool) if self._on else int(wt.Window)
        # Always-on-top is applied with a Win32 z-order change (_apply_on_top), not this flag: on Windows,
        # toggling WindowStaysOnTopHint rebuilds/re-shows the native window, which turns a capture-excluded
        # window into a visible rectangle in a screen share. Other platforms have no such exclusion, so there
        # the flag is the simplest way.
        flags &= ~int(wt.WindowStaysOnTopHint)
        if self._on_top and not _IS_WIN:
            flags |= int(wt.WindowStaysOnTopHint)

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
                # Stamp the brand-new native handle BEFORE it is shown, so no frame of the recreated window
                # (its border or drop shadow) is ever composited without the capture-exclusion flag.
                _apply_affinity(int(w.winId()), _capture_affinity(self._on))
                w.show()
                w.raise_()
                if not self._click_through:
                    # With click-through on, the keyboard belongs to the app under the window: leave it there.
                    w.activateWindow()
            self._applied_flags = flags

        # While stealth is on, use Qt's own file/colour dialogs, not the OS ones: a native dialog is a separate
        # window this app cannot hide from capture, while a Qt dialog is stamped the moment it is shown.
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_DontUseNativeDialogs, self._on)
        if self._on:
            if not self._sweep.isActive():
                self._sweep.start()
        else:
            self._sweep.stop()

        # Capture exclusion on the (possibly new) main-window handle and every
        # other open top-level window.
        # Only windows that are on screen. Asking a hidden widget for its winId() makes Qt create a native
        # window for it: a closed dialog would get a hidden, dead window of its own again (dozens of them per
        # call), and Windows can bring such a window back on screen blank. A window shown later is stamped by
        # the Show handler above.
        aff = _capture_affinity(self._on)
        for hwnd in _process_windows():
            _apply_affinity(hwnd, aff)

        # Put the window in or out of the always-on-top band, and re-settle its open dialogs.
        self._apply_on_top()
        # A flag change above rewrote the window's extended style: put the click-through bits back.
        self.refresh_click_through()

    def _apply_on_top(self) -> None:
        """Move the main window into or out of the always-on-top band with a pure z-order change -- no window
        flag, so its capture-excluded surface is never torn down (see the module docstring) -- then keep any
        open dialog owned by it, stacked above it, and in the same band."""
        if not _IS_WIN or not self._window.isVisible():
            return
        owner = int(self._window.winId())
        _user32.SetWindowPos(owner, _HWND_TOPMOST if self._on_top else _HWND_NOTOPMOST, 0, 0, 0, 0,
                             _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
        self._reown_dialogs(owner)

    def _reown_dialogs(self, owner: int) -> None:
        """Keep every open dialog of the app owned by the main window, above it, and in its topmost band.
        A dialog is settled once when shown (see _own), but the main window's topmost state can be toggled
        (Ctrl+Alt+Shift+T) or its native handle rebuilt (stealth toggle) while a dialog is open; an owned
        window left in the wrong band floats above everything (the user saw Settings stuck on top) or sinks
        behind the main window instead of staying on it."""
        if not _IS_WIN:
            return
        pos = _HWND_TOPMOST if self._on_top else _HWND_NOTOPMOST
        for w in self._app.topLevelWidgets():
            if w is self._window or not w.isVisible():
                continue
            parent = w.parentWidget()
            if parent is None or parent.window() is not self._window:
                continue
            if w.windowType() in (Qt.WindowType.Popup, Qt.WindowType.ToolTip, Qt.WindowType.SplashScreen):
                continue
            hwnd = int(w.winId())
            _user32.SetWindowLongPtrW(hwnd, _GWLP_HWNDPARENT, owner)
            _user32.SetWindowPos(hwnd, pos, 0, 0, 0, 0, _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)

    def _on_window_shown(self, _hook, _event, hwnd, id_object, _id_child, _thread, _time) -> None:
        # Called by Windows (our process only) when a window is shown. Stamp it at once while stealth is on,
        # and strip the saved-bits/shadow class styles that make a reopened popup a black box in a capture.
        if self._on and id_object == _OBJID_WINDOW and hwnd:
            _apply_affinity(int(hwnd), _capture_affinity(True))

    def _sweep_native(self) -> None:
        if not (_IS_WIN and self._on):
            self._sweep.stop()
            return
        aff = _capture_affinity(True)
        for hwnd in _process_windows():
            _apply_affinity(hwnd, aff)

    def _persist(self) -> None:
        # Write the flags we own, re-loading first so a concurrent change
        # elsewhere is not clobbered.
        try:
            s = Settings.load()
            s.stealth = self._on
            s.always_on_top = self._on_top
            s.click_through = self._click_through
            s.save()
        except Exception:
            pass
        # The window keeps its own copy of the settings and saves all of it when something else changes: give
        # it the new values too, or it would write the old ones back over these.
        live = getattr(self._window, "settings", None)
        if isinstance(live, Settings):
            live.stealth = self._on
            live.always_on_top = self._on_top
            live.click_through = self._click_through

    def _cleanup(self) -> None:
        if _IS_WIN and self._hotkey_ok:
            _user32.UnregisterHotKey(None, _HOTKEY_ID)
            self._hotkey_ok = False
        if _IS_WIN and self._hotkey_top_ok:
            _user32.UnregisterHotKey(None, _HOTKEY_TOP_ID)
            self._hotkey_top_ok = False
        if _IS_WIN and self._hotkey_click_ok:
            _user32.UnregisterHotKey(None, _HOTKEY_CLICK_ID)
            self._hotkey_click_ok = False
        if _IS_WIN and self._winhook:
            _user32.UnhookWinEvent(self._winhook)
            self._winhook = None


def _add_click_through_box(controller: StealthController, window):
    """The Click-through tick box in the status bar. It switches click-through and follows it when the hotkey
    or Settings switch it. An ordinary QCheckBox, so the remote page shows it and can tick it: that is the way
    to switch click-through off from the other machine while this window lets every click through.
    In the status bar, not the toolbar: the toolbar is full already, here and in the remote page's copy."""
    bar = window.statusBar() if isinstance(window, QMainWindow) else None
    if bar is None:
        return None
    box = QCheckBox("Click-through")
    box.setToolTip("Clicks, scrolling and typing go to the apps under this window, which stays on screen over "
                   "them (best with Always on top). While it is on, this window cannot be clicked: turn it off "
                   f"with {CLICK_THROUGH_HOTKEY} or from the remote page. Also in Settings.")

    def follow() -> None:
        on = controller.click_through
        if box.isChecked() != on:
            box.setChecked(on)  # signals on: the remote page follows a box by them
        # While it is on, this window cannot be hovered for the tip: say on the box how to undo it.
        hint = f" ({CLICK_THROUGH_HOTKEY} to undo)" if on and controller.click_hotkey_registered else ""
        box.setText("Click-through" + hint)
        box.setStyleSheet("QCheckBox { color:#e3b341; }" if on else "")

    def clicked(checked: bool) -> None:
        # clicked, not toggled: only a click (here or from the remote page) switches it, the box following the
        # hotkey or Settings does not.
        controller.set_click_through(checked, persist=True)
        follow()

    box.clicked.connect(clicked)
    controller.changed.connect(follow)
    follow()
    bar.addPermanentWidget(box)
    return box


def install_stealth(app, window) -> StealthController:
    """Wire stealth mode onto the running app + main window. Call once, after
    the window exists. The controller is stored on the app so the Settings
    dialog can apply changes live."""
    controller = StealthController(app, window, Settings.load())
    app._stealth_controller = controller
    controller.click_box = _add_click_through_box(controller, window)
    return controller
