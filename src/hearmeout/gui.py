"""Stand-alone review window for `hearmeout record` / `hearmeout process`: the same
meeting view and "Save to Obsidian" bar as the app, in a dialog."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout

from . import config, notify, obsidian
from .obsidian import Meeting

CHOICES_FILE = config.DATA_DIR / "last-choices.json"


def _load_choices(default: list[str]) -> list[str]:
    """The items chosen last time, so the next meeting starts the same way."""
    try:
        return list(json.loads(CHOICES_FILE.read_text()))
    except (OSError, ValueError):
        return default


def _save_choices(keys: list[str]) -> None:
    try:
        CHOICES_FILE.parent.mkdir(parents=True, exist_ok=True)
        CHOICES_FILE.write_text(json.dumps(keys))
    except OSError:
        pass  # only a convenience


class ReviewWindow(QDialog):
    def __init__(self, meeting: Meeting, vaults: list[Path], vault: Path | None, folder: str, initial: list[str]):
        super().__init__()
        from .player import Player
        from .views import MeetingView, SaveBar, from_meeting
        self.setWindowTitle("Save meeting · Hear Me Out")
        self.resize(1000, 740)
        self.meeting = meeting
        self.saved: list[Path] = []
        self.player = Player()
        self.view = MeetingView(from_meeting(meeting), self.player, editable_title=True)
        self.bar = SaveBar(meeting, self.view.title, vaults, vault, folder, initial, _save_choices)
        self.view.bottom_slot.addWidget(self.bar)
        self.view.task_toggled.connect(
            lambda item, keep: ((meeting.excluded_tasks.discard if keep else meeting.excluded_tasks.add)(item.ref),
                                self.bar.update_counts()))
        self.bar.saved.connect(self._done)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.view)

    @property
    def vault_path(self) -> Path | None:
        return self.bar.vault if self.saved else None

    def _done(self, paths: list[Path]) -> None:
        self.saved = paths
        QTimer.singleShot(1200, self.accept)

    def reject(self) -> None:
        if not self.bar.busy:  # can't close mid-save
            self.player.stop()
            super().reject()


def review(meeting: Meeting, *, vault: Path | None, folder: str,
           default_keys: list[str]) -> tuple[Path | None, list[Path]]:
    """Show the review window. Returns (vault saved to, saved paths); the list is
    empty if the window was closed without saving."""
    from . import theme
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationDisplayName("Hear Me Out")
    app.setDesktopFileName(notify.DESKTOP_ENTRY)
    theme.apply(app)
    win = ReviewWindow(meeting, obsidian.find_vaults(), vault, folder, _load_choices(default_keys))
    win.exec()
    return win.vault_path, win.saved
