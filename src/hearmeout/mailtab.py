"""The Mail tab (next to To-dos) and new-mail notifications, both working from the Outlook
pane on Home: an inbox summary (what needs you, FYI, to-dos from mail) and "Explain" for
any one email (what's asked, deadlines, a suggested reply)."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QHBoxLayout, QScrollArea, QVBoxLayout, QWidget

from . import mailweb, outlookweb
from .theme import T
from .views import ElidedLabel, badge, button, callout, card, chip, icon_label, label

CHECK_MS = 60 * 1000  # how often to look at the inbox for new mail


class _Job(QThread):
    """Runs one model request off the UI thread."""
    done = Signal(object)

    def __init__(self, fn):
        super().__init__()
        self.fn = fn

    def run(self) -> None:
        try:
            self.done.emit(self.fn())
        except Exception as e:
            self.done.emit(e)


class MailReader(QObject):
    """Reads the Outlook pane's message list every minute: keeps the latest list for the Mail
    tab, and notifies you about new unread email."""
    changed = Signal()

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.mails: list[mailweb.Mail] = []
        self.readable: bool | None = None  # None: not tried yet
        self.seen: set[str] | None = None  # None until the first read: no flood of old mail at start
        self.notes: dict[int, str] = {}    # notification id -> email key
        self._debug_saved = False
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.read)
        self.timer.start(CHECK_MS)
        QTimer.singleShot(15000, self.read)  # give Outlook time to load and sign in

    def _view(self):
        return getattr(self.win, "_outlook_view", None)

    def read(self, then=None) -> None:
        s = self.win.w.s
        view = self._view()
        if view is None or not s.mail_on_home or s.mail_source != "outlook":
            return
        view.page().runJavaScript(mailweb.LIST_JS, 0, lambda raw: self._got(raw, then))

    def _got(self, raw, then=None) -> None:
        try:
            mails, data = mailweb.parse_list(raw if isinstance(raw, str) else "")
        except ValueError:
            mails, data = [], {}
        if mails:
            new = [m for m in mails if m.unread and self.seen is not None and m.key not in self.seen]
            self.seen = (self.seen or set()) | {m.key for m in mails}
            self.mails, self.readable = mails, True
            self._notify(new)
        else:
            self.readable = False if self.readable is None or not self.mails else self.readable
            signed_in = "login." not in (data.get("url") or "")
            if signed_in and data and not self._debug_saved:
                mailweb.save_debug(data)  # masked outline only, for fixing the reader
                self._debug_saved = True
        self.changed.emit()
        if then:
            then()

    def _notify(self, new: list[mailweb.Mail]) -> None:
        w = self.win.w
        if not w.s.mail_notify or not new:
            return
        if len(new) > 3:  # a burst (e.g. back online): one summary instead of many
            nid = w.notifier.show(f"{len(new)} new emails", ", ".join(m.sender for m in new[:4]) + "…",
                                  [("openmail", "Open")])
            if nid:
                self.notes[nid] = ""
            return
        for m in new:
            body = m.subject + (f"\n{m.preview[:140]}" if m.preview else "")
            nid = w.notifier.show(f"New email from {m.sender}", body, [("openmail", "Open")])
            if nid:
                self.notes[nid] = m.key

    def open_email(self, mail: mailweb.Mail, then) -> None:
        """Open an email in the Outlook pane, then read its text."""
        view = self._view()
        if view is None:
            then({})
            return
        view.page().runJavaScript(mailweb.CLICK_JS % mail.index, 0,
                                  lambda ok: QTimer.singleShot(2500, lambda: view.page().runJavaScript(
                                      mailweb.OPEN_JS, 0, lambda raw: then(_loads(raw)))))


def _loads(raw) -> dict:
    import json
    try:
        return json.loads(raw) if isinstance(raw, str) else {}
    except ValueError:
        return {}


# --------------------------------------------------------------------------- the Mail tab


class MailTab:
    """State for the tab: what's being worked on, and the last explanation."""

    def __init__(self):
        self.job: _Job | None = None
        self.busy = ""          # "summary" or an email key while explaining
        self.error = ""
        self.insight = None     # (Mail, EmailInsight) for the last explained email

    def key(self, win) -> str:
        r = win.mail_reader
        d = mailweb.last_digest() or {}
        return (f"mailtab|{r.readable}|{','.join(m.key + str(int(m.unread)) for m in r.mails)}|{d.get('at')}|"
                f"{self.busy}|{self.error}|{id(self.insight)}|{outlookweb.available()}|{win.w.s.mail_source}")


def page(win) -> QWidget:
    tab: MailTab = win.mail_tab
    reader: MailReader = win.mail_reader
    s = win.w.s
    root = QWidget()
    root.setObjectName("Page")
    root.state_key = tab.key(win)
    outer = QVBoxLayout(root)
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
    col_w.setMaximumWidth(900)
    col = QVBoxLayout(col_w)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(12)
    centre.addStretch(1)
    centre.addWidget(col_w, 100)
    centre.addStretch(1)
    scroll.setWidget(body)

    head = QHBoxLayout()
    titles = QVBoxLayout()
    titles.setSpacing(2)
    titles.addWidget(label("Mail", "h1"))
    titles.addWidget(label("Your newest emails in Outlook, and what they mean for you.", "muted"))
    head.addLayout(titles, 1)
    can_ask = bool(s.openrouter_api_key)
    summarize = button("Summarizing…" if tab.busy == "summary" else "Summarize inbox", "sparkle", "primary",
                       on_click=lambda: _summarize(win))
    summarize.setEnabled(can_ask and bool(reader.mails) and not tab.busy)
    head.addWidget(summarize, 0, Qt.AlignVCenter)
    head.addWidget(button("", "refresh", "ghost", tip="Read the inbox again", icon_color=T["muted"],
                          on_click=lambda: reader.read(then=lambda: win.show_mail(force=True))), 0, Qt.AlignVCenter)
    col.addLayout(head)

    if s.mail_source != "outlook" or not outlookweb.available():
        col.addWidget(callout("info", "mail", "Mail comes from the Outlook pane",
                              "Choose Outlook on the web in Settings › Integrations › Mail.",
                              [button("Open settings", "settings", on_click=win.open_settings)]))
        col.addStretch(1)
        return root
    if not can_ask:
        col.addWidget(callout("warn", "key", "Add an OpenRouter key to summarize",
                              "Summaries use your notes model, like meeting notes do.",
                              [button("Open settings", "settings", on_click=win.open_settings)]))
    if not reader.mails:
        text = ("Reading your inbox from the Outlook pane…" if reader.readable is None else
                "Couldn't read your inbox from the Outlook pane. On Home, sign in to Outlook if it asks, and "
                "keep the Mail pane on your Inbox. Then press ↻.")
        col.addWidget(callout("info", "mail", "No emails yet", text))
    if tab.error:
        col.addWidget(callout("bad", "alert", "Something went wrong", tab.error))
    note = label("Summaries send the emails' senders, subjects and first lines (and, for Explain, that email's "
                 "text) to your notes model. Nothing is sent until you press a button.", "faint", wrap=True)

    # --- an explained email
    if tab.insight is not None:
        m, ins = tab.insight
        col.addWidget(_insight_card(win, m, ins))

    # --- the inbox summary
    d = mailweb.last_digest()
    if d:
        dg = d.get("digest", {})
        col.addSpacing(4)
        row = QHBoxLayout()
        row.addWidget(label("Inbox summary", "h2"))
        row.addWidget(label(f"{d.get('at', '')[11:16]} · {d.get('count', 0)} emails · {d.get('model', '')}",
                            "muted"), 0, Qt.AlignBottom)
        row.addStretch(1)
        col.addLayout(row)
        ov = card()
        ol = QVBoxLayout(ov)
        ol.setContentsMargins(18, 14, 18, 14)
        ol.addWidget(label(dg.get("overview", ""), wrap=True))
        col.addWidget(ov)
        if dg.get("needs_you"):
            col.addWidget(label(f"Needs you  ·  {len(dg['needs_you'])}", "h2"))
            for n in dg["needs_you"]:
                col.addWidget(_needs_card(win, n))
        if dg.get("todos"):
            col.addWidget(label(f"To-dos from mail  ·  {len(dg['todos'])}", "h2"))
            tc = card()
            tl = QVBoxLayout(tc)
            tl.setContentsMargins(16, 10, 16, 10)
            tl.setSpacing(8)
            for t in dg["todos"]:
                r = QHBoxLayout()
                r.addWidget(icon_label("tasks", T["green"], 15), 0, Qt.AlignTop)
                tx = QVBoxLayout()
                tx.setSpacing(0)
                tx.addWidget(label(t.get("task", ""), wrap=True))
                tx.addWidget(ElidedLabel(f"From {t.get('sender', '')}", "muted"))
                r.addLayout(tx, 1)
                if t.get("due"):
                    r.addWidget(chip(str(t["due"]), "calendar"), 0, Qt.AlignTop)
                tl.addLayout(r)
            col.addWidget(tc)
        if dg.get("fyi"):
            col.addWidget(label(f"FYI  ·  {len(dg['fyi'])}", "h2"))
            fc = card()
            fl = QVBoxLayout(fc)
            fl.setContentsMargins(16, 10, 16, 10)
            fl.setSpacing(6)
            for f in dg["fyi"]:
                fl.addWidget(label(f"<b>{_esc(f.get('sender', ''))}</b> · {_esc(f.get('subject', ''))}<br>"
                                   f"<span style='color:{T['muted']}'>{_esc(f.get('gist', ''))}</span>", wrap=True))
            col.addWidget(fc)

    # --- the inbox itself, each with Explain
    if reader.mails:
        col.addSpacing(4)
        row = QHBoxLayout()
        row.addWidget(label("Inbox", "h2"))
        unread = sum(m.unread for m in reader.mails)
        if unread:
            row.addWidget(label(f"{unread} unread", "muted"), 0, Qt.AlignBottom)
        row.addStretch(1)
        col.addLayout(row)
        box = card()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(4, 4, 4, 4)
        bl.setSpacing(0)
        for m in reader.mails[:25]:
            bl.addWidget(_mail_row(win, m, can_ask))
        col.addWidget(box)
    col.addWidget(note)
    col.addStretch(1)
    return root


def _esc(text: str) -> str:
    import html
    return html.escape(text or "")


def _mail_row(win, m: mailweb.Mail, can_ask: bool) -> QWidget:
    tab: MailTab = win.mail_tab
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(12, 8, 10, 8)
    lay.setSpacing(10)
    lay.addWidget(icon_label("record", T["accent"] if m.unread else "transparent", 8), 0, Qt.AlignTop)
    text = QVBoxLayout()
    text.setSpacing(1)
    top = QHBoxLayout()
    who = ElidedLabel(m.sender)
    f = who.font()
    f.setWeight(f.Weight.Bold if m.unread else f.Weight.Medium)
    who.setFont(f)
    top.addWidget(who, 1)
    if m.attachments:
        top.addWidget(icon_label("clip", T["muted"], 13))
    top.addWidget(label(m.when, "muted"))
    text.addLayout(top)
    text.addWidget(ElidedLabel(m.subject))
    if m.preview:
        text.addWidget(ElidedLabel(m.preview, "faint"))
    lay.addLayout(text, 1)
    busy = tab.busy == m.key
    b = button("Explaining…" if busy else "Explain", "sparkle", tip="Open it in the Outlook pane and explain it",
               on_click=lambda: _explain(win, m))
    b.setEnabled(can_ask and not tab.busy)
    lay.addWidget(b, 0, Qt.AlignVCenter)
    return w


def _needs_card(win, n: dict) -> QWidget:
    c = card()
    lay = QHBoxLayout(c)
    lay.setContentsMargins(16, 12, 14, 12)
    lay.setSpacing(12)
    lay.addWidget(icon_label("alert", T["amber"], 16), 0, Qt.AlignTop)
    t = QVBoxLayout()
    t.setSpacing(2)
    t.addWidget(label(f"<b>{_esc(n.get('action', ''))}</b>", wrap=True))
    t.addWidget(label(_esc(n.get("why", "")), "muted", wrap=True))
    t.addWidget(ElidedLabel(f"{n.get('sender', '')} · {n.get('subject', '')}", "faint"))
    lay.addLayout(t, 1)
    if n.get("due"):
        lay.addWidget(chip(str(n["due"]), "calendar", "warn"), 0, Qt.AlignTop)
    match = next((m for m in win.mail_reader.mails if m.subject.strip().lower() == (n.get("subject") or "").strip().lower()),
                 None)
    if match is not None and win.w.s.openrouter_api_key:
        b = button("Explain", "sparkle", on_click=lambda: _explain(win, match))
        b.setEnabled(not win.mail_tab.busy)
        lay.addWidget(b, 0, Qt.AlignTop)
    return c


def _insight_card(win, m: mailweb.Mail, ins) -> QWidget:
    c = card(tone="info")
    lay = QVBoxLayout(c)
    lay.setContentsMargins(18, 14, 18, 16)
    lay.setSpacing(8)
    top = QHBoxLayout()
    top.addWidget(icon_label("sparkle", T["accent"], 16))
    top.addWidget(label(f"<b>{_esc(m.subject)}</b> · {_esc(m.sender)}", wrap=True), 1)
    kind = {"high": "bad", "medium": "warn"}.get((ins.urgency or "").lower(), "plain")
    top.addWidget(badge(f"{(ins.urgency or 'low').capitalize()} urgency", kind))
    top.addWidget(button("", "x", "ghost", tip="Close", icon_color=T["muted"],
                         on_click=lambda: (setattr(win.mail_tab, "insight", None), win.show_mail(force=True))))
    lay.addLayout(top)
    lay.addWidget(label(_esc(ins.summary), wrap=True))
    if ins.asks:
        lay.addWidget(label("<b>What's asked of you</b><br>" + "<br>".join(f"• {_esc(a)}" for a in ins.asks), wrap=True))
    if ins.deadlines:
        lay.addWidget(label("<b>Deadlines</b><br>" + "<br>".join(f"• {_esc(d)}" for d in ins.deadlines), wrap=True))
    if ins.suggested_reply:
        lay.addWidget(label("<b>Suggested reply</b>", wrap=True))
        reply = label(_esc(ins.suggested_reply).replace("\n", "<br>"), "muted", wrap=True, selectable=True)
        lay.addWidget(reply)
        row = QHBoxLayout()
        row.addWidget(button("Copy reply", "check", on_click=lambda: QGuiApplication.clipboard().setText(ins.suggested_reply)))
        row.addStretch(1)
        lay.addLayout(row)
    return c


def _summarize(win) -> None:
    tab: MailTab = win.mail_tab
    mails = list(win.mail_reader.mails[:25])
    s = win.w.s
    _start(win, "summary", lambda: mailweb.digest(mails, s), lambda result: None)


def _explain(win, m: mailweb.Mail) -> None:
    tab: MailTab = win.mail_tab
    tab.busy, tab.error = m.key, ""
    win.show_mail(force=True)

    def opened(email: dict) -> None:
        if not email.get("text"):  # couldn't read the open email: explain from what the list shows
            email = {"sender": m.sender, "subject": m.subject, "preview": m.preview}
        email.setdefault("sender", m.sender)
        email["subject"] = email.get("subject") or m.subject
        s = win.w.s
        _start(win, m.key, lambda: mailweb.explain(email, s),
               lambda ins: setattr(tab, "insight", (m, ins)))

    win.mail_reader.open_email(m, opened)


def _start(win, what: str, fn, on_result) -> None:
    tab: MailTab = win.mail_tab
    tab.busy, tab.error = what, ""
    job = tab.job = _Job(fn)

    def done(result) -> None:
        tab.busy = ""
        if isinstance(result, Exception):
            tab.error = str(result)[:400]
        else:
            on_result(result)
        win.show_mail(force=True)

    job.done.connect(done)
    job.start()
    win.show_mail(force=True)
