"""VS Code-style explorer for the workspace. File names are coloured by who touched them."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QFileInfo, Qt, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QFileIconProvider, QHeaderView, QTreeWidget, QTreeWidgetItem

# Status of a file relative to the problem the user presented.
ORIGINAL = "original"  # already existed in the problem, untouched by the agent
MODIFIED = "modified"  # already existed, edited by the agent
NEW = "new"  # created by the agent

STATUS_COLORS = {ORIGINAL: "#cccccc", MODIFIED: "#e2c08d", NEW: "#73c991"}  # VS Code's git decoration colours
STATUS_BADGES = {ORIGINAL: "", MODIFIED: "M", NEW: "U"}
STATUS_TIPS = {
    ORIGINAL: "Original file from the problem - not changed by the agent",
    MODIFIED: "Original file from the problem - edited by the agent",
    NEW: "New file created by the agent",
}
LEGEND_HTML = (
    f"<span style='color:{STATUS_COLORS[ORIGINAL]}'>● original</span>&nbsp;&nbsp; "
    f"<span style='color:{STATUS_COLORS[MODIFIED]}'>● edited&nbsp;(M)</span>&nbsp;&nbsp; "
    f"<span style='color:{STATUS_COLORS[NEW]}'>● new&nbsp;(U)</span>"
)
_PATH_ROLE = Qt.ItemDataRole.UserRole
_STYLE = """
QTreeWidget { background: #252526; border: 1px solid #30363d; outline: 0; }
QTreeWidget::item { padding: 2px 0; }
QTreeWidget::item:hover { background: #2a2d2e; }
QTreeWidget::item:selected { background: #37373d; }
"""


def _explorer_order(rel: str) -> list[tuple[bool, str]]:
    """Sort like an explorer: at every level folders come before files, names case-insensitively."""
    parts = Path(rel).parts
    return [(i == len(parts) - 1, part.lower()) for i, part in enumerate(parts)]


class FileTree(QTreeWidget):
    file_selected = Signal(str)  # path relative to the root, with the OS separator

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setColumnCount(2)
        self.setHeaderHidden(True)
        self.setIndentation(14)
        self.setUniformRowHeights(True)
        self.setStyleSheet(_STYLE)
        self.header().setStretchLastSection(False)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        self.setColumnWidth(1, 26)
        self._icons = QFileIconProvider()
        self._icon_cache: dict[str, object] = {}  # per file extension: shell icon lookups are slow
        self._root: Path | None = None
        self.currentItemChanged.connect(self._on_current_changed)

    def _file_icon(self, path: Path):
        key = path.suffix.lower() or path.name.lower()
        if key not in self._icon_cache:
            self._icon_cache[key] = self._icons.icon(QFileInfo(str(path)))
        return self._icon_cache[key]

    def current_file(self) -> str | None:
        item = self.currentItem()
        return item.data(0, _PATH_ROLE) if item is not None and item.childCount() == 0 else None

    def populate(self, root: Path, files: list[str], statuses: dict[str, str]) -> None:
        """Rebuild the tree for `files` (paths relative to `root`), keeping selection and collapsed folders."""
        same_root = root == self._root
        self._root = root
        selected = self.current_file() if same_root else None
        # Folders start expanded; ones the user collapsed stay collapsed across refreshes.
        collapsed = {item.data(0, _PATH_ROLE) for item in self._folders() if not item.isExpanded()} if same_root else set()
        self.blockSignals(True)
        self.clear()
        folders: dict[str, QTreeWidgetItem] = {}
        folder_status: dict[str, str] = {}
        select_item = None
        for rel in sorted(files, key=_explorer_order):
            parts = Path(rel).parts
            parent = self.invisibleRootItem()
            status = statuses.get(rel, NEW)
            for depth in range(len(parts) - 1):
                key = str(Path(*parts[: depth + 1]))
                if key not in folders:
                    item = QTreeWidgetItem(parent, [parts[depth], ""])
                    item.setData(0, _PATH_ROLE, key)
                    item.setIcon(0, self._icons.icon(QFileIconProvider.IconType.Folder))
                    folders[key] = item
                parent = folders[key]
                # A folder shows the "strongest" status below it, like VS Code: edited beats new beats original.
                best = folder_status.get(key, ORIGINAL)
                if status == MODIFIED or (status == NEW and best == ORIGINAL):
                    folder_status[key] = status
            item = QTreeWidgetItem(parent, [parts[-1], STATUS_BADGES[status]])
            item.setData(0, _PATH_ROLE, rel)
            item.setIcon(0, self._file_icon(root / rel))
            self._paint(item, status)
            item.setToolTip(0, f"{rel}\n{STATUS_TIPS[status]}")
            if rel == selected:
                select_item = item
        for key, item in folders.items():
            status = folder_status.get(key, ORIGINAL)
            self._paint(item, status)
            item.setText(1, "●" if status != ORIGINAL else "")
            item.setExpanded(key not in collapsed)
        if select_item is not None:
            self.setCurrentItem(select_item)
        self.blockSignals(False)

    def select(self, rel: str) -> None:
        for item in self._all_items():
            if item.childCount() == 0 and item.data(0, _PATH_ROLE) == rel:
                self.setCurrentItem(item)
                return

    @staticmethod
    def _paint(item: QTreeWidgetItem, status: str) -> None:
        brush = QBrush(QColor(STATUS_COLORS[status]))
        item.setForeground(0, brush)
        item.setForeground(1, brush)
        item.setTextAlignment(1, Qt.AlignmentFlag.AlignCenter)

    def _all_items(self):
        stack = [self.invisibleRootItem().child(i) for i in range(self.invisibleRootItem().childCount())]
        while stack:
            item = stack.pop()
            yield item
            stack.extend(item.child(i) for i in range(item.childCount()))

    def _folders(self):
        return [item for item in self._all_items() if item.childCount() > 0]

    def _on_current_changed(self, current, _previous) -> None:
        if current is not None and current.childCount() == 0:
            self.file_selected.emit(current.data(0, _PATH_ROLE))
