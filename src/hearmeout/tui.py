"""Terminal version of the review step, for when there's no desktop (or --no-gui)."""

from __future__ import annotations

import sys
from pathlib import Path

from . import obsidian
from .obsidian import ITEMS, Meeting


def _err(msg: str = "", end: str = "\n") -> None:
    print(msg, end=end, file=sys.stderr, flush=True)


def choose(meeting: Meeting, defaults: list[str]) -> list[str] | None:
    """Checklist prompt. Returns the chosen keys, or None to save nothing."""
    options = obsidian.available(meeting)
    keys = list(options)
    chosen = {k for k in keys if k in defaults}
    if meeting.notes:
        _err(f"\n  {meeting.title}\n")
        for a in meeting.my_tasks:
            _err(f"    ☐ {a.task}" + (f"  (due {a.due})" if a.due else ""))
    while True:
        _err("\nWhat should be saved to Obsidian?")
        for i, k in enumerate(keys, 1):
            _err(f"  [{'x' if k in chosen else ' '}] {i}  {ITEMS[k][0]:<16} {options[k]}")
        _err("Type numbers to tick/untick (e.g. 4 5), Enter to save, q to skip saving: ", end="")
        try:
            answer = input().strip().lower()
        except EOFError:
            answer = ""
        if answer == "q":
            return None
        if not answer:
            if chosen:
                return [k for k in keys if k in chosen]
            _err("Nothing is ticked.")
            continue
        for part in answer.replace(",", " ").split():
            if part.isdigit() and 1 <= int(part) <= len(keys):
                chosen ^= {keys[int(part) - 1]}


def save(meeting: Meeting, vault: Path, folder: str, keys: list[str]) -> list[Path]:
    """Save with a progress bar that moves one step per file."""
    paths: list[Path] = []
    width = 24
    total = len(keys)
    _bar(0, total, width, f"saving {ITEMS[keys[0]][0].lower()}…")
    for p in obsidian.save(meeting, vault, folder, keys):
        paths.append(p.path)
        _bar(p.done, p.total, width, f"✓ {p.label}")
    _err()
    return paths


def _bar(done: int, total: int, width: int, label: str) -> None:
    filled = round(width * done / total)
    bar = "█" * filled + "░" * (width - filled)
    _err(f"\r  {bar} {done}/{total}  {label:<30}", end="")
