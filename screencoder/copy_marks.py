"""Find the parts of a prompt that already exist, character for character, in the workspace files.

The developer's prompts for the external coding agent are typed in by hand. Whatever is already written in a
file of that agent's workspace - a signature, a line of the spec, an error message - can be copied from
there instead. `find_marks` says which stretches of a prompt those are and where each one is to be found.
"""
from __future__ import annotations

import bisect
import re
import time
from dataclasses import dataclass
from pathlib import Path

MIN_CODE_CHARS = 16  # shortest stretch worth copying when it looks like code (names, signatures, paths)
MIN_PROSE_CHARS = 28  # plain words are quicker to type than to find, so prose has to be longer to count
MAX_FILE_BYTES = 300_000
MAX_TOTAL_BYTES = 2_000_000
MAX_PLACES = 40  # how many occurrences of a line are looked at to find the one a block continues from
TIME_BUDGET_S = 1.5  # stop marking the rest of a prompt rather than freeze the window
_SEPARATOR = "\n\x00\n"  # between files in the joined text: nothing searched for can run across it
_CODE_LIKE = re.compile(r"[_(){}\[\]=<>/\\#@\"]|\d|\w\.\w")
_TOKEN = re.compile(r"[^\s`]+")  # backticks only wrap code in a prompt; they are not in the file
_LEADING = "(\"'"  # punctuation around a quoted stretch that belongs to the sentence, not to the stretch
_TRAILING = ".,;:!?)\"'"


@dataclass
class Mark:
    start: int  # offsets in the prompt text
    end: int
    file: str  # where the same text is in the workspace
    line: int  # 1-based
    last_line: int  # larger than `line` when the stretch runs over several lines

    @property
    def where(self) -> str:
        lines = f"{self.line}-{self.last_line}" if self.last_line > self.line else str(self.line)
        return f"{self.file}:{lines}"


class Corpus:
    """The text files of a workspace, searchable as one string."""

    def __init__(self, root: Path, files: list[str]):
        self.names: list[str] = []
        self.lines: list[list[str]] = []  # per file: its lines, stripped
        self._starts: list[int] = []  # where each file begins in the joined text
        self._line_starts: list[list[int]] = []  # per file: where each of its lines begins
        parts: list[str] = []
        offset, budget = 0, MAX_TOTAL_BYTES
        for rel in files:
            path = root / rel
            try:
                if path.stat().st_size > min(MAX_FILE_BYTES, budget):
                    continue
                raw = path.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:
                continue  # binary
            budget -= len(raw)
            text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
            self.names.append(rel.replace("\\", "/"))
            self.lines.append([line.strip() for line in text.split("\n")])
            self._starts.append(offset)
            self._line_starts.append([0] + [match.end() for match in re.finditer("\n", text)])
            parts.append(text)
            offset += len(text) + len(_SEPARATOR)
        self.text = _SEPARATOR.join(parts)

    def _place(self, position: int) -> tuple[int, int]:
        file = bisect.bisect_right(self._starts, position) - 1
        return file, bisect.bisect_right(self._line_starts[file], position - self._starts[file]) - 1

    def locate(self, needle: str) -> tuple[int, int] | None:
        """(file index, 0-based line) of the first place `needle` occurs."""
        position = self.text.find(needle)
        return self._place(position) if position >= 0 else None

    def locate_line(self, line: str, following: str | None) -> tuple[int, int] | None:
        """Where `line` is a whole line of a file - preferably one that is followed by `following` there."""
        first, position = None, -1
        for _ in range(MAX_PLACES):
            position = self.text.find(line, position + 1)
            if position < 0:
                break
            file, number = self._place(position)
            lines = self.lines[file]
            if lines[number] != line:
                continue  # only part of that line
            if first is None:
                first = (file, number)
            if following is None or (number + 1 < len(lines) and lines[number + 1] == following):
                return file, number
        return first


def _line_spans(line: str, corpus: Corpus) -> list[tuple[int, int, int, int]]:
    """Stretches of one prompt line that occur in the corpus: (start, end, file index, 0-based line)."""
    tokens = [(match.start(), match.end()) for match in _TOKEN.finditer(line)]
    spans, i = [], 0
    while i < len(tokens):
        start = tokens[i][0]
        while start < tokens[i][1] - 1 and line[start] in _LEADING:
            start += 1
        best = None  # (end, index of the last token) of the longest stretch from here that is in the corpus
        for j in range(i, len(tokens)):
            full = tokens[j][1]
            trimmed = full
            while trimmed > tokens[j][0] + 1 and line[trimmed - 1] in _TRAILING:
                trimmed -= 1
            shortest = line[start:trimmed]
            if len(shortest) < (MIN_CODE_CHARS if _CODE_LIKE.search(shortest) else MIN_PROSE_CHARS):
                continue  # not worth copying yet: take in the next word
            if shortest not in corpus.text:
                break  # and then nothing longer from here is in it either
            # Of the punctuation after it, keep as much as the file has too (a full stop, a closing bracket).
            end = next(e for e in range(full, trimmed - 1, -1) if line[start:e] in corpus.text)
            best = (end, j)
        if best is None:
            i += 1
            continue
        place = corpus.locate(line[start:best[0]])
        spans.append((start, best[0], place[0], place[1]))
        i = best[1] + 1
    return spans


def find_marks(prompt: str, corpus: Corpus) -> list[Mark]:
    """The stretches of `prompt` that can be copied from the workspace, in the order they appear."""
    marks: list[Mark] = []
    if not corpus.text:
        return marks
    deadline = time.monotonic() + TIME_BUDGET_S
    lines = prompt.split("\n")
    block: Mark | None = None  # a stretch of whole lines that may run on into the next prompt line
    place = (0, 0)  # the file line the last line of `block` is
    offset = 0
    for index, raw in enumerate(lines):
        line_start, offset = offset, offset + len(raw) + 1
        text = raw.strip()
        begin = line_start + len(raw) - len(raw.lstrip())
        if block is not None:
            file, number = place[0], place[1] + 1
            if number < len(corpus.lines[file]) and corpus.lines[file][number] == text:
                place = (file, number)
                if text:  # blank lines in between belong to the block but do not end it
                    block.end, block.last_line = begin + len(text), number + 1
                continue
            block = None
        if not text or time.monotonic() > deadline:
            continue
        for start, end, file, number in _line_spans(text, corpus):
            mark = Mark(begin + start, begin + end, corpus.names[file], number + 1, number + 1)
            marks.append(mark)
            if start == 0 and end == len(text):
                # The whole line: it may be the first of several lines quoted from the same place.
                following = lines[index + 1].strip() if index + 1 < len(lines) else None
                whole = corpus.locate_line(text, following)
                if whole is not None:
                    mark.file, mark.line, mark.last_line = corpus.names[whole[0]], whole[1] + 1, whole[1] + 1
                    block, place = mark, whole
    return marks
