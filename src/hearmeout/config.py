"""Settings, loaded from (lowest to highest priority):

1. built-in defaults
2. ~/.config/hearmeout/config.toml
3. a .env file in the current directory (handy during development)
4. real environment variables
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "hearmeout"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "hearmeout"
CONFIG_FILE = CONFIG_DIR / "config.toml"


@dataclass
class Settings:
    # Who "me" is, so the model can tell which tasks are yours.
    user_name: str = ""
    user_aliases: list[str] = field(default_factory=list)

    elevenlabs_api_key: str = ""
    stt_model: str = "scribe_v2"
    language: str | None = None  # None = auto-detect
    keyterms: list[str] = field(default_factory=list)

    openrouter_api_key: str = ""
    llm_base_url: str = "https://openrouter.ai/api/v1"
    llm_model: str = "google/gemini-3.1-flash-lite"

    vault: str = ""  # path to the Obsidian vault; empty = auto-detect
    folder: str = "Meetings"
    # What to save by default: any of summary, my_todos, team_tasks, transcript, audio.
    # The review window remembers your last choice on top of this.
    save: list[str] = field(default_factory=lambda: ["summary", "my_todos", "team_tasks", "transcript"])

    # Meeting detection (hearmeout watch): "ask" before recording, "auto" record right away, or "off".
    detect: str = "ask"
    detect_ignore: list[str] = field(default_factory=list)  # apps never to ask about, e.g. ["firefox"]
    stop_after: int = 15  # seconds after the call app releases the mic before recording stops
    silence_stop: int = 120  # stop when nobody has spoken for this many seconds (0 = never)

    appearance: str = "system"  # "system", "light" or "dark"

    # Microsoft 365 integration (Outlook calendar, Teams meetings): your app registration's client ID.
    ms_client_id: str = ""
    ms_tenant: str = "organizations"  # or your organisation's tenant ID / domain
    ics_url: str = ""  # a published calendar link (Outlook › Shared calendars › Publish): no sign-in needed
    remind_before: int = 5  # minutes before a meeting to notify you (0 = never)

    mail_on_home: bool = True  # show recent emails on Home (from GNOME Online Accounts › Microsoft 365)
    mail_count: int = 8        # how many (5 to 10)


# env var -> (settings attribute, converter)
_ENV = {
    "ELEVENLABS_API_KEY": ("elevenlabs_api_key", str),
    "ELEVENLABS_STT_MODEL": ("stt_model", str),
    "OPENROUTER_API_KEY": ("openrouter_api_key", str),
    "OPENROUTER_BASE_URL": ("llm_base_url", str),
    "HEARMEOUT_MODEL": ("llm_model", str),
    "HEARMEOUT_USER_NAME": ("user_name", str),
    "HEARMEOUT_VAULT": ("vault", str),
    "HEARMEOUT_MS_CLIENT_ID": ("ms_client_id", str),
}

# config.toml [section] key -> settings attribute
_TOML = {
    ("user", "name"): "user_name",
    ("user", "aliases"): "user_aliases",
    ("stt", "api_key"): "elevenlabs_api_key",
    ("stt", "model"): "stt_model",
    ("stt", "language"): "language",
    ("stt", "keyterms"): "keyterms",
    ("llm", "api_key"): "openrouter_api_key",
    ("llm", "base_url"): "llm_base_url",
    ("llm", "model"): "llm_model",
    ("obsidian", "vault"): "vault",
    ("obsidian", "folder"): "folder",
    ("obsidian", "save"): "save",
    ("detect", "mode"): "detect",
    ("detect", "ignore"): "detect_ignore",
    ("detect", "stop_after"): "stop_after",
    ("detect", "stop_after_silence"): "silence_stop",
    ("app", "appearance"): "appearance",
    ("microsoft", "client_id"): "ms_client_id",
    ("microsoft", "tenant"): "ms_tenant",
    ("microsoft", "ics_url"): "ics_url",
    ("microsoft", "remind_before"): "remind_before",
    ("mail", "show_on_home"): "mail_on_home",
    ("mail", "count"): "mail_count",
}


def _read_dotenv(path: Path) -> dict[str, str]:
    values = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    return values


def load() -> Settings:
    s = Settings()

    if CONFIG_FILE.is_file():
        data = tomllib.loads(CONFIG_FILE.read_text())
        for (section, key), attr in _TOML.items():
            if key in data.get(section, {}):
                setattr(s, attr, data[section][key])

    env = {**_read_dotenv(Path.cwd() / ".env"), **os.environ}
    from_env: dict[str, object] = {}
    for var, (attr, conv) in _ENV.items():
        if env.get(var):
            setattr(s, attr, conv(env[var]))
            from_env[attr] = getattr(s, attr)
    s._from_env = from_env  # remembered so save() doesn't copy them into the file
    return s


def write_template() -> Path:
    """Create a starter config.toml if none exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(
            """\
[user]
name = ""          # your name as people say it in meetings
aliases = []       # nicknames, e.g. ["Kwame"]

[stt]
# api_key = ""     # or set ELEVENLABS_API_KEY
model = "scribe_v2"
keyterms = []      # names and jargon to help transcription

[llm]
# api_key = ""     # or set OPENROUTER_API_KEY
base_url = "https://openrouter.ai/api/v1"   # any OpenAI-compatible API, e.g. http://localhost:11434/v1 for Ollama
model = "google/gemini-3.1-flash-lite"

[obsidian]
vault = ""         # empty = use the vault Obsidian opened most recently
folder = "Meetings"
# What to save by default (you can change it for each meeting before saving):
# summary, my_todos, team_tasks, transcript, audio
save = ["summary", "my_todos", "team_tasks", "transcript"]

[detect]
mode = "ask"       # when a meeting starts: "ask", "auto" (record without asking) or "off"
ignore = []        # apps never to ask about, e.g. ["firefox", "telegram"]
stop_after = 15    # seconds after a call ends before recording stops
stop_after_silence = 120   # stop when nobody has spoken for this long (seconds; 0 = never)

[app]
appearance = "system"   # "system", "light" or "dark"

[microsoft]
# Outlook calendar and Teams meetings: sign in from Settings › Integrations.
client_id = ""     # empty = Hear Me Out's own; or your organisation's app (client) ID
tenant = "organizations"
ics_url = ""       # or, with no sign-in: your calendar's published ICS link
remind_before = 5  # minutes before a meeting to notify you (0 = never)

[mail]
# Recent emails on Home, from the Microsoft 365 account in GNOME Settings › Online Accounts.
show_on_home = true
count = 8          # 5 to 10
"""
        )
        CONFIG_FILE.chmod(0o600)
    return CONFIG_FILE


def _toml_value(v) -> str:
    import json
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    return json.dumps("" if v is None else str(v), ensure_ascii=False)  # JSON strings are valid TOML strings


def _unchanged_env(s: Settings, attr: str) -> bool:
    env = getattr(s, "_from_env", {})
    return attr in env and getattr(s, attr) == env[attr]


def save(s: Settings) -> Path:
    """Write settings to config.toml, keeping the file's layout and comments where possible."""
    import re
    path = write_template()
    lines = path.read_text().splitlines()
    section = None
    done: set[tuple[str, str]] = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*\[(\w+)\]\s*$", line)
        if m:
            section = m.group(1)
            continue
        m = re.match(r"^\s*#?\s*(\w+)\s*=\s*(.*)$", line)
        if not m or (section, m.group(1)) not in _TOML:
            continue
        key = (section, m.group(1))
        if key in done:
            continue
        if _unchanged_env(s, _TOML[key]):
            done.add(key)  # came from an environment variable: leave the file's own value alone
            continue
        comment = re.search(r"\s+#[^\"\]]*$", m.group(2))
        lines[i] = f"{m.group(1)} = {_toml_value(getattr(s, _TOML[key]))}" + (comment.group(0) if comment else "")
        done.add(key)
    missing: dict[str, list[str]] = {}
    for (sec, key), attr in _TOML.items():
        if (sec, key) not in done and not _unchanged_env(s, attr):
            missing.setdefault(sec, []).append(f"{key} = {_toml_value(getattr(s, attr))}")
    for sec, entries in missing.items():
        idx = next((i for i, l in enumerate(lines) if l.strip() == f"[{sec}]"), None)
        if idx is None:
            lines += ["", f"[{sec}]", *entries]
        else:
            lines[idx + 1:idx + 1] = entries
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    return path
