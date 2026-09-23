"""Recording → transcript → notes, shared by the command line and the background watcher.

Each recording lives in its own session folder under ~/.local/share/hearmeout/recordings/
until its notes are saved. Every step caches its result there, so if something fails
(no network, out of credit) nothing is paid for twice when it's retried.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable

import soundfile as sf

from . import config, llm, obsidian, stt

SESSION_FMT = "%Y-%m-%d_%H-%M-%S"


def new_session(app: str | None = None, title_hint: str | None = None) -> Path:
    """Create a session folder for a new recording, remembering where it came from."""
    path = config.DATA_DIR / "recordings" / datetime.now().strftime(SESSION_FMT)
    path.mkdir(parents=True, exist_ok=True)
    (path / "session.json").write_text(json.dumps({"app": app, "title_hint": title_hint}))
    return path


def pending_sessions() -> list[Path]:
    """Recordings whose notes were never saved (closed without saving, or failed)."""
    root = config.DATA_DIR / "recordings"
    return sorted(p for p in root.glob("*") if (p / "audio.ogg").is_file()) if root.is_dir() else []


def prepare(path: Path, s: config.Settings, *, title: str | None = None,
            status: Callable[[str], None] = lambda msg: None) -> tuple[obsidian.Meeting, Path]:
    """Transcribe and summarise a session folder or any audio file.
    Returns the meeting and its working folder (delete it with cleanup() once saved)."""
    if path.is_dir():  # one of our session folders
        work, audio_file, two_track = path, path / "audio.ogg", True
        started = datetime.strptime(path.name, SESSION_FMT)
    else:  # any audio/video file
        audio_file, two_track = path, False
        started = datetime.fromtimestamp(path.stat().st_mtime)
        work = config.DATA_DIR / "recordings" / started.strftime(SESSION_FMT)
        work.mkdir(parents=True, exist_ok=True)
    if not audio_file.is_file():
        raise RuntimeError(f"No audio found at {audio_file}")
    try:
        duration = sf.info(audio_file).duration
    except RuntimeError:
        duration = 0.0  # a format libsndfile can't read (e.g. mp4); ElevenLabs still can
    try:
        info = json.loads((work / "session.json").read_text())
    except (OSError, ValueError):
        info = {}
    me = s.user_name or "Me"

    # 1. Transcript
    cached = work / "transcript.json"
    if cached.exists():
        utterances = stt.load(cached)
    else:
        if not s.elevenlabs_api_key:
            raise RuntimeError("Missing ElevenLabs API key: set ELEVENLABS_API_KEY or [stt] api_key in config.toml")
        status(f"Transcribing with ElevenLabs {s.stt_model}…")
        keyterms = [t for t in [s.user_name, *s.user_aliases, *s.keyterms] if t]
        utterances = stt.transcribe(audio_file, s.elevenlabs_api_key, s.stt_model, language=s.language,
                                    keyterms=keyterms, me=me, two_track=two_track)
        stt.save(utterances, cached)
    if not utterances:
        raise RuntimeError("No speech was detected in the recording.")
    status(f"{len(utterances)} lines of transcript.")

    # 2. Notes
    notes = None
    cached_notes = work / "notes.json"
    if cached_notes.exists():
        notes = llm.MeetingNotes.model_validate_json(cached_notes.read_text())
    elif s.openrouter_api_key:
        status(f"Writing notes with {s.llm_model}…")
        notes = llm.summarize(stt.as_text(utterances), api_key=s.openrouter_api_key, base_url=s.llm_base_url,
                              model=s.llm_model, me=me, names=[s.user_name, *s.user_aliases],
                              day=started.date())
        cached_notes.write_text(llm.notes_to_json(notes))
    else:
        status("No model API key (OPENROUTER_API_KEY), so only the transcript can be saved.")

    title = title or info.get("title_hint") or (notes.title if notes else None) or f"Meeting {started:%H:%M}"
    meeting = obsidian.Meeting(title=title, started=started, duration_s=duration or utterances[-1].end,
                               utterances=utterances, notes=notes, audio=audio_file)
    return meeting, work


def cleanup(work: Path) -> None:
    """Recordings are temporary: delete once the chosen items are safely in the vault."""
    shutil.rmtree(work, ignore_errors=True)
