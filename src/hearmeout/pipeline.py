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

from . import config, detect, llm, obsidian, outlook, stt

SESSION_FMT = "%Y-%m-%d_%H-%M-%S"


def new_session(app: str | None = None, title_hint: str | None = None, people: list[str] | None = None,
                event: outlook.Event | None = None) -> Path:
    """Create a session folder for a new recording, remembering where it came from."""
    path = config.DATA_DIR / "recordings" / datetime.now().strftime(SESSION_FMT)
    path.mkdir(parents=True, exist_ok=True)
    info = {"app": app, "title_hint": title_hint, "people": list(people or [])}
    if event:
        info["event"] = event.to_dict()
    (path / "session.json").write_text(json.dumps(info))
    return path


def _remember(session: Path, **changes) -> None:
    try:
        info = json.loads((session / "session.json").read_text())
    except (OSError, ValueError):
        info = {}
    (session / "session.json").write_text(json.dumps({**info, **changes}))


def calendar_event(session: Path, started: datetime, info: dict,
                   status: Callable[[str], None] = lambda msg: None) -> outlook.Event | None:
    """The calendar event this recording belongs to (looked up once, then kept with the recording).
    A calendar problem never stops notes from being made."""
    if info.get("event"):
        return outlook.Event.from_dict(info["event"])
    if info.get("event_checked") or not outlook.connected():
        return None
    try:
        event = outlook.event_for(started, info.get("title_hint"), info.get("app"))
    except Exception as e:  # offline, signed out…
        status(f"Couldn't check your calendar ({e}).")
        return None
    _remember(session, event=event.to_dict() if event else None, event_checked=True)
    if event:
        status(f"Matched to “{event.subject}” in your calendar.")
    return event


def session_info(session: Path) -> dict:
    """app, title_hint and people for a recording (older recordings: worked out from the title)."""
    try:
        info = json.loads((session / "session.json").read_text())
    except (OSError, ValueError):
        info = {}
    hint = info.get("title_hint") or ""
    if not info.get("people"):
        info["people"] = detect.people_from_title(hint)
    if hint and detect.people_from_title(hint):
        info["title_hint"] = None  # "Richard N (DM) - Team" names a person, not the meeting
    return info


def other_speaker(notes, people: list[str]) -> str | None:
    """The name to show instead of "Them" in a one-on-one, if we know it."""
    if len(people) == 1:
        return people[0]
    found = [n for n in (notes.other_speakers if notes else []) if n]
    return found[0] if len(found) == 1 else None


def pending_sessions() -> list[Path]:
    """Recordings whose notes were never saved (closed without saving, or failed)."""
    root = config.DATA_DIR / "recordings"
    return sorted(p for p in root.glob("*") if (p / "audio.ogg").is_file()) if root.is_dir() else []


def prepare(path: Path, s: config.Settings, *, title: str | None = None,
            status: Callable[[str], None] = lambda msg: None) -> tuple[obsidian.Meeting, Path]:
    """Transcribe and summarise a session folder or any audio file.
    Returns the meeting and its working folder (delete it with cleanup() once saved).
    If it fails, the reason is kept in the session folder (see error())."""
    error_file = path / "error.txt" if path.is_dir() else None
    try:
        result = _prepare(path, s, title=title, status=status)
    except Exception as e:
        if error_file:
            error_file.write_text(str(e))
        raise
    if error_file and error_file.exists():
        error_file.unlink()
    return result


def error(session: Path) -> str | None:
    """Why the last attempt to make notes for this recording failed, if it did."""
    try:
        return (session / "error.txt").read_text() or None
    except OSError:
        return None


def is_ready(session: Path) -> bool:
    """Notes (or at least a transcript, when no model key is set) are already made."""
    return (session / "notes.json").exists()


def _prepare(path: Path, s: config.Settings, *, title: str | None,
             status: Callable[[str], None]) -> tuple[obsidian.Meeting, Path]:
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
    info = session_info(work)
    people = info.get("people") or []
    me = s.user_name or "Me"
    event = calendar_event(work, started, info, status)
    invited = event.others([s.user_name, *s.user_aliases]) if event else []
    if not people and len(invited) == 1:
        people = invited  # a one-on-one in the calendar

    # 1. Transcript
    cached = work / "transcript.json"
    if cached.exists():
        utterances = stt.load(cached)
    else:
        if not s.elevenlabs_api_key:
            raise RuntimeError("Missing ElevenLabs API key: set ELEVENLABS_API_KEY or [stt] api_key in config.toml")
        status(f"Transcribing with ElevenLabs {s.stt_model}…")
        keyterms = [t for t in [s.user_name, *s.user_aliases, *s.keyterms, *invited] if t]
        keyterms = list(dict.fromkeys(keyterms))[:50]  # attendees' names help transcription
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
                              day=started.date(), others=people, event=event)
        cached_notes.write_text(llm.notes_to_json(notes))
    else:
        status("No model API key (OPENROUTER_API_KEY), so only the transcript can be saved.")

    # One-on-one: show the other person's name instead of "Them".
    other = other_speaker(notes, people)
    if other and other != me:
        for u in utterances:
            if u.speaker == "Them":
                u.speaker = other
        if notes and other not in notes.participants:
            notes.participants.append(other)
    elif notes and people:
        notes.participants += [p for p in people if p not in notes.participants]

    title = (title or (event.subject if event else None) or info.get("title_hint")
             or (notes.title if notes else None) or f"Meeting {started:%H:%M}")
    meeting = obsidian.Meeting(title=title, started=started, duration_s=duration or utterances[-1].end,
                               utterances=utterances, notes=notes, audio=audio_file, me=me, event=event)
    return meeting, work


def cleanup(work: Path) -> None:
    """Recordings are temporary: delete once the chosen items are safely in the vault."""
    shutil.rmtree(work, ignore_errors=True)
