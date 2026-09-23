"""Desktop notifications with buttons, via the freedesktop notification service
(GNOME, KDE, XFCE, Cinnamon, dunst, mako… all implement it)."""

from __future__ import annotations

import threading
from typing import Callable

from jeepney import DBusAddress, MatchRule, message_bus, new_method_call
from jeepney.io.blocking import open_dbus_connection

APP_NAME = "Hear Me Out"
DESKTOP_ENTRY = "io.github.brem_21.hearmeout"
ICON = "audio-input-microphone"

_ADDR = DBusAddress("/org/freedesktop/Notifications", bus_name="org.freedesktop.Notifications",
                    interface="org.freedesktop.Notifications")


class Notifier:
    """Send notifications and hear back which button was pressed.

    on_action(notification_id, action_key) is called from a background thread;
    closing a notification without pressing a button reports action_key "".
    """

    def __init__(self, on_action: Callable[[int, str], None]):
        self.on_action = on_action
        self.available = False
        self.actions = False
        try:
            self._conn = open_dbus_connection(bus="SESSION")
            caps = self._conn.send_and_get_reply(new_method_call(_ADDR, "GetCapabilities"), timeout=3).body[0]
            self.available, self.actions = True, "actions" in caps
        except Exception:
            self._conn = None
            return
        self._lock = threading.Lock()
        threading.Thread(target=self._listen, daemon=True).start()

    def show(self, summary: str, body: str = "", actions: list[tuple[str, str]] | None = None, *,
             replaces: int = 0, urgent: bool = False, sticky: bool = False) -> int:
        """Show (or update, with replaces=id) a notification. Returns its id, 0 if unavailable."""
        if not self.available:
            return 0
        flat: list[str] = []
        for key, label in actions or []:
            flat += [key, label]
        hints = {"desktop-entry": ("s", DESKTOP_ENTRY), "urgency": ("y", 2 if urgent else 1)}
        if sticky:
            hints["resident"] = ("b", True)
        msg = new_method_call(_ADDR, "Notify", "susssasa{sv}i",
                              (APP_NAME, replaces, ICON, summary, body, flat, hints, 0 if sticky else -1))
        try:
            with self._lock:
                return self._conn.send_and_get_reply(msg, timeout=3).body[0]
        except Exception:
            return 0

    def close(self, nid: int) -> None:
        if self.available and nid:
            try:
                with self._lock:
                    self._conn.send_and_get_reply(new_method_call(_ADDR, "CloseNotification", "u", (nid,)), timeout=3)
            except Exception:
                pass

    def _listen(self) -> None:
        conn = open_dbus_connection(bus="SESSION")
        for member in ("ActionInvoked", "NotificationClosed"):
            rule = MatchRule(type="signal", interface=_ADDR.interface, member=member, path=_ADDR.object_path)
            conn.send_and_get_reply(message_bus.AddMatch(rule))
        pressed: set[int] = set()
        while True:
            try:
                msg = conn.receive()
            except Exception:
                return
            member = msg.header.fields.get(3)  # HeaderFields.member
            if member == "ActionInvoked":
                nid, key = msg.body
                pressed.add(nid)
                self.on_action(nid, key)
            elif member == "NotificationClosed":
                nid = msg.body[0]
                if nid not in pressed:
                    self.on_action(nid, "")
                pressed.discard(nid)
