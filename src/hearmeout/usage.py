"""What Hear Me Out has used, and what's left on your accounts.

- Every transcription (audio minutes, ElevenLabs) and every set of notes (tokens and cost,
  OpenRouter) is logged to ~/.local/share/hearmeout/usage.jsonl as it happens.
- balances() asks ElevenLabs and OpenRouter what's left on your accounts right now.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from . import config

LOG_FILE = config.DATA_DIR / "usage.jsonl"


def record(service: str, **fields) -> None:
    """Add one line to the usage log. Never raises: usage is nice to have, notes matter more."""
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(json.dumps({"at": time.time(), "service": service, **fields}) + "\n")
    except OSError:
        pass


@dataclass
class Totals:
    recordings: int = 0        # transcriptions
    audio_s: float = 0.0
    notes: int = 0             # sets of notes written
    mail: int = 0              # mail summaries and explanations
    tokens_in: int = 0
    tokens_out: int = 0
    cost: float = 0.0          # USD, as reported by OpenRouter
    models: dict[str, int] = field(default_factory=dict)  # model -> tokens


def totals(since: datetime | None = None) -> Totals:
    """What Hear Me Out used since a time (default: the start of this month)."""
    since = since or datetime.now().replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    cutoff = since.timestamp()
    t = Totals()
    try:
        lines = LOG_FILE.read_text().splitlines()
    except OSError:
        return t
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("at", 0) < cutoff:
            continue
        if row.get("service") == "elevenlabs":
            t.recordings += 1
            t.audio_s += float(row.get("audio_s") or 0)
        elif row.get("service") in ("llm", "mail"):
            if row.get("service") == "mail":
                t.mail += 1
            else:
                t.notes += 1
            t.tokens_in += int(row.get("tokens_in") or 0)
            t.tokens_out += int(row.get("tokens_out") or 0)
            t.cost += float(row.get("cost") or 0)
            m = row.get("model") or "?"
            t.models[m] = t.models.get(m, 0) + int(row.get("tokens_in") or 0) + int(row.get("tokens_out") or 0)
    return t


# --------------------------------------------------------------------------- live balances


@dataclass
class ElevenLabs:
    ok: bool
    error: str = ""
    plan: str = ""
    used: int = 0          # credits used this billing period
    limit: int = 0         # credits in the plan
    resets: datetime | None = None


@dataclass
class OpenRouter:
    ok: bool
    error: str = ""
    remaining: float | None = None   # USD left (account credits, or the key's limit)
    total: float | None = None       # USD bought / the key's limit
    spent_month: float | None = None
    spent_day: float | None = None
    free_tier: bool = False
    applies: bool = True             # False when notes use another service (e.g. Ollama)


def eleven_balance(key: str) -> ElevenLabs:
    if not key:
        return ElevenLabs(False, "No ElevenLabs key")
    try:
        r = httpx.get("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": key}, timeout=15)
    except httpx.HTTPError as e:
        return ElevenLabs(False, f"Couldn't reach ElevenLabs ({e.__class__.__name__})")
    if r.status_code in (401, 403):
        detail = r.text.lower()
        if "permission" in detail or "missing" in detail:
            return ElevenLabs(False, "Your key can't read your plan. Give it the “User: read” permission "
                                     "in ElevenLabs to see credits here.")
        return ElevenLabs(False, "ElevenLabs rejected the key")
    if r.status_code != 200:
        return ElevenLabs(False, f"ElevenLabs answered HTTP {r.status_code}")
    d = r.json()
    reset = d.get("next_character_count_reset_unix")
    return ElevenLabs(True, plan=str(d.get("tier") or "").replace("_", " ").title(),
                      used=int(d.get("character_count") or 0), limit=int(d.get("character_limit") or 0),
                      resets=datetime.fromtimestamp(reset) if reset else None)


def openrouter_balance(key: str, base_url: str) -> OpenRouter:
    if "openrouter" not in base_url:
        return OpenRouter(False, "Notes don't use OpenRouter", applies=False)
    if not key:
        return OpenRouter(False, "No OpenRouter key")
    headers = {"Authorization": f"Bearer {key}"}
    out = OpenRouter(True)
    try:
        r = httpx.get(f"{base_url.rstrip('/')}/key", headers=headers, timeout=15)
        if r.status_code in (401, 403):
            return OpenRouter(False, "OpenRouter rejected the key")
        if r.status_code == 200:
            d = r.json().get("data", {})
            out.free_tier = bool(d.get("is_free_tier"))
            out.spent_month = d.get("usage_monthly")
            out.spent_day = d.get("usage_daily")
            if d.get("limit") is not None:  # a key with its own spending limit
                out.total, out.remaining = float(d["limit"]), float(d.get("limit_remaining") or 0)
        c = httpx.get(f"{base_url.rstrip('/')}/credits", headers=headers, timeout=15)
        if c.status_code == 200:
            d = c.json().get("data", {})
            bought, used = float(d.get("total_credits") or 0), float(d.get("total_usage") or 0)
            left = bought - used
            if out.remaining is None or left < out.remaining:  # whichever runs out first
                out.total, out.remaining = bought, left
    except httpx.HTTPError as e:
        return OpenRouter(False, f"Couldn't reach OpenRouter ({e.__class__.__name__})")
    if out.remaining is None and out.spent_month is None:
        return OpenRouter(False, "OpenRouter didn't say what's left on this key")
    return out


def balances(s: config.Settings) -> tuple[ElevenLabs, OpenRouter]:
    return eleven_balance(s.elevenlabs_api_key), openrouter_balance(s.openrouter_api_key, s.llm_base_url)
