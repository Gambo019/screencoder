"""Settings dialog: Claude CLI location, models, effort levels, permissions."""
from __future__ import annotations

import subprocess
import sys

from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from .config import (
    CODING_MODELS, EFFORT_LEVELS, PERMISSION_MODES, SIDE_MODELS, TEXT_SCALES, Settings, find_claude_cli,
)
from .listener import DEFAULT_SPEECH_MODEL, SPEECH_MODELS
from . import tunnel
from .remote import DEFAULT_PORT, REACH_INTERNET, REACH_LAN, local_addresses, new_key, remote_link


def cli_version(path: str) -> str:
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        return out.stdout.strip() or out.stderr.strip() or "unknown version"
    except (OSError, subprocess.SubprocessError) as exc:
        return f"not runnable ({exc.__class__.__name__})"


def _combo(values: list[str], current: str, editable: bool = False) -> QComboBox:
    combo = QComboBox()
    combo.setEditable(editable)
    combo.addItems(values)
    if current not in values:
        combo.addItem(current)
    combo.setCurrentText(current)
    return combo


class SettingsDialog(QDialog):
    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(640)
        self.settings = settings

        self.cli_edit = QLineEdit(settings.claude_path)
        self.cli_edit.setPlaceholderText(find_claude_cli() or "claude executable not found - browse to it")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_cli)
        test = QPushButton("Test")
        test.clicked.connect(self._test_cli)
        cli_row = QHBoxLayout()
        cli_row.addWidget(self.cli_edit, 1)
        cli_row.addWidget(browse)
        cli_row.addWidget(test)
        self.cli_status = QLabel("Leave empty to auto-detect (PATH, ~/.local/bin, or the VS Code extension's bundled CLI).")
        self.cli_status.setWordWrap(True)
        self.cli_status.setStyleSheet("color:#9aa4b2;")

        self.coding_model = _combo(CODING_MODELS, settings.coding_model, editable=True)
        self.coding_effort = _combo(EFFORT_LEVELS, settings.coding_effort)
        self.side_model = _combo(SIDE_MODELS, settings.side_model, editable=True)
        self.side_effort = _combo(EFFORT_LEVELS, settings.side_effort)
        self.permission = QComboBox()
        for key, label in PERMISSION_MODES.items():
            self.permission.addItem(f"{key} - {label}", key)
        self.permission.setCurrentIndex(max(0, list(PERMISSION_MODES).index(settings.permission_mode))
                                        if settings.permission_mode in PERMISSION_MODES else 0)
        self.show_thinking = QCheckBox("Show the coding agent's thinking summaries in the activity log")
        self.show_thinking.setChecked(settings.show_thinking)
        self.hotkeys = QCheckBox("Global screenshot hotkeys (Ctrl+Alt+Shift+S region, Ctrl+Alt+Shift+F full screen)")
        self.hotkeys.setChecked(settings.hotkeys_enabled)
        self.stealth = QCheckBox(
            "Stealth mode: visible to you, hidden from screen share, screenshots, taskbar && Alt+Tab "
            "(toggle anytime with Ctrl+Alt+Shift+H)"
        )
        self.stealth.setChecked(getattr(settings, "stealth", False))
        self.always_on_top = QCheckBox(
            "Always on top: keep the window above all other apps (toggle anytime with Ctrl+Alt+Shift+T)"
        )
        self.always_on_top.setChecked(getattr(settings, "always_on_top", False))
        self.runs_dir = QLineEdit(settings.runs_dir)
        self.text_scale = QComboBox()
        for scale in TEXT_SCALES:
            self.text_scale.addItem(f"{round(scale * 100)}%", scale)
        nearest = min(range(len(TEXT_SCALES)), key=lambda i: abs(TEXT_SCALES[i] - settings.reading_scale))
        self.text_scale.setCurrentIndex(nearest)
        self.listen_audio = QCheckBox("Listen to PC audio and show a live transcript (Ask answers the new speech)")
        self.listen_audio.setChecked(settings.listen_audio)
        self.speech_model = QComboBox()
        for key, model in SPEECH_MODELS.items():
            suffix = "" if model.installed() else "  - downloads on first use"
            self.speech_model.addItem(model.label + suffix, key)
        keys = list(SPEECH_MODELS)
        current = settings.speech_model if settings.speech_model in SPEECH_MODELS else DEFAULT_SPEECH_MODEL
        self.speech_model.setCurrentIndex(keys.index(current))

        # Remote control: the link is known before it is switched on, so it can be passed on first.
        self._remote_key = getattr(settings, "remote_key", "") or new_key()
        self.remote = QCheckBox("Remote control: see and use this window from a browser on another machine")
        self.remote.setChecked(getattr(settings, "remote_enabled", False))
        self.remote_reach = QComboBox()  # where the other machine is
        self.remote_reach.addItem("from the internet (anywhere, through Cloudflare)", REACH_INTERNET)
        self.remote_reach.addItem("from this network only (same Wi-Fi/LAN)", REACH_LAN)
        self.remote_reach.setCurrentIndex(max(0, self.remote_reach.findData(getattr(settings, "remote_reach", REACH_LAN))))
        self.remote_port = QSpinBox()
        self.remote_port.setRange(1024, 65535)
        self.remote_port.setValue(int(getattr(settings, "remote_port", DEFAULT_PORT) or DEFAULT_PORT))
        self.remote_host = QComboBox()  # which of this machine's addresses the link uses
        self.remote_host.addItems(local_addresses())
        self.remote_host.setToolTip("This machine's address as the other machine reaches it: the first is the local "
                                    "network, others belong to a VPN if one is running.")
        self.remote_link = QLineEdit()
        self.remote_link.setReadOnly(True)
        self.copy_link = QPushButton("Copy link")
        self.copy_link.clicked.connect(lambda: QApplication.clipboard().setText(self.remote_link.text()))
        fresh_key = QPushButton("New key")
        fresh_key.setToolTip("Make a new link. Whoever has the old one is shut out once you save.")
        fresh_key.clicked.connect(self._new_remote_key)
        reach_row = QHBoxLayout()
        reach_row.addWidget(self.remote_reach, 1)
        self.port_label = QLabel("Port")
        reach_row.addWidget(self.port_label)
        reach_row.addWidget(self.remote_port)
        reach_row.addWidget(self.remote_host)
        remote_row = QHBoxLayout()
        remote_row.addWidget(self.remote_link, 1)
        remote_row.addWidget(self.copy_link)
        remote_row.addWidget(fresh_key)
        self.remote_status = QLabel()
        self.remote_status.setWordWrap(True)
        self.remote_status.setStyleSheet("color:#9aa4b2;")
        self.remote_port.valueChanged.connect(self._show_remote_link)
        self.remote_host.currentTextChanged.connect(self._show_remote_link)
        self.remote_reach.currentIndexChanged.connect(self._show_remote_link)
        controller = getattr(QApplication.instance(), "_remote_controller", None)
        if controller is not None:  # the internet link comes up a moment after it is switched on: follow it
            controller.changed.connect(self._show_remote_link)
        self._show_remote_link()

        form = QFormLayout()
        form.addRow("Claude Code CLI", cli_row)
        form.addRow("", self.cli_status)
        form.addRow("Coding model", self.coding_model)
        form.addRow("Coding effort", self.coding_effort)
        form.addRow("Quick-answer model", self.side_model)
        form.addRow("Quick-answer effort", self.side_effort)
        form.addRow("Coding permissions", self.permission)
        form.addRow("Runs folder", self.runs_dir)
        form.addRow("Reading text size", self.text_scale)
        form.addRow("Speech model", self.speech_model)
        form.addRow("", self.listen_audio)
        form.addRow("", self.show_thinking)
        form.addRow("", self.hotkeys)
        form.addRow("", self.stealth)
        form.addRow("", self.always_on_top)
        form.addRow("", self.remote)
        form.addRow("Reach it", reach_row)
        form.addRow("Remote link", remote_row)
        form.addRow("", self.remote_status)
        note = QLabel(
            "The coding agent runs headless, so nobody can approve prompts: with <b>acceptEdits</b> it can write "
            "files but any shell command is denied (it verifies by reasoning). <b>bypassPermissions</b> lets it run "
            "your code and tests - only use it with a workspace you don't mind it changing. Effort is not sent for "
            "Haiku models, which don't support it."
        )
        note.setWordWrap(True)
        note.setStyleSheet("color:#9aa4b2;")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def _browse_cli(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Locate the claude executable")
        if path:
            self.cli_edit.setText(path)

    def _test_cli(self) -> None:
        path = self.cli_edit.text().strip() or find_claude_cli()
        self.cli_status.setText(f"{path}: {cli_version(path)}" if path else "No claude executable found.")

    def _new_remote_key(self) -> None:
        self._remote_key = new_key()
        self._show_remote_link()

    def _show_remote_link(self, *_args) -> None:
        controller = getattr(QApplication.instance(), "_remote_controller", None)
        now = controller.status() if controller is not None else "Off."
        internet = self.remote_reach.currentData() == REACH_INTERNET
        for widget in (self.port_label, self.remote_port, self.remote_host):
            widget.setVisible(not internet)  # the internet link needs no address or port of this machine
        if internet:
            # The address is Cloudflare's to give and new every time: there is one only while the link is up.
            link = controller.link() if controller is not None and controller.internet else ""
            self.remote_link.setText(link)
            self.remote_link.setPlaceholderText("appears here a few seconds after you save (and under "
                                                "\"🔗 Remote\" at the bottom of the window)")
            self.copy_link.setEnabled(bool(link))
            self.remote_status.setText(
                f"{now} Works from anywhere, nothing to set up on the router. The link is new every time the app "
                "starts. It is encrypted on the way and passes through Cloudflare's servers; anyone who has the "
                "link can use this app while it is on."
            )
        else:
            self.remote_link.setText(remote_link(self.remote_port.value(), self._remote_key,
                                                 self.remote_host.currentText() or None))
            self.copy_link.setEnabled(True)
            self.remote_status.setText(
                f"{now} Only for a machine on the same network as this one (an address like 192.168.x.x does not "
                "exist on the internet). Anyone who has the link can use this app while this is on, and the "
                "connection is not encrypted: keep it to a network you trust."
            )
        self.remote_link.setCursorPosition(0)

    def apply(self) -> None:
        s = self.settings
        s.claude_path = self.cli_edit.text().strip()
        s.coding_model = self.coding_model.currentText().strip() or s.coding_model
        s.coding_effort = self.coding_effort.currentText()
        s.side_model = self.side_model.currentText().strip() or s.side_model
        s.side_effort = self.side_effort.currentText()
        s.permission_mode = self.permission.currentData()
        s.show_thinking = self.show_thinking.isChecked()
        s.hotkeys_enabled = self.hotkeys.isChecked()
        s.stealth = self.stealth.isChecked()
        s.always_on_top = self.always_on_top.isChecked()
        s.reading_scale = float(self.text_scale.currentData())
        s.listen_audio = self.listen_audio.isChecked()
        s.speech_model = self.speech_model.currentData()
        s.runs_dir = self.runs_dir.text().strip() or s.runs_dir
        s.remote_enabled = self.remote.isChecked()
        s.remote_reach = self.remote_reach.currentData()
        s.remote_port = self.remote_port.value()
        s.remote_key = self._remote_key
        download = False
        if s.remote_enabled and s.remote_reach == REACH_INTERNET and not tunnel.installed():
            # The internet link runs Cloudflare's own program, which is not part of this app: ask before fetching.
            answer = QMessageBox.question(
                self.parentWidget(), "Remote control",
                f"To be reachable from the internet, ScreenCoder uses Cloudflare's free tunnel program "
                f"(cloudflared).\n\nIt is downloaded once, {tunnel.DOWNLOAD_MB} MB, from Cloudflare's releases on "
                f"GitHub, and kept in\n{tunnel.PROGRAM.parent}\n\nWhile remote control is on, what the remote page "
                "shows and sends travels through Cloudflare's servers (encrypted on the way).\n\nDownload it now?")
            download = answer == QMessageBox.StandardButton.Yes
            if not download:
                s.remote_enabled = False
        s.save()
        # Switch remote control on or off right away (the controller is installed by app.py).
        remote = getattr(QApplication.instance(), "_remote_controller", None)
        if remote is not None and not remote.apply_settings(s, download):
            QMessageBox.warning(self.parentWidget(), "Remote control",
                                f"Remote control could not start on port {s.remote_port}:\n{remote.error}\n\n"
                                "Pick another port in Settings.")
        # Apply stealth immediately if the controller is running (installed by app.py).
        controller = getattr(QApplication.instance(), "_stealth_controller", None)
        if controller is not None:
            controller.set_stealth(s.stealth)
            controller.set_on_top(s.always_on_top)
