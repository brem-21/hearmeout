"""The usage button at the top of Home and the panel it opens: what's left on your
ElevenLabs and OpenRouter accounts, and what Hear Me Out used this month."""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QProgressBar, QPushButton, QVBoxLayout

from . import theme, usage
from .theme import T
from .views import button, divider, icon_label, label


def _short(n: float) -> str:
    """12345 -> '12.3k', 2_500_000 -> '2.5M'."""
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M".replace(".0M", "M")
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}k".replace(".0k", "k")
    return f"{int(n)}"


def _duration(sec: float) -> str:
    if 0 < sec < 60:
        return f"{int(round(sec))} s"
    m = int(round(sec / 60))
    return f"{m // 60} h {m % 60} min" if m >= 60 else f"{m} min"


def summary(balances) -> str:
    """The button's text, e.g. '$4.21 · 77k credits'."""
    if not balances:
        return "Usage"
    eleven, router = balances
    parts = []
    if router.ok and router.remaining is not None:
        parts.append(f"${router.remaining:,.2f}")
    if eleven.ok and eleven.limit:
        parts.append(f"{_short(max(0, eleven.limit - eleven.used))} credits")
    return " · ".join(parts) or "Usage"


class UsageButton(QPushButton):
    def __init__(self, win):
        super().__init__()
        self.win = win
        self.setProperty("variant", "chip")
        self.setIcon(theme.icon("gauge", T["muted"], 15))
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("What's left on your ElevenLabs and OpenRouter accounts, and what Hear Me Out used")
        self.clicked.connect(self.open_panel)
        self.update_text()

    def update_text(self) -> None:
        self.setText(summary(self.win.w.balances))

    def open_panel(self) -> None:
        self.win.w.check_balances(max_age=60)
        panel = UsagePanel(self.win)
        panel.adjustSize()
        where = self.mapToGlobal(QPoint(self.width() - panel.width(), self.height() + 6))
        panel.move(where)
        panel.show()
        self.win._usage_panel = panel


class UsagePanel(QFrame):
    """A small popup under the button. Rebuilds itself when new balances arrive."""

    def __init__(self, win):
        super().__init__(win, Qt.Popup)
        self.win = win
        self.setProperty("card", True)
        self.setFixedWidth(400)
        self.lay = QVBoxLayout(self)
        self.lay.setContentsMargins(18, 16, 18, 14)
        self.lay.setSpacing(10)
        self.build()
        win.w.balances_changed.connect(self._refreshed)

    def _refreshed(self) -> None:
        try:
            self.build()
            self.adjustSize()
        except RuntimeError:  # closed in the meantime
            pass

    def build(self) -> None:
        while self.lay.count():
            item = self.lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
            elif item.layout():
                _clear(item.layout())
        w, s = self.win.w, self.win.w.s
        t = usage.totals()
        head = QHBoxLayout()
        head.addWidget(label("Usage", "h2"))
        head.addWidget(label(f"{datetime.now():%B}", "muted"), 0, Qt.AlignBottom)
        head.addStretch(1)
        checking = w._balance_job is not None and w._balance_job.isRunning()
        head.addWidget(button("", "refresh", "ghost", tip="Check again", icon_color=T["muted"],
                              on_click=lambda: w.check_balances(max_age=0)))
        self.lay.addLayout(head)
        eleven, router = w.balances or (None, None)

        # --- ElevenLabs
        self.lay.addLayout(_service("mic", "ElevenLabs", "Transcription" + (f" · {eleven.plan}" if eleven and eleven.plan else "")))
        if eleven is None:
            self.lay.addWidget(label("Checking…" if checking else "Not checked yet", "muted"))
        elif eleven.ok and eleven.limit:
            left = max(0, eleven.limit - eleven.used)
            self.lay.addWidget(_bar(eleven.used, eleven.limit))
            text = f"<b>{left:,}</b> of {eleven.limit:,} credits left"
            if eleven.resets:
                text += f" · resets {eleven.resets:%-d %b}"
            self.lay.addWidget(label(text, wrap=True))
        else:
            self.lay.addWidget(label(eleven.error or "No credit information", "muted", wrap=True))
        self.lay.addWidget(label(f"Hear Me Out this month: {t.recordings} recording{'s' if t.recordings != 1 else ''}"
                                 f" · {_duration(t.audio_s)} of audio", "muted", wrap=True))
        self.lay.addWidget(divider())

        # --- OpenRouter
        self.lay.addLayout(_service("sparkle", "OpenRouter" if (router is None or router.applies) else "Notes model",
                                    f"Notes · {s.llm_model}"))
        if router is None:
            self.lay.addWidget(label("Checking…" if checking else "Not checked yet", "muted"))
        elif router.ok:
            if router.remaining is not None:
                if router.total:
                    self.lay.addWidget(_bar(router.total - router.remaining, router.total))
                text = f"<b>${router.remaining:,.2f}</b> left" + (f" of ${router.total:,.2f}" if router.total else "")
                self.lay.addWidget(label(text, wrap=True))
            spent = [f"today ${router.spent_day:,.2f}" if router.spent_day is not None else "",
                     f"this month ${router.spent_month:,.2f}" if router.spent_month is not None else ""]
            spent = [x for x in spent if x]
            if spent:
                self.lay.addWidget(label("Spent on this key " + " · ".join(spent), "muted", wrap=True))
            if router.free_tier:
                self.lay.addWidget(label("Free tier: only free models work until you add credits.", "muted", wrap=True))
        else:
            self.lay.addWidget(label(router.error or "No credit information", "muted", wrap=True))
        tokens = t.tokens_in + t.tokens_out
        mail_part = f", {t.mail} mail summar{'ies' if t.mail != 1 else 'y'}" if t.mail else ""
        line = (f"Hear Me Out this month: {t.notes} set{'s' if t.notes != 1 else ''} of notes{mail_part} · "
                f"{_short(tokens)} tokens ({_short(t.tokens_in)} in, {_short(t.tokens_out)} out)")
        if t.cost:
            line += f" · ${t.cost:,.4f}" if t.cost < 0.01 else f" · ${t.cost:,.2f}"
        self.lay.addWidget(label(line, "muted", wrap=True))
        if len(t.models) > 1:
            by_model = ", ".join(f"{m.split('/')[-1]} {_short(n)}" for m, n in
                                 sorted(t.models.items(), key=lambda kv: -kv[1]))
            self.lay.addWidget(label(f"By model: {by_model}", "faint", wrap=True))

        foot = "Checking…" if checking else (f"Checked at {datetime.fromtimestamp(w.balances_at):%H:%M}"
                                            if w.balances_at else "")
        if foot:
            self.lay.addWidget(label(foot, "faint"))


def _clear(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        if item.widget():
            item.widget().deleteLater()
        elif item.layout():
            _clear(item.layout())


def _service(icon: str, name: str, what: str) -> QHBoxLayout:
    row = QHBoxLayout()
    row.setSpacing(8)
    row.addWidget(icon_label(icon, T["accent"], 16))
    n = label(name)
    n.setStyleSheet("font-weight: 650;")
    row.addWidget(n)
    row.addWidget(label(what, "muted"))
    row.addStretch(1)
    return row


def _bar(used: float, total: float) -> QProgressBar:
    bar = QProgressBar()
    bar.setRange(0, 1000)
    bar.setValue(int(1000 * min(1.0, used / total)) if total else 0)
    bar.setTextVisible(False)
    bar.setFixedHeight(6)
    left = 1 - (used / total if total else 0)
    colour = T["red"] if left < 0.1 else T["amber"] if left < 0.25 else T["accent"]
    bar.setStyleSheet(f"QProgressBar {{ border: none; border-radius: 3px; background: {T['hover']}; }}"
                      f"QProgressBar::chunk {{ border-radius: 3px; background: {colour}; }}")
    return bar
