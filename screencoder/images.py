"""Image helpers: API-safe encoding and duplicate detection for screenshots."""
from __future__ import annotations

import hashlib
import math
from collections import Counter
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRect, Qt
from PySide6.QtGui import QColor, QImage, QPainter

# Opus 4.7 and later (and Sonnet 5 and later) read images up to 2576 px on the long edge (~3.75 MP) at
# full fidelity; older models are downscaled further by the API itself.
MAX_LONG_EDGE = 2576
MAX_PIXELS = 3_750_000
# Stricter per-image cap the API applies when one request carries more than 20 images.
MAX_LONG_EDGE_MANY = 2000
# The API rejects images whose base64 payload exceeds 5 MB.
MAX_IMAGE_B64 = 5 * 1024 * 1024 - 64 * 1024

# Two screenshots of the same size whose 128-px grayscale thumbnails differ by less than this
# (mean absolute difference, 0-255) are flagged as near-duplicates (cursor blink, clock change...).
NEAR_DUP_THRESHOLD = 1.0

# Set by a whole-monitor capture: "x,y,w,h" of the part not covered by the taskbar (see capture.py).
FREE_AREA_KEY = "screencoder-free-area"
# A screenshot whose differences from an earlier one fill more than this share of it is sent whole.
MOSTLY_CHANGED = 0.85
CROP_MARGIN = 8           # pixels kept around a part that changed
MIN_PART = 64             # a part sent on its own is at least this wide and tall
SCROLL_MATCH = 0.8        # share of the overlapping lines that must agree to call it a scroll
SCROLL_MIN_LINES = 12     # pixel rows with content that must agree, at least
SCROLL_CONTEXT = 36       # pixel rows of what was already seen kept above (or below) the new lines


def b64_len(n_bytes: int) -> int:
    return 4 * ((n_bytes + 2) // 3)


def encode(img: QImage, fmt: str, quality: int = -1) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, fmt, quality)
    buf.close()
    return bytes(data.data())


def _flatten(img: QImage) -> QImage:
    """Drop alpha (composite on white) so PNGs stay small and transparent areas stay readable."""
    if not img.hasAlphaChannel():
        return img.convertToFormat(QImage.Format.Format_RGB888)
    out = QImage(img.size(), QImage.Format.Format_RGB888)
    out.fill(QColor("white"))
    painter = QPainter(out)
    painter.drawImage(0, 0, img)
    painter.end()
    return out


def _scaled(img: QImage, factor: float) -> QImage:
    return img.scaled(
        max(1, int(img.width() * factor)),
        max(1, int(img.height() * factor)),
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )


def encode_for_api(img: QImage, max_edge: int = MAX_LONG_EDGE) -> tuple[bytes, str]:
    """Return (bytes, media_type) within the API's limits, preferring lossless PNG for crisp code text."""
    img = _flatten(img)
    w, h = img.width(), img.height()
    factor = min(1.0, max_edge / max(w, h), math.sqrt(MAX_PIXELS / (w * h)))
    if factor < 1.0:
        img = _scaled(img, factor)
    while True:
        best = (encode(img, "PNG"), "image/png")
        if b64_len(len(best[0])) > 1_500_000:  # photo-like content: JPEG is usually far smaller
            jpg = encode(img, "JPEG", 92)
            if len(jpg) < len(best[0]):
                best = (jpg, "image/jpeg")
        if b64_len(len(best[0])) <= MAX_IMAGE_B64 or min(img.width(), img.height()) < 200:
            return best
        img = _scaled(img, 0.85)


def _raw_bytes(img: QImage) -> bytes:
    return bytes(img.constBits())[: img.sizeInBytes()]


def exact_signature(img: QImage) -> str:
    rgb = img.convertToFormat(QImage.Format.Format_RGB32)
    digest = hashlib.sha1(f"{rgb.width()}x{rgb.height()}:".encode())
    digest.update(_raw_bytes(rgb))
    return digest.hexdigest()


def gray_thumbnail(img: QImage, width: int = 128) -> bytes:
    height = max(1, round(img.height() * width / max(1, img.width())))
    thumb = img.scaled(
        width, height, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation
    ).convertToFormat(QImage.Format.Format_Grayscale8)
    raw, stride = _raw_bytes(thumb), thumb.bytesPerLine()
    return b"".join(raw[y * stride : y * stride + width] for y in range(thumb.height()))


def near_duplicate(a_size: tuple[int, int], a_thumb: bytes, b_size: tuple[int, int], b_thumb: bytes) -> bool:
    if abs(a_size[0] - b_size[0]) > 2 or abs(a_size[1] - b_size[1]) > 2 or len(a_thumb) != len(b_thumb):
        return False
    diff = sum(abs(x - y) for x, y in zip(a_thumb, b_thumb)) / max(1, len(a_thumb))
    return diff < NEAR_DUP_THRESHOLD


# ---- sending fewer pixels: the model reads and thinks over every one, so what it has seen once is not sent again

def without_taskbar(img: QImage) -> QImage:
    """A whole-monitor capture without its taskbar; any other picture (pasted, snipped) as it is."""
    spot = img.text(FREE_AREA_KEY)
    if not spot:
        return img
    try:
        rect = QRect(*(int(v) for v in spot.split(","))).intersected(img.rect())
    except (TypeError, ValueError):
        return img
    return img.copy(rect) if rect.isValid() and rect != img.rect() else img


def _pixels(img: QImage) -> np.ndarray:
    rgb = img.convertToFormat(QImage.Format.Format_RGB32)
    rows = np.frombuffer(rgb.constBits(), dtype=np.uint32).reshape(rgb.height(), rgb.bytesPerLine() // 4)
    return rows[:, :rgb.width()].copy()


def _box(changed: np.ndarray) -> tuple[int, int, int, int] | None:
    """(x, y, w, h) around every True, or None."""
    rows, cols = np.flatnonzero(changed.any(axis=1)), np.flatnonzero(changed.any(axis=0))
    if not rows.size:
        return None
    return int(cols[0]), int(rows[0]), int(cols[-1] - cols[0] + 1), int(rows[-1] - rows[0] + 1)


def _padded(box: tuple[int, int, int, int], width: int, height: int) -> tuple[int, int, int, int]:
    x, y, w, h = box
    x0, y0 = max(0, x - CROP_MARGIN), max(0, y - CROP_MARGIN)
    x1, y1 = min(width, x + w + CROP_MARGIN), min(height, y + h + CROP_MARGIN)
    for low, high, size in ((0, 2, width), (1, 3, height)):  # never a sliver: at least MIN_PART each way
        edges = [x0, y0, x1, y1]
        short = MIN_PART - (edges[high] - edges[low])
        if short > 0:
            edges[low] = max(0, edges[low] - (short + 1) // 2)
            edges[high] = min(size, edges[low] + MIN_PART)
            edges[low] = max(0, edges[high] - MIN_PART)
        x0, y0, x1, y1 = edges
    return x0, y0, x1 - x0, y1 - y0


def _scroll(before: np.ndarray, after: np.ndarray, box: tuple[int, int, int, int]) -> int:
    """How far the view inside `box` scrolled from `before` to `after`, in pixel rows: positive when the
    content moved up (scrolled down), negative when it moved down, 0 when this is not a scroll."""
    x, y, w, h = box
    band = slice(x + w // 4, x + w - w // 4)  # the middle of it: away from gutters, minimaps and scroll bars
    a, b = before[y:y + h, band], after[y:y + h, band]
    if a.shape[1] < 8 or h < 3 * SCROLL_MIN_LINES:
        return 0
    content = lambda rows: ~(rows == rows[:, :1]).all(axis=1)  # a row of one colour says nothing
    a_has, b_has = content(a), content(b)
    keys_a = [a[i].tobytes() if a_has[i] else None for i in range(h)]
    where: dict[bytes, list[int]] = {}
    for i, key in enumerate(keys_a):
        if key is not None:
            where.setdefault(key, []).append(i)
    votes: Counter = Counter()
    for i in range(h):
        if b_has[i]:
            for j in where.get(b[i].tobytes(), ())[:4]:  # a repeated row (a blank code line) votes little
                if j != i:
                    votes[j - i] += 1
    if not votes:
        return 0
    shift = votes.most_common(1)[0][0]
    lines = [i for i in range(max(0, -shift), min(h, h - shift)) if b_has[i]]
    if len(lines) < SCROLL_MIN_LINES:
        return 0
    agree = sum(1 for i in lines if keys_a[i + shift] is not None and b[i].tobytes() == keys_a[i + shift])
    return shift if agree >= SCROLL_MATCH * len(lines) else 0


@dataclass
class ShotPlan:
    """What is sent of one screenshot. kind: "whole" (all of it), "same" (identical to `ref`: nothing),
    "part" (only `rect`, where it differs from `ref`; the rest is exactly as there), "new" (`ref` scrolled by
    `shift` rows: only the new lines, in `rect`)."""
    kind: str
    image: QImage | None
    ref: int = -1
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    shift: int = 0


def plan_shots(imgs: list[QImage], new_part_only: bool = False) -> list[ShotPlan]:
    """Decide what to send of each screenshot of one message, in order.

    Always: a whole-monitor capture goes without its taskbar; a screenshot identical to an earlier one is not
    sent again; one of the same size as an earlier one is cut down to the rectangle where it differs from the
    first of them (which is sent whole), unless that is most of it. With `new_part_only`, a screenshot that is
    the previous one of its size scrolled up or down is cut down to the lines that scrolled into view."""
    shown = [without_taskbar(img) for img in imgs]
    plans: list[ShotPlan] = []
    firsts: dict[tuple[int, int], int] = {}   # size -> index of the first screenshot of that size (sent whole)
    previous: dict[tuple[int, int], int] = {}  # size -> index of the latest one of that size
    seen: dict[str, int] = {}
    cache: dict[int, np.ndarray] = {}
    pixels = lambda i: cache[i] if i in cache else cache.setdefault(i, _pixels(shown[i]))
    for i, img in enumerate(shown):
        sig = exact_signature(img)
        if sig in seen:
            plans.append(ShotPlan("same", None, ref=seen[sig]))
            continue
        seen[sig] = i
        size = (img.width(), img.height())
        first = firsts.setdefault(size, i)
        last = previous.get(size)
        previous[size] = i
        if first == i:
            plans.append(ShotPlan("whole", img))
            continue
        now = pixels(i)
        if new_part_only and last is not None:
            before = pixels(last)
            moved = _box(before != now)
            shift = _scroll(before, now, moved) if moved else 0
            if shift:
                x, y, w, h = moved
                if shift > 0:  # scrolled down: the new lines are at the bottom of the view
                    top = max(y, y + h - shift - SCROLL_CONTEXT)
                    rect = (x, top, w, y + h - top)
                else:
                    rect = (x, y, w, min(h, -shift + SCROLL_CONTEXT))
                plans.append(ShotPlan("new", img.copy(QRect(*rect)), ref=last, rect=rect, shift=shift))
                continue
        box = _box(pixels(first) != now)
        if box is None or box[2] * box[3] > MOSTLY_CHANGED * size[0] * size[1]:
            plans.append(ShotPlan("whole", img))
            continue
        rect = _padded(box, *size)
        plans.append(ShotPlan("part", img.copy(QRect(*rect)), ref=first, rect=rect))
    return plans
