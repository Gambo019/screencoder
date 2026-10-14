"""Image helpers: API-safe encoding and duplicate detection for screenshots."""
from __future__ import annotations

import hashlib
import math

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, Qt
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
