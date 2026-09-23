"""The running app: tray icon, automatic meeting detection, recording and note-making,
plus the main window (app.py) to browse meetings and save them.

`hearmeout app` opens the window; `hearmeout watch` starts quietly in the tray (used at
login). Only one copy runs: starting it again just brings the window forward.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import time
from pathlib import Path

from PySide6.QtCore import QLockFile, QObject, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QIcon, QPainter, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from . import audio, config, detect, notify, obsidian, pipeline

STATE_FILE = config.DATA_DIR / "watch-state.json"
AUTOSTART_FILE = config.CONFIG_DIR.parent / "autostart" / f"{notify.DESKTOP_ENTRY}.desktop"
LAUNCHER_FILE = config.DATA_DIR.parent / "applications" / f"{notify.DESKTOP_ENTRY}.desktop"
SOCKET_NAME = f"hearmeout-{os.getuid()}"
POLL_MS = 2000
CONFIRM_POLLS = 2  # a call must be seen twice in a row (about 2-4 s) before we ask


# --------------------------------------------------------------------------- small helpers


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_state(**changes) -> None:
    state = {**_load_state(), **changes}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1))


def _dot(color: str) -> QIcon:
    pix = QPixmap(64, 64)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QColor(color).darker(130))
    p.setBrush(QColor(color))
    p.drawEllipse(10, 10, 44, 44)
    p.end()
    return QIcon(pix)


def icon(state: str) -> QIcon:
    if state == "recording":
        return _dot("#e5484d")  # always a red dot, so recording is unmistakable
    name = "content-loading-symbolic" if state == "processing" else "audio-input-microphone-symbolic"
    return QIcon.fromTheme(name, _dot("#f5a524" if state == "processing" else "#8b8d98"))


def _command() -> str:
    """How to start this program again (for desktop entries)."""
    exe = Path(sys.argv[0])
    if exe.name == "hearmeout" and exe.exists():
        return shlex.quote(str(exe.resolve()))
    found = shutil.which("hearmeout")
    return shlex.quote(found) if found else f"{shlex.quote(sys.executable)} -m hearmeout"


def _desktop_entry(args: str, extra: str = "") -> str:
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Hear Me Out\n"
            "GenericName=Meeting notes\n"
            "Comment=Record meetings and save transcripts, summaries and to-dos to Obsidian\n"
            f"Exec={_command()} {args}\n"
            f"Icon={notify.ICON}\n"
            "Categories=Office;AudioVideo;\n"
            "Keywords=meeting;transcript;obsidian;notes;record;\n"
            f"{extra}")


def autostart_enabled() -> bool:
    return AUTOSTART_FILE.exists()


def set_autostart(on: bool) -> None:
    if on:
        AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
        AUTOSTART_FILE.write_text(_desktop_entry("watch", "X-GNOME-Autostart-enabled=true\n"))
    elif AUTOSTART_FILE.exists():
        AUTOSTART_FILE.unlink()


def install_launcher() -> Path:
    """Add Hear Me Out to the desktop's app menu (for pipx installs; Flatpak does this itself)."""
    LAUNCHER_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAUNCHER_FILE.write_text(_desktop_entry("app", "StartupWMClass=hearmeout\n"))
    return LAUNCHER_FILE


def _short(app: str) -> str:
    """'Google Meet (Firefox)' -> 'Firefox': the app the "don't ask" choice applies to."""
    return app.split("(")[-1].rstrip(")") if "(" in app else app


# --------------------------------------------------------------------------- background work


class _Processor(QThread):
    """Stops the recorder (if any), then transcribes and writes notes, off the UI thread."""
    status = Signal(str)
    done = Signal(object)   # Meeting
    failed = Signal(str)

    def __init__(self, session: Path, settings: config.Settings, recorder: audio.Recorder | None = None):
        super().__init__()
        self.session, self.settings, self.recorder = session, settings, recorder
        self.message = "Saving the recording…" if recorder else "Starting…"

    def run(self) -> None:
        try:
            if self.recorder is not None:
                self.recorder.stop()
            meeting, _ = pipeline.prepare(self.session, self.settings, status=self.status.emit)
            self.done.emit(meeting)
        except Exception as e:  # network, API credit, no speech… keep the recording and report
            self.failed.emit(str(e))


# --------------------------------------------------------------------------- the app


class Watcher(QObject):
    changed = Signal()                 # something the window shows has changed
    _action = Signal(int, str)         # notification button presses, moved onto the UI thread

    def __init__(self):
        super().__init__()
        self.s = config.load()
        state = _load_state()
        self.mode = state.get("mode", self.s.detect)
        self.ignore: set[str] = {a.lower() for a in [*self.s.detect_ignore, *state.get("ignore", [])]}
        self.detector = detect.Detector(sorted(self.ignore))

        self.notifier = notify.Notifier(lambda nid, key: self._action.emit(nid, key))
        self._action.connect(self._on_action)

        self.seen: dict[str, int] = {}     # call key -> consecutive polls seen
        self.snoozed: set[str] = set()     # "Not now" for this call; cleared when the call ends
        self.asked: detect.Call | None = None
        self.ask_id = 0
        self.call: detect.Call | None = None  # the call being recorded (None if started by hand)
        self.recorder: audio.Recorder | None = None
        self.session: Path | None = None
        self.rec_started = 0.0
        self.last_heard = 0.0
        self.rec_id = 0
        self.jobs: dict[Path, _Processor] = {}       # session -> note-making in progress
        self.prepared: dict[Path, obsidian.Meeting] = {}  # session -> notes ready to review
        self.ready_ids: dict[int, Path] = {}          # "Notes ready" notification -> session
        self.window = None
        self._hid_once = False

        self.tray = QSystemTrayIcon(icon("idle"))
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self._build_tray_menu)
        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._tray_clicked)
        self.tray.messageClicked.connect(self._tray_message_clicked)
        self.tray.show()
        self._build_tray_menu()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(POLL_MS)
        self._update_tray()

    # ------------------------------------------------------------------ detection loop

    def tick(self) -> None:
        watching = self.mode != "off" or (self.recorder is not None and self.call is not None)
        calls = self.detector.poll() if watching else []
        present = {c.key: c for c in calls}
        self.snoozed &= set(present)
        self.seen = {k: self.seen.get(k, 0) + 1 for k in present}

        if self.asked and self.asked.key not in present:  # call ended before anyone answered
            self.notifier.close(self.ask_id)
            self.asked, self.ask_id = None, 0

        if self.recorder is not None and self.call is not None:
            if self.call.key in present:
                self.last_heard = time.time()
                now = present[self.call.key]
                if self.recorder.follow(now.mic, now.speaker):  # e.g. a headset was plugged in
                    self.call = now
            elif time.time() - self.last_heard > self.s.stop_after:
                self.stop()
        elif self.recorder is None and self.asked is None and self.mode != "off":
            for c in calls:
                if c.key in self.snoozed or self.seen[c.key] < CONFIRM_POLLS:
                    continue
                if self.mode == "auto" and c.confident:
                    self.start(c)
                else:
                    self.ask(c)
                break
        self._update_tray()

    def ask(self, call: detect.Call) -> None:
        self.asked = call
        if call.confident:
            summary, question = f"Meeting detected: {call.app}", "Record it and take notes?"
        else:
            summary, question = f"{call.app} is using your microphone", "Is this a meeting? Record it and take notes?"
        body = (f"“{call.title_hint}”\n" if call.title_hint else "") + question
        if self.notifier.actions:
            self.ask_id = self.notifier.show(summary, body, [("record", "Record"), ("skip", "Not now"),
                                                             ("never", f"Never for {_short(call.app)}")], sticky=True)
        else:  # notification server without buttons: fall back to a clickable tray message
            self.ask_id = -1
            self.tray.showMessage(summary, body + "\nClick to record.", icon("idle"), 15000)

    def _on_action(self, nid: int, key: str) -> None:
        if self.asked and nid == self.ask_id:
            call, self.asked, self.ask_id = self.asked, None, 0
            if key == "record":
                self.start(call)
            elif key == "never":
                self.set_ignored(call.key, True)
                self.notifier.show(f"Won't ask about {_short(call.app)} again",
                                   "You can change this in Hear Me Out's settings menu.")
            else:  # "Not now", or the notification was dismissed
                self.snoozed.add(call.key)
        elif nid and nid == self.rec_id and key == "stop":
            self.stop()
        elif nid in self.ready_ids and key in ("open", "default"):
            self.show_window(f"pending:{self.ready_ids[nid]}")

    def _tray_message_clicked(self) -> None:
        if self.asked and self.ask_id == -1:
            call, self.asked, self.ask_id = self.asked, None, 0
            self.start(call)

    # ------------------------------------------------------------------ recording

    def start(self, call: detect.Call | None = None) -> None:
        if self.recorder is not None:
            return
        if self.asked:
            self.notifier.close(self.ask_id)
            self.asked, self.ask_id = None, 0
        self.s = config.load()  # pick up any config edits
        self.session = pipeline.new_session(app=call.app if call else None,
                                            title_hint=call.title_hint if call else None)
        self.recorder = audio.Recorder(self.session)
        try:
            self.recorder.start()
        except RuntimeError as e:
            self.recorder = None
            self.notifier.show("Couldn't start recording", str(e), urgent=True)
            return
        self.call = call
        if call:  # record from the devices the call uses (say, a headset), once our streams exist
            QTimer.singleShot(600, lambda: self.recorder and self.recorder.follow(call.mic, call.speaker))
        self.rec_started = self.last_heard = time.time()
        what = f"{call.app} meeting" if call else "meeting"
        body = ("Recording stops by itself when the call ends." if call
                else "Stop from here or the tray icon when you're done.")
        self.rec_id = self.notifier.show(f"● Recording {what}", body, [("stop", "Stop recording")], sticky=True)
        self._update_tray()
        self.changed.emit()

    def stop(self) -> None:
        if self.recorder is None:
            return
        recorder, session, call = self.recorder, self.session, self.call
        self.recorder = self.session = self.call = None
        if call:
            self.snoozed.add(call.key)  # don't ask again about the same call if it's still going
        nid = self.notifier.show("Making notes…", "Transcribing and summarising your meeting.", replaces=self.rec_id)
        self.rec_id = 0
        self.process(session, recorder, nid)
        if self.window is not None and self.window.isVisible():
            self.window.select(f"pending:{session}")

    def process(self, session: Path, recorder: audio.Recorder | None = None, nid: int = 0) -> None:
        """Make notes for a recording (again, after a failure)."""
        if session in self.jobs:
            return
        job = _Processor(session, config.load(), recorder)

        def status(msg: str) -> None:
            job.message = msg
            self.changed.emit()

        job.status.connect(status)
        job.done.connect(lambda meeting: self._ready(session, meeting, nid))
        job.failed.connect(lambda err: self._failed(session, err, nid))
        job.finished.connect(lambda: self._job_finished(session))
        self.jobs[session] = job
        job.start()
        self._update_tray()
        self.changed.emit()

    def _job_finished(self, session: Path) -> None:
        self.jobs.pop(session, None)
        self._update_tray()
        self.changed.emit()

    def _ready(self, session: Path, meeting: obsidian.Meeting, nid: int) -> None:
        self.prepared[session] = meeting
        nid = self.notifier.show("Notes ready", f"“{meeting.title}”: choose what to save to Obsidian.",
                                 [("open", "Review and save")], replaces=nid)
        self.ready_ids[nid] = session
        self.show_window(f"pending:{session}")

    def _failed(self, session: Path, error: str, nid: int) -> None:
        self.notifier.show("Couldn't make notes", f"{error[:300]}\nThe recording is kept. Open Hear Me Out to try again.",
                           replaces=nid, urgent=True)

    def meeting_for(self, session: Path) -> obsidian.Meeting | None:
        """The prepared meeting for a recording whose notes are made (loaded from cache if needed)."""
        if session not in self.prepared and pipeline.is_ready(session) and session not in self.jobs:
            try:
                self.prepared[session], _ = pipeline.prepare(session, self.s)
            except Exception:
                return None
        return self.prepared.get(session)

    def finish(self, session: Path) -> None:
        """The recording's notes are in Obsidian: forget the recording."""
        self.prepared.pop(session, None)
        for nid, s in list(self.ready_ids.items()):
            if s == session:
                self.notifier.close(nid)
                del self.ready_ids[nid]
        pipeline.cleanup(session)
        self.changed.emit()

    def delete(self, session: Path) -> None:
        if session in self.jobs or session == self.session:
            return
        self.finish(session)

    # ------------------------------------------------------------------ settings

    def set_mode(self, mode: str) -> None:
        self.mode = mode
        _save_state(mode=mode)
        self._update_tray()
        self.changed.emit()

    def set_ignored(self, key: str, ignored: bool) -> None:
        (self.ignore.add if ignored else self.ignore.discard)(key)
        self.detector.ignore = set(self.ignore)
        _save_state(ignore=sorted(self.ignore - {a.lower() for a in self.s.detect_ignore}))

    def fill_settings_menu(self, m: QMenu) -> None:
        detect_on = QAction("Detect meetings", m, checkable=True, checked=self.mode != "off")
        detect_on.toggled.connect(lambda on: self.set_mode("ask" if on else "off"))
        m.addAction(detect_on)
        auto = QAction("Record without asking", m, checkable=True, checked=self.mode == "auto")
        auto.setEnabled(self.mode != "off")
        auto.toggled.connect(lambda on: self.set_mode("auto" if on else "ask"))
        m.addAction(auto)
        if self.ignore:
            sub = m.addMenu("Never ask about")
            for key in sorted(self.ignore):
                sub.addAction(f"{key.title()}: ask again", lambda k=key: self.set_ignored(k, False))
        login = QAction("Start at login", m, checkable=True, checked=autostart_enabled())
        login.toggled.connect(set_autostart)
        m.addAction(login)
        m.addAction("Settings file…", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(config.write_template()))))
        m.addSeparator()
        m.addAction("Quit Hear Me Out", self.quit)

    # ------------------------------------------------------------------ window + tray

    def show_window(self, select: str | None = None) -> None:
        from .app import MainWindow
        if self.window is None:
            self.window = MainWindow(self)
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
        if select:
            self.window.select(select)

    def window_hidden(self) -> None:
        if not self._hid_once and self.tray.isVisible():
            self._hid_once = True
            self.notifier.show("Hear Me Out is still running",
                               "It keeps watching for meetings. Open it again from the tray icon or app menu.")

    def status_text(self) -> str:
        if self.recorder is not None:
            m, s = divmod(int(time.time() - self.rec_started), 60)
            return f"Recording {self.call.app if self.call else 'meeting'} · {m:02d}:{s:02d}"
        if self.jobs:
            return "Making notes…"
        return "Watching for meetings" if self.mode != "off" else "Meeting detection is off"

    def _update_tray(self) -> None:
        state = "recording" if self.recorder is not None else "processing" if self.jobs else "idle"
        self.tray.setIcon(icon(state))
        self.tray.setToolTip(f"Hear Me Out: {self.status_text()}")

    def _tray_clicked(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self.show_window()

    def _build_tray_menu(self) -> None:
        m = self.menu
        m.clear()
        status = QAction(self.status_text(), m)
        status.setEnabled(False)
        m.addAction(status)
        if self.recorder is not None:
            m.addAction("■ Stop recording", self.stop)
        else:
            m.addAction("● Record now", self.start)
        waiting = len(pipeline.pending_sessions())
        m.addAction("Open Hear Me Out" + (f" ({waiting} to save)" if waiting else ""), self.show_window)
        m.addSeparator()
        self.fill_settings_menu(m)

    def quit(self) -> None:
        if self.recorder is not None:  # keep what was recorded; it shows up in the app to save later
            self.recorder.stop()
            self.recorder = None
        for job in list(self.jobs.values()):
            job.wait()
        if self.window is not None:
            self.window.player.stop()
        self.detector.close()
        self.tray.hide()
        QApplication.quit()


def run(show_window: bool, autostart: bool | None = None) -> int:
    if autostart is not None:
        set_autostart(autostart)
        print(f"Start at login {'on' if autostart else 'off'}.", file=sys.stderr)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("hearmeout")
    app.setApplicationDisplayName("Hear Me Out")
    app.setDesktopFileName(notify.DESKTOP_ENTRY)
    app.setWindowIcon(QIcon.fromTheme(notify.ICON))
    app.setQuitOnLastWindowClosed(False)  # closing the window keeps meeting detection running

    # Only one copy: if one is running, ask it to show its window and leave.
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(config.DATA_DIR / "app.lock"))
    if not lock.tryLock(100):
        sock = QLocalSocket()
        sock.connectToServer(SOCKET_NAME)
        if sock.waitForConnected(1000):
            sock.write(b"show" if show_window else b"ping")
            sock.waitForBytesWritten(1000)
            sock.disconnectFromServer()
        if not show_window:
            print("Hear Me Out is already running.", file=sys.stderr)
        return 0

    watcher = Watcher()
    QLocalServer.removeServer(SOCKET_NAME)
    server = QLocalServer()
    server.listen(SOCKET_NAME)

    def on_connection():
        conn = server.nextPendingConnection()
        conn.waitForReadyRead(500)
        if bytes(conn.readAll()) == b"show":
            watcher.show_window()
        conn.disconnectFromServer()

    server.newConnection.connect(on_connection)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("Note: no system tray found (on GNOME, enable the AppIndicator extension). "
              "Detection and notifications still work.", file=sys.stderr)
        show_window = True
    if show_window:
        watcher.show_window()
    print("Hear Me Out is running. Quit from the tray icon or the ⚙ menu, or press Ctrl+C.", file=sys.stderr)

    import signal
    signal.signal(signal.SIGINT, lambda *_: watcher.quit())
    keepalive = QTimer()  # let Python handle Ctrl+C while Qt's loop runs
    keepalive.start(500)
    keepalive.timeout.connect(lambda: None)
    return app.exec()
