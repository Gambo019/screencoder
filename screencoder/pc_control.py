"""Controlling the PC itself from the remote page - its whole screen, not only this app. Off by default (Settings:
remote_pc_control): whoever has the remote link could then use the PC.

Nothing is streamed. The viewer clicks, drags, scrolls or types on a picture of a monitor; the page sends what it
did as one message per action (a whole drag is one message: where it was pressed, the path, where it was let go,
with the timing); this replays it with Windows' own input (SendInput), gives the screen a moment to show the
result, and sends back a picture of that monitor - only the parts that changed since the viewer's last picture.
Actions that come in quicker than they are answered are replayed one after another, and only the last of them
gets a picture.

Messages from the page (`mon` is the monitor's index, `width` how wide the viewer wants the picture; places are
fractions 0..1 of the monitor):
    ["pcv", mon, width]                                   a picture, nothing else
    ["pcm", mon, width, button, clicks, points, mods]     mouse: button 0 left / 1 middle / 2 right, points
                                                          [[x, y, ms since pressed], ...] from press to release
    ["pcw", mon, width, x, y, dx, dy]                     wheel, in the browser's pixels (down and right positive)
    ["pck", mon, width, events]                           keys: ["t", text] or ["k", key name, mods]
mods: 1 Shift, 2 Ctrl, 4 Alt, 8 Win. The answer is ["PC", {...}]: the monitors, the picture or its changed pieces,
where the mouse pointer is, or why nothing could be done."""
from __future__ import annotations

import base64
import ctypes
import sys
import time
from collections import deque

import numpy as np
from PySide6.QtCore import QBuffer, QIODevice, QObject, Qt, QTimer
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from .config import Settings

SETTLE_MS = 350          # after the input the screen gets this long to show what it did, then it is pictured
QUICK_SETTLE_MS = 60     # ...when another action is already waiting: no picture in between
MAX_GESTURE_MS = 4000    # a drag or a held press is replayed at its own speed, but never longer than this
DOUBLE_GAP_MS = 60       # between the two clicks of a double-click
MIN_WIDTH, MAX_WIDTH = 480, 2560
QUALITY = 72             # as WebP: text stays readable
WHOLE_FROM = 0.55        # when more of the picture than this changed, it goes whole
BAND_GAP = 24            # rows without a change that part two changed pieces
MAX_PIECES = 6
SMALL_PIECE = 120_000    # pixels; a smaller piece goes as PNG: exact, and small anyway

# ---- Windows input ------------------------------------------------------------------------------------------------
_MOVE, _ABSOLUTE, _VIRTUALDESK = 0x0001, 0x8000, 0x4000
_WHEEL, _HWHEEL = 0x0800, 0x1000
_BUTTON_FLAGS = {0: (0x0002, 0x0004), 1: (0x0020, 0x0040), 2: (0x0008, 0x0010)}  # left, middle, right: down, up
_KEYUP, _UNICODE, _EXTENDED = 0x0002, 0x0004, 0x0001
_MODIFIER_KEYS = ((1, 0x10), (2, 0x11), (4, 0x12), (8, 0x5B))  # Shift, Ctrl, Alt, Win
_VK = {  # the page's key names (KeyboardEvent.key) -> virtual key, extended
    "Enter": (0x0D, False), "Backspace": (0x08, False), "Tab": (0x09, False), "Escape": (0x1B, False),
    " ": (0x20, False), "Delete": (0x2E, True), "Insert": (0x2D, True), "Home": (0x24, True), "End": (0x23, True),
    "PageUp": (0x21, True), "PageDown": (0x22, True), "ArrowLeft": (0x25, True), "ArrowUp": (0x26, True),
    "ArrowRight": (0x27, True), "ArrowDown": (0x28, True), "ContextMenu": (0x5D, True), "PrintScreen": (0x2C, True),
    "Meta": (0x5B, True), "OS": (0x5B, True), "Pause": (0x13, False), "CapsLock": (0x14, False),
    **{f"F{n}": (0x6F + n, False) for n in range(1, 13)},
}

if sys.platform == "win32":
    from ctypes import wintypes as _wt

    class _MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", _wt.LONG), ("dy", _wt.LONG), ("mouseData", _wt.DWORD), ("dwFlags", _wt.DWORD),
                    ("time", _wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]

    class _KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", _wt.WORD), ("wScan", _wt.WORD), ("dwFlags", _wt.DWORD), ("time", _wt.DWORD),
                    ("dwExtraInfo", ctypes.c_size_t)]

    class _HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", _wt.DWORD), ("wParamL", _wt.WORD), ("wParamH", _wt.WORD)]

    class _UNION(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT), ("hi", _HARDWAREINPUT)]

    class _INPUT(ctypes.Structure):
        _fields_ = [("type", _wt.DWORD), ("u", _UNION)]


class Injector:
    """Windows' own input: what SendInput is given is what a mouse and a keyboard would have done. Tests replace
    `send` to see what would be sent without touching the real screen."""

    def __init__(self):
        self.sent: list = []  # kept only when `record` is set (tests)
        self.record = False

    def send(self, items: list) -> None:
        if self.record:
            self.sent.extend(items)
            return
        if sys.platform != "win32" or not items:
            return
        array = (_INPUT * len(items))()
        for i, item in enumerate(items):
            if item[0] == "m":  # ("m", dx, dy, data, flags)
                array[i].type = 0
                array[i].u.mi = _MOUSEINPUT(item[1], item[2], item[3] & 0xFFFFFFFF, item[4], 0, 0)
            else:  # ("k", vk, scan, flags)
                array[i].type = 1
                array[i].u.ki = _KEYBDINPUT(item[1], item[2], item[3], 0, 0)
        ctypes.windll.user32.SendInput(len(items), array, ctypes.sizeof(_INPUT))

    @staticmethod
    def desktop() -> tuple[int, int, int, int]:
        """The whole desktop, every monitor, in physical pixels: left, top, width, height."""
        if sys.platform != "win32":
            return 0, 0, 1, 1
        metric = ctypes.windll.user32.GetSystemMetrics
        return metric(76), metric(77), max(1, metric(78)), max(1, metric(79))

    @staticmethod
    def cursor() -> tuple[int, int] | None:
        if sys.platform != "win32":
            return None
        point = _wt.POINT()
        return (point.x, point.y) if ctypes.windll.user32.GetCursorPos(ctypes.byref(point)) else None

    def move(self, x: int, y: int) -> None:
        left, top, width, height = self.desktop()
        nx = round((x - left) * 65535 / max(1, width - 1))
        ny = round((y - top) * 65535 / max(1, height - 1))
        self.send([("m", nx, ny, 0, _MOVE | _ABSOLUTE | _VIRTUALDESK)])

    def button(self, button: int, down: bool) -> None:
        flags = _BUTTON_FLAGS.get(button, _BUTTON_FLAGS[0])
        self.send([("m", 0, 0, 0, flags[0] if down else flags[1])])

    def wheel(self, dy: int, dx: int = 0) -> None:
        items = []
        if dy:
            items.append(("m", 0, 0, dy, _WHEEL))
        if dx:
            items.append(("m", 0, 0, dx, _HWHEEL))
        self.send(items)

    def modifiers(self, mods: int, down: bool) -> None:
        keys = [vk for bit, vk in _MODIFIER_KEYS if mods & bit]
        self.send([("k", vk, 0, 0 if down else _KEYUP) for vk in (keys if down else reversed(keys))])

    def text(self, text: str) -> None:
        items = []
        for ch in text.replace("\r\n", "\n"):
            if ch == "\n":
                items += [("k", 0x0D, 0, 0), ("k", 0x0D, 0, _KEYUP)]
                continue
            units = ch.encode("utf-16-le")
            for i in range(0, len(units), 2):  # characters beyond the first plane go as two halves
                unit = int.from_bytes(units[i:i + 2], "little")
                items += [("k", 0, unit, _UNICODE), ("k", 0, unit, _UNICODE | _KEYUP)]
        self.send(items)

    def key(self, name: str, mods: int) -> None:
        """A key, maybe with Ctrl, Alt, Shift or Win held: Ctrl+C, Alt+Tab, Shift+ArrowRight, Enter..."""
        found = _VK.get(name)
        if found is None and len(name) == 1:
            if sys.platform == "win32":
                scan = ctypes.windll.user32.VkKeyScanW(ord(name))
                if scan == -1 or scan & 0xFF == 0xFF:
                    self.text(name)
                    return
                found = (scan & 0xFF, False)
                if scan & 0x100 and not mods & 1:
                    mods |= 1  # the character needs Shift on this keyboard layout
            else:
                found = (ord(name.upper()), False)
        if found is None:
            return
        vk, extended = found
        flags = _EXTENDED if extended else 0
        self.modifiers(mods, True)
        self.send([("k", vk, 0, flags), ("k", vk, 0, flags | _KEYUP)])
        self.modifiers(mods, False)


# ---- pictures -----------------------------------------------------------------------------------------------------
def _encode(image: QImage) -> str:
    """A picture as a data URL: a small piece as PNG (exact), a larger one as WebP (JPEG if WebP is missing)."""
    small = image.width() * image.height() <= SMALL_PIECE
    for kind in (("png",) if small else ("webp", "jpeg")):
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        if image.save(buffer, kind.upper(), 40 if small else QUALITY):
            return f"data:image/{kind};base64," + base64.b64encode(bytes(buffer.data())).decode("ascii")
    return ""


def pixels(image: QImage) -> np.ndarray:
    """The picture's pixels (height x width x 4), to compare with the last one sent."""
    image = image.convertToFormat(QImage.Format.Format_RGB32)
    raw = np.frombuffer(image.constBits(), dtype=np.uint8, count=image.sizeInBytes())
    return raw.reshape(image.height(), image.bytesPerLine())[:, : image.width() * 4] \
        .reshape(image.height(), image.width(), 4).copy()


def changed_pieces(before: np.ndarray | None, after: np.ndarray) -> list[tuple[int, int, int, int]] | None:
    """Where two pictures differ, as at most MAX_PIECES rectangles (x, y, w, h): [] when nothing changed, None
    when they are not comparable or so much changed that the whole picture should go."""
    if before is None or before.shape != after.shape:
        return None
    changed = np.any(before != after, axis=2)
    rows = np.flatnonzero(changed.any(axis=1))
    if rows.size == 0:
        return []
    bands, start, last = [], rows[0], rows[0]
    for row in rows[1:]:
        if row - last > BAND_GAP:
            bands.append((start, last))
            start = row
        last = row
    bands.append((start, last))
    if len(bands) > MAX_PIECES:
        bands = [(rows[0], rows[-1])]
    height, width = changed.shape
    pieces = []
    for top, bottom in bands:
        cols = np.flatnonzero(changed[top:bottom + 1].any(axis=0))
        left, right = max(0, int(cols[0]) - 2), min(width - 1, int(cols[-1]) + 2)
        top, bottom = max(0, int(top) - 2), min(height - 1, int(bottom) + 2)
        pieces.append((left, top, right - left + 1, bottom - top + 1))
    if sum(w * h for _x, _y, w, h in pieces) > WHOLE_FROM * width * height:
        return None
    return pieces


def _monitors() -> list:
    """Each monitor: (screen, [left, top, width, height] in physical pixels). Qt keeps a monitor's corner where it
    really is and scales what lies inside, so the size is the logical one times the monitor's scaling."""
    found = []
    for screen in QApplication.screens():
        box, ratio = screen.geometry(), screen.devicePixelRatio()
        found.append((screen, [box.x(), box.y(), round(box.width() * ratio), round(box.height() * ratio)]))
    return found


def allowed() -> bool:
    return bool(getattr(Settings.load(), "remote_pc_control", False))


class PCControl(QObject):
    """Replays the viewers' actions on this PC and answers each with a picture of the monitor (see the top)."""

    def __init__(self, send, parent=None):
        super().__init__(parent)
        self._send = send  # send(message, only=client)
        self.injector = Injector()
        self._queue: deque = deque()
        self._busy = False
        self._frames: dict[tuple[int, int], tuple[int, np.ndarray]] = {}  # (viewer, monitor) -> (width, pixels)
        self.allowed = allowed  # replaced in tests

    # ---- what comes in

    def handle(self, client, message: list) -> None:
        if not isinstance(message, list) or len(message) < 3 or message[0] not in ("pcv", "pcm", "pcw", "pck"):
            return
        self._queue.append((client, message))
        if not self._busy:
            QTimer.singleShot(0, self._next)

    def forget(self, client) -> None:
        """A viewer left: its last pictures are no longer needed."""
        for key in [key for key in self._frames if key[0] == id(client)]:
            del self._frames[key]
        self._queue = deque(item for item in self._queue if item[0] is not client)

    def _next(self) -> None:
        if self._busy or not self._queue:
            return
        client, message = self._queue.popleft()
        if not self.allowed():
            self._send(["PC", {"err": "Controlling this PC from here is switched off. It can be switched on in "
                                      "Settings on the PC (Remote control)."}], only=client)
            self._queue = deque(item for item in self._queue if item[0] is not client)
            return
        monitors = _monitors()
        try:
            mon = max(0, min(int(message[1]), len(monitors) - 1))
            width = int(message[2])
        except (TypeError, ValueError):
            return
        steps = self._steps(message, monitors[mon][1]) if monitors else []
        self._busy = True
        self._play(steps, lambda: self._after(client, mon, width))

    # ---- replaying

    def _steps(self, message: list, box: list[int]) -> list:
        """What to do, as (milliseconds to wait first, action) pairs."""
        op = message[0]
        inj = self.injector

        def at(x, y):
            return (round(box[0] + min(max(float(x), 0.0), 1.0) * (box[2] - 1)),
                    round(box[1] + min(max(float(y), 0.0), 1.0) * (box[3] - 1)))

        steps = []
        try:
            if op == "pcm":
                button, clicks, points, mods = int(message[3]), int(message[4]), list(message[5]), int(message[6] or 0)
                if not points:
                    return []
                first = at(*points[0][:2])
                steps.append((0, lambda: inj.modifiers(mods, True)))
                steps.append((0, lambda: inj.move(*first)))
                if clicks >= 2:  # a double-click: two clicks at one place, quickly
                    for n in range(2):
                        steps.append((DOUBLE_GAP_MS if n else 10, lambda: inj.button(button, True)))
                        steps.append((20, lambda: inj.button(button, False)))
                else:
                    steps.append((10, lambda: inj.button(button, True)))
                    # Held and moved: along the same path, at its own speed (shortened to MAX_GESTURE_MS at most).
                    span = max(1, int(points[-1][2]) if len(points[-1]) > 2 else 0)
                    scale = min(1.0, MAX_GESTURE_MS / span)
                    last_ms = 0
                    for point in points[1:]:
                        ms = int(point[2]) if len(point) > 2 else last_ms + 16
                        target = at(*point[:2])
                        steps.append((max(0, round((ms - last_ms) * scale)), lambda t=target: inj.move(*t)))
                        last_ms = ms
                    steps.append((max(20, 0 if len(points) > 1 else 40), lambda: inj.button(button, False)))
                steps.append((0, lambda: inj.modifiers(mods, False)))
            elif op == "pcw":
                place = at(message[3], message[4])
                dx, dy = float(message[5] or 0), float(message[6] or 0)
                # The browser counts about 100 pixels a notch, down positive; Windows 120 a notch, up positive.
                wheel = max(-1800, min(1800, round(-dy / 100 * 120)))
                tilt = max(-1800, min(1800, round(dx / 100 * 120)))
                steps.append((0, lambda: inj.move(*place)))
                steps.append((10, lambda: inj.wheel(wheel, tilt)))
            elif op == "pck":
                for event in list(message[3])[:400]:
                    if not isinstance(event, list) or not event:
                        continue
                    if event[0] == "t" and len(event) > 1:
                        steps.append((5, lambda text=str(event[1])[:4000]: inj.text(text)))
                    elif event[0] == "k" and len(event) > 1:
                        steps.append((5, lambda name=str(event[1]), mods=int(event[2] or 0) if len(event) > 2 else 0:
                                      inj.key(name, mods)))
        except (TypeError, ValueError, IndexError):
            return []
        return steps

    def _play(self, steps: list, then) -> None:
        """Run the steps one after another without blocking: the app itself goes on working meanwhile (and may be
        what is being clicked)."""
        if not steps:
            then()
            return
        wait, action = steps[0]

        def step():
            try:
                action()
            except Exception:  # noqa: BLE001 - a failed input must not stop the queue
                pass
            self._play(steps[1:], then)

        QTimer.singleShot(wait, step) if wait else step()

    def _after(self, client, mon: int, width: int) -> None:
        """Done replaying: a picture once the screen has settled - unless the same viewer has more waiting."""
        more = any(item[0] is client for item in self._queue)

        def finish():
            if not more:
                self._picture(client, mon, width)
            self._busy = False
            self._next()

        QTimer.singleShot(QUICK_SETTLE_MS if more else SETTLE_MS, finish)

    # ---- the picture

    def _picture(self, client, mon: int, width: int) -> None:
        started = time.perf_counter()
        monitors = _monitors()
        if not monitors:
            self._send(["PC", {"err": "No monitor to show."}], only=client)
            return
        mon = min(mon, len(monitors) - 1)
        screen, box = monitors[mon]
        image = screen.grabWindow(0).toImage().convertToFormat(QImage.Format.Format_RGB32)
        want = max(MIN_WIDTH, min(MAX_WIDTH, int(width or box[2]), image.width() or box[2]))
        if image.width() > want:
            image = image.scaledToWidth(want, Qt.TransformationMode.SmoothTransformation)
        now = pixels(image)
        key = (id(client), mon)
        before = self._frames.get(key)
        pieces = changed_pieces(before[1] if before and before[0] == image.width() else None, now)
        self._frames[key] = (image.width(), now)
        answer = {"mon": mon, "w": image.width(), "h": image.height(),
                  "mons": [[s.name(), b[2], b[3], 1 if s is QApplication.primaryScreen() else 0] for s, b in monitors]}
        if pieces is None:
            answer["whole"] = _encode(image)
        elif pieces:
            answer["pieces"] = [[x, y, w, h, _encode(image.copy(x, y, w, h))] for x, y, w, h in pieces]
        else:
            answer["same"] = 1
        where = self.injector.cursor()
        if where and box[0] <= where[0] < box[0] + box[2] and box[1] <= where[1] < box[1] + box[3]:
            answer["cur"] = [round((where[0] - box[0]) / box[2] * image.width()),
                             round((where[1] - box[1]) / box[3] * image.height())]
        answer["ms"] = round((time.perf_counter() - started) * 1000)
        self._send(["PC", answer], only=client)
