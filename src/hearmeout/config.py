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


# env var -> (settings attribute, converter)
_ENV = {
    "ELEVENLABS_API_KEY": ("elevenlabs_api_key", str),
    "ELEVENLABS_STT_MODEL": ("stt_model", str),
    "OPENROUTER_API_KEY": ("openrouter_api_key", str),
    "OPENROUTER_BASE_URL": ("llm_base_url", str),
    "HEARMEOUT_MODEL": ("llm_model", str),
    "HEARMEOUT_USER_NAME": ("user_name", str),
    "HEARMEOUT_VAULT": ("vault", str),
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
    for var, (attr, conv) in _ENV.items():
        if env.get(var):
            setattr(s, attr, conv(env[var]))

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
"""
        )
        CONFIG_FILE.chmod(0o600)
    return CONFIG_FILE
