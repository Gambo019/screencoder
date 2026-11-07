"""Application entry point."""
from __future__ import annotations

import sys

from PySide6.QtCore import QCoreApplication, QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from .config import APP_NAME

# Qt WebEngine - the offscreen page that draws the Mermaid diagrams (diagram_view.py) - needs this before the
# application object exists.
QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

STYLE = """
QGroupBox { font-weight: 600; border: 1px solid #30363d; border-radius: 6px; margin-top: 10px; padding-top: 6px; }
QGroupBox::title { subcontrol-origin: margin; left: 8px; padding: 0 4px; }
QPushButton { padding: 4px 10px; }
QPushButton#primary { background: #238636; color: white; border: 1px solid #2ea043; border-radius: 4px; font-weight: 600; }
QPushButton#primary:hover { background: #2ea043; }
QPushButton#primary:disabled { background: #30363d; color: #8b949e; border-color: #30363d; }
QFrame#approval { background: #2d2a1a; border: 1px solid #e3b341; border-radius: 4px; }
QSplitter::handle { background: #21262d; }
QSplitter::handle:hover { background: #58a6ff; }
QSplitter#mainSplit::handle { background: #30363d; border-left: 3px solid #161b22; border-right: 3px solid #161b22; }
QSplitter#mainSplit::handle:hover { background: #58a6ff; }
QTextBrowser, QPlainTextEdit, QListWidget { background: #0d1117; border: 1px solid #30363d; }
"""


def _dark_palette() -> QPalette:
    p = QPalette()
    base, window, text, mid = QColor("#0d1117"), QColor("#161b22"), QColor("#e6edf3"), QColor("#8b949e")
    p.setColor(QPalette.ColorRole.Window, window)
    p.setColor(QPalette.ColorRole.WindowText, text)
    p.setColor(QPalette.ColorRole.Base, base)
    p.setColor(QPalette.ColorRole.AlternateBase, window)
    p.setColor(QPalette.ColorRole.Text, text)
    p.setColor(QPalette.ColorRole.Button, QColor("#21262d"))
    p.setColor(QPalette.ColorRole.ButtonText, text)
    p.setColor(QPalette.ColorRole.ToolTipBase, window)
    p.setColor(QPalette.ColorRole.ToolTipText, text)
    p.setColor(QPalette.ColorRole.PlaceholderText, mid)
    p.setColor(QPalette.ColorRole.Highlight, QColor("#1f6feb"))
    p.setColor(QPalette.ColorRole.HighlightedText, QColor("white"))
    p.setColor(QPalette.ColorRole.Link, QColor("#58a6ff"))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText, QPalette.ColorRole.WindowText):
        p.setColor(QPalette.ColorGroup.Disabled, role, mid)
    return p


class _QuitWhenClosed(QObject):
    """Ends the app when its main window has been closed, whatever state the window was in.

    Qt normally does this itself, but only for windows it counts as "main" ones, and a window that has been a
    tool window (stealth mode) is not. An app that keeps running with no window keeps its ports, its remote
    link and its hotkeys: closed has to mean ended."""

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 (Qt override)
        if event.type() == QEvent.Type.Close:
            QTimer.singleShot(0, lambda: None if obj.isVisible() else QApplication.instance().quit())
        return False


def main() -> int:
    if sys.platform == "win32":
        import ctypes

        # Own taskbar identity instead of python.exe's.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("ScreenCoder.App")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    app.setPalette(_dark_palette())
    app.setStyleSheet(STYLE)

    # One copy per user: if ScreenCoder is already running, bring that window to the front and exit instead of
    # opening a second window. Checked before the (heavy) main window is built so a second launch bows out fast.
    from .single_instance import SingleInstance

    instance = SingleInstance(app)
    if not instance.claim():
        return 0

    from .main_window import MainWindow

    window = MainWindow(app)
    window.show()
    instance.bind(window)
    closer = _QuitWhenClosed(app)
    window.installEventFilter(closer)

    from .opacity import install_opacity
    from .remote import install_remote
    from .stealth import install_stealth

    from .see_through import install_see_through

    install_stealth(app, window)
    install_opacity(app, window)
    install_see_through(app, window)  # after stealth: its frameless flag goes through stealth's window flags
    install_remote(app, window)
    return app.exec()
