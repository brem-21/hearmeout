"""The Hear Me Out window.

Sidebar: Record, search, recordings still to save, then saved meetings by day.
Main area: the selected meeting (summary, to-dos, team tasks, transcript), with a
"Save to Obsidian" bar for new recordings, and a banner while recording.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path

import soundfile as sf
from PySide6.QtCore import QEvent, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from . import agenda, library, obsidian, outlook, theme, usageview
from .gui import _load_choices, _save_choices
from .player import Player
from .settings import SettingsPage
from .theme import T
from .views import (
    ClickableCard, ElidedLabel, MeetingData, MeetingView, SaveBar, badge, button, callout, card, chip, divider,
    friendly_due, from_meeting, from_saved, icon_label, label,
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


def _split_task(md: str) -> tuple[str, date | None]:
    """'Finish the manifest 📅 2026-09-25' -> ('Finish the manifest', date(2026, 9, 25))."""
    import re
    m = re.search(r"\s*📅\s*(\d{4}-\d{2}-\d{2})", md)
    if not m:
        return md.strip(), None
    try:
        due = date.fromisoformat(m.group(1))
    except ValueError:
        due = None
    return (md[:m.start()] + md[m.end():]).strip(), due


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
        self.weeks = agenda.Weeks()  # which week Home's calendar shows
        self.weeks.changed.connect(lambda: self.refresh())
        self.todo_view = {"by": "date", "done": False, "team": True}
        self.setWindowTitle("Hear Me Out")
        self.resize(1180, 760)
        self.setMinimumSize(860, 560)
        self._build()
        QShortcut(QKeySequence.Find, self, lambda: self.search.setFocus())
        QShortcut(QKeySequence("Ctrl+R"), self, lambda: self.rec_btn.click())
        QShortcut(QKeySequence("Ctrl+,"), self, self.open_settings)
        QShortcut(QKeySequence("Esc"), self, self.go_home)
        QShortcut(QKeySequence("Alt+Home"), self, self.go_home)
        self.w.changed.connect(self.refresh)
        self.w.balances_changed.connect(self._balances_changed)
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
        self.home_btn = button("Home", "home", "nav", tip="Home (Esc)", icon_color=T["muted"],
                               on_click=self.go_home)
        self.home_btn.setCheckable(True)
        s.addWidget(self.home_btn)
        self.todos_btn = button("To-dos", "tasks", "nav", tip="Your to-dos and team tasks, by date or by meeting",
                                icon_color=T["muted"], on_click=lambda: self.select("todos"))
        self.todos_btn.setCheckable(True)
        s.addWidget(self.todos_btn)
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
        self.list.setSelectionMode(QListWidget.ExtendedSelection)  # Ctrl/Shift-click to pick several
        self.list.currentItemChanged.connect(lambda cur, _: self._select_item(cur))
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.list.customContextMenuRequested.connect(self._context_menu)
        QShortcut(QKeySequence.Delete, self.list, self._delete_selected, context=Qt.WidgetShortcut)
        s.addWidget(self.list, 1)

        # Light | Dark: always your choice, whatever the system theme is
        seg = QHBoxLayout()
        seg.setSpacing(0)
        self.theme_btns = {}
        dark = theme.is_dark()
        for mode, text, icon in (("light", "Light", "sun"), ("dark", "Dark", "moon")):
            on = (mode == "dark") == dark
            b = button(text, icon, variant=f"seg-{'left' if mode == 'light' else 'right'}",
                       icon_color=T["accent"] if on else T["muted"],
                       tip=f"Use the {text.lower()} theme (Settings › Appearance has “Match system”)",
                       on_click=lambda _=False, m=mode: self.set_theme(m))
            b.setCheckable(True)
            b.setChecked(on)
            self.theme_btns[mode] = b
            seg.addWidget(b, 1)
        s.addLayout(seg)
        s.addWidget(divider())
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.status_dot = icon_label("record", T["green"], 9)
        foot.addWidget(self.status_dot)
        self.status = ElidedLabel("", "muted")
        foot.addWidget(self.status, 1)
        gear = button("", "settings", "ghost", tip="Settings (Ctrl+,)", icon_color=T["muted"],
                      on_click=self.open_settings)
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

    def set_theme(self, mode: str) -> None:
        """Light or dark (or "system"), saved and applied straight away."""
        from . import config
        s = config.load()
        s.appearance = mode
        config.save(s)
        theme.apply(QApplication.instance(), mode)
        self.retheme()

    def _toggle_theme(self) -> None:
        self.set_theme("light" if theme.is_dark() else "dark")

    def retheme(self) -> None:
        """Light/dark changed: rebuild everything with the new colours."""
        if any(b.busy for b in self.bars.values()):
            return
        if isinstance(self.detail.currentWidget(), SettingsPage) and self.detail.currentWidget().dirty:
            self.detail.currentWidget().dirty = False  # keep it simple: unsaved edits there are dropped
        cur, query = self.current, self.search.text()
        self.views.clear()
        self.bars.clear()
        self._home_widget = None  # its colours are the old theme's
        self.takeCentralWidget().deleteLater()
        self._build()
        self.search.setText(query)
        self.current = cur
        self.refresh(force=True)

    # ------------------------------------------------------------------ sidebar

    def refresh(self, force: bool = False) -> None:
        query = self.search.text().strip()
        # Read the recordings and the vault once per refresh; the pages below reuse these.
        self.pending = library.pending_recordings()
        self.saved = library.saved_meetings(self.w.s)

        rows: list[tuple] = []  # ("section", text) or (key, obj, title, sub, badge, kind, dot)
        pending = [p for p in self.pending if not query or query.lower() in p.title.lower()]
        if self.w.recorder is not None or pending:
            rows.append(("section", "To save"))
        if self.w.recorder is not None:
            rows.append(("recording", None, "Recording now", self.w.call.app if self.w.call else "Started by you",
                         "REC", "rec", T["red"]))
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
            rows.append((p.key, p, p.title, sub, *b, None))
        shown = [m for m in self.saved if not query or m.matches(query)]
        day = None
        for m in shown:
            if m.started.date() != day:
                day = m.started.date()
                rows.append(("section", _day(day)))
            todos = m.open_todos()
            rows.append((m.key, m, m.title, f"{m.started:%H:%M} · {max(1, m.duration_min)} min",
                         f"{todos} to-do{'s' if todos != 1 else ''}" if todos else "", "ok", None))
        if query and not shown and not pending:
            rows.append(("section", "No matches"))

        # Rebuilding the list is the slow part, so only do it when something in it changed.
        signature = [r if r[0] == "section" else (r[0], *r[2:]) for r in rows]
        if force or signature != getattr(self, "_list_signature", None):
            self._list_signature = signature
            self.items.clear()
            self.list.blockSignals(True)
            scroll = self.list.verticalScrollBar().value()
            self.list.clear()
            for r in rows:
                item = QListWidgetItem(self.list)
                if r[0] == "section":
                    item.setFlags(Qt.ItemIsEnabled)
                    w = label(r[1].upper(), "section")
                    w.setContentsMargins(12, 12, 0, 2)
                    item.setSizeHint(QSize(0, 32))
                    self.list.setItemWidget(item, w)
                else:
                    key, obj, title, sub, badge_text, kind, dot = r
                    item.setData(Qt.UserRole, key)
                    item.setSizeHint(QSize(0, 56))
                    self.list.setItemWidget(item, _Row(title, sub, badge_text, kind, dot=dot))
                    self.items[key] = obj
            self.list.verticalScrollBar().setValue(scroll)
            self.list.blockSignals(False)
        else:
            for r in rows:  # same rows: just point at the fresh objects
                if r[0] != "section":
                    self.items[r[0]] = r[1]

        keys = [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]
        if self.current == "settings":
            if force or not isinstance(self.detail.currentWidget(), SettingsPage):
                self._show(self._settings_page())
            self._tick()
            return
        if self.current == "todos":
            self.list.blockSignals(True)
            self.list.clearSelection()
            self.list.setCurrentRow(-1)
            self.list.blockSignals(False)
            self.show_todos(force)
            self._tick()
            return
        target = self.current if self.current and self.current in keys else "home"
        if target == "home":
            self.current = "home"
            self.list.blockSignals(True)
            self.list.clearSelection()
            self.list.setCurrentRow(-1)
            self.list.blockSignals(False)
            self.w.check_balances()  # at most every 5 minutes, in the background
            key = self._home_key()
            if force or getattr(self.detail.currentWidget(), "state_key", None) != key:
                # Home is kept once built: coming back to it is instant unless something on it changed.
                cached = getattr(self, "_home_widget", None)
                if cached is not None and not force and cached.state_key == key:
                    self._show(cached)
                else:
                    page = self._home_page()
                    self._show(page)
                    if cached is not None and cached is not page:
                        self.detail.removeWidget(cached)
                        cached.deleteLater()
                    self._home_widget = page
        else:
            self._set_current(target, rebuild=force or target != self.current or self._stale(target))
        self._tick()

    def _balances_changed(self) -> None:
        btn = getattr(self, "_usage_btn", None)
        try:
            if btn is not None:
                btn.update_text()
        except RuntimeError:  # that Home page was replaced
            pass

    def show_todos(self, force: bool = False) -> None:
        """The To-dos page (rebuilt when a to-do or the view changes)."""
        if not hasattr(self, "saved"):
            self.saved = library.saved_meetings(self.w.s)
        if force or getattr(self.detail.currentWidget(), "state_key", None) != agenda.todos_key(self):
            self._show(agenda.todos_page(self))

    def go_home(self) -> None:
        if self.current == "home" or not self._leave_settings():
            return
        self.current = "home"
        self.refresh()

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
        if key != self.current and not self._leave_settings():
            return
        self.current = key
        self.refresh()

    def _leave_settings(self) -> bool:
        """Leaving the Settings page: offer to save unsaved changes. False = stay."""
        page = self.detail.currentWidget()
        if not isinstance(page, SettingsPage) or not page.dirty:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved settings")
        box.setText("Save your changes to the settings?")
        save = box.addButton("Save", QMessageBox.AcceptRole)
        discard = box.addButton("Discard", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is save:
            page.save()
            return True
        return box.clickedButton() is discard

    def _set_current(self, key: str, rebuild: bool = True) -> None:
        if key != self.current and not self._leave_settings():
            self.list.blockSignals(True)
            self.list.clearSelection()
            self.list.blockSignals(False)
            return
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
        if len(self._selected_keys()) > 1:
            return  # several picked: the selection page is showing
        if item is not None and item.data(Qt.UserRole):
            self._set_current(item.data(Qt.UserRole))
        elif self.current and self.current != "settings":  # a day heading: keep the meeting selected
            self._set_current(self.current, rebuild=False)

    # ------------------------------------------------------------------ selecting several + deleting

    def _selected_keys(self) -> list[str]:
        return [i.data(Qt.UserRole) for i in self.list.selectedItems()
                if i.data(Qt.UserRole) and i.data(Qt.UserRole) != "recording"]

    def _selection_changed(self) -> None:
        keys = self._selected_keys()
        if len(keys) > 1:
            self._show(self._selection_page(keys))
        elif len(keys) == 1 and keys[0] != self.current:
            self._set_current(keys[0])
        elif len(keys) == 1 and getattr(self.detail.currentWidget(), "state_key", "").startswith("selection"):
            self._set_current(keys[0], rebuild=True)

    def _context_menu(self, pos) -> None:
        item = self.list.itemAt(pos)
        if item is None or not item.data(Qt.UserRole) or item.data(Qt.UserRole) == "recording":
            return
        if not item.isSelected():
            self.list.clearSelection()
            item.setSelected(True)
        keys = self._selected_keys()
        menu = QMenu(self)
        if len(keys) == 1:
            obj = self.items.get(keys[0])
            if isinstance(obj, library.SavedMeeting):
                main = obsidian.main_note(list(obj.files.values()))
                if main:
                    menu.addAction(theme.icon("external", T["text"], 16), "Open in Obsidian",
                                   lambda: QDesktopServices.openUrl(QUrl(obsidian.open_uri(obj.vault, main))))
                menu.addAction(theme.icon("folder", T["text"], 16), "Show folder",
                               lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(obj.folder))))
            elif isinstance(obj, library.PendingRecording) and obj.session in self.w.jobs:
                menu.addAction(theme.icon("stop", T["text"], 16), "Stop making notes",
                               lambda: self.w.stop_notes(obj.session))
            elif isinstance(obj, library.PendingRecording) and not obj.ready:
                menu.addAction(theme.icon("sparkle", T["text"], 16), "Make notes",
                               lambda: self.w.process(obj.session))
            menu.addSeparator()
        pending = [k for k in self.items if k.startswith("pending:")]
        if pending:
            menu.addAction("Select all recordings to save", lambda: self._select_keys(pending))
        menu.addAction("Select everything", lambda: self._select_keys(list(self.items)))
        menu.addSeparator()
        menu.addAction(theme.icon("trash", T["red"], 16),
                       f"Move {len(keys)} to Trash…" if len(keys) > 1 else "Move to Trash…", self._delete_selected)
        menu.exec(self.list.viewport().mapToGlobal(pos))

    def _select_keys(self, keys: list[str]) -> None:
        self.list.blockSignals(True)
        self.list.clearSelection()
        for i in range(self.list.count()):
            if self.list.item(i).data(Qt.UserRole) in keys and self.list.item(i).data(Qt.UserRole) != "recording":
                self.list.item(i).setSelected(True)
        self.list.blockSignals(False)
        self._selection_changed()

    def _selection_page(self, keys: list[str]) -> QWidget:
        page, lay = self._page("selection:" + ",".join(keys))
        pending = [self.items[k] for k in keys if isinstance(self.items.get(k), library.PendingRecording)]
        saved = [self.items[k] for k in keys if isinstance(self.items.get(k), library.SavedMeeting)]
        lay.addStretch(1)
        col = QVBoxLayout()
        col.setSpacing(10)
        col.addWidget(icon_label("tasks", T["accent"], 40), 0, Qt.AlignHCenter)
        col.addWidget(label(f"{len(keys)} selected", "h1"), 0, Qt.AlignHCenter)
        parts = []
        if pending:
            parts.append(f"{len(pending)} recording{'s' if len(pending) != 1 else ''} not saved yet")
        if saved:
            parts.append(f"{len(saved)} meeting{'s' if len(saved) != 1 else ''} saved in Obsidian")
        col.addWidget(label(" and ".join(parts), "muted"), 0, Qt.AlignHCenter)
        box = card()
        box.setFixedWidth(480)
        bl = QVBoxLayout(box)
        bl.setContentsMargins(16, 12, 16, 12)
        bl.setSpacing(6)
        for obj in (pending + saved)[:8]:
            row = QHBoxLayout()
            row.addWidget(icon_label("mic" if isinstance(obj, library.PendingRecording) else "gem", T["muted"], 14))
            row.addWidget(ElidedLabel(obj.title), 1)
            row.addWidget(label(library.when(obj.started), "muted"))
            bl.addLayout(row)
        if len(keys) > 8:
            bl.addWidget(label(f"…and {len(keys) - 8} more", "muted"))
        col.addWidget(box, 0, Qt.AlignHCenter)
        col.addSpacing(8)
        btns = QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(button("Cancel", on_click=self._clear_selection))
        btns.addWidget(button(f"Move {len(keys)} to Trash", "trash", "danger", on_click=self._delete_selected))
        btns.addStretch(1)
        col.addLayout(btns)
        col.addWidget(label("You can restore them from your system's Trash.", "faint"), 0, Qt.AlignHCenter)
        lay.addLayout(col)
        lay.addStretch(2)
        return page

    def _clear_selection(self) -> None:
        cur = self.current
        self.list.clearSelection()
        if cur:
            self._set_current(cur, rebuild=True)

    def _delete_selected(self) -> None:
        keys = self._selected_keys() or ([self.current] if self.current and self.current not in
                                         ("recording", "settings") else [])
        objs = [self.items[k] for k in keys if k in self.items]
        busy = [o for o in objs if isinstance(o, library.PendingRecording) and o.session in self.w.jobs]
        objs = [o for o in objs if o not in busy]
        if not objs:
            if busy:
                QMessageBox.information(self, "Still making notes",
                                        "That recording is being turned into notes. Try again when it's done.")
            return
        saved = [o for o in objs if isinstance(o, library.SavedMeeting)]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("Move to Trash?")
        if len(objs) == 1:
            box.setText(f"Move “{objs[0].title}” to the Trash?")
        else:
            box.setText(f"Move {len(objs)} items to the Trash?")
        info = []
        if saved:
            info.append("Their notes will be removed from your Obsidian vault." if len(saved) > 1 or len(objs) > 1
                        else "Its notes will be removed from your Obsidian vault.")
        info.append("You can restore anything from your system's Trash.")
        if busy:
            info.append(f"{len(busy)} still being turned into notes will be left alone.")
        box.setInformativeText(" ".join(info))
        go = box.addButton("Move to Trash", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        if box.clickedButton() is not go:
            return
        from .watch import move_to_trash
        failed = []
        for o in objs:
            if isinstance(o, library.PendingRecording):
                if self.player.path == o.audio:
                    self.player.stop()
                view = self.views.pop(o.session, None)
                self.bars.pop(o.session, None)
                if view is not None:
                    self.detail.removeWidget(view)
                    view.deleteLater()
                if not self.w.delete(o.session):
                    failed.append(o.title)
            else:
                if self.player.path == o.audio:
                    self.player.stop()
                if not move_to_trash(o.folder):
                    failed.append(o.title)
        if failed:
            QMessageBox.warning(self, "Couldn't move to Trash",
                                "These couldn't be moved to the Trash:\n\n" + "\n".join(failed))
        self.list.clearSelection()
        self.current = None
        self.refresh()

    def _show(self, page: QWidget) -> None:
        old = self.detail.currentWidget()
        if page is old:
            return
        if self.detail.indexOf(page) < 0:
            self.detail.addWidget(page)
        self.detail.setCurrentWidget(page)
        if old is not None and old not in self.views.values() and old is not getattr(self, "_home_widget", None):
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
        self.home_btn.setChecked(self.current == "home")
        self.todos_btn.setChecked(self.current == "todos")
        n = sum(m.open_todos(team=self.todo_view["team"]) for m in getattr(self, "saved", []))
        self.todos_btn.setText(f"To-dos  ·  {n}" if n else "To-dos")
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
        if key == "home":
            return self._home_page()
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
        return self._home_page()

    def _recent(self) -> list[object]:
        """The latest meetings: recordings still to save and saved ones, newest first."""
        pending = getattr(self, "pending", None) or library.pending_recordings()
        items = sorted([*pending, *getattr(self, "saved", [])], key=lambda o: o.started, reverse=True)
        return items[:5]

    def _open_todos(self, limit: int = 5) -> list[tuple[library.SavedMeeting, library.Task]]:
        out = []
        for m in getattr(self, "saved", [])[:20]:
            for t in m.tasks("my_todos"):
                if not t.done:
                    out.append((m, t))
        return out[:limit]

    def _badge_for(self, o) -> tuple[str, str]:
        if isinstance(o, library.PendingRecording):
            if o.session in self.w.jobs:
                return "Making notes", "busy"
            if o.error:
                return "Failed", "bad"
            return ("Ready to save", "ready") if o.ready else ("Not processed", "plain")
        n = o.open_todos()
        return (f"{n} to-do{'s' if n != 1 else ''}", "ok") if n else ("Saved", "plain")

    def _home_key(self) -> str:
        parts = [f"{getattr(o, 'key', '')}:{self._badge_for(o)[0]}:{o.title}" for o in self._recent()]
        parts += [f"{t.file}:{t.line}" for _, t in self._open_todos()]
        now = datetime.now()
        days = self.weeks.days() if outlook.connected() else {}
        parts += [f"{e.id}:{e.subject}:{e.start}:{e.start <= now}:{e.end <= now}"
                  for evs in (days or {}).values() for e in evs]
        parts.append(f"{outlook.connected()}:{self.weeks.key()}:{date.today()}")
        parts.append(agenda.mail_key(self))
        parts.append(f"{self._home_wide()}:{self._pane_width()}")
        return "home|" + "|".join(parts) + f"|{self.w.mode}|{','.join(self.setup_missing())}"

    def _home_width(self) -> int:
        """How wide Home's column is in this window (it's centred, at most 1240 px)."""
        return min(1240, max(0, self.detail.width() - 64))

    def _home_wide(self) -> bool:
        """Room for two panes side by side?"""
        return self._home_width() >= agenda.SideBySide.NARROW

    def _pane_width(self) -> int:
        return agenda.SideBySide.PANE if self._home_width() >= 1080 else 280

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Home lays out its panes for the window's width: rebuild it when that changes enough
        layout = (self._home_wide(), self._pane_width())
        if getattr(self, "_home_layout", None) not in (None, layout) and self.current == "home":
            QTimer.singleShot(0, self.refresh)
        self._home_layout = layout

    def _home_page(self) -> QWidget:
        page = QWidget()
        page.setObjectName("Page")
        page.state_key = self._home_key()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        outer.addWidget(scroll)
        body = QWidget()
        body.setObjectName("Page")
        centre = QHBoxLayout(body)
        centre.setContentsMargins(32, 28, 32, 28)
        col_w = QWidget()
        col_w.setMaximumWidth(1240)  # two panes side by side need the room
        col = QVBoxLayout(col_w)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        centre.addStretch(1)
        centre.addWidget(col_w, 100)  # the full width, up to the maximum; any extra is split either side
        centre.addStretch(1)
        scroll.setWidget(body)

        # greeting
        hour = time.localtime().tm_hour
        part = "morning" if hour < 12 else "afternoon" if hour < 18 else "evening"
        first = (self.w.s.user_name or "").split()[0] if self.w.s.user_name else ""
        head = QHBoxLayout()
        texts = QVBoxLayout()
        texts.setSpacing(2)
        greeting = label(f"Good {part}{', ' + first if first else ''}", "greeting", wrap=True)
        greeting.setMinimumWidth(200)
        texts.addWidget(greeting)
        texts.addWidget(label(f"{date.today():%A %-d %B}", "muted"))
        head.addLayout(texts, 1)
        self._usage_btn = usageview.UsageButton(self)  # credits left, top right
        head.addWidget(self._usage_btn, 0, Qt.AlignVCenter)
        head.addSpacing(6)
        if self.w.recorder is None:
            head.addWidget(button("Record now", "record", "record", on_click=self._record_clicked), 0, Qt.AlignVCenter)
        col.addLayout(head)

        missing = self.setup_missing()
        if missing:
            col.addWidget(callout("warn", "key", "Finish setting up",
                                  "Hear Me Out still needs " + ", ".join(missing) + ".",
                                  [button("Open settings", "settings", "primary", on_click=self.open_settings)]))

        # at-a-glance tiles
        pending = getattr(self, "pending", None) or library.pending_recordings()
        todos_all = sum(m.open_todos() for m in getattr(self, "saved", []))
        tiles = QHBoxLayout()
        tiles.setSpacing(12)

        def tile(number: str, text: str, icon: str, colour: str, on_click=None, tip: str = "") -> None:
            c = ClickableCard() if on_click else card()
            if on_click:
                c.clicked.connect(on_click)
            if tip:
                c.setToolTip(tip)
            lay = QHBoxLayout(c)
            lay.setContentsMargins(16, 14, 16, 14)
            lay.setSpacing(12)
            lay.addWidget(icon_label(icon, colour, 22))
            t = QVBoxLayout()
            t.setSpacing(0)
            t.addWidget(label(number, "stat"))
            sub = label(text, "muted", wrap=True)
            sub.setMinimumWidth(60)
            t.addWidget(sub)
            lay.addLayout(t, 1)
            tiles.addWidget(c, 1)

        first_pending = pending[0].key if pending else None
        tile(str(len(pending)), "recording to save" if len(pending) == 1 else "recordings to save", "mic",
             T["accent"], (lambda: self.select(first_pending)) if first_pending else None)
        tile(str(todos_all), "your open to-do" if todos_all == 1 else "your open to-dos", "tasks", T["green"],
             lambda: self.select("todos"), "See all your to-dos")
        watching = self.w.mode != "off"
        tile("On" if watching else "Off", "watching for meetings" if watching else "meeting detection",
             "wave", T["green"] if watching else T["faint"], self.open_settings, "Change in Settings")
        col.addLayout(tiles)

        if outlook.connected() and self.w.s.mail_on_home:  # calendar with the mail pane beside it
            left, right = QWidget(), QWidget()
            lcol, rcol = QVBoxLayout(left), QVBoxLayout(right)
            for c in (lcol, rcol):
                c.setContentsMargins(0, 0, 0, 0)
                c.setSpacing(10)
            agenda.week_section(self, lcol)
            lcol.addStretch(1)
            rcol.addSpacing(6)
            agenda.mail_section(self, rcol)
            col.addWidget(agenda.SideBySide(left, right, pane=self._pane_width(), stacked=not self._home_wide()))
        else:
            agenda.week_section(self, col)
            agenda.mail_section(self, col)

        # recent meetings, with your open to-dos beside them
        main_col = col
        meetings_w, todos_w = QWidget(), QWidget()
        col = QVBoxLayout(meetings_w)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        recent = self._recent()
        col.addSpacing(6)
        col.addWidget(label("Recent meetings", "h2"))
        if not recent:
            empty = card(dim=True)
            el = QVBoxLayout(empty)
            el.setContentsMargins(24, 22, 24, 22)
            el.setSpacing(8)
            el.addWidget(label("Your meetings will appear here", "h2"))
            intro = label("Join a call in Zoom, Teams, Meet, Slack or Discord and Hear Me Out offers to record it. "
                          "When it ends you get a summary, your to-dos and a transcript, and you choose what to "
                          "save to Obsidian.", "muted", wrap=True)
            intro.setMinimumWidth(200)
            el.addWidget(intro)
            col.addWidget(empty)
            tips = QHBoxLayout()
            tips.setSpacing(12)
            for icon, title, text in (
                    ("mic", "Notices meetings", "Zoom, Teams, Meet, Slack, Discord… it asks before recording."),
                    ("headphones", "Headset or speakers", "Follows the devices your call uses, even mid-call."),
                    ("gem", "You choose what's saved", "Summary, to-dos, team tasks and transcript, as notes.")):
                c = card()
                cl = QVBoxLayout(c)
                cl.setContentsMargins(16, 16, 16, 16)
                cl.setSpacing(6)
                cl.addWidget(icon_label(icon, T["accent"], 20))
                heading = label(title, "h2", wrap=True)
                heading.setMinimumWidth(100)
                cl.addWidget(heading)
                tip = label(text, "muted", wrap=True)
                tip.setMinimumWidth(120)
                cl.addWidget(tip)
                cl.addStretch(1)
                tips.addWidget(c, 1)
            col.addLayout(tips)
        for o in recent:
            c = ClickableCard()
            c.clicked.connect(lambda key=o.key: self.select(key))
            lay = QHBoxLayout(c)
            lay.setContentsMargins(16, 12, 14, 12)
            lay.setSpacing(12)
            is_pending = isinstance(o, library.PendingRecording)
            lay.addWidget(icon_label("mic" if is_pending else "gem", T["accent"] if is_pending else T["muted"], 18))
            t = QVBoxLayout()
            t.setSpacing(2)
            title = ElidedLabel(o.title)
            f = title.font()
            f.setWeight(f.Weight.DemiBold)
            title.setFont(f)
            t.addWidget(title)
            me = self.w.s.user_name
            people = o.people if is_pending else [p for p in o.participants if p != me]
            mins = max(1, round(o.duration_s / 60)) if is_pending else max(1, o.duration_min)
            sub = " · ".join(x for x in (library.when(o.started), f"{mins} min",
                                         ", ".join(people[:3]) if people else (o.app if is_pending else "")) if x)
            t.addWidget(ElidedLabel(sub, "muted"))
            lay.addLayout(t, 1)
            text, kind = self._badge_for(o)
            lay.addWidget(badge(text, kind))
            lay.addWidget(icon_label("chevron", T["faint"], 16))
            col.addWidget(c)

        # open to-dos
        col.addStretch(1)
        col = QVBoxLayout(todos_w)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)
        todos = self._open_todos()
        if todos:
            col.addSpacing(6)
            head = QHBoxLayout()
            head.addWidget(label("Your open to-dos", "h2"))
            head.addStretch(1)
            head.addWidget(button("See all", "chevron", "ghost", icon_color=T["muted"],
                                  on_click=lambda: self.select("todos")))
            col.addLayout(head)
            for m, t in todos:
                c = card()
                lay = QHBoxLayout(c)
                lay.setContentsMargins(14, 10, 14, 10)
                lay.setSpacing(12)
                box = QCheckBox()
                box.setToolTip("Mark as done (updates the note in Obsidian)")
                box.setCursor(Qt.PointingHandCursor)
                box.toggled.connect(lambda done, task=t: (library.set_task_done(task, done),
                                                          QTimer.singleShot(600, self.refresh)))
                lay.addWidget(box)
                text, due = _split_task(t.text)
                tl = QVBoxLayout()
                tl.setSpacing(2)
                tl.addWidget(label(text, wrap=True))
                tl.addWidget(ElidedLabel(f"From {m.title} · {library.when(m.started)}", "muted"))
                lay.addLayout(tl, 1)
                if due:
                    txt, kind = friendly_due(due)
                    lay.addWidget(chip(txt, "calendar", kind), 0, Qt.AlignVCenter)
                open_btn = button("", "chevron", "ghost", tip="Open the meeting", icon_color=T["faint"],
                                  on_click=lambda _=False, key=m.key: self.select(key))
                lay.addWidget(open_btn)
                col.addWidget(c)
        col.addStretch(1)
        col = main_col
        if todos:
            col.addWidget(agenda.SideBySide(meetings_w, todos_w, stacked=not self._home_wide()))  # equal halves
        else:
            col.addWidget(meetings_w)
        col.addStretch(1)
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
            view.set_callout(self._progress_card(self.w.jobs[rec.session].message, rec.session))
        elif rec.error:
            view.set_callout(callout(
                "bad", "alert", "Couldn't make notes for this recording",
                f"{rec.error}\nThe recording is safe. Check your internet connection and keys in Settings, "
                "then try again.",
                [button("Settings", "settings", on_click=self.open_settings),
                 button("Try again", "refresh", "primary", on_click=lambda: self.w.process(rec.session))]))
        else:
            has_transcript = (rec.session / "transcript.json").exists()
            view.set_callout(callout(
                "info", "sparkle", "Not turned into notes yet",
                "The transcript is already made, so this only writes the summary and to-dos." if has_transcript
                else "Make a transcript, summary and to-dos from this recording.",
                [button("Make notes", "sparkle", "primary", on_click=lambda: self.w.process(rec.session))]))
        return view

    def _progress_card(self, message: str, session: Path | None = None) -> QWidget:
        """Saving the recording → Transcribing → Writing notes, with the current step highlighted."""
        m = message.lower()
        step = 0 if ("saving the recording" in m or "starting" in m) else 1 if "transcrib" in m else 2
        c = card()
        lay = QVBoxLayout(c)
        lay.setContentsMargins(18, 16, 18, 16)
        lay.setSpacing(12)
        top = QHBoxLayout()
        top.addWidget(label("Making notes…", "h2"))
        top.addStretch(1)
        if session is not None:
            top.addWidget(button("Stop", "stop", tip="Stop transcribing and writing notes. The recording is kept.",
                                 on_click=lambda: self.w.stop_notes(session)))
        lay.addLayout(top)
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
        view = MeetingView(from_saved(m, length, self.w.s.user_name or "Me"), self.player,
                           actions=[folder_btn, open_btn],
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
        if self.current == "settings":
            return
        if not self._leave_settings():
            return
        self.list.blockSignals(True)
        self.list.clearSelection()
        self.list.blockSignals(False)
        self.current = "settings"
        self._show(self._settings_page())

    def _settings_page(self) -> QWidget:
        page = SettingsPage(self.w)
        page.saved.connect(lambda: QTimer.singleShot(0, self.refresh))  # sidebar status, setup prompts…
        page.discarded.connect(lambda: QTimer.singleShot(0, lambda: self._show(self._settings_page())))
        page.appearance_changed.connect(lambda _: QTimer.singleShot(0, self.retheme))
        return page

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
