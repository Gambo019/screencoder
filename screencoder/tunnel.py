"""The internet link for remote control: a Cloudflare "quick tunnel" run next to the app.

The app keeps listening on this machine only (127.0.0.1). `cloudflared`, Cloudflare's own tunnel program,
connects out to Cloudflare and hands the app a public https address; whoever opens that address is passed
through to the app. Nothing has to be opened on the router or in the firewall, and it works behind a VPN.
No account is needed; the price of that is a new address every time the tunnel starts.

`cloudflared` is not part of the app: it is downloaded once, from Cloudflare's releases on GitHub, when the user
switches the internet link on (the Settings dialog asks first).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QTimer, QUrl, Signal
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .config import HOME_DIR

DOWNLOAD_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
DOWNLOAD_MB = 55
PROGRAM = HOME_DIR / "bin" / "cloudflared.exe"
SIGNER = 'O="Cloudflare, Inc."'  # who must have signed the downloaded program for it to be run
EXTRA_ARGS: list[str] = []  # further cloudflared options; "--protocol http2" was measured: no quicker than the default
PUBLIC_URL = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")
START_TIMEOUT_MS = 45000   # no address by then: something is in the way
RETRIES = 2                # a tunnel that dies is started again this many times before giving up
RETRY_MS = 3000
LOG_KEPT = 4000            # characters of cloudflared's output kept, to explain a failure

OFF, DOWNLOADING, STARTING, ON, FAILED = "off", "downloading", "starting", "on", "failed"


def supported() -> bool:
    return sys.platform == "win32"


def installed() -> bool:
    return PROGRAM.is_file() and PROGRAM.stat().st_size > 5_000_000


def _tie_to_this_process(pid: int) -> None:
    """Make Windows end the tunnel when this app ends, however it ends (so no tunnel is left pointing at nothing)."""
    if sys.platform != "win32" or not pid:
        return
    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes",
                                                         "OtherBytes")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("Basic", BasicLimits), ("Io", IoCounters), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    global _job  # one job for the life of the app: when its last handle closes (the app is gone), Windows kills
    if _job is None:
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        limits = ExtendedLimits()
        limits.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            kernel32.CloseHandle(job)
            return
        _job = job
    process = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
    if process:
        kernel32.AssignProcessToJobObject(_job, process)
        kernel32.CloseHandle(process)


_job = None


class Tunnel(QObject):
    """Runs cloudflared for one local port and reports the public address it was given."""

    changed = Signal()  # state, address or download progress changed

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state, self.url, self.error, self.progress = OFF, "", "", 0
        self._port = 0
        self._process: QProcess | None = None
        self._reply: QNetworkReply | None = None
        self._net: QNetworkAccessManager | None = None
        self._log = ""
        self._retries = 0
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.setInterval(START_TIMEOUT_MS)
        self._deadline.timeout.connect(self._too_slow)

    # ---- on and off

    def start(self, port: int, download: bool = False) -> None:
        """Bring the link up for the app listening on `port`. `download` allows fetching cloudflared first."""
        if self.state in (STARTING, ON, DOWNLOADING) and port == self._port:
            return
        self.stop()
        self._port, self._retries = port, 0
        if not supported():
            self._fail("The internet link is only available on Windows.")
        elif installed():
            self._launch()
        elif download:
            self._download()
        else:
            self._fail("Cloudflare's tunnel program is not on this machine yet: open Settings and save again.")

    def stop(self) -> None:
        self._deadline.stop()
        if self._reply is not None:
            reply, self._reply = self._reply, None
            reply.abort()
            reply.deleteLater()
        if self._process is not None:
            process, self._process = self._process, None
            process.blockSignals(True)
            process.kill()
            process.waitForFinished(1500)
            process.deleteLater()
        if (self.state, self.url, self.error) != (OFF, "", ""):
            self.state, self.url, self.error, self.progress = OFF, "", "", 0
            self.changed.emit()

    def _set(self, state: str, url: str = "", error: str = "") -> None:
        self.state, self.url, self.error = state, url, error
        self.changed.emit()

    def _fail(self, error: str) -> None:
        self._deadline.stop()
        self._set(FAILED, error=error)

    # ---- getting cloudflared

    def _download(self) -> None:
        self.progress = 0
        self._set(DOWNLOADING)
        if self._net is None:
            self._net = QNetworkAccessManager(self)
        request = QNetworkRequest(QUrl(DOWNLOAD_URL))
        request.setAttribute(QNetworkRequest.Attribute.RedirectPolicyAttribute,
                             QNetworkRequest.RedirectPolicy.NoLessSafeRedirectPolicy)
        self._reply = reply = self._net.get(request)
        reply.downloadProgress.connect(self._downloading)
        reply.finished.connect(self._downloaded)

    def _downloading(self, received: int, total: int) -> None:
        percent = int(received * 100 / total) if total > 0 else 0
        if percent != self.progress:
            self.progress = percent
            self.changed.emit()

    def _downloaded(self) -> None:
        reply, self._reply = self._reply, None
        if reply is None:
            return
        reply.deleteLater()
        if reply.error() != QNetworkReply.NetworkError.NoError:
            self._fail(f"Downloading Cloudflare's tunnel program failed: {reply.errorString()}")
            return
        data = bytes(reply.readAll())
        if len(data) < 5_000_000 or data[:2] != b"MZ":  # not a Windows program: an error page, or cut short
            self._fail("Downloading Cloudflare's tunnel program failed: what arrived is not the program.")
            return
        try:
            PROGRAM.parent.mkdir(parents=True, exist_ok=True)
            part = PROGRAM.with_suffix(".part")
            part.write_bytes(data)
        except OSError as problem:
            self._fail(f"Could not save Cloudflare's tunnel program: {problem}")
            return
        self._verify(part)

    def _verify(self, part: Path) -> None:
        """Run it only if Windows confirms it is signed by Cloudflare: a download is not trusted on its word."""
        check = QProcess(self)
        script = (f"$s = Get-AuthenticodeSignature -LiteralPath '{str(part).replace(chr(39), chr(39) * 2)}'; "
                  "$s.Status; $s.SignerCertificate.Subject")

        handled = []

        def done(*_args) -> None:
            if handled:
                return
            handled.append(True)
            answer = bytes(check.readAllStandardOutput()).decode("utf-8", "replace").strip().splitlines()
            check.deleteLater()
            if self.state != DOWNLOADING:  # switched off in the meantime
                part.unlink(missing_ok=True)
                return
            if len(answer) >= 2 and answer[0].strip() == "Valid" and SIGNER in answer[1]:
                try:
                    part.replace(PROGRAM)
                except OSError as problem:
                    self._fail(f"Could not save Cloudflare's tunnel program: {problem}")
                    return
                self._launch()
            else:
                part.unlink(missing_ok=True)
                self._fail("The downloaded tunnel program is not signed by Cloudflare, so it was not used "
                           f"({' / '.join(answer)[:160] or 'no signature information'}).")

        check.finished.connect(done)
        check.errorOccurred.connect(done)  # could not even ask: treated as "not confirmed"
        check.start("powershell", ["-NoProfile", "-NonInteractive", "-Command", script])

    # ---- running it

    def _launch(self) -> None:
        self._log = ""
        self._set(STARTING)
        self._process = process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.readyReadStandardOutput.connect(self._output)
        process.finished.connect(self._ended)
        process.errorOccurred.connect(self._broken)
        process.started.connect(lambda: _tie_to_this_process(process.processId()))
        # 127.0.0.1, not "localhost": the app listens on IPv4 only in this mode.
        process.start(str(PROGRAM), ["tunnel", "--no-autoupdate", *EXTRA_ARGS, "--url", f"http://127.0.0.1:{self._port}"])
        self._deadline.start()

    def _output(self) -> None:
        if self._process is None:
            return
        text = bytes(self._process.readAllStandardOutput()).decode("utf-8", "replace")
        self._log = (self._log + text)[-LOG_KEPT:]
        if self.state == STARTING:
            match = PUBLIC_URL.search(self._log)
            if match:
                self._deadline.stop()
                self._retries = 0
                self._set(ON, url=match.group(0))

    def _reason(self) -> str:
        """The last thing cloudflared said that reads like an error, or just its last line."""
        lines = [line.strip() for line in self._log.splitlines() if line.strip()]
        errors = [line for line in lines if " ERR " in line or "error" in line.lower()]
        line = (errors or lines or ["no output"])[-1]
        return re.sub(r"^\S+Z\s+(ERR|INF|WRN)\s+", "", line)[:300]

    def _again_or_fail(self, error: str) -> None:
        if self._process is not None:
            process, self._process = self._process, None
            process.blockSignals(True)
            process.kill()
            process.deleteLater()
        if self._retries < RETRIES and self.state != OFF:
            self._retries += 1
            self._deadline.stop()
            self._set(STARTING)
            QTimer.singleShot(RETRY_MS, lambda: self._launch() if self.state == STARTING and self._process is None else None)
        else:
            self._fail(error)

    def _ended(self, _code: int = 0, _status=None) -> None:
        if self._process is not None:  # it stopped by itself: the address is gone with it
            self._again_or_fail(f"The internet link stopped: {self._reason()}")

    def _broken(self, problem) -> None:
        if self._process is not None and problem == QProcess.ProcessError.FailedToStart:
            self._process.blockSignals(True)
            self._process.deleteLater()
            self._process = None
            self._fail(f"Cloudflare's tunnel program could not be started ({PROGRAM}).")

    def _too_slow(self) -> None:
        if self.state == STARTING:
            self._again_or_fail(f"The internet link did not come up: {self._reason()}")
