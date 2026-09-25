"""The Settings page inside the app: your name, keys, model, vault, meeting
detection, integrations and appearance. Changes are saved with the Save button."""

from __future__ import annotations

from pathlib import Path

import httpx
from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFormLayout, QHBoxLayout, QLineEdit,
    QPushButton, QRadioButton, QScrollArea, QVBoxLayout, QWidget,
)

from . import config, mail, obsidian, outlook, theme
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
                ok, msg = r.status_code == 200, "Works"
                if ok and "openrouter" in self.base_url:
                    left = r.json().get("data", {}).get("limit_remaining")
                    if left is not None:
                        msg = f"Works · ${left:.2f} credit left"
                self.done.emit("router", ok, msg if ok else f"Rejected (HTTP {r.status_code})")
            else:
                self.done.emit("router", False, "Missing")
        except httpx.HTTPError as e:
            self.done.emit("router", False, f"Couldn't reach the model service ({e.__class__.__name__})")


class _SignIn(QThread):
    """Connects the calendar off the UI thread: checks a published link, or signs in in the browser."""
    done = Signal(bool, str)  # ok, what's connected or the error

    def __init__(self, client_id: str = "", tenant: str = "", ics: str = ""):
        super().__init__()
        self.client_id, self.tenant, self.ics = client_id, tenant, ics
        self.flow: outlook.SignIn | None = None

    def run(self) -> None:
        try:
            if self.ics:
                n = outlook.connect_ics(self.ics)
                self.done.emit(True, f"your Outlook calendar ({n} meeting{'s' if n != 1 else ''} in the next few days)")
                return
            self.flow = outlook.SignIn(self.client_id, self.tenant)
            name = self.flow.run()
            outlook.refresh()
            self.done.emit(True, name)
        except Exception as e:
            self.done.emit(False, str(e))

    def cancel(self) -> None:
        if self.flow:
            self.flow.cancel()


# The sign-in in progress. Kept here, not on the page, because the page is rebuilt
# (e.g. on a theme change) while you're still in the browser.
_active: _SignIn | None = None


def cancel_sign_in() -> None:
    """Stop waiting for the browser (when the app quits)."""
    if _active is not None and _active.isRunning():
        _active.cancel()
        _active.wait(2000)


def _open_online_accounts() -> None:
    from .agenda import open_online_accounts
    open_online_accounts()


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


def _secret(value: str, placeholder: str) -> QLineEdit:
    edit = QLineEdit(value)
    edit.setEchoMode(QLineEdit.Password)
    edit.setPlaceholderText(placeholder)
    show = edit.addAction(theme.icon("eye", T["faint"], 16), QLineEdit.TrailingPosition)
    show.setToolTip("Show / hide")
    show.triggered.connect(lambda: edit.setEchoMode(
        QLineEdit.Normal if edit.echoMode() == QLineEdit.Password else QLineEdit.Password))
    return edit


class SettingsPage(QWidget):
    saved = Signal()               # settings were saved
    discarded = Signal()           # unsaved changes were thrown away
    appearance_changed = Signal(str)

    def __init__(self, watcher):
        super().__init__()
        from . import watch
        self.setObjectName("Page")
        self.state_key = "settings"
        self.w, self.watch = watcher, watch
        self.s = config.load()
        self._loading = True

        body = QWidget()
        body.setObjectName("Page")
        col = QVBoxLayout(body)
        col.setContentsMargins(32, 24, 32, 24)
        col.setSpacing(14)
        col.addWidget(label("Settings", "h1"))
        self._sections: dict[str, QWidget] = {}
        jump = QHBoxLayout()
        jump.setSpacing(6)
        self._jump_row = jump
        col.addLayout(jump)

        # --- appearance
        c, f = _section("Appearance", "sun")
        self.appearance = QButtonGroup(self)
        row = QHBoxLayout()
        row.setSpacing(6)
        for key, text, icon in (("system", "Match system", "monitor"), ("light", "Light", "sun"),
                                ("dark", "Dark", "moon")):
            b = QPushButton(text)
            b.setProperty("variant", "chip")
            b.setProperty("mode", key)
            b.setCheckable(True)
            b.setChecked(key == theme.MODE)
            b.setIcon(theme.icon(icon, T["accent"] if key == theme.MODE else T["muted"], 15))
            b.setCursor(Qt.PointingHandCursor)
            self.appearance.addButton(b)
            row.addWidget(b)
        row.addStretch(1)
        self.appearance.buttonClicked.connect(self._appearance_clicked)
        f.addRow("Theme", row)
        col.addWidget(c)
        self._sections["Appearance"] = c

        # --- you
        c, f = _section("You", "users", "So Hear Me Out knows which tasks in a meeting are yours.")
        self.name = QLineEdit(self.s.user_name)
        self.name.setPlaceholderText("e.g. Brempong")
        self.aliases = QLineEdit(", ".join(self.s.user_aliases))
        self.aliases.setPlaceholderText("Nicknames people use, separated by commas")
        f.addRow("Your name", self.name)
        f.addRow("Also called", self.aliases)
        col.addWidget(c)
        self._sections["You"] = c

        # --- services
        c, f = _section("Transcription and notes", "key",
                        "Your own keys: nothing goes through anyone else's server. "
                        "They're kept on this computer, readable only by you.")
        self.eleven = _secret(self.s.elevenlabs_api_key, "sk_…")
        f.addRow("ElevenLabs key", self.eleven)
        self.router = _secret(self.s.openrouter_api_key, "sk-or-v1-…")
        f.addRow("OpenRouter key", self.router)
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
        self._sections["Keys"] = c

        # --- obsidian
        c, f = _section("Obsidian", "gem")
        self.vault = QComboBox()
        self.vault.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.vault.setMinimumContentsLength(18)
        self.vault.addItem("The vault Obsidian opened last", "")
        for v in obsidian.find_vaults():
            self._add_vault(v)
        configured = str(Path(self.s.vault).expanduser()) if self.s.vault else ""
        if configured and self.vault.findData(configured) < 0:
            self._add_vault(Path(configured))
        self.vault.setCurrentIndex(max(0, self.vault.findData(configured)))
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
        self._sections["Obsidian"] = c

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
            self.silence.addItem("Never" if minutes == 0 else
                                 f"After {minutes} minute{'s' if minutes > 1 else ''} with nobody talking", minutes * 60)
        if self.silence.findData(self.s.silence_stop) < 0:
            self.silence.addItem(f"After {self.s.silence_stop} seconds with nobody talking", self.s.silence_stop)
        self.silence.setCurrentIndex(self.silence.findData(self.s.silence_stop))
        self.silence.setToolTip("You get a warning with a Keep recording button 30 seconds before it stops.")
        f.addRow("Stop recording", self.silence)
        self.login = QCheckBox("Start Hear Me Out when I log in")
        self.login.setChecked(watch.autostart_enabled())
        f.addRow("", self.login)
        self.unignore: dict[str, QCheckBox] = {}
        ignored = sorted(self.w.ignore) if self.w else sorted(self.s.detect_ignore)
        if ignored:
            box = QVBoxLayout()
            box.setSpacing(4)
            for key in ignored:
                cb = QCheckBox(f"Never ask about {key.title()}")
                cb.setChecked(True)
                self.unignore[key] = cb
                box.addWidget(cb)
            f.addRow("Apps", box)
        col.addWidget(c)
        self._sections["Meetings"] = c

        # --- integrations
        c, f = _section("Integrations", "calendar",
                        "Connect your calendar so recordings are named after the meeting, know who was "
                        "invited and use the agenda. Your upcoming meetings show on Home.")
        ms = QVBoxLayout()
        ms.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(8)
        name = label("Microsoft 365")
        name.setStyleSheet("font-weight: 600;")
        head.addWidget(name)
        head.addWidget(label("Outlook calendar and Teams meetings", "muted"))
        head.addStretch(1)
        ms.addLayout(head)
        self.ms_status = label("", "muted", wrap=True)
        ms.addWidget(self.ms_status)
        self.ms_help = label("In Outlook on the web: <b>Settings › Calendar › Shared calendars › Publish a "
                             "calendar</b>. Choose your calendar and <b>Can view all details</b>, press "
                             "<b>Publish</b>, then copy the <b>ICS</b> link and paste it here. No sign-in "
                             "or admin needed. Keep the link private: anyone with it can see your calendar.",
                             "muted", wrap=True)
        ms.addWidget(self.ms_help)
        self.ms_ics = QLineEdit()
        self.ms_ics.setPlaceholderText("https://outlook.office365.com/owa/calendar/…/calendar.ics")
        self.ms_ics.returnPressed.connect(self._ms_connect_ics)
        ms.addWidget(self.ms_ics)
        # Signing in with Microsoft needs an app registration; most people use the link instead.
        self.ms_client = QLineEdit(self.s.ms_client_id)
        self.ms_client.setPlaceholderText("Application (client) ID from your organisation's app registration")
        ms.addWidget(self.ms_client)
        self._own_app = bool(self.s.ms_client_id or outlook.BUILT_IN_CLIENT_ID)
        buttons = QHBoxLayout()
        self.ms_link_btn = button("Connect", "calendar", "primary", on_click=self._ms_connect_ics)
        self.ms_connect = button("Sign in with Microsoft", "external", on_click=self._ms_sign_in)
        self.ms_cancel = button("Cancel", on_click=self._ms_cancel)
        self.ms_disconnect = button("Disconnect", "x", on_click=self._ms_sign_out)
        self.ms_own = button("Sign in with an app registration instead…", variant="ghost",
                             on_click=self._ms_show_own)
        for b in (self.ms_link_btn, self.ms_connect, self.ms_cancel, self.ms_disconnect, self.ms_own):
            buttons.addWidget(b)
        buttons.addStretch(1)
        ms.addLayout(buttons)
        f.addRow(ms)
        self.remind = QComboBox()
        for minutes in (0, 1, 2, 5, 10, 15):
            self.remind.addItem("Never" if minutes == 0 else
                                f"{minutes} minute{'s' if minutes > 1 else ''} before a meeting", minutes)
        if self.remind.findData(self.s.remind_before) < 0:
            self.remind.addItem(f"{self.s.remind_before} minutes before a meeting", self.s.remind_before)
        self.remind.setCurrentIndex(self.remind.findData(self.s.remind_before))
        self.remind.setToolTip("A notification with a Join button, for meetings in your connected calendar")
        f.addRow("Remind me", self.remind)

        # mail on Home: Outlook on the web in the pane, or a list through GNOME Online Accounts
        from . import outlookweb
        mail_box = QVBoxLayout()
        mail_box.setSpacing(6)
        row = QHBoxLayout()
        self.mail_show = QCheckBox("Show my mail on Home")
        self.mail_show.setChecked(self.s.mail_on_home)
        row.addWidget(self.mail_show)
        self.mail_source = QComboBox()
        self.mail_source.addItem("Outlook on the web", "outlook")
        self.mail_source.addItem("GNOME Online Accounts", "gnome")
        self.mail_source.setCurrentIndex(max(0, self.mail_source.findData(self.s.mail_source)))
        row.addWidget(self.mail_source)
        self.mail_count = QComboBox()
        for n in range(5, 11):
            self.mail_count.addItem(f"{n} emails", n)
        self.mail_count.setCurrentIndex(max(0, self.mail_count.findData(max(5, min(10, self.s.mail_count)))))
        row.addWidget(self.mail_count)
        row.addStretch(1)
        mail_box.addLayout(row)
        self.mail_notify = QCheckBox("Notify me when new email arrives")
        self.mail_notify.setChecked(self.s.mail_notify)
        mail_box.addWidget(self.mail_notify)
        self.mail_status = label("", "muted", wrap=True)
        mail_box.addWidget(self.mail_status)
        actions = QHBoxLayout()
        self.mail_signout = button("Sign out of Outlook", "x", on_click=self._outlook_sign_out)
        self.mail_goa = button("Open Online Accounts", "external", on_click=_open_online_accounts)
        actions.addWidget(self.mail_signout)
        actions.addWidget(self.mail_goa)
        actions.addStretch(1)
        mail_box.addLayout(actions)
        f.addRow("Mail", mail_box)
        self.mail_source.currentIndexChanged.connect(self._mail_source_changed)
        self._mail_source_changed()
        col.addWidget(c)
        self._sections["Integrations"] = c
        if _active is not None and _active.isRunning():
            _active.done.connect(self._ms_done)
        self._ms_refresh()
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(body)
        self._scroll = scroll
        icons = {"Appearance": "sun", "You": "users", "Keys": "key", "Obsidian": "gem", "Meetings": "mic",
                 "Integrations": "calendar"}
        for name in self._sections:
            b = QPushButton(name)
            b.setProperty("variant", "chip")
            b.setIcon(theme.icon(icons[name], T["muted"], 14))
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, n=name: self.show_section(n))
            self._jump_row.addWidget(b)
        self._jump_row.addStretch(1)

        # footer
        self.status = label("", "muted")
        self.discard = button("Discard changes", on_click=self.discard_changes)
        self.save_btn = button("Save changes", "check", "primary", on_click=self.save)
        foot_w = QWidget()
        foot_w.setObjectName("Page")
        foot = QHBoxLayout(foot_w)
        foot.setContentsMargins(32, 10, 32, 16)
        foot.addWidget(self.status, 1)
        foot.addWidget(self.discard)
        foot.addWidget(self.save_btn)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(scroll, 1)
        lay.addWidget(foot_w)

        for w in (self.name, self.aliases, self.eleven, self.router, self.folder, self.ms_client):
            w.textChanged.connect(self._changed)
        for w in (self.model, self.vault, self.silence, self.remind, self.mail_count, self.mail_source):
            w.currentIndexChanged.connect(self._changed)
        self.model.editTextChanged.connect(self._changed)
        for w in (*self.items.values(), self.login, *self.unignore.values(), self.mail_show, self.mail_notify):
            w.toggled.connect(self._changed)
        self.mode.buttonToggled.connect(self._changed)
        self._checker: _KeyCheck | None = None
        self._results: dict[str, tuple[bool, str]] = {}
        self._loading = False
        self.dirty = False
        self._update_footer()

    def show_section(self, name: str) -> None:
        """Scroll to a section, e.g. show_section("Obsidian")."""
        w = self._sections.get(name)
        if w is not None:
            self._scroll.verticalScrollBar().setValue(max(0, w.y() - 12))

    # --- state

    def _changed(self, *_) -> None:
        if not self._loading:
            self.dirty = True
            self._update_footer()

    def _update_footer(self) -> None:
        self.save_btn.setEnabled(self.dirty)
        self.discard.setEnabled(self.dirty)
        if self.dirty:
            self.status.setText("You have unsaved changes")
            self.status.setStyleSheet(f"color: {T['amber']};")

    def _add_vault(self, v: Path) -> None:
        self.vault.addItem(theme.icon("gem", T["accent"]), v.name, str(v))
        self.vault.setItemData(self.vault.count() - 1, str(v), Qt.ToolTipRole)

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose your Obsidian vault", str(Path.home()))
        if path:
            if self.vault.findData(path) < 0:
                self._add_vault(Path(path))
            self.vault.setCurrentIndex(self.vault.findData(path))

    def _appearance_clicked(self, b: QPushButton) -> None:
        """Light / dark / system takes effect (and is saved) straight away."""
        mode = b.property("mode")
        s = config.load()
        s.appearance = mode
        config.save(s)
        self.s.appearance = mode
        theme.apply(QApplication.instance(), mode)
        self.appearance_changed.emit(mode)

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

    def _mail_source_changed(self, *_) -> None:
        from . import outlookweb
        outlook = self.mail_source.currentData() == "outlook"
        self.mail_count.setVisible(not outlook)
        self.mail_notify.setVisible(outlook)
        self.mail_signout.setVisible(outlook and outlookweb.available())
        self.mail_goa.setVisible(not outlook)
        if outlook:
            text = ("Your Outlook inbox shows in the Mail pane on Home. Sign in there once, as in a "
                    "browser: it works with any work account, and the sign-in is remembered."
                    if outlookweb.available() else
                    "Outlook on the web needs Qt's web view: reinstall Hear Me Out to get it.")
        else:
            who = mail.account_name()
            text = (f"Using {who} from GNOME Online Accounts." if who else
                    "Add an account in GNOME <b>Settings › Online Accounts › Microsoft 365</b>. On Ubuntu "
                    "this only accepts personal Microsoft accounts, not work ones.")
        self.mail_status.setText(text)

    def _outlook_sign_out(self) -> None:
        from . import outlookweb
        outlookweb.sign_out()
        view = getattr(self.w.window, "_outlook_view", None) if self.w else None
        if view is not None:
            view.load(QUrl(outlookweb.INBOX))
        self.mail_status.setText("Signed out of Outlook. Sign in again in the Mail pane on Home.")

    # --- Microsoft 365

    def _ms_refresh(self, message: str = "", ok: bool | None = None) -> None:
        busy = _active is not None and _active.isRunning()
        on = outlook.connected()
        if message:
            text = message
        elif busy:
            text = "Connecting…" if _active.ics else "Finish signing in in your browser…"
        elif on:
            err = getattr(self.w, "calendar_error", "") if self.w else ""
            text = f"Connected: {outlook.account()}." + (f" Last sync failed: {err}" if err else "")
            ok = not err
        else:
            text = "Not connected."
        colour = T["green"] if ok else T["red"] if ok is False else T["muted"]
        self.ms_status.setText(text)
        self.ms_status.setStyleSheet(f"color: {colour};")
        idle = not on and not busy
        for w in (self.ms_help, self.ms_ics, self.ms_link_btn):
            w.setVisible(idle)
        self.ms_client.setVisible(idle and self._own_app)
        self.ms_connect.setVisible(idle and self._own_app)
        self.ms_own.setVisible(idle and not self._own_app)
        self.ms_cancel.setVisible(busy)
        self.ms_disconnect.setVisible(on and not busy)

    def _start(self, job: "_SignIn") -> None:
        global _active
        _active = job
        _active.done.connect(self._ms_done)
        _active.start()
        self._ms_refresh()

    def _ms_connect_ics(self) -> None:
        url = self.ms_ics.text().strip()
        if not url:
            self._ms_refresh("Paste your calendar's ICS link first.", False)
            return
        self._start(_SignIn(ics=url))

    def _ms_show_own(self) -> None:
        self._own_app = True
        self._ms_refresh()
        self.ms_client.setFocus()

    def _ms_sign_in(self) -> None:
        own = self.ms_client.text().strip()
        client_id = own or outlook.BUILT_IN_CLIENT_ID
        if not client_id:
            self._ms_refresh("Paste an application (client) ID first, or use a calendar link instead.", False)
            return
        s = config.load()
        if own != s.ms_client_id:  # keep your own ID even if the rest of the page isn't saved
            s.ms_client_id = own
            config.save(s)
        self._start(_SignIn(client_id, s.ms_tenant))

    def _ms_cancel(self) -> None:
        if _active is not None:
            _active.cancel()

    def _ms_done(self, ok: bool, text: str) -> None:
        if ok:
            self._ms_refresh(f"Connected: {text}.", True)
            if self.w is not None:
                self.w.calendar_error = ""
                self.w.changed.emit()
        else:
            self._ms_refresh(text, False)

    def _ms_sign_out(self) -> None:
        outlook.sign_out()
        self._ms_refresh("Disconnected. Your meeting notes aren't affected.")
        if self.w is not None:
            self.w.changed.emit()

    # --- actions

    def save(self) -> None:
        s = config.load()
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
        s.appearance = theme.MODE
        s.ms_client_id = self.ms_client.text().strip() or s.ms_client_id
        s.remind_before = int(self.remind.currentData())
        s.mail_on_home = self.mail_show.isChecked()
        s.mail_count = int(self.mail_count.currentData())
        s.mail_source = self.mail_source.currentData()
        s.mail_notify = self.mail_notify.isChecked()
        config.save(s)
        self.watch.set_autostart(self.login.isChecked())
        if self.w is not None:
            self.w.s = config.load()
            self.w.refresh_mail()  # e.g. mail was just switched on
            self.w.set_mode(mode)
            for key, cb in self.unignore.items():
                if not cb.isChecked():
                    self.w.set_ignored(key, False)
        self.s = s
        self.dirty = False
        self._update_footer()
        self.status.setText("✓ Settings saved")
        self.status.setStyleSheet(f"color: {T['green']}; font-weight: 600;")
        self.saved.emit()

    def discard_changes(self) -> None:
        """Put every field back to what's saved (the window rebuilds this page)."""
        self.dirty = False
        self.discarded.emit()
