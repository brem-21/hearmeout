"""Transcription with ElevenLabs Scribe, using multichannel mode so that
channel 0 (mic) is "Me" and channel 1 (system audio) is "Them"."""

from __future__ import annotations

import json
import mimetypes
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

API_URL = "https://api.elevenlabs.io/v1/speech-to-text"

# Start a new line when a speaker pauses this long (seconds).
PAUSE = 1.2


@dataclass
class Utterance:
    speaker: str
    start: float
    end: float
    text: str


def transcribe(audio: Path, api_key: str, model: str, *, language: str | None = None,
               keyterms: list[str] | None = None, me: str = "Me", two_track: bool = True) -> list[Utterance]:
    """two_track: the file is our stereo mic+system recording. Otherwise (any other
    audio file) speakers are told apart by diarization instead."""
    data: dict = {"model_id": model, "tag_audio_events": "false", "timestamps_granularity": "word"}
    data["use_multi_channel" if two_track else "diarize"] = "true"
    if language:
        data["language_code"] = language
    if keyterms:
        data["keyterms"] = keyterms  # sent as repeated form fields

    with open(audio, "rb") as f:
        resp = httpx.post(API_URL, headers={"xi-api-key": api_key}, data=data,
                          files={"file": (audio.name, f, mimetypes.guess_type(audio.name)[0] or "application/octet-stream")}, timeout=httpx.Timeout(30, read=1800))
    if resp.status_code != 200:
        raise RuntimeError(f"ElevenLabs transcription failed ({resp.status_code}): {resp.text[:500]}")
    body = resp.json()

    utterances: list[Utterance] = []
    if two_track:
        for channel in body.get("transcripts") or [body]:
            speaker = me if channel.get("channel_index", 0) == 0 else "Them"
            utterances += _group(channel.get("words", []), lambda w: speaker)
    else:
        utterances = _group(body.get("words", []),
                            lambda w: "Speaker " + str(int(str(w.get("speaker_id") or "0").rsplit("_", 1)[-1]) + 1))
    utterances.sort(key=lambda u: u.start)
    return utterances


def _group(words: list[dict], speaker_of) -> list[Utterance]:
    out: list[Utterance] = []
    for w in words:
        if w.get("type") != "word" or w.get("start") is None:
            continue
        speaker = speaker_of(w)
        if out and out[-1].speaker == speaker and w["start"] - out[-1].end < PAUSE:
            out[-1].text += " " + w["text"]
            out[-1].end = w["end"]
        else:
            out.append(Utterance(speaker, w["start"], w["end"], w["text"]))
    return out


def save(utterances: list[Utterance], path: Path) -> None:
    path.write_text(json.dumps([asdict(u) for u in utterances], indent=1))


def load(path: Path) -> list[Utterance]:
    return [Utterance(**u) for u in json.loads(path.read_text())]


def timestamp(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def as_text(utterances: list[Utterance]) -> str:
    return "\n".join(f"[{timestamp(u.start)}] {u.speaker}: {u.text}" for u in utterances)
