"""Settings window: your name, API keys, model, vault and meeting detection,
so nobody has to edit config.toml by hand."""

from __future__ import annotations

from pathlib import Path

import httpx
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit,
    QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

from . import config, obsidian, theme
from .obsidian import ITEMS
from .theme import T
from .views import button, card, icon_label, label

MODELS = [
    ("google/gemini-3.1-flash-lite", "Gemini 3.1 Flash Lite: fast and cheap (recommended)"),
    ("anthropic/claude-sonnet-5", "Claude Sonnet 5: most careful"),
    ("openai/gpt-5.4-mini", "GPT-5.4 mini"),
    ("deepseek/deepseek-v4-flash", "DeepSeek V4 Flash: cheapest"),
]


class _KeyCheck(QThread):
    done = Signal(str, bool, str)  # which, ok, message

    def __init__(self, eleven: str, router: str, base_url: str):
        super().__init__()
        self.eleven, self.router, self.base_url = eleven, router, base_url

    def run(self) -> None:
        try:
            if self.eleven:
                r = httpx.get("https://api.elevenlabs.io/v1/models", headers={"xi-api-key": self.eleven}, timeout=10)
                bad = r.status_code == 401 and "invalid" in r.text.lower()
                self.done.emit("eleven", not bad, "Key rejected by ElevenLabs" if bad else "Works")
            else:
                self.done.emit("eleven", False, "Missing")
        except httpx.HTTPError as e:
            self.done.emit("eleven", False, f"Couldn't reach ElevenLabs ({e.__class__.__name__})")
        try:
            if self.router:
                url = f"{self.base_url}/key" if "openrouter" in self.base_url else f"{self.base_url}/models"
                r = httpx.get(url, headers={"Authorization": f"Bearer {self.router}"}, timeout=10)
                ok = r.status_code == 200
                msg = "Works"
                if ok and "openrouter" in self.base_url:
                    data = r.json().get("data", {})
                    left = data.get("limit_remaining")
                    if left is not None:
                        msg = f"Works · ${left:.2f} credit left"
                self.done.emit("router", ok, msg if ok else f"Rejected (HTTP {r.status_code})")
            else:
                self.done.emit("router", False, "Missing")
        except httpx.HTTPError as e:
            self.done.emit("router", False, f"Couldn't reach the model service ({e.__class__.__name__})")


def _section(title: str, icon: str, subtitle: str = "") -> tuple[QWidget, QFormLayout]:
    c = card()
    lay = QVBoxLayout(c)
    lay.setContentsMargins(20, 16, 20, 18)
    lay.setSpacing(12)
    head = QHBoxLayout()
    head.setSpacing(8)
    head.addWidget(icon_label(icon, T["accent"], 18))
    head.addWidget(label(title, "h2"))
    head.addStretch(1)
    lay.addLayout(head)
    if subtitle:
        lay.addWidget(label(subtitle, "muted", wrap=True))
    form = QFormLayout()
    form.setHorizontalSpacing(16)
    form.setVerticalSpacing(10)
    form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
    lay.addLayout(form)
    return c, form


def _secret(value: str, placeholder: str) -> tuple[QWidget, QLineEdit]:
    edit = QLineEdit(value)
    edit.setEchoMode(QLineEdit.Password)
    edit.setPlaceholderText(placeholder)
    show = edit.addAction(theme.icon("eye", T["faint"], 16), QLineEdit.TrailingPosition)
    show.setToolTip("Show / hide")
    show.triggered.connect(lambda: edit.setEchoMode(
        QLineEdit.Normal if edit.echoMode() == QLineEdit.Password else QLineEdit.Password))
    return edit, edit


class SettingsDialog(QDialog):
    def __init__(self, watcher, parent=None):
        super().__init__(parent)
        from . import watch
        self.w, self.watch = watcher, watch
        self.s = config.load()
        self.setWindowTitle("Settings · Hear Me Out")
        self.resize(720, 760)

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(24, 20, 24, 20)
        col.setSpacing(14)
        col.addWidget(label("Settings", "h1"))

        # --- you
        c, f = _section("You", "users", "So Hear Me Out knows which tasks in a meeting are yours.")
        self.name = QLineEdit(self.s.user_name)
        self.name.setPlaceholderText("e.g. Brempong")
        self.aliases = QLineEdit(", ".join(self.s.user_aliases))
        self.aliases.setPlaceholderText("Nicknames people use, separated by commas")
        f.addRow("Your name", self.name)
        f.addRow("Also called", self.aliases)
        col.addWidget(c)

        # --- services
        c, f = _section("Transcription and notes", "key",
                        "Your own keys: nothing goes through anyone else's server. "
                        "They're stored in ~/.config/hearmeout/config.toml, readable only by you.")
        w, self.eleven = _secret(self.s.elevenlabs_api_key, "sk_…")
        f.addRow("ElevenLabs key", w)
        w, self.router = _secret(self.s.openrouter_api_key, "sk-or-v1-…")
        f.addRow("OpenRouter key", w)
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.model.setMinimumContentsLength(20)
        for mid, desc in MODELS:
            self.model.addItem(desc, mid)
        idx = self.model.findData(self.s.llm_model)
        if idx >= 0:
            self.model.setCurrentIndex(idx)
        else:
            self.model.setEditText(self.s.llm_model)
        self.model.setToolTip("Any OpenRouter model id works, e.g. openai/gpt-5.4-mini")
        f.addRow("Notes model", self.model)
        row = QHBoxLayout()
        self.check_btn = button("Check keys", "check", on_click=self._check_keys)
        row.addWidget(self.check_btn)
        self.check_result = label("", "muted", wrap=True)
        row.addWidget(self.check_result, 1)
        f.addRow("", row)
        col.addWidget(c)

        # --- obsidian
        c, f = _section("Obsidian", "gem")
        self.vault = QComboBox()
        self.vault.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.vault.setMinimumContentsLength(18)
        for v in obsidian.find_vaults():
            self.vault.addItem(theme.icon("gem", T["accent"]), v.name, str(v))
            self.vault.setItemData(self.vault.count() - 1, str(v), Qt.ToolTipRole)
        configured = str(Path(self.s.vault).expanduser()) if self.s.vault else ""
        if configured and self.vault.findData(configured) < 0:
            self.vault.addItem(theme.icon("gem", T["accent"]), Path(configured).name, configured)
            self.vault.setItemData(self.vault.count() - 1, configured, Qt.ToolTipRole)
        self.vault.insertItem(0, "The vault Obsidian opened last", "")
        self.vault.setCurrentIndex(max(0, self.vault.findData(str(Path(self.s.vault).expanduser()))
                                       if self.s.vault else 0))
        vrow = QHBoxLayout()
        vrow.addWidget(self.vault, 1)
        vrow.addWidget(button("Browse…", "folder", on_click=self._browse))
        f.addRow("Vault", vrow)
        self.folder = QLineEdit(self.s.folder)
        f.addRow("Folder", self.folder)
        items = QHBoxLayout()
        items.setSpacing(12)
        self.items: dict[str, QCheckBox] = {}
        for key, (name, _) in ITEMS.items():
            box = QCheckBox(name)
            box.setChecked(key in self.s.save)
            self.items[key] = box
            items.addWidget(box)
        items.addStretch(1)
        f.addRow("Save by default", items)
        col.addWidget(c)

        # --- meetings
        c, f = _section("Meetings", "mic")
        self.mode = QButtonGroup(self)
        modes = QVBoxLayout()
        modes.setSpacing(6)
        current = self.w.mode if self.w else self.s.detect
        for key, text in (("ask", "Ask me before recording a meeting"),
                          ("auto", "Record meetings straight away (you can still stop)"),
                          ("off", "Don't watch for meetings: I'll press Record myself")):
            r = QRadioButton(text)
            r.setProperty("mode", key)
            r.setChecked(key == current)
            self.mode.addButton(r)
            modes.addWidget(r)
        f.addRow("When a call starts", modes)
        self.silence = QComboBox()
        for minutes in (0, 1, 2, 3, 5, 10, 15, 30):
            self.silence.addItem("Never" if minutes == 0 else f"After {minutes} minute{'s' if minutes > 1 else ''} "
                                 "with nobody talking", minutes * 60)
        current_silence = self.s.silence_stop
        if self.silence.findData(current_silence) < 0:
            self.silence.addItem(f"After {current_silence} seconds with nobody talking", current_silence)
        self.silence.setCurrentIndex(self.silence.findData(current_silence))
        self.silence.setToolTip("You get a warning with a Keep recording button 30 seconds before it stops.")
        f.addRow("Stop recording", self.silence)
        self.login = QCheckBox("Start Hear Me Out when I log in")
        self.login.setChecked(watch.autostart_enabled())
        f.addRow("", self.login)
        self.ignored = sorted(self.w.ignore) if self.w else sorted(self.s.detect_ignore)
        if self.ignored:
            box = QVBoxLayout()
            box.setSpacing(4)
            self.unignore: dict[str, QCheckBox] = {}
            for key in self.ignored:
                cb = QCheckBox(f"{key.title()}: never ask")
                cb.setChecked(True)
                self.unignore[key] = cb
                box.addWidget(cb)
            f.addRow("Apps", box)
        col.addWidget(c)
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        foot = QHBoxLayout()
        foot.setContentsMargins(24, 10, 24, 16)
        foot.addWidget(label(f"Settings file: {config.CONFIG_FILE}", "faint"), 1)
        foot.addWidget(button("Cancel", on_click=self.reject))
        save = button("Save settings", "check", "primary", on_click=self._save)
        save.setDefault(True)
        foot.addWidget(save)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(scroll, 1)
        lay.addLayout(foot)
        self._checker: _KeyCheck | None = None
        self._results: dict[str, tuple[bool, str]] = {}

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose your Obsidian vault", str(Path.home()))
        if path:
            if self.vault.findData(path) < 0:
                self.vault.addItem(theme.icon("gem", T["accent"]), Path(path).name, path)
            self.vault.setCurrentIndex(self.vault.findData(path))

    def _model_id(self) -> str:
        text = self.model.currentText().strip()
        idx = self.model.findText(text)
        return self.model.itemData(idx) if idx >= 0 and self.model.itemData(idx) else text

    def _check_keys(self) -> None:
        self.check_btn.setEnabled(False)
        self.check_result.setText("Checking…")
        self._results = {}
        self._checker = _KeyCheck(self.eleven.text().strip(), self.router.text().strip(), self.s.llm_base_url)
        self._checker.done.connect(self._key_result)
        self._checker.finished.connect(lambda: self.check_btn.setEnabled(True))
        self._checker.start()

    def _key_result(self, which: str, ok: bool, msg: str) -> None:
        self._results[which] = (ok, msg)
        parts = []
        for key, name in (("eleven", "ElevenLabs"), ("router", "OpenRouter")):
            if key in self._results:
                good, text = self._results[key]
                colour = T["green"] if good else T["red"]
                parts.append(f'<span style="color:{colour}">{"✓" if good else "✗"} {name}: {text}</span>')
        self.check_result.setText("<br>".join(parts))

    def _save(self) -> None:
        s = self.s
        s.user_name = self.name.text().strip()
        s.user_aliases = [a.strip() for a in self.aliases.text().split(",") if a.strip()]
        s.elevenlabs_api_key = self.eleven.text().strip()
        s.openrouter_api_key = self.router.text().strip()
        s.llm_model = self._model_id() or s.llm_model
        s.vault = self.vault.currentData() or ""
        s.folder = self.folder.text().strip() or "Meetings"
        s.save = [k for k, b in self.items.items() if b.isChecked()] or ["summary"]
        s.silence_stop = int(self.silence.currentData())
        mode = next(b.property("mode") for b in self.mode.buttons() if b.isChecked())
        s.detect = mode
        config.save(s)
        self.watch.set_autostart(self.login.isChecked())
        if self.w is not None:
            self.w.s = config.load()
            self.w.set_mode(mode)
            for key, cb in getattr(self, "unignore", {}).items():
                if not cb.isChecked():
                    self.w.set_ignored(key, False)
        self.accept()
