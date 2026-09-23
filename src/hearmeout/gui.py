"""Review step after a meeting is processed: choose what to save to Obsidian,
check the notes, then save with a per-item progress bar.

ReviewPanel is embedded in the main app window; ReviewWindow wraps it as a
stand-alone dialog for the command line."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from PySide6.QtCore import QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QProgressBar, QPushButton, QSplitter, QStackedWidget,
    QTextBrowser, QVBoxLayout, QWidget,
)

from . import config, obsidian
from .obsidian import ITEMS, Meeting

CHOICES_FILE = config.DATA_DIR / "last-choices.json"

STYLE = """
QLabel#meta, QLabel#desc, QLabel#dest, QLabel#status { color: palette(placeholder-text); }
QLineEdit#title { font-size: 18px; font-weight: 600; padding: 6px 8px; }
QLabel#section { font-weight: 600; margin-top: 4px; }
QListWidget#items { border: none; background: transparent; }
QListWidget#items::item { border-radius: 6px; margin: 1px 0; border: 1px solid transparent; }
QListWidget#items::item:selected { background: palette(base); border: 1px solid palette(highlight); }
QPushButton#primary { font-weight: 600; padding: 7px 16px; }
QProgressBar { min-height: 20px; }
"""


def _load_choices(default: list[str]) -> list[str]:
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


class _Saver(QThread):
    """Writes the files off the UI thread (the audio copy can be large)."""
    progressed = Signal(object)  # obsidian.Progress
    failed = Signal(str)

    def __init__(self, meeting: Meeting, vault: Path, folder: str, keys: list[str]):
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


class _ItemRow(QWidget):
    """One row in the "Save to Obsidian" list: checkbox, name, short description."""

    def __init__(self, label: str, desc: str, checked: bool):
        super().__init__()
        self.box = QCheckBox()
        self.box.setChecked(checked)
        self.box.setAccessibleName(label)
        name = QLabel(label)
        f = name.font()
        f.setBold(True)
        name.setFont(f)
        self.desc = QLabel(desc)
        self.desc.setObjectName("desc")
        text = QVBoxLayout()
        text.setSpacing(0)
        text.addWidget(name)
        text.addWidget(self.desc)
        row = QHBoxLayout(self)
        row.setContentsMargins(8, 6, 8, 6)
        row.addWidget(self.box)
        row.addLayout(text, 1)


class ReviewPanel(QWidget):
    saved_paths = Signal(list)  # paths written, once saving has finished
    closed = Signal()      # "Close without saving" / "Done" (stand-alone window only)

    def __init__(self, meeting: Meeting, vaults: list[Path], vault: Path | None, folder: str,
                 initial: list[str], embedded: bool = False):
        super().__init__()
        self.embedded = embedded
        self.meeting = meeting
        self.saved: list[Path] = []
        self.vault_path: Path | None = None
        self._saver: _Saver | None = None
        self.setStyleSheet(STYLE)

        # --- header: title + meta
        self.title = QLineEdit(meeting.title)
        self.title.setObjectName("title")
        self.title.setPlaceholderText("Meeting title")
        self.title.setAccessibleName("Meeting title")
        who = ", ".join(meeting.notes.participants) if meeting.notes and meeting.notes.participants else ""
        mins = max(1, round(meeting.duration_s / 60))
        meta = QLabel(" · ".join(x for x in (f"{meeting.started:%A %-d %B %Y · %H:%M}", f"{mins} min", who) if x))
        meta.setObjectName("meta")

        # --- left: what to save + where
        available = obsidian.available(meeting)
        self.list = QListWidget()
        self.list.setObjectName("items")
        self.rows: dict[str, _ItemRow] = {}
        for key, desc in available.items():
            row = _ItemRow(ITEMS[key][0], desc, key in initial)
            row.box.toggled.connect(self._refresh)
            item = QListWidgetItem(self.list)
            item.setData(Qt.UserRole, key)
            item.setSizeHint(row.sizeHint())
            self.list.setItemWidget(item, row)
            self.rows[key] = row
        self.list.currentRowChanged.connect(self._show_preview)

        self.vault = QComboBox()
        self.vault.setAccessibleName("Obsidian vault")
        for v in vaults:
            self.vault.addItem(v.name, str(v))
            self.vault.setItemData(self.vault.count() - 1, str(v), Qt.ToolTipRole)
        if vault and str(vault) not in [str(v) for v in vaults]:
            self.vault.addItem(vault.name, str(vault))
        if vault:
            self.vault.setCurrentIndex(self.vault.findData(str(vault)))
        self.browse = browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        self.folder = QLineEdit(folder)
        self.folder.setAccessibleName("Folder inside the vault")
        self.dest = QLabel()
        self.dest.setObjectName("dest")
        self.dest.setWordWrap(True)
        for w in (self.title, self.folder):
            w.textChanged.connect(self._refresh)
        self.vault.currentIndexChanged.connect(self._refresh)

        left = QVBoxLayout()
        left.addWidget(self._section("Save to Obsidian"))
        left.addWidget(self.list, 1)
        left.addWidget(self._section("Vault"))
        vrow = QHBoxLayout()
        vrow.addWidget(self.vault, 1)
        vrow.addWidget(browse)
        left.addLayout(vrow)
        left.addWidget(self._section("Folder"))
        left.addWidget(self.folder)
        left.addWidget(self.dest)
        left_w = QWidget()
        left_w.setLayout(left)
        left_w.setMinimumWidth(270)

        # --- right: preview of the selected item
        self.stack = QStackedWidget()
        self.pages: dict[str, QWidget] = {}
        self.task_lists: dict[str, QListWidget] = {}
        for key in available:
            page = self._task_page(key) if key in ("my_todos", "team_tasks") else self._text_page(key)
            self.pages[key] = page
            self.stack.addWidget(page)

        split = QSplitter()
        split.addWidget(left_w)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 1)
        split.setSizes([290, 650])

        # --- footer: progress + actions
        self.progress = QProgressBar()
        self.progress.setVisible(False)
        self.status = QLabel()
        self.status.setObjectName("status")
        self.close_btn = QPushButton("Close without saving")
        self.close_btn.setToolTip("The recording is kept, so you can save it later with 'hearmeout process'.")
        self.close_btn.clicked.connect(self.closed.emit)
        self.close_btn.setVisible(not embedded)  # in the app, the recording simply stays in the list
        self.open_btn = QPushButton("Open in Obsidian")
        self.open_btn.setVisible(False)
        self.open_btn.clicked.connect(self._open_obsidian)
        self.folder_btn = QPushButton("Show folder")
        self.folder_btn.setVisible(False)
        self.folder_btn.clicked.connect(self._open_folder)
        self.save_btn = QPushButton()
        self.save_btn.setObjectName("primary")
        self.save_btn.setDefault(True)
        self.save_btn.clicked.connect(self._save)

        footer = QHBoxLayout()
        footer.addWidget(self.status, 1)
        footer.addWidget(self.close_btn)
        footer.addWidget(self.folder_btn)
        footer.addWidget(self.open_btn)
        footer.addWidget(self.save_btn)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)

        root = QVBoxLayout(self)
        root.setContentsMargins(*((0, 0, 0, 0) if embedded else (16, 14, 16, 14)))
        root.addWidget(self.title)
        root.addWidget(meta)
        root.addSpacing(6)
        root.addWidget(split, 1)
        root.addWidget(line)
        root.addWidget(self.progress)
        root.addLayout(footer)

        self.list.setCurrentRow(0)
        self._refresh()

    # ----------------------------------------------------------- building blocks

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("section")
        return label

    def _text_page(self, key: str) -> QWidget:
        view = QTextBrowser()
        view.setOpenLinks(False)
        if key == "audio":
            view.setMarkdown(f"**Audio recording** ({obsidian.available(self.meeting)['audio']})\n\n"
                             "The two-track recording (your mic on the left channel, the other people on the "
                             "right). If you don't save it, it is deleted once your notes are saved.")
        return view

    def _task_page(self, key: str) -> QWidget:
        tasks = [(i, a) for i, a in enumerate(self.meeting.notes.action_items) if a.owner_is_me == (key == "my_todos")]
        hint = QLabel("Untick any task you don't want saved. Hover a task to see where it was said.")
        hint.setObjectName("desc")
        hint.setWordWrap(True)
        lst = QListWidget()
        lst.setAccessibleName(ITEMS[key][0])
        for i, a in tasks:
            parts = [a.task]
            if key == "team_tasks" and a.owner:
                parts.insert(0, f"{a.owner}:")
            if a.due:
                parts.append(f"(due {self._due(a.due)})")
            item = QListWidgetItem(" ".join(parts))
            item.setData(Qt.UserRole, i)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setToolTip(f"“{a.evidence}”" if a.evidence else "")
            lst.addItem(item)
        if not tasks:
            empty = QListWidgetItem("No tasks for you were found in this meeting." if key == "my_todos"
                                    else "No tasks for other people were found in this meeting.")
            empty.setFlags(Qt.NoItemFlags)
            lst.addItem(empty)
        lst.itemChanged.connect(self._task_toggled)
        self.task_lists[key] = lst
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(hint)
        lay.addWidget(lst, 1)
        return page

    @staticmethod
    def _due(iso: str) -> str:
        from datetime import date
        try:
            return date.fromisoformat(iso).strftime("%a %-d %b")
        except ValueError:
            return iso

    # ----------------------------------------------------------- state

    def keys(self) -> list[str]:
        return [k for k, row in self.rows.items() if row.box.isChecked()]

    def _current_vault(self) -> Path | None:
        data = self.vault.currentData()
        return Path(data) if data else None

    def _sync_meeting(self) -> None:
        self.meeting.title = self.title.text().strip() or "Meeting"

    def _refresh(self) -> None:
        self._sync_meeting()
        n = len(self.keys())
        vault = self._current_vault()
        self.save_btn.setEnabled(n > 0 and vault is not None and self._saver is None)
        self.save_btn.setText(f"Save {n} item{'s' if n != 1 else ''} to Obsidian" if n else "Choose what to save")
        if self.vault_path is not None:
            pass  # saving started: keep showing where it went
        elif vault:
            folder = obsidian.meeting_folder(vault, self.folder.text().strip(), self.meeting)
            self.dest.setText(f"Saves to {folder.relative_to(vault)}/")
        else:
            self.dest.setText("Choose a vault to save to.")
        for key in ("my_todos", "team_tasks"):
            if key in self.rows:
                count = len(self.meeting.my_tasks if key == "my_todos" else self.meeting.team_tasks)
                self.rows[key].desc.setText(f"{count} task{'s' if count != 1 else ''}")
        self._show_preview(self.list.currentRow())

    def _show_preview(self, row: int) -> None:
        if row < 0:
            return
        key = self.list.item(row).data(Qt.UserRole)
        page = self.pages[key]
        self.stack.setCurrentWidget(page)
        if key in ("summary", "transcript"):
            text = obsidian._RENDER[key](self.meeting, [])
            page.setMarkdown(text.split("---\n", 2)[-1])  # hide the frontmatter

    def _task_toggled(self, item: QListWidgetItem) -> None:
        idx = item.data(Qt.UserRole)
        if idx is None:
            return
        if item.checkState() == Qt.Checked:
            self.meeting.excluded_tasks.discard(idx)
        else:
            self.meeting.excluded_tasks.add(idx)
        self._refresh()

    # ----------------------------------------------------------- actions

    def _browse(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Choose your Obsidian vault", str(Path.home()))
        if path:
            if self.vault.findData(path) < 0:
                self.vault.addItem(Path(path).name, path)
            self.vault.setCurrentIndex(self.vault.findData(path))

    def _save(self) -> None:
        keys, vault = self.keys(), self._current_vault()
        self._sync_meeting()
        _save_choices(keys)
        for w in (self.title, self.list, self.vault, self.browse, self.folder, self.close_btn, self.stack):
            w.setEnabled(False)
        self.progress.setRange(0, len(keys))
        self.progress.setValue(0)
        self.progress.setFormat(f"Saving 0 of {len(keys)}")
        self.progress.setVisible(True)
        self.status.setText(f"Saving {ITEMS[keys[0]][0].lower()}…")
        self._error = None
        self._saver = _Saver(self.meeting, vault, self.folder.text().strip() or "Meetings", keys)
        self._saver.progressed.connect(self._on_progress)
        self._saver.failed.connect(self._on_failed)
        self._saver.finished.connect(self._on_finished)
        self.vault_path = vault
        self._refresh()
        self._saver.start()

    def _on_progress(self, p: obsidian.Progress) -> None:
        self.progress.setValue(p.done)
        self.progress.setFormat(f"Saved {p.done} of {p.total}")
        nxt = self.keys()[p.done] if p.done < p.total else None
        self.status.setText(f"✓ {p.label} saved" + (f" · saving {ITEMS[nxt][0].lower()}…" if nxt else ""))

    def _on_failed(self, error: str) -> None:
        self._error = error

    def _on_finished(self) -> None:
        saver, self._saver = self._saver, None
        self.saved = saver.paths
        error = getattr(self, "_error", None)
        if error:
            self.progress.setFormat(f"Saved {len(self.saved)} of {self.progress.maximum()}")
            self.status.setText("Saving stopped.")
            QMessageBox.critical(self, "Couldn't save", f"Saving to Obsidian failed:\n\n{error}")
            self.close_btn.setEnabled(True)
            if self.saved:  # some files did make it: let the app know
                self.saved_paths.emit(self.saved)
            return
        folder = self.saved[0].parent.relative_to(self.vault_path) if self.saved else ""
        self.status.setText(f"✓ Saved {len(self.saved)} item{'s' if len(self.saved) != 1 else ''} to {folder}/")
        self.save_btn.setVisible(False)
        self.close_btn.setText("Done")
        self.close_btn.setToolTip("")
        self.close_btn.setEnabled(True)
        self.close_btn.setVisible(not self.embedded)
        self.open_btn.setVisible(obsidian.main_note(self.saved) is not None)
        self.folder_btn.setVisible(True)
        self.open_btn.setDefault(True)
        self.open_btn.setFocus()
        self.saved_paths.emit(self.saved)

    def _open_obsidian(self) -> None:
        note = obsidian.main_note(self.saved)
        if note:
            QDesktopServices.openUrl(QUrl(obsidian.open_uri(self.vault_path, note)))

    def _open_folder(self) -> None:
        if self.saved:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.saved[0].parent)))

    @property
    def busy(self) -> bool:
        return self._saver is not None


class ReviewWindow(QDialog):
    """The review panel as a stand-alone window (used by `hearmeout record` / `process`)."""

    def __init__(self, meeting: Meeting, vaults: list[Path], vault: Path | None, folder: str, initial: list[str]):
        super().__init__()
        self.setWindowTitle("Save meeting · Hear Me Out")
        self.resize(940, 640)
        self.panel = ReviewPanel(meeting, vaults, vault, folder, initial)
        self.panel.closed.connect(lambda: self.accept() if self.panel.saved else self.reject())
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.panel)

    @property
    def meeting(self) -> Meeting:
        return self.panel.meeting

    @property
    def saved(self) -> list[Path]:
        return self.panel.saved

    @property
    def vault_path(self) -> Path | None:
        return self.panel.vault_path

    def reject(self) -> None:
        if not self.panel.busy:  # can't close mid-save
            super().reject()


def available() -> bool:
    """Is there a display to show a window on?"""
    import os
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def review(meeting: Meeting, *, vault: Path | None, folder: str,
           default_keys: list[str]) -> tuple[Path | None, list[Path]]:
    """Show the review window. Returns (vault saved to, saved paths); the list is
    empty if the user closed the window without saving."""
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationDisplayName("Hear Me Out")
    app.setDesktopFileName("io.github.brem_21.hearmeout")
    initial = _load_choices(default_keys)
    win = ReviewWindow(meeting, obsidian.find_vaults(), vault, folder, initial)
    win.exec()
    return win.vault_path, win.saved
