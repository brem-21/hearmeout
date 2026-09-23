"""Meeting notes from a transcript, via any OpenAI-compatible chat API
(OpenRouter by default; Ollama, LM Studio, etc. work by changing base_url)."""

from __future__ import annotations

import json
from datetime import date

import httpx
from pydantic import BaseModel, Field, ValidationError


class ActionItem(BaseModel):
    task: str = Field(description="What needs to be done, as a short imperative sentence.")
    owner: str | None = Field(description="Who is responsible, as named in the meeting; null if nobody was named.")
    owner_is_me: bool = Field(description="True if the owner is the user (the 'Me' speaker or one of their names).")
    due: str | None = Field(description="Due date as YYYY-MM-DD if one was stated or clearly implied, else null.")
    evidence: str = Field(description="Short verbatim quote from the transcript where this was agreed.")


class MeetingNotes(BaseModel):
    title: str = Field(description="Short descriptive meeting title, max 8 words, no date.")
    summary: str = Field(description="3-6 sentence overview of what the meeting was about and its outcome.")
    key_points: list[str] = Field(description="Main discussion points, one line each.")
    decisions: list[str] = Field(description="Decisions that were made. Empty if none.")
    action_items: list[ActionItem] = Field(description="Every task or follow-up someone committed to or was asked to do.")
    participants: list[str] = Field(description="Names of people who spoke or were mentioned as attending.")


SYSTEM = """You turn meeting transcripts into accurate notes.

The transcript has two speaker labels:
- "{me}" is the user: everything said into their microphone.
- "Them" is everyone else on the call (possibly several people).

The user is called {names}. A task belongs to the user (owner_is_me = true) when:
- the user commits to it ("I'll send the deck"), or
- someone asks the user by name to do it, or
- someone says "you" to the user and the user accepts or does not refuse.
A task addressed to another named person ("Kofi, please send the invoice") belongs to
that person, even if the user is the one listening. Tasks said by "Them" are NOT the
user's unless the user is named or clearly addressed.

Rules:
- Only include what is actually in the transcript. Never invent tasks, owners, dates or names.
- Every action item needs a short verbatim quote as evidence.
- The meeting took place on {day} ({weekday}); resolve relative dates like "Friday" or "next week" against it.
- Transcription can contain errors; interpret obvious mistakes sensibly.
- Write in the language used in the meeting."""


def _strict(schema):
    """Make a pydantic JSON schema acceptable to strict structured-output mode:
    every property required, no extra properties, no annotation-only keywords."""
    if isinstance(schema, list):
        return [_strict(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for key, value in schema.items():
        if key in ("title", "default"):
            continue  # annotations (a *property* called "title" lives inside "properties", handled below)
        if key in ("properties", "$defs"):
            out[key] = {name: _strict(sub) for name, sub in value.items()}
        else:
            out[key] = _strict(value)
    if out.get("type") == "object" and "properties" in out:
        out["additionalProperties"] = False
        out["required"] = list(out["properties"])
    return out


def summarize(transcript: str, *, api_key: str, base_url: str, model: str, me: str = "Me",
              names: list[str] | None = None, day: date | None = None) -> MeetingNotes:
    day = day or date.today()
    names_text = ", ".join(f'"{n}"' for n in (names or []) if n) or "(name unknown)"
    messages = [
        {"role": "system", "content": SYSTEM.format(me=me, names=names_text, day=day.isoformat(),
                                                    weekday=day.strftime("%A"))},
        {"role": "user", "content": f"Transcript:\n\n{transcript}"},
    ]
    schema = _strict(MeetingNotes.model_json_schema())
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "meeting_notes", "strict": True, "schema": schema}},
        "provider": {"require_parameters": True},  # OpenRouter: only route to providers that honour the schema
    }
    headers = {"Authorization": f"Bearer {api_key}", "X-Title": "Hear Me Out",
               "HTTP-Referer": "https://github.com/brem-21/hearmeout"}

    with httpx.Client(base_url=base_url, headers=headers, timeout=httpx.Timeout(30, read=600)) as client:
        for attempt in range(2):
            resp = client.post("/chat/completions", json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Model request failed ({resp.status_code}): {resp.text[:500]}")
            content = resp.json()["choices"][0]["message"]["content"] or ""
            try:
                notes = MeetingNotes.model_validate_json(_strip_fences(content))
                return _fix_owners(notes, [me, *(names or [])])
            except ValidationError as e:
                if attempt:
                    raise RuntimeError(f"Model returned invalid notes: {e}") from e
                messages += [{"role": "assistant", "content": content},
                             {"role": "user", "content": f"That did not match the schema:\n{e}\nReply with corrected JSON only."}]
    raise AssertionError("unreachable")


def _fix_owners(notes: MeetingNotes, my_names: list[str]) -> MeetingNotes:
    """Guard against a common model slip: a task whose named owner is someone
    else can't be the user's, whatever owner_is_me says."""
    mine = {n.strip().lower() for n in my_names if n and n.strip()} | {"me", "i", "myself", "the user"}
    for item in notes.action_items:
        owner = (item.owner or "").strip().lower()
        if owner:
            item.owner_is_me = owner in mine or any(w in mine for w in owner.replace(",", " ").split())
    return notes


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    return text


def notes_to_json(notes: MeetingNotes) -> str:
    return json.dumps(notes.model_dump(), indent=1, ensure_ascii=False)
