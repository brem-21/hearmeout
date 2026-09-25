"""Home's week calendar (browse weeks, join, see which meetings you recorded) and the
To-dos page (every to-do from your saved meetings, by due date or by meeting)."""

from __future__ import annotations

import re
import time
from datetime import date, datetime, timedelta

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget

from . import library, outlook, theme
from .theme import T
from .views import ClickableCard, ElidedLabel, button, card, chip, friendly_due, icon_label, label

EARLY = timedelta(minutes=15)  # a recording can start this long before its meeting
STALE_AFTER = 10 * 60          # re-fetch another week after this many seconds


# --------------------------------------------------------------------------- weeks


class _WeekFetch(QThread):
    done = Signal(object, object)  # monday, list of events or an error message

    def __init__(self, monday: date):
        super().__init__()
        self.monday = monday

    def run(self) -> None:
        try:
            self.done.emit(self.monday, outlook.fetch_week(self.monday))
        except Exception as e:
            self.done.emit(self.monday, str(e))


class Weeks(QObject):
    """Which week Home shows. This week comes from the calendar cache; other weeks are
    fetched in the background the first time you go to them."""
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.monday = outlook.week_start()
        self.loaded: dict[date, tuple[float, list[outlook.Event]]] = {}
        self.errors: dict[date, str] = {}
        self.jobs: dict[date, _WeekFetch] = {}

    def go(self, weeks: int | None) -> None:
        """Move by a number of weeks, or back to this week with None."""
        self.monday = outlook.week_start() if weeks is None else self.monday + timedelta(weeks=weeks)
        self.changed.emit()

    @property
    def is_this_week(self) -> bool:
        return self.monday == outlook.week_start()

    def days(self) -> dict[date, list[outlook.Event]] | None:
        """The shown week's days and events, or None while they're being fetched."""
        m = self.monday
        if outlook.covers(m):
            return outlook.week(m)
        got = self.loaded.get(m)
        if got is None or time.time() - got[0] > STALE_AFTER:
            self._fetch(m)
        return outlook.week(m, got[1]) if got else None

    def _fetch(self, monday: date) -> None:
        if monday in self.jobs:
            return
        job = self.jobs[monday] = _WeekFetch(monday)
        job.done.connect(self._fetched)
        job.finished.connect(lambda: self.jobs.pop(monday, None))
        job.start()

    def _fetched(self, monday: date, result) -> None:
        if isinstance(result, str):
            self.errors[monday] = result
        else:
            self.errors.pop(monday, None)
            self.loaded[monday] = (time.time(), result)
        if monday == self.monday:
            self.changed.emit()

    def key(self) -> str:
        m = self.monday
        return f"{m}:{m in self.loaded}:{self.errors.get(m, '')}:{m in self.jobs}"

    def wait(self) -> None:
        for job in list(self.jobs.values()):
            job.wait(5000)


def assign(events: list[outlook.Event], meetings: list) -> dict[tuple, object]:
    """Which calendar event each recording belongs to: {(event id, start): meeting}.
    Each recording goes to one event only: the one already running when it started (the most
    recently started, if several overlap), else the one starting within 15 minutes."""
    out: dict[tuple, object] = {}
    timed = [e for e in events if not e.all_day]
    for m in sorted(meetings, key=lambda m: m.started):
        running = [e for e in timed if e.start <= m.started < e.end]
        soon = [e for e in timed if m.started < e.start <= m.started + EARLY]
        same = [e for e in running + soon if e.subject.strip().lower() == m.title.strip().lower()]
        best = (same[0] if same else max(running, key=lambda e: e.start) if running
                else min(soon, key=lambda e: e.start) if soon else None)
        if best is not None:
            out.setdefault((best.id, best.start), m)
    return out


def recorded(e: outlook.Event, meetings: list) -> object | None:
    """The saved meeting or recording still to save that belongs to this calendar event, if any."""
    return assign([e], meetings).get((e.id, e.start))


def week_section(win, col: QVBoxLayout) -> None:
    """The week calendar on Home: ‹ Today ›, Monday to Friday, click a meeting to join it or open its notes."""
    if not outlook.connected():
        return
    weeks: Weeks = win.weeks
    days = weeks.days()
    if days is not None:
        days = {d: evs for d, evs in days.items() if d.weekday() < 5}  # the working week: Monday to Friday
    today, now = date.today(), datetime.now()
    monday, sunday = weeks.monday, weeks.monday + timedelta(days=4)

    col.addSpacing(6)
    head = QHBoxLayout()
    head.setSpacing(6)
    this = weeks.is_this_week
    diff = (monday - outlook.week_start()).days // 7
    title = "This week" if this else "Next week" if diff == 1 else "Last week" if diff == -1 else "Week"
    head.addWidget(label(title, "h2"))
    span = (f"{monday:%-d}–{sunday:%-d %B}" if monday.month == sunday.month
            else f"{monday:%-d %b} – {sunday:%-d %b}") + ("" if monday.year == today.year else f" {sunday:%Y}")
    head.addWidget(label(span, "muted"), 0, Qt.AlignBottom)
    head.addStretch(1)
    if days is not None:
        total = len({(e.id, e.start) for evs in days.values() for e in evs if not e.all_day})
        head.addWidget(label(f"{total} meeting{'s' if total != 1 else ''}", "muted"), 0, Qt.AlignVCenter)
        head.addSpacing(6)
    prev = button("", "chevron-left", "ghost", tip="Previous week", icon_color=T["muted"],
                  on_click=lambda: weeks.go(-1))
    today_btn = button("Today", tip="Back to this week", on_click=lambda: weeks.go(None))
    today_btn.setEnabled(not this)
    nxt = button("", "chevron", "ghost", tip="Next week", icon_color=T["muted"], on_click=lambda: weeks.go(1))
    for b in (prev, today_btn, nxt):
        head.addWidget(b, 0, Qt.AlignVCenter)
    col.addLayout(head)

    grid = card()
    if days is None:
        lay = QVBoxLayout(grid)
        lay.setContentsMargins(20, 26, 20, 26)
        err = weeks.errors.get(monday)
        msg = label(f"Couldn't load this week: {err}" if err else "Loading your calendar…",
                    "muted", wrap=True)
        msg.setAlignment(Qt.AlignHCenter)
        lay.addWidget(msg)
        col.addWidget(grid)
        return

    row = QHBoxLayout(grid)
    row.setContentsMargins(6, 8, 6, 8)
    row.setSpacing(4)
    me = [win.w.s.user_name, *win.w.s.user_aliases]
    meetings = [*(getattr(win, "pending", None) or library.pending_recordings()), *getattr(win, "saved", [])]
    owners = assign([e for evs in days.values() for e in evs], meetings)
    for d, events in days.items():
        day_col = QVBoxLayout()
        day_col.setContentsMargins(3, 6, 3, 6)
        day_col.setSpacing(4)
        is_today, past_day = d == today, d < today
        name = label(f"{d:%a}".upper(), "faint")
        name.setAlignment(Qt.AlignHCenter)
        num = label(f"{d:%-d}")
        num.setAlignment(Qt.AlignHCenter)
        num.setFixedHeight(28)
        num.setStyleSheet("font-size: 15px; font-weight: 700; border-radius: 14px; "
                          + (f"background: {T['accent']}; color: {T['accent_text']};" if is_today
                             else f"color: {T['faint'] if past_day else T['text']};"))
        day_col.addWidget(name)
        day_col.addWidget(num)
        if not events:
            none = label("·", "faint")
            none.setAlignment(Qt.AlignHCenter)
            day_col.addWidget(none)
        for e in events:
            day_col.addWidget(_event_box(win, e, now, me, owners.get((e.id, e.start))))
        day_col.addStretch(1)
        holder = QWidget()
        holder.setLayout(day_col)
        holder.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)  # five equal columns
        if is_today:
            holder.setObjectName("today")
            holder.setAttribute(Qt.WA_StyledBackground, True)
        row.addWidget(holder, 1)
    col.addWidget(grid)


def _event_box(win, e: outlook.Event, now: datetime, me: list[str], notes) -> QWidget:
    """One meeting: time and title, with an icon for Join / notes saved / recording to save."""
    live, over = e.start <= now < e.end, e.end <= now
    state = "live" if live else "over" if over else "next"
    box = ClickableCard()
    box.setProperty("card", False)
    box.setProperty("ev", state)  # styled by the app's stylesheet (much faster than one per box)
    box.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
    lay = QVBoxLayout(box)
    lay.setContentsMargins(5, 3, 3, 4)
    lay.setSpacing(1)
    top = QHBoxLayout()
    top.setSpacing(2)
    when = label("All day" if e.all_day else ("Now" if live else f"{e.start:%H:%M}"))
    when.setProperty("evtime", state)
    top.addWidget(when)
    top.addStretch(1)
    joinable = bool(e.join_url) and not over
    is_saved = isinstance(notes, library.SavedMeeting)
    if notes is not None:
        top.addWidget(icon_label("gem" if is_saved else "mic", T["green"] if is_saved else T["amber"], 12))
    elif joinable:
        top.addWidget(icon_label("external", T["accent"], 12))
    lay.addLayout(top)
    text = e.subject if len(e.subject) <= 48 else e.subject[:46].rstrip() + "…"  # full title on hover
    title = label(text, wrap=True)
    title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
    title.setProperty("evtitle", "dim" if over and notes is None else "")
    lay.addWidget(title)

    others = e.others(me)
    tip = [e.subject, "All day" if e.all_day else f"{e.start:%a %-d %b, %H:%M}–{e.end:%H:%M}"]
    if others:
        tip.append("With " + ", ".join(others[:6]) + (f" +{len(others) - 6}" if len(others) > 6 else ""))
    if e.location and not e.location.lower().startswith("microsoft teams"):
        tip.append(e.location)
    if live and joinable:
        action, tip_action = (lambda: _open(e.join_url)), "Click to join"
    elif notes is not None:
        action = lambda key=notes.key: win.select(key)
        tip_action = "Recorded: click to see the notes" if is_saved else "Recorded: click to review and save"
    elif joinable:
        action, tip_action = (lambda: _open(e.join_url)), "Click to join"
    elif e.web_link:
        action, tip_action = (lambda: _open(e.web_link)), "Click to open in Outlook"
    else:
        action, tip_action = None, ("Not recorded" if over and not e.all_day else "")
    if tip_action:
        tip.append(tip_action)
    box.setToolTip("\n".join(tip))
    if action:
        box.clicked.connect(action)
    else:
        box.setCursor(Qt.ArrowCursor)
    return box


def _open(url: str) -> None:
    QDesktopServices.openUrl(QUrl(url))


# --------------------------------------------------------------------------- to-dos


def split_task(md: str) -> tuple[str, str | None, date | None]:
    """'**Kofi**: Send the invoice 📅 2026-09-25' -> ('Send the invoice', 'Kofi', date(2026, 9, 25))."""
    owner = None
    m = re.match(r"^\*\*(.+?)\*\*:\s*(.*)$", md)
    if m:
        owner, md = m.group(1), m.group(2)
    due = None
    m = re.search(r"\s*📅\s*(\d{4}-\d{2}-\d{2})", md)
    if m:
        try:
            due = date.fromisoformat(m.group(1))
        except ValueError:
            pass
        md = md[:m.start()] + md[m.end():]
    return md.strip(), owner, due


def _group(due: date | None, done: bool) -> tuple[int, str]:
    """(sort order, heading) for a to-do in the "By date" view."""
    if done:
        return 9, "Done"
    if due is None:
        return 8, "No due date"
    today = date.today()
    days = (due - today).days
    if days < 0:
        return 0, "Overdue"
    if days == 0:
        return 1, "Today"
    if days == 1:
        return 2, "Tomorrow"
    monday = outlook.week_start(today)
    if due < monday + timedelta(days=7):
        return 3, "Later this week"
    if due < monday + timedelta(days=14):
        return 4, "Next week"
    return 5, "Later"


def _collect(win) -> list[tuple[library.SavedMeeting, library.Task, bool]]:
    """Every task in your saved meetings: (meeting, task, is it a team task)."""
    view = win.todo_view
    out = []
    for m in getattr(win, "saved", []):
        for t in m.tasks("my_todos"):
            out.append((m, t, False))
        if view["team"]:
            for t in m.tasks("team_tasks"):
                out.append((m, t, True))
    return out


def todos_key(win) -> str:
    view = win.todo_view
    tasks = _collect(win)
    return (f"todos|{view['by']}|{view['done']}|{view['team']}|{date.today()}|"
            + "|".join(f"{t.file}:{t.line}:{t.done}" for _, t, _ in tasks))


def todos_page(win) -> QWidget:
    """All your to-dos, grouped by due date or by meeting. Ticking one updates its Obsidian note."""
    view = win.todo_view
    page = QWidget()
    page.setObjectName("Page")
    page.state_key = todos_key(win)
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
    col_w.setMaximumWidth(880)
    col = QVBoxLayout(col_w)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(10)
    centre.addStretch(1)
    centre.addWidget(col_w, 8)
    centre.addStretch(1)
    scroll.setWidget(body)

    tasks = _collect(win)
    open_tasks = [(m, t, team) for m, t, team in tasks if not t.done]
    mine = sum(not team for _, _, team in open_tasks)
    theirs = len(open_tasks) - mine
    col.addWidget(label("To-dos", "h1"))
    n_meet = len({m.folder for m, _, _ in open_tasks})
    counts = f"{mine} yours" + (f" · {theirs} for the team" if view["team"] else "")
    col.addWidget(label(f"{counts} open" + (f", from {n_meet} meeting{'s' if n_meet != 1 else ''}" if n_meet else ""),
                        "muted"))

    bar = QHBoxLayout()
    bar.setSpacing(6)
    for key, text, icon in (("date", "By date", "calendar"), ("meeting", "By meeting", "gem")):
        b = QPushButton(text)
        b.setProperty("variant", "chip")
        b.setCheckable(True)
        b.setChecked(view["by"] == key)
        b.setIcon(theme.icon(icon, T["accent"] if view["by"] == key else T["muted"], 14))
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda _=False, k=key: _set_view(win, by=k))
        bar.addWidget(b)
    bar.addSpacing(10)
    for key, text in (("team", "Team tasks"), ("done", "Show done")):
        cb = QCheckBox(text)
        cb.setChecked(view[key])
        cb.toggled.connect(lambda on, k=key: _set_view(win, **{k: on}))
        bar.addWidget(cb)
    bar.addStretch(1)
    col.addLayout(bar)
    col.addSpacing(4)

    shown = [(m, t, team) for m, t, team in tasks if view["done"] or not t.done]
    if not shown:
        empty = card(dim=True)
        el = QVBoxLayout(empty)
        el.setContentsMargins(24, 22, 24, 22)
        el.addWidget(label("Nothing to do" if tasks else "No to-dos yet", "h2"))
        el.addWidget(label("To-dos and team tasks from the meetings you save to Obsidian show up here." if not tasks
                           else "Everything is done. Tick “Show done” to see finished to-dos.", "muted", wrap=True))
        col.addWidget(empty)
        col.addStretch(1)
        return page

    if view["by"] == "date":
        groups: dict[tuple[int, str], list] = {}
        for m, t, team in shown:
            text, owner, due = split_task(t.text)
            groups.setdefault(_group(due, t.done), []).append((m, t, team, text, owner, due))
        for (_, heading), rows in sorted(groups.items()):
            rows.sort(key=lambda r: (r[5] or date.max, -r[0].started.timestamp()))
            col.addSpacing(6)
            col.addWidget(label(f"{heading}  ·  {len(rows)}", "h2"))
            for r in rows:
                col.addWidget(_todo_row(win, *r, show_meeting=True))
    else:
        by_meeting: dict = {}
        for m, t, team in shown:
            by_meeting.setdefault(m.folder, (m, []))[1].append((m, t, team, *split_task(t.text)))
        for m, rows in sorted(by_meeting.values(), key=lambda v: v[0].started, reverse=True):
            col.addSpacing(6)
            head = QHBoxLayout()
            head.addWidget(label(m.title, "h2"))
            head.addWidget(label(library.when(m.started), "muted"), 0, Qt.AlignBottom)
            head.addStretch(1)
            head.addWidget(button("Open meeting", "chevron", "ghost", icon_color=T["muted"],
                                  on_click=lambda _=False, key=m.key: win.select(key)))
            col.addLayout(head)
            rows.sort(key=lambda r: (r[1].done, r[2], r[5] or date.max))
            for r in rows:
                col.addWidget(_todo_row(win, *r, show_meeting=False))
    col.addStretch(1)
    return page


def _set_view(win, **changes) -> None:
    win.todo_view.update(changes)
    win.show_todos(force=True)


def _todo_row(win, m, t: library.Task, team: bool, text: str, owner: str | None, due: date | None,
              show_meeting: bool) -> QWidget:
    c = card(dim=t.done)
    lay = QHBoxLayout(c)
    lay.setContentsMargins(14, 10, 14, 10)
    lay.setSpacing(12)
    box = QCheckBox()
    box.setChecked(t.done)
    box.setToolTip("Mark as done (updates the note in Obsidian)")
    box.setCursor(Qt.PointingHandCursor)
    box.toggled.connect(lambda done: (library.set_task_done(t, done),
                                      QTimer.singleShot(500, lambda: win.show_todos(force=True))))
    lay.addWidget(box, 0, Qt.AlignTop)
    tl = QVBoxLayout()
    tl.setSpacing(2)
    main = label(text, wrap=True)
    main.setMinimumWidth(120)
    if t.done:
        main.setStyleSheet(f"color: {T['faint']}; text-decoration: line-through;")
    tl.addWidget(main)
    if show_meeting:
        tl.addWidget(ElidedLabel(f"From {m.title} · {library.when(m.started)}", "muted"))
    if t.quote:
        q = ElidedLabel(f"“{t.quote}”", "faint")
        tl.addWidget(q)
    lay.addLayout(tl, 1)
    if team:
        lay.addWidget(chip(owner or "Team", "users"), 0, Qt.AlignTop)
    else:
        lay.addWidget(chip("You", "check"), 0, Qt.AlignTop)
    if due:
        txt, kind = friendly_due(due, t.done)
        lay.addWidget(chip(txt, "calendar", kind), 0, Qt.AlignTop)
    if show_meeting:
        lay.addWidget(button("", "chevron", "ghost", tip="Open the meeting", icon_color=T["faint"],
                             on_click=lambda _=False, key=m.key: win.select(key)), 0, Qt.AlignTop)
    return c
