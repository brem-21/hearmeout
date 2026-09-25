"""Your Microsoft 365 (Outlook / Teams) calendar, read in one of two ways:

- a published ICS link (Outlook › Settings › Calendar › Shared calendars › Publish a calendar):
  paste it once, no sign-in and no app registration. This is the usual way.
- Microsoft Graph, after signing in (needs an app registration, see below).

Signing in opens the browser once; the tokens are kept in ~/.config/hearmeout/microsoft.json
(readable only by you) and refreshed as needed. Nothing goes through any other server.

Events are used to:
- name a recording after the meeting it overlaps, and know who was invited,
- give the model the invite's agenda when writing notes,
- show what's coming up on the Home screen.

Like Granola, Hear Me Out has one multitenant app registration of its own (BUILT_IN_CLIENT_ID),
so people just press "Sign in with Microsoft". An organisation that wants its own registration
can set [microsoft] client_id instead. A public client ID isn't a secret: it only names the app.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import re
import secrets
import threading
import time
import webbrowser
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from . import config

# Hear Me Out's own registration in Microsoft Entra (multitenant, public client, http://localhost
# redirect, delegated Calendars.Read). Empty until the maintainer registers it; see the README.
BUILT_IN_CLIENT_ID = ""

TOKEN_FILE = config.CONFIG_DIR / "microsoft.json"
CACHE_FILE = config.DATA_DIR / "calendar.json"
GRAPH = "https://graph.microsoft.com/v1.0"
SCOPES = "offline_access User.Read Calendars.Read"
SIGN_IN_TIMEOUT = 300  # seconds to finish signing in in the browser
EARLY = timedelta(minutes=15)  # a recording can start this long before the event does


@dataclass
class Event:
    id: str
    subject: str
    start: datetime  # local time, naive (like recording start times)
    end: datetime
    organizer: str = ""
    attendees: list[str] = field(default_factory=list)  # invited people who haven't declined, organiser first
    agenda: str = ""
    join_url: str = ""
    web_link: str = ""
    location: str = ""
    online: bool = False
    all_day: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"], d["end"] = self.start.isoformat(), self.end.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        d = dict(d)
        d["start"], d["end"] = datetime.fromisoformat(d["start"]), datetime.fromisoformat(d["end"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    def others(self, me: list[str]) -> list[str]:
        """Invited people other than you."""
        mine = {n.strip().lower() for n in me if n and n.strip()}
        return [a for a in self.attendees if a.lower() not in mine and a.split()[0].lower() not in mine]


# --------------------------------------------------------------------------- sign-in


def _auth_url(tenant: str, path: str) -> str:
    return f"https://login.microsoftonline.com/{tenant or 'organizations'}/oauth2/v2.0/{path}"


def _load_tokens() -> dict:
    try:
        return json.loads(TOKEN_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_tokens(tokens: dict) -> None:
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.touch(mode=0o600, exist_ok=True)
    TOKEN_FILE.chmod(0o600)
    TOKEN_FILE.write_text(json.dumps(tokens, indent=1))


def client_id(s: config.Settings) -> str:
    """Your own registration if you set one, else Hear Me Out's."""
    return (s.ms_client_id or BUILT_IN_CLIENT_ID).strip()


def signed_in() -> bool:
    return bool(_load_tokens().get("refresh_token"))


_ics_cache: tuple[float, str] = (-1.0, "")


def ics_url() -> str:
    """The published calendar link from config.toml (re-read only when the file changes)."""
    global _ics_cache
    try:
        mtime = config.CONFIG_FILE.stat().st_mtime
    except OSError:
        mtime = 0.0
    if _ics_cache[0] != mtime:
        _ics_cache = (mtime, config.load().ics_url.strip())
    url = _ics_cache[1]
    return "https://" + url[len("webcal://"):] if url.lower().startswith("webcal://") else url


def connected() -> bool:
    return signed_in() or bool(ics_url())


def account() -> str:
    """Who or what is connected, e.g. "brempong@example.com" ("" if nothing is)."""
    if signed_in():
        return _load_tokens().get("account", "") or "your Microsoft account"
    return "your published Outlook calendar" if ics_url() else ""


def sign_out() -> None:
    TOKEN_FILE.unlink(missing_ok=True)
    CACHE_FILE.unlink(missing_ok=True)
    s = config.load()
    if s.ics_url:
        s.ics_url = ""
        config.save(s)


def connect_ics(url: str) -> int:
    """Check a published calendar link works, keep it, and return how many events are coming up."""
    url = url.strip()
    if not re.match(r"^(https?|webcal)://", url, re.I):
        raise RuntimeError("That doesn't look like a calendar link. It should start with https:// and end in .ics")
    probe = "https://" + url[len("webcal://"):] if url.lower().startswith("webcal://") else url
    _parse_ics(_download_ics(probe), datetime.now() - timedelta(days=1), datetime.now() + timedelta(days=1))
    s = config.load()
    s.ics_url = url
    config.save(s)
    return len(refresh())


class SignIn:
    """Browser sign-in (authorization code with PKCE and a localhost redirect).
    run() blocks until it's done, cancelled or times out; call it off the UI thread."""

    def __init__(self, client_id: str, tenant: str = "organizations"):
        if not client_id:
            raise RuntimeError("This copy of Hear Me Out has no Microsoft app ID yet. Add your organisation's "
                               "(Settings › Integrations › Use my own app registration). The README shows how.")
        self.client_id, self.tenant = client_id.strip(), tenant or "organizations"
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def run(self, open_browser=webbrowser.open) -> str:
        """Returns the signed-in account name."""
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        result: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802 (http.server naming)
                q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
                if "code" not in q and "error" not in q:
                    self.send_response(404)
                    self.end_headers()
                    return
                result.update(q)
                ok = "code" in q and q.get("state") == state
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                msg = ("You're signed in. You can close this tab and go back to Hear Me Out." if ok else
                       "Signing in didn't work. Go back to Hear Me Out and try again.")
                self.wfile.write(f"<html><body style='font-family:sans-serif;padding:3em'><h2>Hear Me Out</h2>"
                                 f"<p>{msg}</p></body></html>".encode())

            def log_message(self, *args):
                pass

        server = HTTPServer(("localhost", 0), Handler)
        server.timeout = 0.5
        redirect = f"http://localhost:{server.server_address[1]}"
        params = {"client_id": self.client_id, "response_type": "code", "redirect_uri": redirect,
                  "response_mode": "query", "scope": SCOPES, "state": state, "prompt": "select_account",
                  "code_challenge": challenge, "code_challenge_method": "S256"}
        try:
            open_browser(_auth_url(self.tenant, "authorize") + "?" + urlencode(params))
            deadline = time.time() + SIGN_IN_TIMEOUT
            while not result and time.time() < deadline and not self._cancelled.is_set():
                server.handle_request()
        finally:
            server.server_close()
        if self._cancelled.is_set():
            raise RuntimeError("Sign-in cancelled.")
        if not result:
            raise RuntimeError("Sign-in timed out. Try again.")
        if "error" in result:
            raise RuntimeError(_friendly_error(result.get("error", ""), result.get("error_description", "")))
        if result.get("state") != state:
            raise RuntimeError("Sign-in answer didn't match. Try again.")

        r = httpx.post(_auth_url(self.tenant, "token"), timeout=30, data={
            "client_id": self.client_id, "grant_type": "authorization_code", "code": result["code"],
            "redirect_uri": redirect, "code_verifier": verifier, "scope": SCOPES})
        tokens = _token_response(r)
        tokens.update(client_id=self.client_id, tenant=self.tenant)
        me = httpx.get(f"{GRAPH}/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}, timeout=30)
        if me.status_code == 200:
            info = me.json()
            tokens["account"] = info.get("mail") or info.get("userPrincipalName") or ""
            tokens["name"] = info.get("displayName") or ""
        _save_tokens(tokens)
        return tokens.get("account") or tokens.get("name") or "your Microsoft account"


def _friendly_error(code: str, description: str) -> str:
    first = (description or "").split("\n")[0].strip()
    if "AADSTS65001" in description or code == "consent_required":
        return "Your organisation needs an admin to approve Hear Me Out for calendar access. " + first
    if "AADSTS700016" in description:
        return "Microsoft doesn't recognise that application (client) ID. Check it in Settings › Integrations."
    if "AADSTS50011" in description:
        return ("The app registration is missing the redirect address. Add a \"Mobile and desktop "
                "applications\" platform with http://localhost (see the README).")
    if "AADSTS50194" in description:
        return ("The app registration only allows your own organisation. Put your directory (tenant) ID "
                "under [microsoft] tenant in config.toml, or make the registration multitenant.")
    if code == "access_denied":
        return "Sign-in was cancelled or access was refused."
    return f"Microsoft sign-in failed: {first or code}"


def _token_response(r: httpx.Response) -> dict:
    try:
        body = r.json()
    except ValueError:
        body = {}
    if r.status_code != 200 or "access_token" not in body:
        raise RuntimeError(_friendly_error(body.get("error", f"HTTP {r.status_code}"),
                                           body.get("error_description", "")))
    return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token", ""),
            "expires_at": time.time() + int(body.get("expires_in", 3600)) - 60}


def _access_token() -> str:
    tokens = _load_tokens()
    if not tokens.get("refresh_token"):
        raise RuntimeError("Not signed in to Microsoft.")
    if tokens.get("access_token") and tokens.get("expires_at", 0) > time.time():
        return tokens["access_token"]
    r = httpx.post(_auth_url(tokens.get("tenant", ""), "token"), timeout=30, data={
        "client_id": tokens.get("client_id", ""), "grant_type": "refresh_token",
        "refresh_token": tokens["refresh_token"], "scope": SCOPES})
    fresh = _token_response(r)
    tokens.update({k: v for k, v in fresh.items() if v})  # Microsoft may or may not send a new refresh token
    _save_tokens(tokens)
    return tokens["access_token"]


# --------------------------------------------------------------------------- events

# Where the Teams / Zoom / Meet joining instructions start in an invite's body.
_BOILERPLATE = re.compile(r"^\s*(?:_{10,}|Microsoft Teams(?: meeting| Need help\?)|Join (?:the meeting now|on your "
                          r"computer)|Join Zoom Meeting|Join with Google Meet|-::~:~::~:~)", re.I | re.M)


def clean_agenda(body: str) -> str:
    """The invite text without joining instructions, links-only lines and extra blank lines."""
    m = _BOILERPLATE.search(body or "")
    text = (body or "")[:m.start()] if m else (body or "")
    lines = [l.rstrip() for l in text.replace("\r", "").split("\n")]
    lines = [l for l in lines if not re.fullmatch(r"\s*<?https?://\S+>?\s*", l)]
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
    return text[:3000]


def _name(person: dict) -> str:
    e = person.get("emailAddress") or {}
    name = (e.get("name") or "").strip()
    if not name or "@" in name:
        name = (e.get("address") or name).split("@")[0].replace(".", " ").title()
    if name.count(",") == 1:  # "Surname, Given" -> "Given Surname"
        last, first = [p.strip() for p in name.split(",")]
        name = f"{first} {last}"
    return name


def _local(stamp: dict) -> datetime:
    """Graph's {"dateTime": "2026-09-25T09:00:00.0000000", "timeZone": "UTC"} -> naive local time."""
    raw = re.sub(r"(\.\d{6})\d*", r"\1", stamp["dateTime"])
    dt = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
    return dt.astimezone().replace(tzinfo=None)


def parse_event(e: dict) -> Event:
    organizer = _name(e["organizer"]) if e.get("organizer") else ""
    attendees = [organizer] if organizer else []
    for a in e.get("attendees") or []:
        if a.get("type") == "resource" or (a.get("status") or {}).get("response") == "declined":
            continue
        n = _name(a)
        if n and n not in attendees:
            attendees.append(n)
    online = e.get("onlineMeeting") or {}
    body = (e.get("body") or {}).get("content") or e.get("bodyPreview") or ""
    if (e.get("body") or {}).get("contentType") == "html":
        body = re.sub(r"<[^>]+>", " ", body)
    return Event(
        id=e.get("id", ""), subject=(e.get("subject") or "").strip() or "Untitled meeting",
        start=_local(e["start"]), end=_local(e["end"]), organizer=organizer, attendees=attendees[:40],
        agenda=clean_agenda(body), join_url=online.get("joinUrl") or e.get("onlineMeetingUrl") or "",
        web_link=e.get("webLink") or "", location=((e.get("location") or {}).get("displayName") or "").strip(),
        online=bool(e.get("isOnlineMeeting")), all_day=bool(e.get("isAllDay")))


def fetch(start: datetime, end: datetime) -> list[Event]:
    """Your events between two local times (recurring meetings expanded), cancelled ones left out."""
    if not signed_in() and ics_url():
        return _parse_ics(_download_ics(ics_url()), start.replace(tzinfo=None), end.replace(tzinfo=None))
    return _fetch_graph(start, end)


# --------------------------------------------------------------------------- published calendar (ICS)

_TEAMS_LINK = re.compile(r"https://teams\.microsoft\.com/l/meetup-join/[^\s<>\"]+")
_ANY_CALL_LINK = re.compile(r"https://(?:[\w-]+\.)?(?:zoom\.us/j|meet\.google\.com|teams\.live\.com/meet|"
                            r"[\w.-]*webex\.com/meet)[^\s<>\"]*")


def _download_ics(url: str) -> bytes:
    try:
        r = httpx.get(url, timeout=30, follow_redirects=True)
    except httpx.HTTPError as e:
        raise RuntimeError(f"Couldn't reach your calendar link ({e.__class__.__name__}).") from e
    if r.status_code != 200:
        raise RuntimeError(f"Your calendar link didn't work (HTTP {r.status_code}). Publish it again in Outlook "
                           "and paste the new ICS link.")
    if b"BEGIN:VCALENDAR" not in r.content[:2000]:
        raise RuntimeError("That link isn't a calendar. Use the ICS link (ending in .ics), not the HTML one.")
    return r.content


def _person(prop) -> str:
    cn = str(prop.params.get("CN", "")).strip().strip('"') if hasattr(prop, "params") else ""
    addr = str(prop).split(":", 1)[-1]
    return _name({"emailAddress": {"name": cn, "address": addr}})


def _ics_time(value) -> tuple[datetime, bool]:
    """A DTSTART/DTEND value -> (naive local time, is it a whole day)."""
    if not isinstance(value, datetime):  # a date: all-day event
        return datetime(value.year, value.month, value.day), True
    if value.tzinfo is None:
        return value, False  # "floating" time: already local
    return value.astimezone().replace(tzinfo=None), False


def _parse_ics(data: bytes, start: datetime, end: datetime) -> list[Event]:
    import icalendar
    import recurring_ical_events
    try:
        cal = icalendar.Calendar.from_ical(data)
        items = recurring_ical_events.of(cal).between(start.astimezone(), end.astimezone())
    except Exception as e:
        raise RuntimeError(f"Couldn't read your calendar ({e}).") from e
    out = []
    for v in items:
        if str(v.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if str(v.get("X-MICROSOFT-CDO-BUSYSTATUS", "")).upper() == "FREE" or \
                str(v.get("TRANSP", "")).upper() == "TRANSPARENT":
            continue
        begin, all_day = _ics_time(v.decoded("DTSTART"))
        finish = _ics_time(v.decoded("DTEND"))[0] if v.get("DTEND") else \
            begin + (v.decoded("DURATION") if v.get("DURATION") else timedelta(days=1 if all_day else 0))
        organizer = _person(v["ORGANIZER"]) if v.get("ORGANIZER") else ""
        people = [organizer] if organizer else []
        attendees = v.get("ATTENDEE") or []
        for a in attendees if isinstance(attendees, list) else [attendees]:
            if str(a.params.get("PARTSTAT", "")).upper() == "DECLINED" or \
                    str(a.params.get("CUTYPE", "")).upper() in ("RESOURCE", "ROOM"):
                continue
            n = _person(a)
            if n and n not in people:
                people.append(n)
        description = str(v.get("DESCRIPTION", "") or "")
        location = str(v.get("LOCATION", "") or "").strip()
        text = " ".join([str(v.get("X-MICROSOFT-SKYPETEAMSMEETINGURL", "") or ""), location, description])
        link = _TEAMS_LINK.search(text) or _ANY_CALL_LINK.search(text)
        uid = str(v.get("UID", ""))
        out.append(Event(
            id=f"{uid}:{begin.isoformat()}", subject=str(v.get("SUMMARY", "") or "").strip() or "Busy",
            start=begin, end=finish, organizer=organizer, attendees=people[:40],
            agenda=clean_agenda(description), join_url=link.group(0) if link else "",
            web_link=str(v.get("URL", "") or ""), location=location, online=bool(link), all_day=all_day))
    out.sort(key=lambda e: e.start)
    return out


def _fetch_graph(start: datetime, end: datetime) -> list[Event]:
    utc = lambda d: d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"startDateTime": utc(start), "endDateTime": utc(end), "$top": "100",
              "$orderby": "start/dateTime",
              "$select": "id,subject,start,end,organizer,attendees,body,bodyPreview,isOnlineMeeting,"
                         "onlineMeeting,onlineMeetingUrl,webLink,location,isAllDay,isCancelled,showAs"}
    headers = [("Authorization", f"Bearer {_access_token()}"),
               ("Prefer", 'outlook.timezone="UTC"'), ("Prefer", 'outlook.body-content-type="text"')]
    out: list[Event] = []
    url: str | None = f"{GRAPH}/me/calendarView"
    with httpx.Client(timeout=30) as client:
        while url:
            r = client.get(url, params=params if url.endswith("calendarView") else None, headers=headers)
            if r.status_code == 401:
                raise RuntimeError("Microsoft sign-in expired. Sign in again in Settings › Integrations.")
            if r.status_code != 200:
                raise RuntimeError(f"Couldn't read your calendar (HTTP {r.status_code}): {r.text[:300]}")
            body = r.json()
            out += [parse_event(e) for e in body.get("value", [])
                    if not e.get("isCancelled") and e.get("showAs") != "free"]
            url = body.get("@odata.nextLink")
    return out


# --------------------------------------------------------------------------- cache (for Home and quick lookups)


def week_start(day: date | None = None) -> date:
    """The Monday of the week `day` is in."""
    day = day or date.today()
    return day - timedelta(days=day.weekday())


def refresh(days: int = 2) -> list[Event]:
    """Fetch this whole week's events (and at least the next few days) for the Home screen."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    monday = datetime.combine(week_start(today.date()), datetime.min.time())
    start = min(today - timedelta(days=1), monday)
    end = max(today + timedelta(days=days + 1), monday + timedelta(days=7))
    events = fetch(start.astimezone(), end.astimezone())
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps({"from": start.isoformat(), "to": end.isoformat(), "fetched": time.time(),
                                      "events": [e.to_dict() for e in events]}, indent=1))
    return events


_cache_read: tuple[float, tuple] = (-1.0, ([], None, None))


def cached() -> tuple[list[Event], datetime | None, datetime | None]:
    """The last fetched events and the time range they cover (re-read only when the file changes)."""
    global _cache_read
    try:
        mtime = CACHE_FILE.stat().st_mtime
    except OSError:
        return [], None, None
    if _cache_read[0] == mtime:
        return _cache_read[1]
    _cache_read = (mtime, _read_cache())
    return _cache_read[1]


def _read_cache() -> tuple[list[Event], datetime | None, datetime | None]:
    try:
        data = json.loads(CACHE_FILE.read_text())
        return ([Event.from_dict(e) for e in data["events"]],
                datetime.fromisoformat(data["from"]), datetime.fromisoformat(data["to"]))
    except (OSError, ValueError, KeyError, TypeError):
        return [], None, None


def upcoming(limit: int = 5, now: datetime | None = None) -> list[Event]:
    """What's on now and next, from the cache: the rest of today, then tomorrow."""
    now = now or datetime.now()
    horizon = (now + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    events, *_ = cached()
    return [e for e in events if not e.all_day and e.end > now and e.start < horizon][:limit]


def covers(monday: date) -> bool:
    """Whether the cache holds this whole week."""
    _, start, end = cached()
    begin = datetime.combine(monday, datetime.min.time())
    return bool(start and end and start <= begin and begin + timedelta(days=7) <= end)


def fetch_week(monday: date) -> list[Event]:
    """Another week's events, straight from the calendar (for browsing weeks on Home)."""
    begin = datetime.combine(monday, datetime.min.time())
    return fetch(begin.astimezone(), (begin + timedelta(days=7)).astimezone())


def week(monday: date | None = None, events: list[Event] | None = None) -> dict[date, list[Event]]:
    """Each day of a week (Monday first) with its events, all-day ones first.
    Events come from the cache unless given (e.g. from fetch_week)."""
    monday = monday or week_start()
    days = {monday + timedelta(days=i): [] for i in range(7)}
    if events is None:
        events, *_ = cached()
    for e in events:
        last = (e.end - timedelta(seconds=1)).date() if e.end > e.start else e.start.date()
        d = e.start.date()
        while d <= last:  # a multi-day event shows on each of its days
            if d in days:
                days[d].append(e)
            d += timedelta(days=1)
    for d in days:
        days[d].sort(key=lambda e: (not e.all_day, e.start))
    return days


def starting_soon(minutes: int, now: datetime | None = None) -> list[Event]:
    """Meetings from the cache starting within the next `minutes` (for reminders)."""
    now = now or datetime.now()
    events, *_ = cached()
    return [e for e in events if not e.all_day and now < e.start <= now + timedelta(minutes=minutes)]


def match(events: list[Event], when: datetime, hint: str | None = None, app: str | None = None) -> Event | None:
    """The calendar event a recording started at `when` belongs to, if any."""
    best, best_score = None, float("-inf")
    for e in events:
        if e.all_day or not (e.start - EARLY <= when < e.end):
            continue
        score = -abs((when - e.start).total_seconds()) / 1800  # the closest start wins…
        if hint:
            score += 3 * difflib.SequenceMatcher(None, hint.lower(), e.subject.lower()).ratio()
        if e.online or e.join_url:
            score += 1  # …then one with a call link
        if app and "teams" in app.lower() and "teams" in e.join_url.lower():
            score += 1
        if score > best_score:
            best, best_score = e, score
    return best


def event_for(when: datetime, hint: str | None = None, app: str | None = None, *,
              network: bool = True) -> Event | None:
    """Find the event for a recording, from the cache when it covers that time, else from Graph."""
    if not connected():
        return None
    events, start, end = cached()
    if start and end and start <= when - EARLY and when + EARLY <= end:
        found = match(events, when, hint, app)
        if found or not network:
            return found
    if not network:
        return None
    return match(fetch((when - timedelta(hours=12)).astimezone(), (when + timedelta(hours=2)).astimezone()),
                 when, hint, app)
