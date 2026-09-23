"""The Hear Me Out window: every meeting in one place.

Left: recordings still to save, then meetings already in Obsidian (searchable).
Right: the selected meeting. Summary, to-dos (tick them off; it updates the
Obsidian note), team tasks and a transcript you can click to hear. For
recordings not saved yet, the review panel where you choose what to save.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QProgressBar, QPushButton, QSlider, QSplitter, QStackedWidget, QTabWidget, QTextBrowser, QToolButton,
    QVBoxLayout, QWidget,
)

from . import library, obsidian, stt
from .gui import ReviewPanel, _load_choices
from .player import Player

STYLE = """
QWidget#sidebar { background: palette(window); }
QListWidget#meetings { border: none; background: transparent; outline: none; }
QListWidget#meetings::item { border-radius: 8px; margin: 1px 6px; padding: 0; border: 1px solid transparent; }
QListWidget#meetings::item:selected { background: palette(base); border: 1px solid palette(highlight); }
QListWidget#meetings::item:hover:!selected { background: palette(alternate-base); }
QLabel#section { font-size: 11px; font-weight: 700; letter-spacing: 1px; color: palette(placeholder-text);
                 padding: 10px 14px 4px 14px; }
QLabel#rowtitle { font-weight: 600; }
QLabel#rowsub, QLabel#meta, QLabel#hint, QLabel#time { color: palette(placeholder-text); }
QLabel#badge { border-radius: 8px; padding: 1px 7px; font-size: 11px; font-weight: 600; }
QLabel#badge[kind="rec"] { background: rgba(229,72,77,0.18); color: #e5484d; }
QLabel#badge[kind="busy"] { background: rgba(245,165,36,0.20); color: #b27400; }
QLabel#badge[kind="ready"] { background: rgba(62,99,221,0.16); color: #3e63dd; }
QLabel#badge[kind="failed"] { background: rgba(229,72,77,0.18); color: #d13438; }
QLabel#badge[kind="todo"] { background: rgba(48,164,108,0.18); color: #218358; }
QLabel#badge[kind="new"] { background: rgba(139,141,152,0.20); }
QLabel#pagetitle { font-size: 20px; font-weight: 700; }
QLabel#big { font-size: 26px; font-weight: 700; }
QPushButton#record { background: #e5484d; color: white; border: none; border-radius: 6px;
                     padding: 7px 14px; font-weight: 600; }
QPushButton#record:hover { background: #d13438; }
QPushButton#primary { font-weight: 600; padding: 7px 16px; }
QLineEdit#search { padding: 6px 10px; border-radius: 6px; }
QFrame#banner { background: rgba(48,164,108,0.14); border-radius: 6px; }
QFrame#errorbox { background: rgba(229,72,77,0.10); border-radius: 6px; }
"""


def _label(text: str = "", name: str | None = None, wrap: bool = False) -> QLabel:
    label = QLabel(text)
    if name:
        label.setObjectName(name)
    label.setWordWrap(wrap)
    return label


def _mmss(sec: float) -> str:
    return stt.timestamp(sec)


class _Row(QWidget):
    """A meeting in the sidebar: title, when/how long, and a status badge."""

    def __init__(self, title: str, sub: str, badge: str = "", kind: str = ""):
        super().__init__()
        t = _label(title, "rowtitle")
        t.setMinimumWidth(0)
        s = _label(sub, "rowsub")
        text = QVBoxLayout()
        text.setSpacing(1)
        text.addWidget(t)
        text.addWidget(s)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 7, 10, 7)
        row.addLayout(text, 1)
        if badge:
            b = _label(badge, "badge")
            b.setProperty("kind", kind)
            row.addWidget(b, 0, Qt.AlignTop)


class _AudioBar(QWidget):
    """Play / pause and a position slider for one recording."""

    def __init__(self, player: Player, path: Path, duration: float):
        super().__init__()
        self.player, self.path, self.duration = player, path, duration
        self.btn = QPushButton("▶ Play")
        self.btn.setFixedWidth(90)
        self.btn.clicked.connect(self.toggle)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, max(1, int(duration)))
        self.slider.sliderReleased.connect(lambda: self.play_from(self.slider.value()))
        self.time = _label(f"00:00 / {_mmss(duration)}", "time")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.btn)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.time)
        player.position.connect(self._moved)
        player.state.connect(self._state)
        if not Player.available():
            self.setEnabled(False)
            self.btn.setToolTip("No audio player found (pw-play or paplay).")

    def _mine(self) -> bool:
        return self.player.path == self.path

    def toggle(self) -> None:
        if self.player.playing and self._mine():
            self.player.pause()
            self._state(False)
        else:
            start = self.player.offset if self._mine() else 0.0
            self.play_from(start if start < self.duration - 0.5 else 0.0)

    def play_from(self, sec: float) -> None:
        self.player.play(self.path, sec)

    def _moved(self, sec: float) -> None:
        if self._mine() and not self.slider.isSliderDown():
            self.slider.setValue(int(sec))
            self.time.setText(f"{_mmss(sec)} / {_mmss(self.duration)}")

    def _state(self, playing: bool) -> None:
        if self._mine():
            self.btn.setText("⏸ Pause" if playing and self.player.playing else "▶ Play")


class MainWindow(QMainWindow):
    def __init__(self, watcher):
        super().__init__()
        self.w = watcher
        self.player = Player()
        self.panels: dict[Path, ReviewPanel] = {}
        self.items: dict[str, object] = {}
        self.current: str | None = None
        self.banner_for: str | None = None  # show "Saved" banner on this saved meeting once
        self.setWindowTitle("Hear Me Out")
        self.resize(1120, 740)
        self.setStyleSheet(STYLE)

        # --- top bar
        self.rec_btn = QPushButton("● Record")
        self.rec_btn.setObjectName("record")
        self.rec_btn.clicked.connect(lambda: self.w.stop() if self.w.recorder else self.w.start())
        self.status = _label("", "meta")
        self.search = QLineEdit()
        self.search.setObjectName("search")
        self.search.setPlaceholderText("Search meetings, transcripts and tasks")
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(380)
        self.search.textChanged.connect(self.refresh)
        gear = QToolButton()
        gear.setText("Settings")
        gear.setToolTip("Meeting detection, start at login, settings file, quit")
        gear.setPopupMode(QToolButton.InstantPopup)
        self.gear_menu = QMenu(gear)
        self.gear_menu.aboutToShow.connect(lambda: (self.gear_menu.clear(), self.w.fill_settings_menu(self.gear_menu)))
        gear.setMenu(self.gear_menu)
        top = QHBoxLayout()
        top.setContentsMargins(14, 10, 14, 10)
        top.addWidget(self.rec_btn)
        top.addSpacing(8)
        top.addWidget(self.status)
        top.addStretch(1)
        top.addWidget(self.search, 1)
        top.addWidget(gear)

        # --- sidebar
        self.list = QListWidget()
        self.list.setObjectName("meetings")
        self.list.currentItemChanged.connect(lambda cur, _: self._select_item(cur))
        side = QWidget()
        side.setObjectName("sidebar")
        side_l = QVBoxLayout(side)
        side_l.setContentsMargins(0, 0, 0, 8)
        side_l.addWidget(self.list)
        side.setMinimumWidth(280)

        # --- detail
        self.detail = QStackedWidget()
        split = QSplitter()
        split.addWidget(side)
        split.addWidget(self.detail)
        split.setStretchFactor(1, 1)
        split.setSizes([320, 800])
        split.setChildrenCollapsible(False)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        root = QWidget()
        lay = QVBoxLayout(root)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addLayout(top)
        lay.addWidget(line)
        lay.addWidget(split, 1)
        self.setCentralWidget(root)

        QShortcut(QKeySequence.Find, self, self.search.setFocus)
        QShortcut(QKeySequence("Ctrl+R"), self, self.rec_btn.click)
        self.w.changed.connect(self.refresh)
        self.clock = QTimer(self)
        self.clock.timeout.connect(self._tick)
        self.clock.start(1000)
        self.refresh()

    # ------------------------------------------------------------------ list

    def refresh(self) -> None:
        """Rebuild the sidebar from what's on disk, keeping the selection."""
        query = self.search.text().strip()
        self.items.clear()
        self.list.blockSignals(True)
        self.list.clear()

        def section(text: str) -> None:
            item = QListWidgetItem(text.upper(), self.list)
            item.setFlags(Qt.ItemIsEnabled)  # shown, but not selectable
            f = item.font()
            f.setBold(True)
            f.setPointSizeF(f.pointSizeF() * 0.8)
            f.setLetterSpacing(QFont.PercentageSpacing, 108)
            item.setFont(f)
            item.setForeground(self.palette().placeholderText())
            item.setSizeHint(item.sizeHint().expandedTo(QSize(0, 34)))
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignBottom)

        def add(key: str, obj, row: _Row) -> None:
            item = QListWidgetItem(self.list)
            item.setData(Qt.UserRole, key)
            item.setSizeHint(row.sizeHint())
            self.list.setItemWidget(item, row)
            self.items[key] = obj

        pending = [p for p in library.pending_recordings()
                   if not query or query.lower() in p.title.lower()]
        if self.w.recorder is not None or pending:
            section("To save")
        if self.w.recorder is not None:
            app = self.w.call.app if self.w.call else "Started by hand"
            add("recording", None, _Row("Recording now", app, "● REC", "rec"))
        for p in pending:
            sub = " · ".join(x for x in (library.when(p.started), f"{max(1, round(p.duration_s / 60))} min", p.app) if x)
            if p.session in self.w.jobs:
                badge, kind = "Making notes", "busy"
            elif p.error:
                badge, kind = "Failed", "failed"
            elif p.ready:
                badge, kind = "Ready to save", "ready"
            else:
                badge, kind = "Not processed", "new"
            add(p.key, p, _Row(p.title, sub, badge, kind))

        self.saved = library.saved_meetings(self.w.s)
        shown = [m for m in self.saved if not query or m.matches(query)]
        if shown:
            section("Saved in Obsidian" if not query else f"Saved in Obsidian: {len(shown)} found")
        for m in shown:
            todos = m.open_todos()
            add(m.key, m, _Row(m.title, f"{library.when(m.started)} · {max(1, m.duration_min)} min",
                               f"{todos} to-do{'s' if todos != 1 else ''}" if todos else "", "todo"))
        if query and not shown and not pending:
            section("Nothing found")

        self.list.blockSignals(False)
        keys = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]
        target = self.current if self.current in keys else next((k for k in keys if k), None)
        if target:
            self._set_current(target, rebuild=target != self.current or self._needs_rebuild(target))
        else:
            self.current = None
            self._show(self._empty_page(bool(query)))
        self._tick()

    def _needs_rebuild(self, key: str) -> bool:
        """Status pages must change when the recording's state changes."""
        page = self.detail.currentWidget()
        return getattr(page, "state_key", None) != self._state_key(key)

    def _state_key(self, key: str) -> str:
        obj = self.items.get(key)
        if isinstance(obj, library.PendingRecording):
            s = obj.session
            state = ("busy:" + self.w.jobs[s].message if s in self.w.jobs else "failed" if obj.error
                     else "ready" if obj.ready else "new")
            return f"{key}|{state}"
        return key if key != "recording" else "recording"

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
        elif self.current:  # clicked a heading: keep the current meeting selected
            self._set_current(self.current, rebuild=False)

    def _show(self, page: QWidget) -> None:
        old = self.detail.currentWidget()
        self.detail.addWidget(page)
        self.detail.setCurrentWidget(page)
        if old is not None and old is not page:
            for panel in self.panels.values():  # keep review panels (and your edits in them) alive
                if old.isAncestorOf(panel):
                    panel.setParent(None)
            self.detail.removeWidget(old)
            old.deleteLater()

    def _tick(self) -> None:
        recording = self.w.recorder is not None
        self.rec_btn.setText("■ Stop recording" if recording else "● Record")
        self.status.setText(self.w.status_text())
        page = self.detail.currentWidget()
        if recording and getattr(page, "clock", None) is not None:
            page.clock.setText(self.w.status_text().split("·")[-1].strip())

    # ------------------------------------------------------------------ pages

    def _page(self, state_key: str = "") -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        page.state_key = state_key
        lay = QVBoxLayout(page)
        lay.setContentsMargins(24, 18, 24, 18)
        return page, lay

    def _page_for(self, key: str) -> QWidget:
        obj = self.items.get(key)
        if key == "recording":
            return self._recording_page()
        if isinstance(obj, library.PendingRecording):
            return self._pending_page(obj)
        if isinstance(obj, library.SavedMeeting):
            return self._saved_page(obj)
        return self._empty_page(False)

    def _empty_page(self, searching: bool) -> QWidget:
        page, lay = self._page()
        lay.addStretch(1)
        if searching:
            lay.addWidget(_label("No meetings match your search.", "big"), 0, Qt.AlignCenter)
        else:
            lay.addWidget(_label("No meetings yet", "big"), 0, Qt.AlignCenter)
            hint = _label("Join a call in Zoom, Teams, Meet, Slack or Discord and Hear Me Out will offer to "
                          "record it. Or press Record to start now.\n\nWhen the meeting ends you choose "
                          "what to save to Obsidian: summary, your to-dos, team tasks and the transcript.",
                          "hint", wrap=True)
            hint.setAlignment(Qt.AlignCenter)
            hint.setMaximumWidth(520)
            lay.addWidget(hint, 0, Qt.AlignCenter)
        lay.addStretch(2)
        return page

    def _recording_page(self) -> QWidget:
        page, lay = self._page("recording")
        what = self.w.call.app if self.w.call else "meeting"
        lay.addStretch(1)
        big = _label(f'<span style="color:#e5484d">●</span> Recording {html.escape(what)}', "big")
        lay.addWidget(big, 0, Qt.AlignCenter)
        page.clock = _label("00:00", "pagetitle")
        lay.addWidget(page.clock, 0, Qt.AlignCenter)
        hint = _label("Recording stops by itself when the call ends." if self.w.call else
                      "Press Stop when the meeting is over.", "hint")
        lay.addWidget(hint, 0, Qt.AlignCenter)
        stop = QPushButton("■ Stop recording")
        stop.setObjectName("record")
        stop.clicked.connect(self.w.stop)
        lay.addSpacing(10)
        lay.addWidget(stop, 0, Qt.AlignCenter)
        lay.addStretch(2)
        return page

    def _header(self, lay: QVBoxLayout, title: str, meta: str, buttons: list[QPushButton]) -> None:
        row = QHBoxLayout()
        text = QVBoxLayout()
        text.addWidget(_label(title, "pagetitle", wrap=True))
        text.addWidget(_label(meta, "meta"))
        row.addLayout(text, 1)
        for b in buttons:
            row.addWidget(b, 0, Qt.AlignTop)
        lay.addLayout(row)

    def _delete_button(self, rec: library.PendingRecording) -> QPushButton:
        btn = QPushButton("Delete recording")
        btn.clicked.connect(lambda: self._delete(rec))
        return btn

    def _delete(self, rec: library.PendingRecording) -> None:
        answer = QMessageBox.question(self, "Delete recording?",
                                      f"Delete the recording “{rec.title}” and its transcript? "
                                      "This can't be undone.", QMessageBox.Delete | QMessageBox.Cancel)
        if answer == QMessageBox.Delete:
            if self.player.path == rec.audio:
                self.player.stop()
            panel = self.panels.pop(rec.session, None)
            if panel is not None:
                self.detail.removeWidget(panel)
                panel.deleteLater()
            self.w.delete(rec.session)

    def _pending_page(self, rec: library.PendingRecording) -> QWidget:
        busy = rec.session in self.w.jobs
        if not busy and rec.ready and not rec.error:
            return self._review_page(rec)
        page, lay = self._page(self._state_key(rec.key))
        meta = " · ".join(x for x in (f"{rec.started:%A %-d %B %Y · %H:%M}",
                                      f"{max(1, round(rec.duration_s / 60))} min", rec.app) if x)
        self._header(lay, rec.title, meta, [] if busy else [self._delete_button(rec)])
        lay.addSpacing(8)
        if rec.audio.exists() and not busy:
            lay.addWidget(_AudioBar(self.player, rec.audio, rec.duration_s))
        lay.addSpacing(12)
        if not rec.error or busy:
            lay.addStretch(1)
        if busy:
            lay.addWidget(_label("Making notes…", "big"), 0, Qt.AlignCenter)
            lay.addWidget(_label(self.w.jobs[rec.session].message, "hint"), 0, Qt.AlignCenter)
            bar = QProgressBar()
            bar.setRange(0, 0)  # we can't know how long the services take
            bar.setMaximumWidth(360)
            lay.addWidget(bar, 0, Qt.AlignCenter)
        elif rec.error:
            box = QFrame()
            box.setObjectName("errorbox")
            b = QVBoxLayout(box)
            b.addWidget(_label("Couldn't make notes for this recording", "rowtitle"))
            b.addWidget(_label(rec.error, "hint", wrap=True))
            b.addWidget(_label("The recording is safe. Check your internet connection and API keys "
                               "(hearmeout doctor), then try again.", "hint", wrap=True))
            lay.addWidget(box)
            retry = QPushButton("Try again")
            retry.setObjectName("primary")
            retry.clicked.connect(lambda: self.w.process(rec.session))
            lay.addWidget(retry, 0, Qt.AlignLeft)
            lay.addStretch(1)
        else:
            lay.addWidget(_label("This recording hasn't been turned into notes yet.", "hint"), 0, Qt.AlignCenter)
            go = QPushButton("Make notes")
            go.setObjectName("primary")
            go.clicked.connect(lambda: self.w.process(rec.session))
            lay.addWidget(go, 0, Qt.AlignCenter)
        lay.addStretch(2)
        return page

    def _review_page(self, rec: library.PendingRecording) -> QWidget:
        meeting = self.w.meeting_for(rec.session)
        if meeting is None:
            page, lay = self._page(self._state_key(rec.key))
            lay.addWidget(_label("Couldn't load this recording's notes.", "big"), 0, Qt.AlignCenter)
            return page
        page, lay = self._page(self._state_key(rec.key))
        top = QHBoxLayout()
        top.addWidget(_label("Ready to save: choose what goes into Obsidian", "rowtitle"), 1)
        top.addWidget(self._delete_button(rec))
        lay.addLayout(top)
        if rec.audio.exists():
            lay.addWidget(_AudioBar(self.player, rec.audio, rec.duration_s))
        panel = self.panels.get(rec.session)
        if panel is None:
            vault = obsidian.resolve_vault(self.w.s.vault) if self.w.s.vault or obsidian.find_vaults() else None
            panel = ReviewPanel(meeting, obsidian.find_vaults(), vault, self.w.s.folder,
                                _load_choices(self.w.s.save), embedded=True)
            panel.saved_paths.connect(lambda paths, r=rec: self._saved(r, paths))
            self.panels[rec.session] = panel
        lay.addWidget(panel, 1)
        return page

    def _saved(self, rec: library.PendingRecording, paths: list[Path]) -> None:
        """Saving finished: show the result for a moment, then switch to the saved meeting."""
        folder = paths[0].parent if paths else None

        def move_on() -> None:
            panel = self.panels.pop(rec.session, None)
            if self.player.path == rec.audio:
                self.player.stop()
            self.w.finish(rec.session)
            if panel is not None:
                self.detail.removeWidget(panel)
                panel.deleteLater()
            if folder is not None:
                self.banner_for = f"saved:{folder}"
                self.select(self.banner_for)

        QTimer.singleShot(1200, move_on)

    def _saved_page(self, m: library.SavedMeeting) -> QWidget:
        page, lay = self._page(m.key)
        open_btn = QPushButton("Open in Obsidian")
        open_btn.setObjectName("primary")
        main = obsidian.main_note(list(m.files.values()))
        open_btn.setEnabled(main is not None)
        open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(obsidian.open_uri(m.vault, main))))
        folder_btn = QPushButton("Show folder")
        folder_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(m.folder))))
        meta = " · ".join(x for x in (f"{m.started:%A %-d %B %Y · %H:%M}", f"{max(1, m.duration_min)} min",
                                      ", ".join(m.participants)) if x)
        self._header(lay, m.title, meta, [folder_btn, open_btn])

        if self.banner_for == m.key:
            self.banner_for = None
            banner = QFrame()
            banner.setObjectName("banner")
            b = QHBoxLayout(banner)
            b.addWidget(_label(f"✓ Saved to Obsidian: {m.folder.relative_to(m.vault)}/", "rowtitle"))
            lay.addWidget(banner)

        if m.audio:
            lay.addWidget(_AudioBar(self.player, m.audio, m.duration_min * 60 or 1))

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        if "summary" in m.files:
            view = QTextBrowser()
            view.setMarkdown(_strip_links(re.sub(r"^# .*\n+", "", m.text("summary"))))
            tabs.addTab(view, "Summary")
        for key, label in (("my_todos", "My to-dos"), ("team_tasks", "Team tasks")):
            if key in m.files:
                tasks = m.tasks(key)
                open_count = sum(not t.done for t in tasks)
                tabs.addTab(self._task_view(m, key, tasks), f"{label} ({open_count})" if tasks else label)
        if "transcript" in m.files:
            tabs.addTab(self._transcript_view(m), "Transcript")
        if tabs.count() == 0:
            lay.addWidget(_label("Only the audio was saved for this meeting.", "hint"))
        lay.addSpacing(6)
        lay.addWidget(tabs, 1)
        return page

    def _task_view(self, m: library.SavedMeeting, key: str, tasks: list[library.Task]) -> QWidget:
        box = QWidget()
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 8, 0, 0)
        hint = _label("Tick a task when it's done. The note in Obsidian is updated too. "
                      "Hover a task to see where it was said.", "hint", wrap=True)
        lay.addWidget(hint)
        lst = QListWidget()
        for t in tasks:
            item = QListWidgetItem(_task_text(t.text))
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked if t.done else Qt.Unchecked)
            item.setToolTip(f"“{t.quote}”" if t.quote else "")
            item.setData(Qt.UserRole, t)
            f = item.font()
            f.setStrikeOut(t.done)
            item.setFont(f)
            lst.addItem(item)
        if not tasks:
            empty = QListWidgetItem("No tasks from this meeting.")
            empty.setFlags(Qt.NoItemFlags)
            lst.addItem(empty)

        def toggled(item: QListWidgetItem) -> None:
            t: library.Task | None = item.data(Qt.UserRole)
            if t is None:
                return
            done = item.checkState() == Qt.Checked
            try:
                library.set_task_done(t, done)
            except OSError as e:
                QMessageBox.warning(self, "Couldn't update the note", str(e))
                return
            f = item.font()
            f.setStrikeOut(done)
            lst.blockSignals(True)
            item.setFont(f)
            lst.blockSignals(False)

        lst.itemChanged.connect(toggled)
        lay.addWidget(lst, 1)
        return box

    def _transcript_view(self, m: library.SavedMeeting) -> QWidget:
        view = QTextBrowser()
        view.setOpenLinks(False)
        rows = m.transcript()
        can_play = m.audio is not None and Player.available()
        parts = []
        for speaker, start, text in rows:
            ts = _mmss(start)
            stamp = f'<a href="t:{start}">{ts}</a>' if can_play else ts
            parts.append(f'<p style="margin:0 0 10px 0"><b>{html.escape(speaker)}</b> '
                         f'<span style="color:gray">{stamp}</span><br>{html.escape(text)}</p>')
        view.setHtml("".join(parts) or "<p>No transcript.</p>")
        if can_play:
            view.setToolTip("Click a time to hear that moment.")
            view.anchorClicked.connect(lambda url: self.player.play(m.audio, float(url.toString()[2:])))
        return view

    # ------------------------------------------------------------------ window behaviour

    def changeEvent(self, event) -> None:
        # Coming back to the window: pick up changes made in Obsidian meanwhile.
        if event.type() == QEvent.ActivationChange and self.isActiveWindow():
            QTimer.singleShot(0, self.refresh)
        super().changeEvent(event)

    def closeEvent(self, event) -> None:
        busy = any(p.busy for p in self.panels.values())
        if busy:
            event.ignore()  # let the save finish
            return
        self.player.stop()
        self.hide()
        event.ignore()
        self.w.window_hidden()


def _strip_links(md: str) -> str:
    """[[path|Label]] -> Label, for display outside Obsidian."""
    return re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", md)


def _task_text(md: str) -> str:
    """'**Ama**: Review the PR 📅 2026-09-24' -> 'Ama: Review the PR  ·  due Thu 24 Sep'."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", md)
    m = re.search(r"\s*📅\s*(\d{4}-\d{2}-\d{2})", text)
    if m:
        from datetime import date
        try:
            due = date.fromisoformat(m.group(1)).strftime("%a %-d %b")
        except ValueError:
            due = m.group(1)
        text = text[:m.start()] + f"  ·  due {due}" + text[m.end():]
    return text.strip()
