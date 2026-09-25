"""The meetings the app shows: ones already saved in Obsidian (read back from the
vault, so edits made in Obsidian show up) and recordings still waiting to be saved."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import cached_property
from datetime import datetime
from pathlib import Path

import soundfile as sf

from . import config, obsidian, pipeline

TASK = re.compile(r"^(\s*- \[)([ xX])(\] .*)$")
TRANSCRIPT_LINE = re.compile(r"^\*\*(?P<speaker>.+?)\*\* `(?P<ts>[\d:]+)`\s*$")


@dataclass
class Task:
    file: Path
    line: int          # line number in the file, for ticking it off
    text: str
    done: bool
    quote: str = ""


@dataclass
class SavedMeeting:
    folder: Path
    vault: Path
    title: str
    started: datetime
    duration_min: int
    participants: list[str]
    files: dict[str, Path]  # item key -> file, for the items that were saved
    _text: str | None = field(default=None, repr=False)

    @property
    def key(self) -> str:
        return f"saved:{self.folder}"

    @property
    def audio(self) -> Path | None:
        return self.files.get("audio")

    def text(self, key: str) -> str:
        """A saved note's Markdown without its frontmatter and "Also from this meeting" footer."""
        path = self.files.get(key)
        if not path or not path.exists():
            return ""
        body = path.read_text(encoding="utf-8", errors="replace")
        if body.startswith("---\n"):
            body = body.split("\n---\n", 1)[-1]
        return body.split("\n---\nAlso from this meeting:", 1)[0].strip()

    def tasks(self, key: str) -> list[Task]:
        path = self.files.get(key)
        if not path or not path.exists():
            return []
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        out: list[Task] = []
        for i, line in enumerate(lines):
            m = TASK.match(line)
            if m and not line.startswith(" "):
                out.append(Task(path, i, m.group(3)[2:].strip(), m.group(2) != " "))
            elif out and line.strip().startswith("- >"):
                out[-1].quote = line.strip()[3:].strip()
        return out

    def open_todos(self, team: bool = False) -> int:
        """Open to-dos (yours; with team=True, the team's tasks too)."""
        keys = ("my_todos", "team_tasks") if team else ("my_todos",)
        return sum(not t.done for k in keys for t in self.tasks(k))

    def transcript(self) -> list[tuple[str, float, str]]:
        """(speaker, start seconds, text) for each line of the saved transcript."""
        rows: list[tuple[str, float, str]] = []
        speaker, start = None, 0.0
        for line in self.text("transcript").splitlines():
            m = TRANSCRIPT_LINE.match(line)
            if m:
                speaker = m["speaker"]
                parts = [int(x) for x in m["ts"].split(":")]
                start = sum(v * 60 ** i for i, v in enumerate(reversed(parts)))
            elif speaker and line.strip():
                rows.append((speaker, float(start), line.strip()))
                speaker = None
        return rows

    def matches(self, query: str) -> bool:
        if self._text is None:
            self._text = " ".join([self.title, *self.participants] +
                                  [self.text(k) for k in ("summary", "my_todos", "team_tasks", "transcript")]).lower()
        return all(word in self._text for word in query.lower().split())


def set_task_done(task: Task, done: bool) -> None:
    """Tick or untick a task in its Obsidian note."""
    lines = task.file.read_text(encoding="utf-8").splitlines(keepends=True)
    m = TASK.match(lines[task.line].rstrip("\n"))
    if m:
        end = "\n" if lines[task.line].endswith("\n") else ""
        lines[task.line] = f"{m.group(1)}{'x' if done else ' '}{m.group(3)}{end}"
        task.file.write_text("".join(lines), encoding="utf-8")
        task.done = done


def _frontmatter(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            if f.readline().strip() != "---":
                return out
            for line in f:
                if line.strip() == "---":
                    break
                key, _, value = line.partition(":")
                out[key.strip()] = value.strip()
    except OSError:
        pass
    return out


def _json(value: str, default):
    try:
        return json.loads(value)
    except ValueError:
        return default


def _files_in(folder: Path) -> dict[str, Path]:
    """A meeting folder's notes, by the end of their names (" - Summary.md"…), so they're
    still found after you rename the folder or the files in Obsidian."""
    files: dict[str, Path] = {}
    for p in folder.iterdir():
        for key, (_, suffix) in obsidian.ITEMS.items():
            if key not in files and p.name.endswith(suffix) and p.is_file():
                files[key] = p
    return files


def saved_meetings(s: config.Settings) -> list[SavedMeeting]:
    """Meetings Hear Me Out saved into any known vault, newest first: the vault and folder in
    Settings, every other place you've saved to, and any folder of them inside (so a meeting
    you moved into a subfolder in Obsidian is still found)."""
    places: list[tuple[Path, str]] = []
    if s.vault and Path(s.vault).expanduser().is_dir():
        places.append((Path(s.vault).expanduser(), s.folder))
    places += [(v, s.folder) for v in obsidian.find_vaults()]
    places += obsidian.saved_places()
    out: list[SavedMeeting] = []
    seen: set[Path] = set()
    for vault, folder in places:
        base = vault / folder
        if not base.is_dir():
            continue
        # every note Hear Me Out wrote under this folder, however deep
        for note in base.rglob("*.md"):
            meeting_dir = note.parent
            if meeting_dir in seen or not any(note.name.endswith(sfx) for _, sfx in obsidian.ITEMS.values()):
                continue
            seen.add(meeting_dir)
            files = _files_in(meeting_dir)
            notes = [p for k, p in files.items() if k != "audio"]
            fm = _frontmatter(notes[0]) if notes else {}
            if fm.get("source") != "hearmeout":
                continue
            try:
                started = datetime.strptime(f"{fm.get('date')} {fm.get('time', '00:00').strip(chr(34))}",
                                            "%Y-%m-%d %H:%M")
            except ValueError:
                started = datetime.fromtimestamp(meeting_dir.stat().st_mtime)
            out.append(SavedMeeting(
                folder=meeting_dir, vault=vault, title=_json(fm.get("meeting", '""'), "") or meeting_dir.name[11:],
                started=started, duration_min=int(fm.get("duration_minutes") or 0),
                participants=_json(fm.get("participants", "[]"), []), files=files))
    out.sort(key=lambda m: m.started, reverse=True)
    return out


@dataclass
class PendingRecording:
    """A recording whose notes haven't been saved to Obsidian yet."""
    session: Path
    app: str | None
    title_hint: str | None
    started: datetime
    duration_s: float
    people: list[str] = field(default_factory=list)
    event_title: str | None = None  # the calendar event it was matched to

    @property
    def key(self) -> str:
        return f"pending:{self.session}"

    @property
    def audio(self) -> Path:
        return self.session / "audio.ogg"

    @cached_property
    def title(self) -> str:
        try:  # the model's title, once notes are made
            return json.loads((self.session / "notes.json").read_text())["title"]
        except (OSError, ValueError, KeyError):
            pass
        if self.event_title:
            return self.event_title
        if self.title_hint:
            return self.title_hint
        if self.people:
            return f"Call with {', '.join(p.split()[0] for p in self.people)}"
        return f"{self.app or 'Recording'}, {self.started:%H:%M}"

    @property
    def error(self) -> str | None:
        return pipeline.error(self.session)

    @property
    def ready(self) -> bool:
        return pipeline.is_ready(self.session)


def _event_title(info: dict) -> str | None:
    """The matched calendar event's title, unless it can't be this call (e.g. a Slack call
    matched to the Teams meeting booked at the same time, before matching was stricter)."""
    from . import outlook
    if not info.get("event"):
        return None
    try:
        event = outlook.Event.from_dict(info["event"])
    except (KeyError, TypeError, ValueError):
        return None
    return event.subject if outlook.fits(event, info.get("app"), info.get("people") or []) else None


def pending_recordings() -> list[PendingRecording]:
    out = []
    for session in reversed(pipeline.pending_sessions()):
        info = pipeline.session_info(session)
        try:
            duration = sf.info(session / "audio.ogg").duration
        except RuntimeError:
            duration = 0.0
        try:
            started = datetime.strptime(session.name, pipeline.SESSION_FMT)
        except ValueError:
            continue
        out.append(PendingRecording(session, info.get("app"), info.get("title_hint"), started, duration,
                                    info.get("people") or [], _event_title(info)))
    return out


def when(dt: datetime) -> str:
    """'Today 21:21', 'Yesterday 09:00', 'Mon 21 Sep 14:00', '3 Mar 2025'."""
    today = datetime.now().date()
    days = (today - dt.date()).days
    if days == 0:
        return f"Today {dt:%H:%M}"
    if days == 1:
        return f"Yesterday {dt:%H:%M}"
    if dt.year == today.year:
        return f"{dt:%a %-d %b} {dt:%H:%M}"
    return f"{dt:%-d %b %Y}"
