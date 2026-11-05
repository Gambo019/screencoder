"""Settings dialog: Claude CLI location, models, effort levels, permissions."""
from __future__ import annotations

import subprocess
import sys

from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget,
)

from .config import (
    CODING_MODELS, EFFORT_LEVELS, PERMISSION_MODES, SIDE_MODELS, TEXT_SCALES, Settings, find_claude_cli,
)
from .listener import DEFAULT_SPEECH_MODEL, SPEECH_MODELS
from . import llm, tunnel
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

        # A name for the AI developer (coding agent + quick answers), so the interviewer can address it by name.
        self.dev_name = QLineEdit(getattr(settings, "dev_name", ""))
        self.dev_name.setPlaceholderText("e.g. Alex Carter — what the interviewer calls you (optional)")
        self.dev_name.setToolTip("A name for the AI developer: the coding agent and the quick answers. The person "
                                 "talking to you may call you by it. Leave empty for no name.")

        # API keys for the OpenAI and Anthropic providers (hidden as you type; "Test" makes one tiny call).
        self.openai_key = QLineEdit(settings.openai_key)
        self.openai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.openai_key.setPlaceholderText("sk-...  (only needed if a part uses OpenAI)")
        self.anthropic_key = QLineEdit(settings.anthropic_key)
        self.anthropic_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.anthropic_key.setPlaceholderText("sk-ant-...  (only needed if a part uses Anthropic, instead of Claude Code)")
        self.openai_models_edit = QLineEdit(", ".join(settings.openai_models))
        self.openai_models_edit.setPlaceholderText("gpt-5, gpt-5-mini, gpt-4.1, ...")
        self.openai_models_edit.setToolTip("The OpenAI models offered in the menus below. Comma-separated; edit freely.")
        self.key_status = QLabel()
        self.key_status.setWordWrap(True)
        self.key_status.setStyleSheet("color:#9aa4b2;")

        # Each of the three parts picks a provider and a model. The model menu follows the provider.
        self.coding_provider, self.coding_model = self._provider_pair(settings.coding_provider, settings.coding_model)
        self.side_provider, self.side_model = self._provider_pair(settings.side_provider, settings.side_model)
        self.openai_models_edit.textChanged.connect(self._refresh_models)
        self.coding_note = QLabel()
        self.coding_note.setWordWrap(True)
        self.coding_note.setStyleSheet("color:#e3b341;")
        self.coding_provider.currentIndexChanged.connect(self._update_coding_note)
        self.side_provider.currentIndexChanged.connect(self._update_coding_note)
        self._update_coding_note()
        self.coding_effort = _combo(EFFORT_LEVELS, settings.coding_effort)
        self.coding_effort.setToolTip("Vibe-Coding only. In a coding task the agent judges the effort itself: it "
                                      "takes the task in and gives its understanding and approach at medium, and "
                                      "solves each piece of work at the level it judges that work needs.")
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
        # Keep these two in step with the real state: the global hotkeys (Ctrl+Alt+Shift+H/T) can flip them
        # while this dialog is open, and the checkbox should show what is actually in effect.
        self._stealth_controller = getattr(QApplication.instance(), "_stealth_controller", None)
        if self._stealth_controller is not None:
            self._sync_toggles()
            self._stealth_controller.changed.connect(self._sync_toggles)
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
        form.addRow("Developer name", self.dev_name)
        form.addRow(self._heading("AI providers"))
        form.addRow("OpenAI API key", self._key_row(self.openai_key, llm.OPENAI))
        form.addRow("Anthropic API key", self._key_row(self.anthropic_key, llm.ANTHROPIC))
        form.addRow("OpenAI models", self.openai_models_edit)
        form.addRow("", self.key_status)
        form.addRow("Coding", self._pair_row(self.coding_provider, self.coding_model))
        form.addRow("Vibe-Coding effort", self.coding_effort)
        form.addRow("Quick answers (Ask)", self._pair_row(self.side_provider, self.side_model))
        form.addRow("Quick-answer effort", self.side_effort)
        form.addRow("", self.coding_note)
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
        # The dialog has grown; put everything but the buttons in a scroll area so it fits any screen.
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.addLayout(form)
        content_layout.addWidget(note)
        content_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(content)
        layout = QVBoxLayout(self)
        layout.addWidget(scroll, 1)
        layout.addWidget(buttons)
        self.resize(720, 720)

    def _provider_pair(self, current_provider: str, current_model: str):
        """A provider menu paired with a model menu that follows it. Used for each of the three parts."""
        provider = QComboBox()
        for name in llm.PROVIDERS:
            provider.addItem(llm.PROVIDER_LABELS[name], name)
        provider.setCurrentIndex(llm.PROVIDERS.index(current_provider) if current_provider in llm.PROVIDERS else 0)
        model = QComboBox()
        model.setEditable(False)  # only the listed models; edit the OpenAI list via the field above
        if not hasattr(self, "_model_combos"):
            self._model_combos = []
        self._model_combos.append((provider, model))
        self._fill_models(provider, model, current_model)
        provider.currentIndexChanged.connect(lambda _i, p=provider, m=model: self._fill_models(p, m, ""))
        return provider, model

    def _models_for(self, provider: str) -> list:
        if provider == llm.OPENAI:
            names = [m.strip() for m in self.openai_models_edit.text().split(",") if m.strip()]
            return names or llm.OPENAI_MODELS
        return llm.models_for(provider)

    def _fill_models(self, provider_combo: QComboBox, model_combo: QComboBox, prefer: str) -> None:
        provider = provider_combo.currentData()
        keep = prefer or model_combo.currentText()
        model_combo.blockSignals(True)
        model_combo.clear()
        model_combo.addItems(self._models_for(provider))
        model_combo.setCurrentText(keep or llm.default_model(provider, self.settings))
        model_combo.blockSignals(False)

    def _refresh_models(self) -> None:
        for provider_combo, model_combo in getattr(self, "_model_combos", []):
            if provider_combo.currentData() == llm.OPENAI:
                self._fill_models(provider_combo, model_combo, model_combo.currentText())

    def _update_coding_note(self, *_args) -> None:
        other = (self.coding_provider.currentData() != llm.CLAUDE_CODE
                 or self.side_provider.currentData() != llm.CLAUDE_CODE)
        self.coding_note.setVisible(other)
        if other:
            self.coding_note.setText(
                "With OpenAI or Anthropic, the part runs as an agent over the API with the same file tools "
                "(Read/Write/Edit/Glob/Grep) in your workspace. Running commands and tests (Bash) needs the "
                "bypassPermissions setting below, as with Claude Code. Quality depends on the model you choose.")

    def _key_row(self, edit: QLineEdit, provider: str) -> QHBoxLayout:
        show = QPushButton("Show")
        show.setCheckable(True)
        show.toggled.connect(lambda on: edit.setEchoMode(
            QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password))
        test = QPushButton("Test")
        test.clicked.connect(lambda: self._test_key(provider))
        row = QHBoxLayout()
        row.addWidget(edit, 1)
        row.addWidget(show)
        row.addWidget(test)
        return row

    def _test_key(self, provider: str) -> None:
        edit = self.openai_key if provider == llm.OPENAI else self.anthropic_key
        key = edit.text().strip()
        if not key:
            self.key_status.setText("Enter a key first, then press Test.")
            return
        model = self._models_for(provider)[0]
        self.key_status.setText(f"Testing the {provider} key with {model}…")
        client = llm.ChatClient(self)

        def done(ok: bool, why: str = "") -> None:
            self.key_status.setText(f"✓ The {provider} key works ({model})." if ok else f"✗ {why}")
            client.deleteLater()

        client.finished.connect(lambda: done(True))
        client.failed.connect(lambda why: done(False, why))
        client.send([{"role": "user", "text": "Reply with the single word OK."}],
                    provider=provider, model=model, api_key=key, max_tokens=16)

    @staticmethod
    def _pair_row(provider: QComboBox, model: QComboBox) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(provider)
        row.addWidget(model, 1)
        return row

    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet("color:#e6edf3; font-weight:600; padding-top:8px;")
        return label

    def _sync_toggles(self, *_args) -> None:
        controller = self._stealth_controller
        if controller is None:
            return
        for box, state in ((self.stealth, controller.enabled), (self.always_on_top, controller.on_top)):
            box.blockSignals(True)
            box.setChecked(bool(state))
            box.blockSignals(False)

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
        s.dev_name = self.dev_name.text().strip()
        s.openai_key = self.openai_key.text().strip()
        s.anthropic_key = self.anthropic_key.text().strip()
        s.openai_models = [m.strip() for m in self.openai_models_edit.text().split(",") if m.strip()] or s.openai_models
        s.coding_provider = self.coding_provider.currentData()
        s.coding_model = self.coding_model.currentText().strip() or s.coding_model
        s.coding_effort = self.coding_effort.currentText()
        s.side_provider = self.side_provider.currentData()
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
