"""Single instance: run ScreenCoder once per user. A second launch (clicking run.bat again) hands the already
running copy a nudge to come to the front and then exits, instead of opening another window.

Self-contained and wired from app.py with two calls: claim() once at startup (exit the process if it returns
False), and bind(window) once the main window exists. It uses a QLocalServer named pipe -- no extra
dependency and nothing left on disk. It does not touch the window's own code; bringing the window forward is
show + raise + focus (restore if minimised), never always-on-top.
"""
from __future__ import annotations

import getpass
import sys

from PySide6.QtCore import QObject, Qt, QTimer
from PySide6.QtNetwork import QLocalServer, QLocalSocket

_PING = b"activate\n"

_IS_WIN = sys.platform == "win32"

if _IS_WIN:
    import ctypes
    from ctypes import wintypes

    _u = ctypes.WinDLL("user32", use_last_error=True)
    _k = ctypes.WinDLL("kernel32", use_last_error=True)
    _u.GetForegroundWindow.restype = wintypes.HWND
    _u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
    _u.GetWindowThreadProcessId.restype = wintypes.DWORD
    _u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    _u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    _u.BringWindowToTop.argtypes = [wintypes.HWND]
    _u.SetForegroundWindow.argtypes = [wintypes.HWND]
    _u.SetActiveWindow.argtypes = [wintypes.HWND]
    _u.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
    _k.GetCurrentThreadId.restype = wintypes.DWORD
    _ASFW_ANY = 0xFFFFFFFF
    _SW_RESTORE = 9


def _pipe_name() -> str:
    # Named pipes are machine-wide on Windows, so scope the lock to the current user: one app per login,
    # but two different users on the same PC do not block each other.
    try:
        who = getpass.getuser()
    except Exception:
        who = "user"
    return f"ScreenCoder-single-instance-{who}"


def _force_foreground(hwnd: int) -> None:
    """Pull hwnd to the front and give it focus despite Windows' foreground lock (a background process may not
    call SetForegroundWindow on its own). The second instance grants permission with AllowSetForegroundWindow
    before it signals; here we also attach to the current foreground thread's input so the steal is allowed.
    This changes z-order and focus only -- it does NOT make the window always-on-top."""
    if not _IS_WIN:
        return
    try:
        _u.ShowWindow(hwnd, _SW_RESTORE)  # undo a minimised state
        fg = _u.GetForegroundWindow()
        target = _u.GetWindowThreadProcessId(hwnd, None)
        fg_thread = _u.GetWindowThreadProcessId(fg, None) if fg else 0
        cur = _k.GetCurrentThreadId()
        attached = []
        for t in (fg_thread, target):
            if t and t != cur and _u.AttachThreadInput(cur, t, True):
                attached.append(t)
        _u.BringWindowToTop(hwnd)
        _u.SetForegroundWindow(hwnd)
        _u.SetActiveWindow(hwnd)
        for t in attached:
            _u.AttachThreadInput(cur, t, False)
    except Exception:
        pass


class SingleInstance(QObject):
    """Guards against a second copy. See the module docstring for how app.py wires it in."""

    def __init__(self, app) -> None:
        super().__init__(app)
        self._name = _pipe_name()
        self._server: QLocalServer | None = None
        self._window = None

    def claim(self) -> bool:
        """True when this is the first instance (carry on). False when another instance is already running --
        it has been asked to come to the front, and this process should exit at once."""
        if self._signal_existing():
            return False
        # Become the primary. A pipe can linger from a run that was killed; clear it before listening.
        QLocalServer.removeServer(self._name)
        self._server = QLocalServer(self)
        if not self._server.listen(self._name):
            # Lost a start-up race with another fresh instance: let that one be primary and bow out if it is
            # already answering; otherwise carry on (better one window than none).
            self._server = None
            return not self._signal_existing()
        self._server.newConnection.connect(self._on_connection)
        return True

    def bind(self, window) -> None:
        """Give the guard the window to raise when a later launch asks for it."""
        self._window = window

    def _signal_existing(self) -> bool:
        sock = QLocalSocket()
        sock.connectToServer(self._name)
        if not sock.waitForConnected(300):
            return False
        if _IS_WIN:
            try:
                _u.AllowSetForegroundWindow(_ASFW_ANY)  # let the running instance take the foreground
            except Exception:
                pass
        sock.write(_PING)
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True

    def _on_connection(self) -> None:
        conn = self._server.nextPendingConnection() if self._server is not None else None
        if conn is not None:
            conn.disconnected.connect(conn.deleteLater)
        # Defer to the next loop turn so the connecting process has finished its AllowSetForegroundWindow call.
        QTimer.singleShot(0, self._activate)

    def _activate(self) -> None:
        w = self._window
        if w is None:
            return
        w.setWindowState((w.windowState() & ~Qt.WindowState.WindowMinimized) | Qt.WindowState.WindowActive)
        w.show()
        w.raise_()
        w.activateWindow()
        if _IS_WIN:
            _force_foreground(int(w.winId()))
