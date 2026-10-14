"""Attachments (screenshots + local files) and turning them into Claude message content blocks."""
from __future__ import annotations

import base64
import itertools
import shutil
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QImage

from . import images

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp"}
TEXT_INLINE_LIMIT = 256 * 1024
# Base64 budget for images inlined in one message; the API caps a whole request at 32 MB.
INLINE_IMAGE_BUDGET = 24 * 1024 * 1024

FENCE_LANGS = {
    ".py": "python", ".js": "javascript", ".jsx": "jsx", ".ts": "typescript", ".tsx": "tsx",
    ".java": "java", ".kt": "kotlin", ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".go": "go", ".rs": "rust", ".rb": "ruby", ".php": "php", ".swift": "swift",
    ".scala": "scala", ".sql": "sql", ".sh": "bash", ".ps1": "powershell", ".html": "html",
    ".css": "css", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".xml": "xml", ".md": "markdown",
}

_ids = itertools.count(1)


@dataclass
class Attachment:
    kind: str  # "image" | "file"
    name: str
    image: QImage | None = None
    path: str = ""  # original location on disk (files, or images loaded from files)
    exact_sig: str = ""
    thumb: bytes = b""
    near_dup_of: str = ""  # name of a near-identical screenshot already attached
    id: int = 0

    def __post_init__(self) -> None:
        self.id = next(_ids)

    @property
    def size(self) -> tuple[int, int]:
        return (self.image.width(), self.image.height()) if self.image is not None else (0, 0)


@dataclass
class PreparedImage:
    label: str
    path: Path
    media_type: str
    data_b64: str | None  # None when it did not fit the inline budget (the model reads it from disk)


@dataclass
class PreparedFile:
    name: str
    path: Path
    original: str
    text: str | None  # inlined content; None for binary or large files


def _read_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > TEXT_INLINE_LIMIT:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\x00" in raw[:8192]:
        return None
    return raw.decode("utf-8", errors="replace")


def _unique(dest: Path) -> Path:
    if not dest.exists():
        return dest
    for n in itertools.count(2):
        candidate = dest.with_name(f"{dest.stem}_{n}{dest.suffix}")
        if not candidate.exists():
            return candidate
    raise AssertionError("unreachable")


def prepare(atts: list[Attachment], dest: Path, label: str = "Screenshot") -> tuple[list[PreparedImage], list[PreparedFile]]:
    """Save attachments under `dest` and encode images for the API (within the inline budget)."""
    shots = [a for a in atts if a.kind == "image" and a.image is not None]
    max_edge = images.MAX_LONG_EDGE if len(shots) <= 20 else images.MAX_LONG_EDGE_MANY
    budget = INLINE_IMAGE_BUDGET
    prepared_images: list[PreparedImage] = []
    if shots:
        (dest / "screenshots").mkdir(parents=True, exist_ok=True)
    for i, att in enumerate(shots, 1):
        data, media_type = images.encode_for_api(att.image, max_edge)
        path = dest / "screenshots" / f"{i:02d}{'.png' if media_type == 'image/png' else '.jpg'}"
        path.write_bytes(data)
        b64 = base64.b64encode(data).decode("ascii")
        inline = len(b64) <= budget
        if inline:
            budget -= len(b64)
        name = f"{label} {i}" + (f" ({att.name})" if att.path else "")
        prepared_images.append(PreparedImage(name, path, media_type, b64 if inline else None))

    prepared_files: list[PreparedFile] = []
    others = [a for a in atts if a.kind == "file"]
    if others:
        (dest / "files").mkdir(parents=True, exist_ok=True)
    for att in others:
        src = Path(att.path)
        copy = _unique(dest / "files" / src.name)
        shutil.copy2(src, copy)
        prepared_files.append(PreparedFile(src.name, copy, str(src), _read_text(copy)))
    return prepared_images, prepared_files


def fence_for(text: str) -> str:
    """A backtick fence longer than any backtick run inside `text`."""
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def content_blocks(imgs: list[PreparedImage], files: list[PreparedFile], text: str) -> list[dict]:
    """Labelled images first, then inlined files, then the instructions (best order for vision prompts)."""
    blocks: list[dict] = []
    for img in imgs:
        if img.data_b64 is None:
            continue
        blocks.append({"type": "text", "text": f"{img.label} - saved at {img.path}"})
        blocks.append({"type": "image", "source": {"type": "base64", "media_type": img.media_type, "data": img.data_b64}})
    for f in files:
        if f.text is None:
            continue
        fence = fence_for(f.text)
        lang = FENCE_LANGS.get(f.path.suffix.lower(), "")
        blocks.append({"type": "text", "text": f"Attached file `{f.name}` (copy saved at {f.path}):\n{fence}{lang}\n{f.text}\n{fence}"})
    blocks.append({"type": "text", "text": text})
    return blocks


def describe_inputs(imgs: list[PreparedImage], files: list[PreparedFile]) -> str:
    """Markdown list of what was attached and where it is saved (for the prompt text)."""
    lines = []
    for img in imgs:
        where = "attached above" if img.data_b64 else "NOT attached inline (size limit) - Read it from disk"
        lines.append(f"- {img.label}: {where}; saved at `{img.path}`")
    for f in files:
        how = "content attached above" if f.text is not None else "binary or large - Read it from disk"
        lines.append(f"- File `{f.name}` (from `{f.original}`): {how}; copy saved at `{f.path}`")
    return "\n".join(lines) if lines else "- (none)"
