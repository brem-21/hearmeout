"""Find the local Obsidian vault and save meetings into it as plain Markdown files.

Obsidian doesn't need to be running: it picks up new files on its own. Every
item the user chooses to save becomes its own note in the meeting's folder:

  <vault>/Meetings/2026-09-23 Linux App Sync/
      2026-09-23 Linux App Sync - Summary.md
      2026-09-23 Linux App Sync - My To-dos.md
      2026-09-23 Linux App Sync - Team Tasks.md
      2026-09-23 Linux App Sync - Transcript.md
      2026-09-23 Linux App Sync.ogg              (only if audio is kept)

Tasks use the Obsidian Tasks plugin format ("- [ ] … 📅 YYYY-MM-DD"), so they
show up in Tasks/Dataview queries across the vault, but plain checkboxes work too.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import quote

from .llm import ActionItem, MeetingNotes
from .stt import Utterance, timestamp

HOME = Path.home()
# Where each Obsidian packaging format keeps its list of vaults.
OBSIDIAN_CONFIGS = [
    HOME / ".config/obsidian/obsidian.json",                                # .deb, AppImage, tarball
    HOME / ".var/app/md.obsidian.Obsidian/config/obsidian/obsidian.json",   # Flatpak
    HOME / "snap/obsidian/current/.config/obsidian/obsidian.json",          # Snap
]


def find_vaults() -> list[Path]:
    """All vaults Obsidian knows about, the open / most recently used first."""
    found: list[tuple[bool, int, Path]] = []
    for cfg in OBSIDIAN_CONFIGS:
        try:
            vaults = json.loads(cfg.read_text()).get("vaults", {})
        except (OSError, ValueError):
            continue
        for v in vaults.values():
            path = Path(v.get("path", ""))
            if path.is_dir():
                found.append((bool(v.get("open")), int(v.get("ts", 0)), path))
    found.sort(key=lambda t: (t[0], t[1]), reverse=True)
    seen, out = set(), []
    for *_, p in found:
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def resolve_vault(configured: str) -> Path:
    if configured:
        path = Path(configured).expanduser()
        if not path.is_dir():
            raise RuntimeError(f"Obsidian vault not found: {path}")
        return path
    vaults = find_vaults()
    if not vaults:
        raise RuntimeError("No Obsidian vault found. Set one with --vault or in config.toml.")
    return vaults[0]


def open_uri(vault: Path, note: Path) -> str:
    rel = note.relative_to(vault).with_suffix("")
    return f"obsidian://open?vault={quote(vault.name)}&file={quote(str(rel))}"


# --------------------------------------------------------------------------- meeting


@dataclass
class Meeting:
    """Everything we know about one meeting, ready to be saved."""
    title: str
    started: datetime
    duration_s: float
    utterances: list[Utterance]
    notes: MeetingNotes | None = None
    audio: Path | None = None
    me: str = "Me"  # the speaker label used for the user's own microphone
    # Tasks the user unticked in the review screen are left out of the saved notes.
    excluded_tasks: set[int] = field(default_factory=set)

    @property
    def tasks(self) -> list[tuple[int, ActionItem]]:
        items = self.notes.action_items if self.notes else []
        return [(i, a) for i, a in enumerate(items) if i not in self.excluded_tasks]

    @property
    def my_tasks(self) -> list[ActionItem]:
        return [a for _, a in self.tasks if a.owner_is_me]

    @property
    def team_tasks(self) -> list[ActionItem]:
        return [a for _, a in self.tasks if not a.owner_is_me]


# What can be saved, in save order. key -> (label, file suffix)
ITEMS = {
    "summary": ("Summary", " - Summary.md"),
    "my_todos": ("My to-dos", " - My To-dos.md"),
    "team_tasks": ("Team tasks", " - Team Tasks.md"),
    "transcript": ("Transcript", " - Transcript.md"),
    "audio": ("Audio recording", ".ogg"),
}


def available(meeting: Meeting) -> dict[str, str]:
    """Items that can be saved for this meeting, with a short description of each."""
    out = {}
    if meeting.notes:
        out["summary"] = f"{len(meeting.notes.decisions)} decisions, {len(meeting.notes.key_points)} key points"
        out["my_todos"] = _count(len(meeting.my_tasks), "task")
        out["team_tasks"] = _count(len(meeting.team_tasks), "task")
    out["transcript"] = _count(len(meeting.utterances), "line")
    if meeting.audio and meeting.audio.is_file():
        out["audio"] = f"{meeting.audio.stat().st_size / 1e6:.1f} MB"
    return out


def _count(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


# --------------------------------------------------------------------------- saving


@dataclass
class Progress:
    done: int          # items finished so far
    total: int
    key: str           # item just written
    label: str
    path: Path


def meeting_folder(vault: Path, folder: str, meeting: Meeting) -> Path:
    """A folder for this meeting that doesn't clash with an existing one."""
    base = f"{meeting.started:%Y-%m-%d} {_safe(meeting.title)}"
    path, n = vault / folder / base, 2
    while path.exists():
        path, n = vault / folder / f"{base} ({n})", n + 1
    return path


def save(meeting: Meeting, vault: Path, folder: str, keys: list[str]) -> Iterator[Progress]:
    """Write the chosen items one by one, yielding progress after each file."""
    keys = [k for k in ITEMS if k in keys]  # canonical order
    if not keys:
        return
    target = meeting_folder(vault, folder, meeting)
    target.mkdir(parents=True)
    name = target.name
    paths = {k: target / f"{name}{ITEMS[k][1]}" for k in keys}
    links = {k: _wikilink(vault, paths[k], ITEMS[k][0]) for k in keys if k != "audio"}

    for i, key in enumerate(keys, 1):
        path = paths[key]
        if key == "audio":
            shutil.copy2(meeting.audio, path)
        else:
            others = [links[k] for k in links if k != key]
            path.write_text(_RENDER[key](meeting, others))
        yield Progress(i, len(keys), key, ITEMS[key][0], path)


def main_note(paths: list[Path]) -> Path | None:
    """The note to open after saving: the summary if saved, else the first note."""
    notes = [p for p in paths if p.suffix == ".md"]
    return next((p for p in notes if p.name.endswith(" - Summary.md")), notes[0] if notes else None)


# --------------------------------------------------------------------------- rendering


def _safe(name: str) -> str:
    name = re.sub(r'[\\/:*?"<>|#^\[\]]', "", name).strip().strip(".")
    return re.sub(r"\s+", " ", name)[:80] or "Meeting"


def _wikilink(vault: Path, path: Path, label: str) -> str:
    return f"[[{path.relative_to(vault).with_suffix('').as_posix()}|{label}]]"


def _frontmatter(m: Meeting, kind: str) -> list[str]:
    participants = m.notes.participants if m.notes else []
    return [
        "---",
        f"date: {m.started:%Y-%m-%d}",
        f'time: "{m.started:%H:%M}"',
        f"duration_minutes: {max(1, round(m.duration_s / 60))}",
        f"participants: [{', '.join(json.dumps(p, ensure_ascii=False) for p in participants)}]",
        f"tags: [meeting, meeting/{kind}]",
        f"meeting: {json.dumps(m.title, ensure_ascii=False)}",
        "source: hearmeout",
        "---",
        "",
    ]


def _see_also(others: list[str]) -> list[str]:
    return ["", "---", "Also from this meeting: " + " · ".join(others), ""] if others else [""]


def _task_lines(items: list[ActionItem], with_owner: bool) -> list[str]:
    if not items:
        return ["_None_"]
    lines = []
    for a in items:
        owner = f"**{a.owner}**: " if with_owner and a.owner else ""
        due = f" 📅 {a.due}" if a.due else ""
        lines.append(f"- [ ] {owner}{a.task}{due}")
        if a.evidence:
            lines.append(f"    - > {a.evidence}")
    return lines


def _render_summary(m: Meeting, others: list[str]) -> str:
    n = m.notes
    body = _frontmatter(m, "summary") + [f"# {m.title}", "", n.summary, ""]
    if n.decisions:
        body += ["## Decisions", "", *[f"- {d}" for d in n.decisions], ""]
    if n.key_points:
        body += ["## Key points", "", *[f"- {p}" for p in n.key_points], ""]
    return "\n".join(body + _see_also(others))


def _render_my_todos(m: Meeting, others: list[str]) -> str:
    body = _frontmatter(m, "todos") + [f"# My to-dos: {m.title}", "", *_task_lines(m.my_tasks, False)]
    return "\n".join(body + _see_also(others))


def _render_team_tasks(m: Meeting, others: list[str]) -> str:
    body = _frontmatter(m, "tasks") + [f"# Team tasks: {m.title}", "", *_task_lines(m.team_tasks, True)]
    return "\n".join(body + _see_also(others))


def _render_transcript(m: Meeting, others: list[str]) -> str:
    body = _frontmatter(m, "transcript") + [f"# Transcript: {m.title}", ""]
    body += [f"**{u.speaker}** `{timestamp(u.start)}`  \n{u.text}\n" for u in m.utterances]
    return "\n".join(body + _see_also(others))


_RENDER: dict[str, Callable[[Meeting, list[str]], str]] = {
    "summary": _render_summary,
    "my_todos": _render_my_todos,
    "team_tasks": _render_team_tasks,
    "transcript": _render_transcript,
}
