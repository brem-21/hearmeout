"""Spot meetings: an app that makes calls (or a browser) is using the microphone.

Reads the list of capture streams from the sound server (PulseAudio, or PipeWire's
PulseAudio layer, which is nearly every desktop). On X11 it also reads window titles, to
name the meeting ("Google Meet: Weekly Sync") and to tell a Meet tab from other browsing.
Nothing here leaves the machine.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import pulsectl

# Apps whose mic use means a call. key: display name; values: fragments matched against
# the stream's program name and application name (lowercase).
CALL_APPS = {
    "Zoom": ["zoom"],
    "Microsoft Teams": ["teams"],
    "Slack": ["slack"],
    "Discord": ["discord", "vesktop", "webcord"],
    "Webex": ["webex", "ciscocollabhost"],
    "Skype": ["skype"],
    "Telegram": ["telegram"],
    "Signal": ["signal"],
    "Element": ["element"],
    "WhatsApp": ["whatsapp", "zapzap"],
    "Mattermost": ["mattermost"],
    "Rocket.Chat": ["rocket.chat"],
    "Jitsi Meet": ["jitsi"],
}

BROWSERS = {
    "Firefox": ["firefox", "librewolf", "floorp", "zen"],
    "Chrome": ["chrome"],
    "Chromium": ["chromium"],
    "Brave": ["brave"],
    "Edge": ["msedge", "microsoft-edge"],
    "Vivaldi": ["vivaldi"],
    "Opera": ["opera"],
    "GNOME Web": ["epiphany"],
}

# Web meetings recognised from a browser tab title: (pattern, service name).
WEB_MEETINGS = [
    (re.compile(r"^Meet\s*[-–—]\s*(?P<name>.+)$|meet\.google\.com", re.I), "Google Meet"),
    (re.compile(r"(?P<name>.+?)\s*\|\s*Microsoft Teams|Microsoft Teams", re.I), "Microsoft Teams"),
    (re.compile(r"\bZoom\b", re.I), "Zoom"),
    (re.compile(r"Jitsi Meet|meet\.jit\.si", re.I), "Jitsi Meet"),
    (re.compile(r"\bWhereby\b", re.I), "Whereby"),
    (re.compile(r"\bWebex\b", re.I), "Webex"),
    (re.compile(r"\bDiscord\b", re.I), "Discord"),
    (re.compile(r"\bhuddle\b", re.I), "Slack"),
]

# Streams to never count: our own recorder and system tools.
OWN = ("hearmeout", "pw-record", "parec", "pw-cat", "pavucontrol", "gnome-control-center", "plasmashell",
       "speech-dispatcher", "easyeffects", "pipewire", "wireplumber")

# Window titles that say nothing about which meeting it is.
GENERIC_TITLES = re.compile(
    r"^(zoom( meeting| workplace| cloud meetings)?|meeting|meet|chat|calendar|activity|calls?|teams|"
    r"microsoft teams|slack|huddle|discord|home|new tab|[a-z]{3}-[a-z]{4}-[a-z]{3})$", re.I)


@dataclass(frozen=True)
class Call:
    key: str               # stable id for the app, used for "don't ask again"
    app: str               # what to show the user, e.g. "Zoom" or "Google Meet (Firefox)"
    title_hint: str | None  # meeting name taken from the window title, if any
    confident: bool        # True for call apps / recognised web meetings, False for "a browser uses the mic"


def _match(text: str, table: dict[str, list[str]]) -> str | None:
    for name, fragments in table.items():
        if any(f in text for f in fragments):
            return name
    return None


def _clean_title(title: str) -> str | None:
    t = re.sub(r"^\(\d+\)\s*", "", title)                                   # "(25) Chat" -> "Chat"
    t = re.sub(r"\s*[-–—|]\s*(Original profile|Mozilla Firefox|Brave|Google Chrome|Chromium|"
               r"Microsoft Edge|Vivaldi|Opera|Microsoft Teams|Slack|Zoom|Discord).*$", "", t, flags=re.I)
    t = re.sub(r"\s*[-–—|]\s*$", "", t).strip()
    t = re.sub(r"^(Meet\s*[-–—]|Huddle\s*[-–—:]?)\s*", "", t, flags=re.I).strip()
    t = t.split(" | ")[0].strip()  # "Chat | Serva CTM" -> "Chat" (then rejected as generic)
    if not t or GENERIC_TITLES.match(t) or len(t) < 3:
        return None
    return t[:80]


class _Windows:
    """Window titles on X11 (also covers XWayland apps). Silently empty elsewhere."""

    def __init__(self):
        self.d = None
        if os.environ.get("DISPLAY"):
            try:
                from Xlib import display
                self.d = display.Display()
            except Exception:
                self.d = None

    def titles(self) -> list[tuple[str, str]]:
        """(window class, title) of every top-level window."""
        if self.d is None:
            return []
        try:
            from Xlib import X
            atom = self.d.intern_atom
            root = self.d.screen().root
            ids = root.get_full_property(atom("_NET_CLIENT_LIST"), X.AnyPropertyType)
            out = []
            for wid in ids.value if ids else []:
                w = self.d.create_resource_object("window", wid)
                name = w.get_full_property(atom("_NET_WM_NAME"), atom("UTF8_STRING"))
                cls = w.get_wm_class() or ("", "")
                title = name.value.decode("utf-8", "replace") if name else ""
                out.append((" ".join(cls).lower(), title))
            return out
        except Exception:
            return []


class Detector:
    def __init__(self, ignore: list[str] | None = None):
        self.ignore = {i.lower() for i in ignore or []}
        self._pulse: pulsectl.Pulse | None = None
        self._windows = _Windows()

    def _streams(self) -> list[dict]:
        for attempt in range(2):  # reconnect once if the sound server restarted
            try:
                if self._pulse is None:
                    self._pulse = pulsectl.Pulse("hearmeout-detector")
                return [so.proplist for so in self._pulse.source_output_list()]
            except pulsectl.PulseError:
                if self._pulse is not None:
                    self._pulse.close()
                self._pulse = None
        return []

    def poll(self) -> list[Call]:
        """Calls happening right now, most confident first."""
        calls: dict[str, Call] = {}
        windows = None
        for props in self._streams():
            binary = (props.get("application.process.binary") or "").lower()
            name = (props.get("application.name") or "").lower()
            node = (props.get("node.name") or "").lower()
            text = f"{binary} {name}"
            if node.startswith("hearmeout") or binary in OWN or name in OWN:
                continue

            app = _match(text, CALL_APPS)
            if app:
                key = app.lower()
                if key in self.ignore or key in calls:
                    continue
                windows = windows if windows is not None else self._windows.titles()
                frag = CALL_APPS[app]
                hint = next((h for cls, t in windows if any(f in cls for f in frag)
                             for h in [_clean_title(t)] if h), None)
                calls[key] = Call(key, app, hint, True)
                continue

            browser = _match(text, BROWSERS)
            if browser:
                key = browser.lower()
                if key in self.ignore or key in calls:
                    continue
                windows = windows if windows is not None else self._windows.titles()
                frag = BROWSERS[browser]
                found = None
                for cls, t in windows:
                    if not any(f in cls for f in frag):
                        continue
                    for pattern, service in WEB_MEETINGS:
                        m = pattern.search(t)
                        if m:
                            hint = _clean_title(m.groupdict().get("name") or "") if m.groupdict().get("name") else None
                            found = (service, hint)
                            break
                    if found:
                        break
                if found:
                    calls[key] = Call(key, f"{found[0]} ({browser})", found[1], True)
                else:
                    calls[key] = Call(key, browser, None, False)
        return sorted(calls.values(), key=lambda c: not c.confident)

    def close(self) -> None:
        if self._pulse is not None:
            self._pulse.close()
            self._pulse = None
