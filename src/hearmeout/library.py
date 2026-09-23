"""The meetings the app shows: ones already saved in Obsidian (read back from the
vault, so edits made in Obsidian show up) and recordings still waiting to be saved."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
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

    def open_todos(self) -> int:
        return sum(not t.done for t in self.tasks("my_todos"))

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


def saved_meetings(s: config.Settings) -> list[SavedMeeting]:
    """Meetings Hear Me Out saved into any known vault, newest first."""
    vaults = obsidian.find_vaults()
    if s.vault and Path(s.vault).expanduser().is_dir():
        vaults.insert(0, Path(s.vault).expanduser())
    out: list[SavedMeeting] = []
    seen: set[Path] = set()
    for vault in vaults:
        base = vault / s.folder
        if base in seen or not base.is_dir():
            continue
        seen.add(base)
        for folder in base.iterdir():
            if not folder.is_dir():
                continue
            files = {key: folder / f"{folder.name}{suffix}" for key, (_, suffix) in obsidian.ITEMS.items()}
            files = {k: p for k, p in files.items() if p.exists()}
            notes = [p for k, p in files.items() if k != "audio"]
            if not notes:
                continue
            fm = _frontmatter(notes[0])
            if fm.get("source") != "hearmeout":
                continue
            try:
                started = datetime.strptime(f"{fm.get('date')} {fm.get('time', '00:00').strip(chr(34))}",
                                            "%Y-%m-%d %H:%M")
            except ValueError:
                started = datetime.fromtimestamp(folder.stat().st_mtime)
            out.append(SavedMeeting(
                folder=folder, vault=vault, title=_json(fm.get("meeting", '""'), "") or folder.name[11:],
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

    @property
    def key(self) -> str:
        return f"pending:{self.session}"

    @property
    def audio(self) -> Path:
        return self.session / "audio.ogg"

    @property
    def title(self) -> str:
        if self.title_hint:
            return self.title_hint
        try:
            return json.loads((self.session / "notes.json").read_text())["title"]
        except (OSError, ValueError, KeyError):
            pass
        if self.people:
            return f"Call with {', '.join(p.split()[0] for p in self.people)}"
        return f"{self.app or 'Recording'}, {self.started:%H:%M}"

    @property
    def error(self) -> str | None:
        return pipeline.error(self.session)

    @property
    def ready(self) -> bool:
        return pipeline.is_ready(self.session)


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
                                    info.get("people") or []))
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
