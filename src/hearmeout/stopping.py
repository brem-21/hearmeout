"""Stopping note-making part way. The Stop button sets an Event; a request in flight
(transcription, notes) is left to finish on its own, and note-making stops waiting at once."""

from __future__ import annotations

import threading
from typing import Callable, TypeVar

T = TypeVar("T")


class Stopped(Exception):
    """Note-making was stopped by the user."""


def check(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Stopped()


def run(fn: Callable[[], T], cancel: threading.Event | None,
        late: Callable[[T], None] | None = None, finished: Callable[[], None] | None = None) -> T:
    """Run fn (a network request) but stop waiting for it as soon as `cancel` is set.
    If it still finishes after that, `late` gets its result (e.g. to keep a transcript
    that was paid for anyway). finished() is called when fn is really done, either way."""
    if cancel is None:
        try:
            return fn()
        finally:
            if finished is not None:
                finished()
    box: dict = {}
    done = threading.Event()

    def work() -> None:
        try:
            box["value"] = fn()
        except BaseException as e:  # handed back to the caller below
            box["error"] = e
        finally:
            done.set()
            if cancel.is_set() and late is not None and "value" in box:
                try:
                    late(box["value"])
                except Exception:
                    pass
            if finished is not None:
                finished()

    threading.Thread(target=work, daemon=True).start()
    while not done.wait(0.2):
        if cancel.is_set():
            raise Stopped()
    if "error" in box:
        raise box["error"]
    return box["value"]
