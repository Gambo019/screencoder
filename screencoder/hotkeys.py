"""System-wide hotkeys (Windows RegisterHotKey) so screenshots can be taken while the IDE has focus."""
from __future__ import annotations

import sys

from PySide6.QtCore import QAbstractNativeEventFilter, QObject, QTimer, Signal

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000
WM_HOTKEY = 0x0312

# name -> (modifiers, virtual key, human readable)
DEFAULT_HOTKEYS = {
    "region": (MOD_CONTROL | MOD_ALT | MOD_SHIFT, ord("S"), "Ctrl+Alt+Shift+S"),
    "full": (MOD_CONTROL | MOD_ALT | MOD_SHIFT, ord("F"), "Ctrl+Alt+Shift+F"),
}

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
    _user32.RegisterHotKey.restype = wintypes.BOOL
    _user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.UnregisterHotKey.restype = wintypes.BOOL


class _HotkeyFilter(QAbstractNativeEventFilter):
    def __init__(self, on_hotkey):
        super().__init__()
        self._on_hotkey = on_hotkey

    def nativeEventFilter(self, event_type, message):
        name = event_type.data() if hasattr(event_type, "data") else event_type
        if isinstance(name, str):
            name = name.encode()
        # Hotkeys registered without a window arrive as thread messages ("windows_dispatcher_MSG").
        if name in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == WM_HOTKEY:
                self._on_hotkey(int(msg.wParam))
                return True, 0
        return False, 0


class GlobalHotkeys(QObject):
    triggered = Signal(str)

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self._app = app
        self._ids: dict[int, str] = {}
        self._filter = None
        if sys.platform == "win32":
            self._filter = _HotkeyFilter(self._dispatch)
            app.installNativeEventFilter(self._filter)

    @property
    def supported(self) -> bool:
        return self._filter is not None

    def register_defaults(self) -> list[str]:
        """Register DEFAULT_HOTKEYS; returns human-readable names of the ones that failed."""
        failed = []
        for name, (mods, vk, label) in DEFAULT_HOTKEYS.items():
            if not self.register(name, mods, vk):
                failed.append(label)
        return failed

    def register(self, name: str, mods: int, vk: int) -> bool:
        if not self.supported:
            return False
        hotkey_id = 0xB000 + len(self._ids) + 1
        if not _user32.RegisterHotKey(None, hotkey_id, mods | MOD_NOREPEAT, vk):
            return False
        self._ids[hotkey_id] = name
        return True

    def unregister_all(self) -> None:
        if self.supported:
            for hotkey_id in self._ids:
                _user32.UnregisterHotKey(None, hotkey_id)
        self._ids.clear()

    def _dispatch(self, hotkey_id: int) -> None:
        name = self._ids.get(hotkey_id)
        if name:
            # Leave the native event filter before opening capture overlays.
            QTimer.singleShot(0, lambda: self.triggered.emit(name))
