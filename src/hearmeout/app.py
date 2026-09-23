"""The Hear Me Out window.

Sidebar: Record, search, recordings still to save, then saved meetings by day.
Main area: the selected meeting (summary, to-dos, team tasks, transcript), with a
"Save to Obsidian" bar for new recordings, and a banner while recording.
"""

from __future__ import annotations

import time
from datetime import date
from pathlib import Path

import soundfile as sf
from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import library, obsidian, theme
from .gui import _load_choices, _save_choices
from .player import Player
from .theme import T
from .views import (
    ElidedLabel, MeetingData, MeetingView, SaveBar, badge, button, callout, card, divider, from_meeting,
    from_saved, icon_label, label,
)


def _day(d: date) -> str:
    days = (date.today() - d).days
    if days == 0:
        return "Today"
    if days == 1:
        return "Yesterday"
    if days < 7:
        return f"{d:%A}"
    return f"{d:%a %-d %B}" if d.year == date.today().year else f"{d:%-d %B %Y}"


def _clock(sec: float) -> str:
    m, s = divmod(int(sec), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _minutes(sec: int) -> str:
    if sec % 60 == 0 and sec >= 60:
        n = sec // 60
        return f"{n} minute{'s' if n != 1 else ''}"
    return f"{sec} seconds"


class _Row(QWidget):
    """A meeting in the sidebar."""

    def __init__(self, title: str, sub: str, badge_text: str = "", kind: str = "", dot: str | None = None):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(8)
        if dot:
            lay.addWidget(icon_label("record", dot, 10), 0, Qt.AlignVCenter)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        t = ElidedLabel(title)
        f = t.font()
        f.setWeight(f.Weight.DemiBold)
        t.setFont(f)
        texts.addWidget(t)
        texts.addWidget(ElidedLabel(sub, "muted"))
        lay.addLayout(texts, 1)
        if badge_text:
            lay.addWidget(badge(badge_text, kind), 0, Qt.AlignTop)


class _Meter(QWidget):
    def __init__(self, name: str, width: int = 70):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(label(name, "muted"))
        self.bar = QProgressBar()
        self.bar.setProperty("role", "meter")
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(width)
        lay.addWidget(self.bar)

    def set(self, level: float) -> None:
        self.bar.setValue(int(level * 100))


class _RecBanner(QFrame):
    """Shown across the top while recording, whatever you're looking at."""

    def __init__(self, win: "MainWindow"):
        super().__init__()
        self.setObjectName("RecBanner")
        self.win = win
        lay = QHBoxLayout(self)
        lay.setContentsMargins(20, 9, 16, 9)
        lay.setSpacing(12)
        self.dot = icon_label("record", T["red"], 14)
        lay.addWidget(self.dot)
        self.what = label("")
        self.what.setStyleSheet(f"color: {T['red']}; font-weight: 650;")
        lay.addWidget(self.what)
        self.time = label("", "mono")
        lay.addWidget(self.time)
        lay.addSpacing(8)
        self.you, self.others = _Meter("You"), _Meter("Others")
        lay.addWidget(self.you)
        lay.addWidget(self.others)
        self.quiet = label("", "muted")
        lay.addWidget(self.quiet)
        lay.addStretch(1)
        self.keep = button("Keep recording", variant="ghost", on_click=lambda: win.w.keep_recording())
        lay.addWidget(self.keep)
        self.open = button("View", variant="ghost", on_click=lambda: win.select("recording"))
        lay.addWidget(self.open)
        lay.addWidget(button("Stop", "stop", "record", on_click=lambda: win.w.stop()))
        self._blink = False

    def update_from(self, w) -> None:
        rec = w.recorder
        if rec is None:
            return
        self._blink = not self._blink
        self.dot.setPixmap(theme.pixmap("record", T["red"] if self._blink else T["red_soft"], 14))
        self.what.setText(f"Recording {w.call.app} call" if w.call else "Recording")
        self.time.setText(_clock(time.time() - w.rec_started))
        self.you.set(rec.levels["mic"])
        self.others.set(rec.levels["system"])
        quiet, limit = rec.silent_for(), w.s.silence_stop
        if limit > 0 and quiet >= 20:
            left = max(0, limit - quiet)
            self.quiet.setText(f"Nobody talking · stops in {_clock(left)}")
            self.quiet.setStyleSheet(f"color: {T['amber'] if left <= 30 else T['muted']};")
        else:
            self.quiet.setText("")
        self.keep.setVisible(limit > 0 and quiet >= limit - 30)
        self.open.setVisible(self.win.current != "recording")


class MainWindow(QMainWindow):
    def __init__(self, watcher):
        super().__init__()
        self.w = watcher
        self.player = Player()
        self.views: dict[Path, MeetingView] = {}   # kept per unsaved recording, so edits survive
        self.bars: dict[Path, SaveBar] = {}
        self.items: dict[str, object] = {}
        self.current: str | None = None
        self.banner_for: str | None = None
        self.setWindowTitle("Hear Me Out")
        self.resize(1180, 760)
        self.setMinimumSize(860, 560)
        self._build()
        QShortcut(QKeySequence.Find, self, lambda: self.search.setFocus())
        QShortcut(QKeySequence("Ctrl+R"), self, lambda: self.rec_btn.click())
        QShortcut(QKeySequence("Ctrl+,"), self, self.open_settings)
        self.w.changed.connect(self.refresh)
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(500)
        self.refresh()

    # ------------------------------------------------------------------ layout

    def _build(self) -> None:
        side = QWidget()
        side.setObjectName("Sidebar")
        side.setFixedWidth(300)
        s = QVBoxLayout(side)
        s.setContentsMargins(14, 16, 14, 12)
        s.setSpacing(10)
        brand = QHBoxLayout()
        brand.setSpacing(8)
        brand.addWidget(icon_label("wave", T["accent"], 20))
        brand.addWidget(label("Hear Me Out", "brand"))
        brand.addStretch(1)
        s.addLayout(brand)
        s.addSpacing(4)
        self.rec_btn = button("Record", "record", "record", on_click=self._record_clicked, tip="Ctrl+R")
        self.rec_btn.setMinimumHeight(38)
        s.addWidget(self.rec_btn)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search meetings")
        self.search.setClearButtonEnabled(True)
        self.search.addAction(theme.icon("search", T["faint"], 16), QLineEdit.LeadingPosition)
        self.search.textChanged.connect(lambda: self.refresh())
        s.addWidget(self.search)
        self.list = QListWidget()
        self.list.setObjectName("Meetings")
        self.list.setVerticalScrollMode(QListWidget.ScrollPerPixel)
        self.list.currentItemChanged.connect(lambda cur, _: self._select_item(cur))
        s.addWidget(self.list, 1)
        s.addWidget(divider())
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.status_dot = icon_label("record", T["green"], 9)
        foot.addWidget(self.status_dot)
        self.status = ElidedLabel("", "muted")
        foot.addWidget(self.status, 1)
        gear = button("", "settings", "ghost", tip="Settings (Ctrl+,)", icon_color=T["muted"])
        menu = QMenu(gear)
        menu.aboutToShow.connect(lambda: self._fill_menu(menu))
        gear.setMenu(menu)
        gear.setStyleSheet("QPushButton::menu-indicator { width: 0; image: none; }")
        foot.addWidget(gear)
        s.addLayout(foot)

        right = QWidget()
        right.setObjectName("Content")
        r = QVBoxLayout(right)
        r.setContentsMargins(0, 0, 0, 0)
        r.setSpacing(0)
        self.banner = _RecBanner(self)
        self.banner.hide()
        r.addWidget(self.banner)
        self.detail = QStackedWidget()
        r.addWidget(self.detail, 1)

        root = QWidget()
        lay = QHBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(side)
        lay.addWidget(right, 1)
        self.setCentralWidget(root)

    def _fill_menu(self, menu: QMenu) -> None:
        menu.clear()
        menu.addAction(theme.icon("settings", T["text"], 16), "Settings…", self.open_settings)
        menu.addSeparator()
        self.w.fill_settings_menu(menu)

    def retheme(self) -> None:
        """The system switched between light and dark: rebuild with the new colours."""
        if any(b.busy for b in self.bars.values()):
            return
        cur, query = self.current, self.search.text()
        self.views.clear()
        self.bars.clear()
        self.takeCentralWidget().deleteLater()
        self._build()
        self.search.setText(query)
        self.current = cur
        self.refresh(force=True)

    # ------------------------------------------------------------------ sidebar

    def refresh(self, force: bool = False) -> None:
        query = self.search.text().strip()
        self.items.clear()
        self.list.blockSignals(True)
        scroll = self.list.verticalScrollBar().value()
        self.list.clear()

        def section(text: str) -> None:
            item = QListWidgetItem(self.list)
            item.setFlags(Qt.ItemIsEnabled)
            w = label(text.upper(), "section")
            w.setContentsMargins(12, 12, 0, 2)
            item.setSizeHint(QSize(0, 32))
            self.list.setItemWidget(item, w)

        def add(key: str, obj, row: _Row) -> None:
            item = QListWidgetItem(self.list)
            item.setData(Qt.UserRole, key)
            item.setSizeHint(QSize(0, 56))
            self.list.setItemWidget(item, row)
            self.items[key] = obj

        pending = [p for p in library.pending_recordings() if not query or query.lower() in p.title.lower()]
        if self.w.recorder is not None or pending:
            section("To save")
        if self.w.recorder is not None:
            add("recording", None, _Row("Recording now", self.w.call.app if self.w.call else "Started by you",
                                        "REC", "rec", dot=T["red"]))
        for p in pending:
            sub = " · ".join(x for x in (library.when(p.started), f"{max(1, round(p.duration_s / 60))} min", p.app)
                             if x)
            if p.session in self.w.jobs:
                b = ("Making notes", "busy")
            elif p.error:
                b = ("Failed", "bad")
            elif p.ready:
                b = ("Ready to save", "ready")
            else:
                b = ("Not processed", "plain")
            add(p.key, p, _Row(p.title, sub, *b))

        self.saved = library.saved_meetings(self.w.s)
        shown = [m for m in self.saved if not query or m.matches(query)]
        day = None
        for m in shown:
            if m.started.date() != day:
                day = m.started.date()
                section(_day(day))
            todos = m.open_todos()
            add(m.key, m, _Row(m.title, f"{m.started:%H:%M} · {max(1, m.duration_min)} min",
                               f"{todos} to-do{'s' if todos != 1 else ''}" if todos else "", "ok"))
        if query and not shown and not pending:
            section("No matches")
        self.list.verticalScrollBar().setValue(scroll)
        self.list.blockSignals(False)

        keys = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]
        target = self.current if self.current in keys else next((k for k in keys if k), None)
        if target:
            self._set_current(target, rebuild=force or target != self.current or self._stale(target))
        else:
            self.current = None
            self._show(self._welcome_page(bool(query)))
        self._tick()

    def _state_key(self, key: str) -> str:
        obj = self.items.get(key)
        if isinstance(obj, library.PendingRecording):
            s = obj.session
            state = ("busy:" + self.w.jobs[s].message if s in self.w.jobs else "failed" if obj.error
                     else "ready" if obj.ready else "new")
            return f"{key}|{state}"
        return key

    def _stale(self, key: str) -> bool:
        return getattr(self.detail.currentWidget(), "state_key", None) != self._state_key(key)

    def select(self, key: str) -> None:
        self.current = key
        self.refresh()

    def _set_current(self, key: str, rebuild: bool = True) -> None:
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) == key:
                self.list.blockSignals(True)
                self.list.setCurrentRow(i)
                self.list.blockSignals(False)
                break
        self.current = key
        if rebuild:
            self._show(self._page_for(key))

    def _select_item(self, item: QListWidgetItem | None) -> None:
        if item is not None and item.data(Qt.UserRole):
            self._set_current(item.data(Qt.UserRole))
        elif self.current:  # a day heading: keep the meeting selected
            self._set_current(self.current, rebuild=False)

    def _show(self, page: QWidget) -> None:
        old = self.detail.currentWidget()
        if page is old:
            return
        if self.detail.indexOf(page) < 0:
            self.detail.addWidget(page)
        self.detail.setCurrentWidget(page)
        if old is not None and old not in self.views.values():
            self.detail.removeWidget(old)
            old.deleteLater()

    def _record_clicked(self) -> None:
        if self.w.recorder is not None:
            self.w.stop()
        else:
            self.w.start()
            if self.w.recorder is not None:
                self.select("recording")

    def _tick(self) -> None:
        recording = self.w.recorder is not None
        self.rec_btn.setText("Stop recording" if recording else "Record")
        self.rec_btn.setIcon(theme.icon("stop" if recording else "record", "white", 16))
        self.banner.setVisible(recording)
        if recording:
            self.banner.update_from(self.w)
        page = self.detail.currentWidget()
        if recording and hasattr(page, "update_recording"):
            page.update_recording()
        self.status.setText("Recording" if recording else self.w.status_text())
        colour = (T["red"] if recording else T["amber"] if self.w.jobs
                  else T["green"] if self.w.mode != "off" else T["faint"])
        self.status_dot.setPixmap(theme.pixmap("record", colour, 9))

    # ------------------------------------------------------------------ pages

    def _page(self, state_key: str = "") -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.setObjectName("Page")
        page.state_key = state_key
        lay = QVBoxLayout(page)
        lay.setContentsMargins(32, 24, 32, 24)
        return page, lay

    def _page_for(self, key: str) -> QWidget:
        obj = self.items.get(key)
        if key == "recording":
            return self._recording_page()
        if isinstance(obj, library.PendingRecording):
            return self._pending_page(obj)
        if isinstance(obj, library.SavedMeeting):
            return self._saved_page(obj)
        return self._welcome_page(False)

    def setup_missing(self) -> list[str]:
        s = self.w.s
        missing = []
        if not s.user_name:
            missing.append("your name")
        if not s.elevenlabs_api_key:
            missing.append("an ElevenLabs key")
        if not s.openrouter_api_key:
            missing.append("an OpenRouter key")
        if not (s.vault or obsidian.find_vaults()):
            missing.append("your Obsidian vault")
        return missing

    def _welcome_page(self, searching: bool) -> QWidget:
        page, lay = self._page()
        lay.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(10)
        if searching:
            col.addWidget(icon_label("search", T["faint"], 40), 0, Qt.AlignHCenter)
            col.addWidget(label("No meetings match your search", "h1"), 0, Qt.AlignHCenter)
            col.addWidget(label("Search looks through titles, summaries, tasks and transcripts.", "muted"),
                          0, Qt.AlignHCenter)
        else:
            col.addWidget(icon_label("wave", T["accent"], 44), 0, Qt.AlignHCenter)
            col.addWidget(label("Your meetings will appear here", "h1"), 0, Qt.AlignHCenter)
            sub = label("Join a call and Hear Me Out offers to record it. When it ends you get a summary, "
                        "your to-dos and a transcript, and you choose what to save to Obsidian.", "muted", wrap=True)
            sub.setAlignment(Qt.AlignCenter)
            sub.setMaximumWidth(520)
            col.addWidget(sub, 0, Qt.AlignHCenter)
            col.addSpacing(8)
            missing = self.setup_missing()
            if missing:
                c = callout("warn", "key", "Finish setting up", "Hear Me Out still needs " + ", ".join(missing) + ".",
                            [button("Open settings", "settings", "primary", on_click=self.open_settings)])
                c.setMaximumWidth(600)
                col.addWidget(c, 0, Qt.AlignHCenter)
            else:
                col.addWidget(button("Record now", "record", "record", on_click=self._record_clicked),
                              0, Qt.AlignHCenter)
            col.addSpacing(18)
            tips = QHBoxLayout()
            tips.setSpacing(12)
            tips.addStretch(1)
            for icon, title, text in (
                    ("mic", "Notices meetings", "Zoom, Teams, Meet, Slack, Discord… it asks before recording."),
                    ("headphones", "Headset or speakers", "Follows the devices your call uses, even mid-call."),
                    ("gem", "You choose what's saved", "Summary, to-dos, team tasks and transcript, as notes.")):
                c = card()
                c.setFixedWidth(220)
                cl = QVBoxLayout(c)
                cl.setContentsMargins(16, 16, 16, 16)
                cl.setSpacing(6)
                cl.addWidget(icon_label(icon, T["accent"], 20))
                cl.addWidget(label(title, "h2"))
                cl.addWidget(label(text, "muted", wrap=True))
                tips.addWidget(c)
            tips.addStretch(1)
            col.addLayout(tips)
        lay.addLayout(col)
        lay.addStretch(2)
        return page

    def _recording_page(self) -> QWidget:
        page, lay = self._page("recording")
        lay.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(8)
        col.addWidget(icon_label("record", T["red"], 40), 0, Qt.AlignHCenter)
        what = f"Recording {self.w.call.app} call" if self.w.call else "Recording"
        col.addWidget(label(what, "h1"), 0, Qt.AlignHCenter)
        clock = label("00:00", "bigtime")
        col.addWidget(clock, 0, Qt.AlignHCenter)
        meters = QHBoxLayout()
        meters.setSpacing(24)
        meters.addStretch(1)
        you, others = _Meter("You", 140), _Meter("Others", 140)
        meters.addWidget(you)
        meters.addWidget(others)
        meters.addStretch(1)
        col.addLayout(meters)
        quiet = label("", "muted")
        col.addWidget(quiet, 0, Qt.AlignHCenter)
        col.addSpacing(14)
        btns = QHBoxLayout()
        btns.setSpacing(8)
        btns.addStretch(1)
        keep = button("Keep recording", variant="ghost", on_click=self.w.keep_recording)
        btns.addWidget(keep)
        btns.addWidget(button("Stop recording", "stop", "record", on_click=self.w.stop))
        btns.addStretch(1)
        col.addLayout(btns)
        col.addSpacing(24)
        limit = self.w.s.silence_stop
        ends = "Stops by itself when the call ends" if self.w.call else "Press Stop when you're done"
        ends += f", or after {_minutes(limit)} with nobody talking." if limit else "."
        for icon, text in (("headphones", "Recording follows the mic and speaker your call uses, headset included."),
                           ("clock", ends)):
            row = QHBoxLayout()
            row.setSpacing(8)
            row.addStretch(1)
            row.addWidget(icon_label(icon, T["faint"], 16))
            row.addWidget(label(text, "muted"))
            row.addStretch(1)
            col.addLayout(row)
        lay.addLayout(col)
        lay.addStretch(2)

        def update() -> None:
            rec = self.w.recorder
            if rec is None:
                return
            clock.setText(_clock(time.time() - self.w.rec_started))
            you.set(rec.levels["mic"])
            others.set(rec.levels["system"])
            q, lim = rec.silent_for(), self.w.s.silence_stop
            quiet.setText(f"Nobody has spoken for {_clock(q)} · stops in {_clock(max(0, lim - q))}"
                          if lim > 0 and q >= 20 else "")
            keep.setVisible(lim > 0 and q >= lim - 30)

        page.update_recording = update
        update()
        return page

    def _bare(self, rec: library.PendingRecording) -> MeetingData:
        return MeetingData("pending", rec.title, rec.started, rec.duration_s, [], rec.app, "", [], [], [],
                           rec.audio, has_notes=False)

    def _delete_btn(self, rec: library.PendingRecording) -> QPushButton:
        return button("", "trash", "ghost", tip="Delete this recording", icon_color=T["muted"],
                      on_click=lambda: self._delete(rec))

    def _pending_page(self, rec: library.PendingRecording) -> QWidget:
        busy = rec.session in self.w.jobs
        if not busy and rec.ready and not rec.error:
            return self._review_page(rec)
        view = MeetingView(self._bare(rec), self.player, actions=[] if busy else [self._delete_btn(rec)])
        view.state_key = self._state_key(rec.key)
        if busy:
            view.set_callout(self._progress_card(self.w.jobs[rec.session].message))
        elif rec.error:
            view.set_callout(callout(
                "bad", "alert", "Couldn't make notes for this recording",
                f"{rec.error}\nThe recording is safe. Check your internet connection and keys in Settings, "
                "then try again.",
                [button("Settings", "settings", on_click=self.open_settings),
                 button("Try again", "refresh", "primary", on_click=lambda: self.w.process(rec.session))]))
        else:
            view.set_callout(callout(
                "info", "sparkle", "Not turned into notes yet",
                "Make a transcript, summary and to-dos from this recording.",
                [button("Make notes", "sparkle", "primary", on_click=lambda: self.w.process(rec.session))]))
        return view

    def _progress_card(self, message: str) -> QWidget:
        """Saving the recording → Transcribing → Writing notes, with the current step highlighted."""
        m = message.lower()
        step = 0 if ("saving the recording" in m or "starting" in m) else 1 if "transcrib" in m else 2
        c = card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        lay.addWidget(label("Making notes…", "h2"))
        steps = QHBoxLayout()
        steps.setSpacing(20)
        for i, name in enumerate(("Saving the recording", "Transcribing", "Writing summary and to-dos")):
            row = QHBoxLayout()
            row.setSpacing(6)
            if i < step:
                row.addWidget(icon_label("check", T["green"], 16))
                row.addWidget(label(name))
            elif i == step:
                row.addWidget(icon_label("record", T["accent"], 12))
                current = label(name)
                current.setStyleSheet(f"color: {T['accent']}; font-weight: 650;")
                row.addWidget(current)
            else:
                row.addWidget(icon_label("record", T["border_strong"], 12))
                row.addWidget(label(name, "faint"))
            steps.addLayout(row)
        steps.addStretch(1)
        lay.addLayout(steps)
        bar = QProgressBar()
        bar.setRange(0, 0)
        lay.addWidget(bar)
        lay.addWidget(label("This usually takes under a minute. You can close the window; "
                            "you'll get a notification when the notes are ready.", "muted", wrap=True))
        return c

    def _review_page(self, rec: library.PendingRecording) -> QWidget:
        cached = self.views.get(rec.session)
        if cached is not None:
            cached.state_key = self._state_key(rec.key)
            return cached
        meeting = self.w.meeting_for(rec.session)
        if meeting is None:
            view = MeetingView(self._bare(rec), self.player, actions=[self._delete_btn(rec)])
            view.set_callout(callout("bad", "alert", "Couldn't load this recording's notes"))
            return view
        view = MeetingView(from_meeting(meeting, rec.app), self.player, actions=[self._delete_btn(rec)],
                           editable_title=True, search=self.search.text().strip())
        view.state_key = self._state_key(rec.key)
        vault = obsidian.resolve_vault(self.w.s.vault) if self.w.s.vault or obsidian.find_vaults() else None
        bar = SaveBar(meeting, view.title, obsidian.find_vaults(), vault, self.w.s.folder,
                      _load_choices(self.w.s.save), _save_choices)
        view.bottom_slot.addWidget(bar)

        def toggled(item, keep: bool) -> None:
            (meeting.excluded_tasks.discard if keep else meeting.excluded_tasks.add)(item.ref)
            bar.update_counts()

        view.task_toggled.connect(toggled)
        bar.saved.connect(lambda paths: self._saved(rec, paths))
        self.views[rec.session] = view
        self.bars[rec.session] = bar
        return view

    def _saved(self, rec: library.PendingRecording, paths: list[Path]) -> None:
        folder = paths[0].parent if paths else None

        def move_on() -> None:
            if self.player.path == rec.audio:
                self.player.stop()
            view = self.views.pop(rec.session, None)
            self.bars.pop(rec.session, None)
            self.w.finish(rec.session)
            if folder is not None:
                self.banner_for = f"saved:{folder}"
                self.select(self.banner_for)
            if view is not None:
                self.detail.removeWidget(view)
                view.deleteLater()

        QTimer.singleShot(1300, move_on)

    def _saved_page(self, m: library.SavedMeeting) -> QWidget:
        length = None
        if m.audio:
            try:
                length = sf.info(m.audio).duration
            except RuntimeError:
                pass
        main = obsidian.main_note(list(m.files.values()))
        open_btn = button("Open in Obsidian", "external", "primary",
                          on_click=lambda: QDesktopServices.openUrl(QUrl(obsidian.open_uri(m.vault, main))))
        open_btn.setEnabled(main is not None)
        folder_btn = button("", "folder", "ghost", tip="Show the notes' folder", icon_color=T["muted"],
                            on_click=lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(m.folder))))
        view = MeetingView(from_saved(m, length), self.player, actions=[folder_btn, open_btn],
                           search=self.search.text().strip())
        view.state_key = m.key
        if self.banner_for == m.key:
            self.banner_for = None
            view.set_callout(callout("ok", "check", "Saved to Obsidian",
                                     f"{m.vault.name} › {m.folder.relative_to(m.vault)}"))

        def toggled(item, done: bool) -> None:
            try:
                library.set_task_done(item.ref, done)
            except OSError as e:
                QMessageBox.warning(self, "Couldn't update the note", str(e))
            self.refresh()  # updates the sidebar's to-do count; the open meeting stays as is

        view.task_toggled.connect(toggled)
        return view

    def _delete(self, rec: library.PendingRecording) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Delete recording?")
        box.setText(f"Delete “{rec.title}”?")
        box.setInformativeText("The recording and its transcript will be removed. This can't be undone.")
        delete = box.addButton("Delete", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not delete:
            return
        if self.player.path == rec.audio:
            self.player.stop()
        view = self.views.pop(rec.session, None)
        self.bars.pop(rec.session, None)
        if view is not None:
            self.detail.removeWidget(view)
            view.deleteLater()
        self.w.delete(rec.session)

    # ------------------------------------------------------------------ settings + window

    def open_settings(self) -> None:
        from .settings import SettingsDialog
        if SettingsDialog(self.w, self).exec():
            self.refresh(force=True)

    def changeEvent(self, event) -> None:
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            QTimer.singleShot(0, self.refresh)  # pick up changes made in Obsidian meanwhile
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        event.ignore()
        if any(b.busy for b in self.bars.values()):
            return  # let the save finish
        self.player.stop()
        self.hide()
        self.w.window_hidden()
