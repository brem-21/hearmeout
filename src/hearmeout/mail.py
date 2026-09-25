"""Your recent emails for the Home screen, through the Microsoft 365 account you added in
GNOME Settings › Online Accounts. GNOME signs you in and hands out access tokens to desktop
apps on request, so Hear Me Out needs no app registration of its own: it asks GNOME for a
token and reads your inbox with Microsoft Graph. Nothing is sent anywhere else.

The latest messages are kept in ~/.local/share/hearmeout/mail.json (readable only by you)
so Home can show them straight away.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

import httpx

from . import config

CACHE_FILE = config.DATA_DIR / "mail.json"
GRAPH = "https://graph.microsoft.com/v1.0"
GOA = "org.gnome.OnlineAccounts"
KEEP = 10  # messages kept for Home


@dataclass
class Message:
    id: str
    sender: str
    address: str
    subject: str
    preview: str
    received: datetime  # local time, naive
    unread: bool
    attachments: bool
    important: bool
    link: str           # opens the message in Outlook on the web

    def to_dict(self) -> dict:
        d = asdict(self)
        d["received"] = self.received.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        d = dict(d)
        d["received"] = datetime.fromisoformat(d["received"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# --------------------------------------------------------------------------- GNOME Online Accounts


@dataclass
class Account:
    path: str       # D-Bus object path
    identity: str   # e.g. brempong@amalitech.com
    mail_disabled: bool


def _bus():
    from jeepney.io.blocking import open_dbus_connection
    return open_dbus_connection(bus="SESSION")


_accounts: tuple[float, list] = (0.0, [])


def accounts(max_age: float = 30) -> list[Account]:
    """Microsoft 365 accounts added in GNOME Settings › Online Accounts (asked at most every 30 s)."""
    global _accounts
    if time.time() - _accounts[0] < max_age:
        return _accounts[1]
    _accounts = (time.time(), _find_accounts())
    return _accounts[1]


def _find_accounts() -> list[Account]:
    from jeepney import DBusAddress, new_method_call
    try:
        with _bus() as conn:
            addr = DBusAddress("/org/gnome/OnlineAccounts", bus_name=GOA,
                               interface="org.freedesktop.DBus.ObjectManager")
            objects = conn.send_and_get_reply(new_method_call(addr, "GetManagedObjects"), timeout=5).body[0]
    except Exception:
        return []  # no GNOME Online Accounts on this desktop
    out = []
    for path, ifaces in objects.items():
        acc = ifaces.get(f"{GOA}.Account")
        if not acc or acc.get("ProviderType", ("", ""))[1] != "ms_graph":
            continue
        val = lambda k, d="": acc.get(k, ("", d))[1]
        out.append(Account(path, val("PresentationIdentity") or val("Identity"),
                           bool(val("MailDisabled", False))))
    return out


def _token(account: Account) -> str:
    from jeepney import DBusAddress, new_method_call
    with _bus() as conn:
        addr = DBusAddress(account.path, bus_name=GOA, interface=f"{GOA}.OAuth2Based")
        reply = conn.send_and_get_reply(new_method_call(addr, "GetAccessToken"), timeout=20)
    if reply.header.message_type.name == "error":
        detail = " ".join(str(x) for x in reply.body) if reply.body else ""
        raise RuntimeError("GNOME couldn't sign in to your Microsoft 365 account. Open Settings › Online "
                           f"Accounts and sign in again. {detail[:200]}".strip())
    return reply.body[0]


def connected() -> bool:
    return bool(accounts())


def account_name() -> str:
    found = accounts()
    return found[0].identity if found else ""


# --------------------------------------------------------------------------- inbox


def _local(stamp: str) -> datetime:
    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone().replace(tzinfo=None)


def parse(m: dict) -> Message:
    sender = (m.get("from") or m.get("sender") or {}).get("emailAddress") or {}
    name = (sender.get("name") or sender.get("address") or "Unknown sender").strip()
    if name.count(",") == 1:  # "Surname, Given" -> "Given Surname"
        last, first = [p.strip() for p in name.split(",")]
        name = f"{first} {last}"
    return Message(
        id=m.get("id", ""), sender=name, address=sender.get("address") or "",
        subject=(m.get("subject") or "").strip() or "(no subject)",
        preview=re.sub(r"\s+", " ", m.get("bodyPreview") or "").strip()[:300],
        received=_local(m.get("receivedDateTime") or datetime.now(timezone.utc).isoformat()),
        unread=not m.get("isRead", True), attachments=bool(m.get("hasAttachments")),
        important=(m.get("importance") or "").lower() == "high", link=m.get("webLink") or "")


def fetch(count: int = KEEP) -> list[Message]:
    """The newest messages in your inbox (needs network: call off the UI thread)."""
    found = [a for a in accounts() if not a.mail_disabled] or accounts()
    if not found:
        raise RuntimeError("Add your work account in Settings › Online Accounts › Microsoft 365.")
    token = _token(found[0])
    r = httpx.get(f"{GRAPH}/me/mailFolders/inbox/messages", timeout=20,
                  headers={"Authorization": f"Bearer {token}", "Prefer": 'outlook.body-content-type="text"'},
                  params={"$top": str(count), "$orderby": "receivedDateTime desc",
                          "$select": "id,subject,from,sender,receivedDateTime,isRead,bodyPreview,webLink,"
                                     "hasAttachments,importance"})
    if r.status_code in (401, 403):
        raise RuntimeError("Microsoft refused access to your mail. Your organisation may need to approve "
                           "GNOME Online Accounts, or sign in again in Settings › Online Accounts.")
    if r.status_code != 200:
        raise RuntimeError(f"Couldn't read your inbox (HTTP {r.status_code}).")
    return [parse(m) for m in r.json().get("value", [])]


def refresh() -> list[Message]:
    messages = fetch()
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.touch(mode=0o600, exist_ok=True)
    CACHE_FILE.chmod(0o600)
    CACHE_FILE.write_text(json.dumps({"fetched": time.time(), "account": account_name(),
                                      "messages": [m.to_dict() for m in messages]}, indent=1))
    return messages


_cache_read: tuple[float, tuple] = (-1.0, ([], 0.0))


def cached() -> tuple[list[Message], float]:
    """The last fetched messages and when they were fetched (re-read only when the file changes)."""
    global _cache_read
    try:
        mtime = CACHE_FILE.stat().st_mtime
    except OSError:
        return [], 0.0
    if _cache_read[0] != mtime:
        try:
            data = json.loads(CACHE_FILE.read_text())
            _cache_read = (mtime, ([Message.from_dict(m) for m in data["messages"]], float(data.get("fetched", 0))))
        except (OSError, ValueError, KeyError, TypeError):
            _cache_read = (mtime, ([], 0.0))
    return _cache_read[1]


def forget() -> None:
    CACHE_FILE.unlink(missing_ok=True)
