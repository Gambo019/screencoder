"""Read-only code viewer with line numbers and VS Code "Dark+"-style syntax colours (via Pygments)."""
from __future__ import annotations

from pygments.lexers import get_lexer_for_filename
from pygments.token import Comment, Keyword, Name, Number, Operator, String, Token
from pygments.util import ClassNotFound
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QPainter, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QPlainTextEdit, QWidget

EDITOR_BG, EDITOR_FG, GUTTER_FG = "#1e1e1e", "#d4d4d4", "#858585"
CHANGE_BAR = "#2ea043"  # gutter marker for lines the agent added or edited
DIM_STRENGTH = 0.42  # how much of their colour untouched lines keep (the rest fades into the background)
MAX_HIGHLIGHT_CHARS = 400_000  # larger files are shown uncoloured

# VS Code Dark+ palette
BLUE, PURPLE, TEAL, YELLOW, LIGHT_BLUE = "#569cd6", "#c586c0", "#4ec9b0", "#dcdcaa", "#9cdcfe"
ORANGE, GREEN, LIGHT_GREEN, CONST_BLUE, ESCAPE, REGEX = "#ce9178", "#6a9955", "#b5cea8", "#4fc1ff", "#d7ba7d", "#d16969"

# Most specific token types first is not needed: _color() walks up from the token to its parents.
_TOKEN_COLORS = {
    Comment: GREEN,
    Comment.Preproc: PURPLE,
    Comment.PreprocFile: ORANGE,
    String: ORANGE,
    String.Escape: ESCAPE,
    String.Interpol: BLUE,
    String.Regex: REGEX,
    Number: LIGHT_GREEN,
    Operator.Word: BLUE,
    Keyword: BLUE,
    Keyword.Namespace: PURPLE,
    Keyword.Type: BLUE,
    Name: LIGHT_BLUE,
    Name.Function: YELLOW,
    Name.Function.Magic: YELLOW,
    Name.Class: TEAL,
    Name.Namespace: TEAL,
    Name.Exception: TEAL,
    Name.Decorator: YELLOW,
    Name.Builtin: YELLOW,
    Name.Builtin.Pseudo: BLUE,
    Name.Constant: CONST_BLUE,
    Name.Tag: BLUE,
    Name.Label: LIGHT_BLUE,
}
# VS Code colours control-flow keywords purple and declaration keywords blue; Pygments does not separate them.
_CONTROL_KEYWORDS = frozenset(
    "if elif else for foreach while do switch case default break continue return yield try except catch finally "
    "throw throws raise with as import from export await async goto pass assert del defer go select fallthrough "
    "range using when match".split()
)
_BUILTIN_TYPES = frozenset(
    "int str float bool list dict set tuple bytes object type frozenset complex bytearray string number boolean "
    "any void never unknown".split()
)


def _color(ttype, value: str, called: bool) -> str:
    if ttype is Keyword and value in _CONTROL_KEYWORDS:
        return PURPLE
    if ttype in Name.Builtin and ttype is not Name.Builtin.Pseudo and value in _BUILTIN_TYPES:
        return TEAL
    if called and ttype in Name and ttype not in (Name.Class, Name.Builtin.Pseudo, Name.Decorator):
        return YELLOW  # an identifier directly followed by "(" is a call
    while ttype is not Token:
        color = _TOKEN_COLORS.get(ttype)
        if color:
            return color
        ttype = ttype.parent
    return EDITOR_FG


def _utf16_len(text: str) -> int:
    """Qt counts columns in UTF-16 code units; Python in code points (they differ for emoji etc.)."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def _dimmed(color: str) -> str:
    """The colour faded towards the editor background (for lines the agent did not touch)."""
    fg, bg = QColor(color), QColor(EDITOR_BG)
    mix = lambda a, b: round(a * DIM_STRENGTH + b * (1 - DIM_STRENGTH))  # noqa: E731
    return QColor(mix(fg.red(), bg.red()), mix(fg.green(), bg.green()), mix(fg.blue(), bg.blue())).name()


class _Highlighter(QSyntaxHighlighter):
    """Colours come from one Pygments pass over the whole file, so multi-line strings/comments are right."""

    def __init__(self, document):
        super().__init__(document)
        self._lines: list[list[tuple[int, int, QTextCharFormat]]] = []
        self._dim: set[int] = set()
        self._formats: dict[str, QTextCharFormat] = {}

    def _format(self, color: str) -> QTextCharFormat:
        fmt = self._formats.get(color)
        if fmt is None:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self._formats[color] = fmt
        return fmt

    def prepare(self, text: str, filename: str, dim_lines: set[int]) -> None:
        """Tokenise `text` (newlines already normalised to \\n); call before putting it in the document.

        Lines whose 0-based number is in `dim_lines` keep their syntax colours, only faded.
        """
        self._lines = [[]]
        self._dim = dim_lines
        if not text or len(text) > MAX_HIGHLIGHT_CHARS:
            return
        try:
            lexer = get_lexer_for_filename(filename or "file.txt", stripnl=False, ensurenl=False)
        except ClassNotFound:
            return
        tokens = [(ttype, value) for _pos, ttype, value in lexer.get_tokens_unprocessed(text)]
        column = 0
        for i, (ttype, value) in enumerate(tokens):
            following = next((v for _t, v in tokens[i + 1 : i + 3] if v.strip()), "")
            color = _color(ttype, value, following.startswith("("))
            for n, part in enumerate(value.split("\n")):
                if n:
                    self._lines.append([])
                    column = 0
                if part:
                    width = _utf16_len(part)
                    if color != EDITOR_FG:
                        shade = _dimmed(color) if len(self._lines) - 1 in dim_lines else color
                        self._lines[-1].append((column, width, self._format(shade)))
                    column += width

    def highlightBlock(self, text: str) -> None:
        number = self.currentBlock().blockNumber()
        if number in self._dim:  # plain text of an untouched line is faded too; token colours go on top
            self.setFormat(0, len(text), self._format(_dimmed(EDITOR_FG)))
        if number < len(self._lines):
            for start, length, fmt in self._lines[number]:
                self.setFormat(start, length, fmt)


class _Gutter(QWidget):
    def __init__(self, editor: "CodeView"):
        super().__init__(editor)
        self._editor = editor

    def sizeHint(self) -> QSize:
        return QSize(self._editor.gutter_width(), 0)

    def paintEvent(self, event) -> None:
        self._editor.paint_gutter(event)


class CodeView(QPlainTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        font = QFontDatabase.systemFont(QFontDatabase.SystemFont.FixedFont)
        for family in ("Cascadia Code", "Consolas"):
            if family in QFontDatabase.families():
                font = QFont(family)
                break
        font.setPointSize(10)
        self.setFont(font)
        self.setTabStopDistance(4 * self.fontMetrics().horizontalAdvance(" "))
        self.setStyleSheet(
            f"QPlainTextEdit {{ background: {EDITOR_BG}; color: {EDITOR_FG}; border: 1px solid #30363d; "
            "selection-background-color: #264f78; }"
        )
        self._unchanged: set[int] = set()
        self._mark_changes = False
        self._gutter = _Gutter(self)
        self._highlighter = _Highlighter(self.document())
        self.blockCountChanged.connect(self._update_margins)
        self.updateRequest.connect(self._on_update_request)
        self._update_margins()

    def set_code(self, text: str, filename: str = "", unchanged: set[int] | None = None) -> None:
        """Show `text`. `unchanged` = 0-based numbers of lines that already existed and were not edited:
        they are faded, while added/edited lines keep full brightness and get a bar in the gutter."""
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        self._unchanged = set(unchanged or ())
        self._mark_changes = unchanged is not None  # no bars when the file has no "before" to compare with
        self._highlighter.prepare(text, filename, self._unchanged)
        self.setPlainText(text)
        self._gutter.update()

    # ----------------------------------------------------------------- line-number gutter
    def gutter_width(self) -> int:
        digits = max(3, len(str(self.blockCount())))
        return 18 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_margins(self, *_args) -> None:
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _on_update_request(self, rect: QRect, dy: int) -> None:
        if dy:
            self._gutter.scroll(0, dy)
        else:
            self._gutter.update(0, rect.y(), self._gutter.width(), rect.height())

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        area = self.contentsRect()
        self._gutter.setGeometry(QRect(area.left(), area.top(), self.gutter_width(), area.height()))

    def paint_gutter(self, event) -> None:
        painter = QPainter(self._gutter)
        painter.fillRect(event.rect(), QColor(EDITOR_BG))
        painter.setPen(QColor(GUTTER_FG))
        painter.setFont(self.font())
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        line_height = self.fontMetrics().height()
        normal, faded, bar = QColor(GUTTER_FG), QColor(_dimmed(GUTTER_FG)), QColor(CHANGE_BAR)
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                number = block.blockNumber()
                untouched = number in self._unchanged
                if self._mark_changes and not untouched:
                    painter.fillRect(self._gutter.width() - 5, top, 3, bottom - top, bar)
                painter.setPen(faded if untouched else normal)
                painter.drawText(0, top, self._gutter.width() - 10, line_height,
                                 Qt.AlignmentFlag.AlignRight, str(number + 1))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
