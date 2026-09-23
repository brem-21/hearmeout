"""Building blocks for the app's screens: one meeting view used for both new
recordings and meetings already in Obsidian, plus the "Save to Obsidian" bar."""

from __future__ import annotations

import difflib
import html
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QSize, Qt, QThread, Signal
from PySide6.QtGui import QFontMetrics, QIcon, QPainter
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFileDialog, QFrame,
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy,
    QSlider, QStackedWidget, QTextBrowser, QVBoxLayout, QWidget,
)

from . import obsidian, stt, theme
from .obsidian import ITEMS
from .player import Player
from .theme import T

# --------------------------------------------------------------------------- small helpers


def label(text: str = "", role: str | None = None, wrap: bool = False, selectable: bool = False) -> QLabel:
    w = QLabel(text)
    if role:
        w.setProperty("role", role)
    w.setWordWrap(wrap)
    if selectable:
        w.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return w


def button(text: str = "", icon: str | None = None, variant: str | None = None, tip: str = "",
           on_click: Callable | None = None, icon_color: str | None = None) -> QPushButton:
    b = QPushButton(text)
    if variant:
        b.setProperty("variant", variant)
    if icon:
        color = icon_color or (T["accent_text"] if variant == "primary" else "white" if variant == "record"
                               else T["red"] if variant == "danger" else T["text"])
        b.setIcon(theme.icon(icon, color, 16))
        b.setIconSize(QSize(16, 16))
    if tip:
        b.setToolTip(tip)
    if on_click:
        b.clicked.connect(on_click)
    b.setCursor(Qt.PointingHandCursor)
    return b


def badge(text: str, kind: str) -> QLabel:
    w = QLabel(text)
    w.setProperty("badge", kind)
    return w


def chip(text: str, icon: str | None = None, kind: str = "plain") -> QFrame:
    """A small pill with an optional icon, e.g. a due date or an owner."""
    colour = {"bad": T["red"], "warn": T["amber"]}.get(kind, T["muted"])
    f = QFrame()
    f.setProperty("chipbox", kind)
    lay = QHBoxLayout(f)
    lay.setContentsMargins(7, 2, 9, 2)
    lay.setSpacing(4)
    if icon:
        lay.addWidget(icon_label(icon, colour, 13))
    t = QLabel(text)
    t.setStyleSheet(f"color: {colour}; font-size: 12px; background: transparent;")
    lay.addWidget(t)
    return f


def card(tone: str | None = None, dim: bool = False) -> QFrame:
    f = QFrame()
    if tone:
        f.setProperty("tone", tone)
    else:
        f.setProperty("card", True)
        if dim:
            f.setProperty("dim", True)
    return f


class ClickableCard(QFrame):
    """A card you can click, e.g. a recent meeting on the Home screen."""
    clicked = Signal()

    def __init__(self):
        super().__init__()
        self.setProperty("card", True)
        self.setProperty("clickable", True)
        self.setCursor(Qt.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


def text_block(text: str, role: str = "muted", width: int = 560, center: bool = False) -> QLabel:
    """Wrapped text that always gets the room it needs (a centred, wrapped QLabel otherwise
    collapses to a strip on some screens and fonts)."""
    w = label(text, role, wrap=True)
    w.setMinimumWidth(min(width, 360))
    w.setMaximumWidth(width)
    w.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
    if center:
        w.setAlignment(Qt.AlignHCenter | Qt.AlignTop)
    return w


def divider() -> QFrame:
    f = QFrame()
    f.setProperty("role", "divider")
    return f


def icon_label(name: str, color: str | None = None, size: int = 18) -> QLabel:
    w = QLabel()
    w.setPixmap(theme.pixmap(name, color, size))
    w.setFixedSize(size, size)
    return w


def mmss(sec: float) -> str:
    return stt.timestamp(sec)


def friendly_due(d: date, done: bool = False) -> tuple[str, str]:
    """('Due tomorrow', 'plain'), ('Overdue · Mon 21 Sep', 'bad')…"""
    days = (d - date.today()).days
    if not done and days < 0:
        return f"Overdue · {d:%a %-d %b}", "bad"
    if days == 0:
        return "Due today", "plain" if done else "warn"
    if days == 1:
        return "Due tomorrow", "plain"
    if 1 < days < 7:
        return f"Due {d:%A}", "plain"
    return f"Due {d:%a %-d %b}", "plain"


class ElidedLabel(QLabel):
    """A one-line label that shortens itself with … instead of stretching its parent."""

    def __init__(self, text: str = "", role: str | None = None):
        super().__init__(text)
        if role:
            self.setProperty("role", role)
        self._full = text
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(10)

    def setText(self, text: str) -> None:  # noqa: N802 (Qt naming)
        self._full = text
        super().setText(text)
        self.setToolTip(text)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        p.setPen(self.palette().color(self.foregroundRole()))
        p.setFont(self.font())
        text = QFontMetrics(self.font()).elidedText(self._full, Qt.ElideRight, self.width())
        p.drawText(self.rect(), Qt.AlignVCenter | Qt.AlignLeft, text)


# --------------------------------------------------------------------------- what a view shows


@dataclass
class TaskItem:
    text: str
    owner: str | None
    due: date | None
    quote: str
    done: bool
    ref: object              # library.Task (saved) or the action-item index (not saved yet)
    at: float | None = None  # where in the recording it was said, if found
    excluded: bool = False


@dataclass
class MeetingData:
    kind: str                # "pending" (not in Obsidian yet) or "saved"
    title: str
    started: datetime
    duration_s: float
    participants: list[str]
    app: str | None
    summary_md: str
    my_tasks: list[TaskItem]
    team_tasks: list[TaskItem]
    transcript: list[tuple[str, float, str, bool]]  # speaker, start, text, is it me
    audio: Path | None
    source: object = None    # obsidian.Meeting (pending) or library.SavedMeeting (saved)
    has_notes: bool = True
    extra: dict = field(default_factory=dict)


def _find_moment(quote: str, rows: list[tuple[str, float, str, bool]]) -> float | None:
    """When a task's quote was said, so you can jump to it."""
    q = re.sub(r"\W+", " ", quote.lower()).strip()
    if not q:
        return None
    best, best_at = 0.0, None
    for _, start, text, _ in rows:
        t = re.sub(r"\W+", " ", text.lower())
        if q in t:
            return start
        r = difflib.SequenceMatcher(None, q, t).ratio()
        if r > best:
            best, best_at = r, start
    return best_at if best > 0.55 else None


def _summary_md(notes) -> str:
    parts = [notes.summary]
    if notes.decisions:
        parts += ["## Decisions", *[f"- {d}" for d in notes.decisions]]
    if notes.key_points:
        parts += ["## Key points", *[f"- {p}" for p in notes.key_points]]
    return "\n\n".join(parts)


def from_meeting(meeting: obsidian.Meeting, app: str | None = None) -> MeetingData:
    """A recording whose notes are made but not saved yet."""
    rows = [(u.speaker, u.start, u.text, u.speaker == meeting.me) for u in meeting.utterances]
    my, team = [], []
    notes = meeting.notes
    for i, a in enumerate(notes.action_items if notes else []):
        due = None
        try:
            due = date.fromisoformat(a.due) if a.due else None
        except ValueError:
            pass
        item = TaskItem(a.task, a.owner, due, a.evidence or "", False, i, _find_moment(a.evidence or "", rows),
                        excluded=i in meeting.excluded_tasks)
        (my if a.owner_is_me else team).append(item)
    return MeetingData("pending", meeting.title, meeting.started, meeting.duration_s,
                       notes.participants if notes else [], app, _summary_md(notes) if notes else "",
                       my, team, rows, meeting.audio, meeting, has_notes=notes is not None)


def from_saved(m, audio_length: float | None = None, me: str = "Me") -> MeetingData:
    """A meeting already in Obsidian (library.SavedMeeting)."""
    rows = [(sp, st, tx, sp == me) for sp, st, tx in m.transcript()]

    def items(key: str) -> list[TaskItem]:
        out = []
        for t in m.tasks(key):
            text, owner, due = t.text, None, None
            mo = re.match(r"^\*\*(.+?)\*\*:\s*(.*)$", text)
            if mo:
                owner, text = mo.group(1), mo.group(2)
            md = re.search(r"\s*📅\s*(\d{4}-\d{2}-\d{2})", text)
            if md:
                try:
                    due = date.fromisoformat(md.group(1))
                except ValueError:
                    pass
                text = (text[:md.start()] + text[md.end():]).strip()
            out.append(TaskItem(text, owner, due, t.quote, t.done, t, _find_moment(t.quote, rows)))
        return out

    summary = re.sub(r"^# .*\n+", "", m.text("summary"))
    summary = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]+)\]\]", r"\1", summary)
    return MeetingData("saved", m.title, m.started, audio_length or m.duration_min * 60, m.participants, None,
                       summary, items("my_todos"), items("team_tasks"), rows, m.audio, m,
                       has_notes="summary" in m.files or "my_todos" in m.files or "team_tasks" in m.files,
                       extra={"files": set(m.files)})


# --------------------------------------------------------------------------- audio bar


class AudioBar(QFrame):
    """Round play button, position slider and time, for one recording."""

    def __init__(self, player: Player, path: Path, duration: float):
        super().__init__()
        self.setProperty("card", True)
        self.player, self.path, self.duration = player, path, max(duration, 0.1)
        self.btn = QPushButton()
        self.btn.setProperty("variant", "round")
        self.btn.setCursor(Qt.PointingHandCursor)
        self.btn.clicked.connect(self.toggle)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, max(1, int(self.duration * 10)))
        self.slider.sliderReleased.connect(lambda: self.play_from(self.slider.value() / 10))
        self.slider.setCursor(Qt.PointingHandCursor)
        self.time = label(f"0:00 / {mmss(self.duration)}", "mono")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(10, 8, 14, 8)
        lay.setSpacing(12)
        lay.addWidget(self.btn)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.time)
        # Bound methods (not lambdas), so Qt disconnects them when this bar is deleted.
        player.position.connect(self._moved)
        player.state.connect(self._on_state)
        self._refresh()
        if not Player.available():
            self.setEnabled(False)
            self.setToolTip("No audio player found (pw-play or paplay).")

    def _mine(self) -> bool:
        return self.player.path == self.path

    def _on_state(self, playing: bool) -> None:
        self._refresh()

    def _refresh(self) -> None:
        playing = self.player.playing and self._mine()
        self.btn.setIcon(theme.icon("pause" if playing else "play", T["accent_text"], 16))
        self.btn.setToolTip("Pause" if playing else "Play recording")

    def toggle(self) -> None:
        if self.player.playing and self._mine():
            self.player.pause()
            self._refresh()
        else:
            start = self.player.offset if self._mine() else 0.0
            self.play_from(start if start < self.duration - 0.5 else 0.0)

    def play_from(self, sec: float) -> None:
        self.player.play(self.path, sec)
        self._refresh()

    def _moved(self, sec: float) -> None:
        if self._mine() and not self.slider.isSliderDown():
            self.slider.setValue(int(sec * 10))
            self.time.setText(f"{mmss(sec)} / {mmss(self.duration)}")


# --------------------------------------------------------------------------- the meeting view


class MeetingView(QWidget):
    """Header, optional status callout and audio, then tabs: Summary, My to-dos,
    Team tasks, Transcript. Bottom slot for the save bar."""

    task_toggled = Signal(object, bool)   # TaskItem, new state (done, or kept for saving)

    def __init__(self, data: MeetingData, player: Player, *, actions: list[QWidget] | None = None,
                 editable_title: bool = False, search: str = ""):
        super().__init__()
        self.setObjectName("Page")
        self.data, self.player, self.search = data, player, search
        self._active_line = -1
        root = QVBoxLayout(self)
        root.setContentsMargins(32, 22, 32, 20)
        root.setSpacing(12)

        # header
        head = QHBoxLayout()
        head.setSpacing(8)
        titles = QVBoxLayout()
        titles.setSpacing(6)
        if editable_title:
            self.title = QLineEdit(data.title)
            self.title.setProperty("role", "title")
            self.title.setPlaceholderText("Meeting title")
            self.title.setToolTip("Click to rename")
            titles.addWidget(self.title)
        else:
            self.title = None
            titles.addWidget(label(data.title, "h1", wrap=True))
        meta = QHBoxLayout()
        meta.setSpacing(14)
        mins = max(1, round(data.duration_s / 60))
        for icon, text in (("calendar", f"{data.started:%a %-d %b %Y · %H:%M}"), ("clock", f"{mins} min"),
                           ("users", ", ".join(data.participants[:5]) + (" …" if len(data.participants) > 5 else "")),
                           ("mic", data.app or "")):
            if text:
                bit = QHBoxLayout()
                bit.setSpacing(5)
                bit.addWidget(icon_label(icon, T["faint"], 14))
                bit.addWidget(label(text, "muted"))
                meta.addLayout(bit)
        meta.addStretch(1)
        titles.addLayout(meta)
        head.addLayout(titles, 1)
        for a in actions or []:
            head.addWidget(a, 0, Qt.AlignTop)
        root.addLayout(head)

        self.callout_slot = QVBoxLayout()
        root.addLayout(self.callout_slot)
        if data.audio and data.audio.exists():
            root.addWidget(AudioBar(player, data.audio, data.duration_s))

        # tabs
        self.tabs = QHBoxLayout()
        self.tabs.setSpacing(0)
        self.pages = QStackedWidget()
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.tab_buttons: dict[str, QPushButton] = {}
        if data.has_notes and data.summary_md.strip():
            self._add_tab("summary", "Summary", "sparkle", self._summary_page())
        if data.has_notes:
            files = data.extra.get("files")
            if files is None or "my_todos" in files:
                self._add_tab("my_todos", self._tasks_title("My to-dos", data.my_tasks), "tasks",
                              self._tasks_page(data.my_tasks, mine=True))
            if files is None or "team_tasks" in files:
                self._add_tab("team_tasks", self._tasks_title("Team tasks", data.team_tasks), "users",
                              self._tasks_page(data.team_tasks, mine=False))
        if data.transcript:
            self._add_tab("transcript", "Transcript", "chat", self._transcript_page())
        if self.tab_buttons:
            bar = QHBoxLayout()
            bar.setSpacing(0)
            bar.addLayout(self.tabs)
            bar.addStretch(1)
            root.addLayout(bar)
            root.addWidget(divider())
            root.addWidget(self.pages, 1)
            first = "transcript" if search and "transcript" in self.tab_buttons and \
                any(search.lower() in r[2].lower() for r in data.transcript) else next(iter(self.tab_buttons))
            self.show_tab(first)
        else:
            root.addStretch(1)
        self.bottom_slot = QVBoxLayout()
        root.addLayout(self.bottom_slot)
        player.position.connect(self._follow_playback)

    # --- tabs

    def _add_tab(self, key: str, text: str, icon: str, page: QWidget) -> None:
        b = QPushButton(text)
        b.setProperty("variant", "tab")
        b.setCheckable(True)
        b.setIcon(theme.icon(icon, T["muted"], 15))
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda: self.show_tab(key))
        self.group.addButton(b)
        self.tabs.addWidget(b)
        self.tab_buttons[key] = b
        self.pages.addWidget(page)
        page.setProperty("tab", key)

    def show_tab(self, key: str) -> None:
        for i in range(self.pages.count()):
            if self.pages.widget(i).property("tab") == key:
                self.pages.setCurrentIndex(i)
        self.tab_buttons[key].setChecked(True)

    @staticmethod
    def _tasks_title(name: str, tasks: list[TaskItem]) -> str:
        open_ = sum(1 for t in tasks if not t.done and not t.excluded)
        return f"{name}  {open_}" if tasks else name

    def refresh_tab_titles(self) -> None:
        for key, name, tasks in (("my_todos", "My to-dos", self.data.my_tasks),
                                 ("team_tasks", "Team tasks", self.data.team_tasks)):
            if key in self.tab_buttons:
                self.tab_buttons[key].setText(self._tasks_title(name, tasks))

    # --- pages

    def _browser(self) -> QTextBrowser:
        v = QTextBrowser()
        v.setOpenLinks(False)
        v.document().setDocumentMargin(4)
        return v

    def _summary_page(self) -> QWidget:
        v = self._browser()
        v.document().setDefaultStyleSheet(
            f"h2 {{ font-size: 15px; margin-top: 18px; margin-bottom: 6px; color: {T['text']}; }}"
            f"p {{ line-height: 150%; }} li {{ margin-bottom: 4px; line-height: 140%; }}")
        v.setMarkdown(self.data.summary_md)
        return v

    def _tasks_page(self, tasks: list[TaskItem], mine: bool) -> QWidget:
        area = QScrollArea()
        area.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 12, 6, 12)
        lay.setSpacing(8)
        if not tasks:
            empty = card(dim=True)
            e = QVBoxLayout(empty)
            e.setContentsMargins(18, 18, 18, 18)
            e.addWidget(label("No tasks for you in this meeting." if mine else
                              "No tasks for other people in this meeting.", "muted"))
            lay.addWidget(empty)
        for t in tasks:
            lay.addWidget(self._task_card(t, mine))
        lay.addStretch(1)
        area.setWidget(inner)
        return area

    def _task_card(self, t: TaskItem, mine: bool) -> QFrame:
        c = card()
        outer = QHBoxLayout(c)
        outer.setContentsMargins(14, 12, 12, 12)
        outer.setSpacing(12)
        text = label(t.text, wrap=True)
        f = text.font()
        f.setPointSizeF(f.pointSizeF() * 1.05)
        text.setFont(f)
        pending = self.data.kind == "pending"

        body = QVBoxLayout()
        body.setSpacing(6)
        body.addWidget(text)
        chips = QHBoxLayout()
        chips.setSpacing(6)
        if t.owner and not mine:
            chips.addWidget(chip(t.owner, "users"))
        if t.due:
            txt, kind = friendly_due(t.due, t.done)
            chips.addWidget(chip(txt, "calendar", kind))
        if t.at is not None and self.data.audio and self.data.audio.exists() and Player.available():
            hear = button(f"Hear it · {mmss(t.at)}", "ear", "link", icon_color=T["accent"],
                          on_click=lambda _=False, at=t.at: self.player.play(self.data.audio, max(0, at - 1)))
            chips.addWidget(hear)
        chips.addStretch(1)
        body.addLayout(chips)
        if t.quote:
            body.addWidget(label(f"“{t.quote}”", "quote", wrap=True))

        def paint() -> None:
            f2 = text.font()
            f2.setStrikeOut(t.done or t.excluded)
            text.setFont(f2)
            text.setProperty("role", "faint" if (t.done or t.excluded) else None)
            theme.repolish(text)

        if pending:
            toggle = QPushButton()
            toggle.setProperty("variant", "ghost")
            toggle.setCursor(Qt.PointingHandCursor)

            def show_state() -> None:
                toggle.setIcon(theme.icon("undo" if t.excluded else "x", T["muted"], 16))
                toggle.setToolTip("Keep this task" if t.excluded else "Leave this task out when saving")
                paint()

            def flip() -> None:
                t.excluded = not t.excluded
                show_state()
                self.task_toggled.emit(t, not t.excluded)
                self.refresh_tab_titles()

            toggle.clicked.connect(flip)
            show_state()
            outer.addLayout(body, 1)
            outer.addWidget(toggle, 0, Qt.AlignTop)
        else:
            box = QCheckBox()
            box.setChecked(t.done)
            box.setToolTip("Mark as done (updates the note in Obsidian)")
            box.setCursor(Qt.PointingHandCursor)

            def done(state: bool) -> None:
                t.done = state
                paint()
                self.task_toggled.emit(t, state)
                self.refresh_tab_titles()

            box.toggled.connect(done)
            paint()
            outer.addWidget(box, 0, Qt.AlignTop)
            outer.addLayout(body, 1)
        return c

    def _transcript_page(self) -> QWidget:
        self.transcript_view = self._browser()
        can_play = bool(self.data.audio and self.data.audio.exists() and Player.available())
        if can_play:
            self.transcript_view.setToolTip("Click a time to hear that moment")
            self.transcript_view.anchorClicked.connect(
                lambda url: self.player.play(self.data.audio, float(url.toString()[2:])))
        self._render_transcript()
        return self.transcript_view

    def _render_transcript(self) -> None:
        can_play = bool(self.data.audio and self.data.audio.exists() and Player.available())
        words = [w for w in self.search.lower().split() if len(w) > 1]
        parts = []
        for i, (speaker, start, text, me) in enumerate(self.data.transcript):
            body = html.escape(text)
            for w in words:
                body = re.sub(f"({re.escape(html.escape(w))})", rf'<span style="background:{T["highlight"]}">\1</span>',
                              body, flags=re.I)
            colour = T["me"] if me else T["them"]
            stamp = (f'<a href="t:{start}" style="color:{T["faint"]}; text-decoration:none">{mmss(start)}</a>'
                     if can_play else f'<span style="color:{T["faint"]}">{mmss(start)}</span>')
            bg = f' bgcolor="{T["accent_soft"]}"' if i == self._active_line else ""
            parts.append(
                f'<a name="u{i}"></a><table width="100%" cellpadding="7" cellspacing="0"{bg}><tr><td>'
                f'<span style="color:{colour}; font-weight:600">{html.escape(speaker)}</span>'
                f'&nbsp;&nbsp;{stamp}<br><span style="line-height:140%">{body}</span></td></tr></table>')
        bar = self.transcript_view.verticalScrollBar()
        pos = bar.value()
        self.transcript_view.setHtml("".join(parts))
        bar.setValue(pos)

    def _follow_playback(self, sec: float) -> None:
        """Highlight the line being played."""
        if not self.data.transcript or self.player.path != self.data.audio or not hasattr(self, "transcript_view"):
            return
        idx = -1
        for i, (_, start, _, _) in enumerate(self.data.transcript):
            if start <= sec + 0.3:
                idx = i
            else:
                break
        if idx != self._active_line:
            self._active_line = idx
            self._render_transcript()
            if idx >= 0:
                self.transcript_view.scrollToAnchor(f"u{max(0, idx - 1)}")

    def set_callout(self, widget: QWidget | None) -> None:
        while self.callout_slot.count():
            w = self.callout_slot.takeAt(0).widget()
            if w:
                w.deleteLater()
        if widget:
            self.callout_slot.addWidget(widget)


def callout(tone: str, icon: str, title: str, text: str = "", buttons: list[QWidget] | None = None) -> QFrame:
    """A coloured message box: ready / failed / saved / info."""
    colour = {"ok": T["green"], "bad": T["red"], "warn": T["amber"], "info": T["accent"]}[tone]
    c = card(tone)
    lay = QHBoxLayout(c)
    lay.setContentsMargins(14, 12, 14, 12)
    lay.setSpacing(12)
    lay.addWidget(icon_label(icon, colour, 20), 0, Qt.AlignTop)
    texts = QVBoxLayout()
    texts.setSpacing(3)
    t = label(title, "h2")
    t.setStyleSheet(f"color: {colour};")
    texts.addWidget(t)
    if text:
        texts.addWidget(label(text, "muted", wrap=True, selectable=True))
    lay.addLayout(texts, 1)
    for b in buttons or []:
        lay.addWidget(b, 0, Qt.AlignVCenter)
    return c


# --------------------------------------------------------------------------- saving


class _Saver(QThread):
    progressed = Signal(object)  # obsidian.Progress
    failed = Signal(str)

    def __init__(self, meeting: obsidian.Meeting, vault: Path, folder: str, keys: list[str]):
        super().__init__()
        self.args = (meeting, vault, folder, keys)
        self.paths: list[Path] = []

    def run(self) -> None:
        try:
            for p in obsidian.save(*self.args):
                self.paths.append(p.path)
                self.progressed.emit(p)
        except OSError as e:
            self.failed.emit(str(e))


class SaveBar(QFrame):
    """Choose what goes into Obsidian and where, and save with a progress bar."""

    saved = Signal(list)  # paths written

    def __init__(self, meeting: obsidian.Meeting, title_edit: QLineEdit | None, vaults: list[Path],
                 vault: Path | None, folder: str, initial: list[str], remember: Callable[[list[str]], None]):
        super().__init__()
        self.setProperty("card", True)
        self.meeting, self.title_edit, self.vaults = meeting, title_edit, vaults
        self.vault, self.folder, self.remember = vault, folder, remember
        self._saver: _Saver | None = None
        self._error: str | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 16, 12)
        lay.setSpacing(10)

        # where: vault + folder, always visible
        top = QHBoxLayout()
        top.setSpacing(8)
        top.addWidget(icon_label("gem", T["accent"], 18))
        top.addWidget(label("Save to Obsidian", "h2"))
        top.addStretch(1)
        top.addWidget(label("Vault", "muted"))
        self.vault_box = QComboBox()
        self.vault_box.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.vault_box.setMinimumWidth(170)
        for v in vaults:
            self._add_vault(v)
        if vault and self.vault_box.findData(str(vault)) < 0:
            self._add_vault(vault)
        self.vault_box.addItem(theme.icon("folder", T["muted"]), "Choose another folder…", "__browse__")
        if vault:
            self.vault_box.setCurrentIndex(self.vault_box.findData(str(vault)))
        self.vault_box.activated.connect(self._vault_chosen)
        top.addWidget(self.vault_box)
        top.addSpacing(6)
        top.addWidget(label("Folder", "muted"))
        self.folder_edit = QLineEdit(folder)
        self.folder_edit.setFixedWidth(150)
        self.folder_edit.setToolTip("Folder inside the vault, e.g. Meetings or Work/Meetings")
        self.folder_edit.textChanged.connect(self._folder_changed)
        top.addWidget(self.folder_edit)
        lay.addLayout(top)

        # what: one chip per item, then Save
        row = QHBoxLayout()
        row.setSpacing(6)
        self.chips: dict[str, QPushButton] = {}
        for key, desc in obsidian.available(meeting).items():
            c = QPushButton(ITEMS[key][0])
            c.setProperty("variant", "chip")
            c.setCheckable(True)
            c.setChecked(key in initial)
            c.setCursor(Qt.PointingHandCursor)
            c.setToolTip(desc)
            c.toggled.connect(self._refresh)
            self.chips[key] = c
            row.addWidget(c)
        row.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(160)
        self.progress.hide()
        self.status = label("", "muted")
        self.status.hide()
        row.addWidget(self.status)
        row.addWidget(self.progress)
        self.save_btn = button("", "check", "primary", on_click=self._save)
        self.save_btn.setDefault(True)
        row.addWidget(self.save_btn)
        lay.addLayout(row)
        self.row = row

        self.dest = ElidedLabel("", "faint")
        lay.addWidget(self.dest)
        self.change = self.vault_box  # disabled together with the chips while saving
        if title_edit is not None:
            title_edit.textChanged.connect(self._refresh)
        self._refresh()

    def _add_vault(self, v: Path) -> None:
        self.vault_box.insertItem(max(0, self.vault_box.count() - (1 if self.vault_box.findData("__browse__") >= 0
                                                                    else 0)),
                                  theme.icon("gem", T["accent"]), v.name, str(v))
        idx = self.vault_box.findData(str(v))
        self.vault_box.setItemData(idx, str(v), Qt.ToolTipRole)

    def _vault_chosen(self, index: int) -> None:
        data = self.vault_box.itemData(index)
        if data == "__browse__":
            path = QFileDialog.getExistingDirectory(self, "Choose your Obsidian vault", str(Path.home()))
            if path:
                if self.vault_box.findData(path) < 0:
                    self._add_vault(Path(path))
                self.vault_box.setCurrentIndex(self.vault_box.findData(path))
                self.vault = Path(path)
            elif self.vault:
                self.vault_box.setCurrentIndex(self.vault_box.findData(str(self.vault)))
        elif data:
            self.vault = Path(data)
        self._refresh()

    def _folder_changed(self, text: str) -> None:
        self.folder = text.strip().strip("/") or "Meetings"
        self._refresh()

    def keys(self) -> list[str]:
        return [k for k, c in self.chips.items() if c.isChecked()]

    @property
    def busy(self) -> bool:
        return self._saver is not None

    def _sync_title(self) -> None:
        if self.title_edit is not None:
            self.meeting.title = self.title_edit.text().strip() or "Meeting"

    def update_counts(self) -> None:
        for key, desc in obsidian.available(self.meeting).items():
            if key in self.chips:
                self.chips[key].setToolTip(desc)
        self._refresh()

    def _refresh(self) -> None:
        if self._saver is not None:
            return
        self._sync_title()
        n = len(self.keys())
        for key, c in self.chips.items():
            count = {"my_todos": len(self.meeting.my_tasks), "team_tasks": len(self.meeting.team_tasks)}.get(key)
            c.setText(ITEMS[key][0] + (f"  {count}" if count else ""))
            c.setIcon(theme.icon("check", T["accent"], 13) if c.isChecked() else QIcon())
        self.save_btn.setEnabled(n > 0 and self.vault is not None)
        self.save_btn.setText(f"Save {n} item{'s' if n != 1 else ''}" if n else "Choose what to save")
        if self.vault:
            where = obsidian.meeting_folder(self.vault, self.folder, self.meeting)
            self.dest.setText(f"Saves to  {self.vault.name} › {where.relative_to(self.vault)}/")
        else:
            self.dest.setText("Choose the Obsidian vault to save to")

    def _save(self) -> None:
        keys = self.keys()
        self._sync_title()
        self.remember(keys)
        for w in (*self.chips.values(), self.vault_box, self.folder_edit, self.save_btn):
            w.setEnabled(False)
        if self.title_edit is not None:
            self.title_edit.setReadOnly(True)
        self.save_btn.hide()
        self.progress.setRange(0, len(keys))
        self.progress.setValue(0)
        self.progress.show()
        self.status.show()
        self.status.setText(f"Saving {ITEMS[keys[0]][0].lower()}…  0 of {len(keys)}")
        self._error = None
        self._saver = _Saver(self.meeting, self.vault, self.folder, keys)
        self._saver.progressed.connect(self._on_progress)
        self._saver.failed.connect(lambda e: setattr(self, "_error", e))
        self._saver.finished.connect(self._on_finished)
        self._saver.start()

    def _on_progress(self, p: obsidian.Progress) -> None:
        self.progress.setValue(p.done)
        keys = self.keys()
        nxt = keys[p.done] if p.done < p.total else None
        self.status.setText(f"Saving {ITEMS[nxt][0].lower()}…  {p.done} of {p.total}" if nxt
                            else f"Saved {p.total} of {p.total}")

    def _on_finished(self) -> None:
        saver, self._saver = self._saver, None
        if self._error:
            self.status.setText(f"Saved {len(saver.paths)} of {self.progress.maximum()}")
            QMessageBox.critical(self, "Couldn't save", f"Saving to Obsidian failed:\n\n{self._error}")
            if saver.paths:
                self.saved.emit(saver.paths)
            return
        self.progress.hide()
        self.status.setText("")
        self.status.setPixmap(theme.pixmap("check", T["green"], 16))
        done = label(f"Saved {len(saver.paths)} item{'s' if len(saver.paths) != 1 else ''} to Obsidian")
        done.setStyleSheet(f"color: {T['green']}; font-weight: 650;")
        self.row.addWidget(done)
        self.saved.emit(saver.paths)
