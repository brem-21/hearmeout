"""hearmeout command line.

  hearmeout watch             run in the background: notice meetings, record, make notes
  hearmeout record            record now; Ctrl+C to stop, then notes go to Obsidian
  hearmeout process FILE|DIR  turn an existing recording into notes
  hearmeout vaults            list the Obsidian vaults found on this machine
  hearmeout doctor            check audio, API keys and vault
  hearmeout init              create ~/.config/hearmeout/config.toml
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import httpx
from . import __version__, audio, config, obsidian, pipeline, tui

def _say(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _settings(args) -> config.Settings:
    s = config.load()
    if getattr(args, "vault", None):
        s.vault = args.vault
    if getattr(args, "model", None):
        s.llm_model = args.model
    if getattr(args, "save", None):
        s.save = [k.strip() for k in args.save.split(",") if k.strip()]
        unknown = set(s.save) - set(obsidian.ITEMS)
        if unknown:
            raise RuntimeError(f"Unknown --save item(s): {', '.join(sorted(unknown))}. "
                               f"Choose from: {', '.join(obsidian.ITEMS)}")
    return s


def cmd_record(args) -> int:
    s = _settings(args)
    if not (s.vault or obsidian.find_vaults()):
        _say("Note: no Obsidian vault found yet. You'll be asked where to save when the meeting ends.")
    session = pipeline.new_session()
    rec = audio.Recorder(session)
    rec.start()
    _say(f"● Recording mic + system audio ({audio.backend()}). Press Ctrl+C to stop.")
    try:
        while True:
            m, sec = divmod(int(time.time() - rec.started_at), 60)
            print(f"\r  {m:02d}:{sec:02d}", end="", file=sys.stderr, flush=True)
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    _say("\n■ Stopped. Saving audio…")
    rec.stop()
    return _process(session, s, args)


def cmd_process(args) -> int:
    return _process(Path(args.path).expanduser(), _settings(args), args)


def _process(path: Path, s: config.Settings, args) -> int:
    meeting, work = pipeline.prepare(path, s, title=args.title, status=_say)

    # 3. Review and save to Obsidian
    vault = obsidian.resolve_vault(s.vault) if s.vault or obsidian.find_vaults() else None
    if args.yes:
        if vault is None:
            raise RuntimeError("No Obsidian vault found. Set one with --vault or in config.toml.")
        keys = [k for k in s.save if k in obsidian.available(meeting)]
        saved = tui.save(meeting, vault, s.folder, keys) if keys else []
    elif not args.no_gui and _gui_available():
        from . import gui
        vault, saved = gui.review(meeting, vault=vault, folder=s.folder, default_keys=s.save)
    else:
        if vault is None:
            raise RuntimeError("No Obsidian vault found. Set one with --vault or in config.toml.")
        keys = tui.choose(meeting, s.save)
        saved = tui.save(meeting, vault, s.folder, keys) if keys else []

    if not saved:
        _say(f"Nothing saved. The recording is kept; save it later with:\n  hearmeout process {work}")
        return 0

    pipeline.cleanup(work)
    _say(f"✓ Saved {len(saved)} item(s) to {saved[0].parent}")
    note = obsidian.main_note(saved)
    if note:
        print(obsidian.open_uri(vault, note))
    return 0


def _gui_available() -> bool:
    import os
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        import PySide6  # noqa: F401
    except ImportError:
        return False
    return True


def cmd_watch(args) -> int:
    if not _gui_available():
        _say("hearmeout watch needs a desktop session (it shows a tray icon and notifications).")
        return 1
    from . import watch
    return watch.run(autostart=args.autostart)


def cmd_vaults(args) -> int:
    vaults = obsidian.find_vaults()
    if not vaults:
        _say("No Obsidian vaults found.")
        return 1
    for i, v in enumerate(vaults):
        print(f"{'*' if i == 0 else ' '} {v}")
    return 0


def cmd_doctor(args) -> int:
    s = config.load()
    ok = True

    def check(label: str, good: bool, detail: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"{'✓' if good else '✗'} {label}{': ' + detail if detail else ''}")

    check("Audio recorder", audio.backend() != "none", audio.backend())
    check("Config file", True, str(config.CONFIG_FILE) if config.CONFIG_FILE.exists() else "not created (optional)")
    check("Your name", bool(s.user_name), s.user_name or "not set: tasks can only be matched to the 'Me' track")
    try:
        check("Obsidian vault", True, str(obsidian.resolve_vault(s.vault)))
    except RuntimeError as e:
        check("Obsidian vault", False, str(e))

    if s.elevenlabs_api_key:
        r = httpx.get("https://api.elevenlabs.io/v1/models", headers={"xi-api-key": s.elevenlabs_api_key})
        # Restricted keys may lack model listing; 401 with "invalid" means a bad key.
        bad = r.status_code == 401 and "invalid" in r.text.lower()
        check("ElevenLabs key", not bad, "rejected" if bad else f"model {s.stt_model}")
    else:
        check("ElevenLabs key", False, "missing")

    if s.openrouter_api_key:
        r = httpx.get(f"{s.llm_base_url}/models/user" if "openrouter" in s.llm_base_url else f"{s.llm_base_url}/models",
                      headers={"Authorization": f"Bearer {s.openrouter_api_key}"})
        check("Model API key", r.status_code == 200, f"{s.llm_base_url} ({s.llm_model})" if r.status_code == 200
              else f"HTTP {r.status_code}")
    else:
        check("Model API key", False, "missing")
    return 0 if ok else 1


def cmd_init(args) -> int:
    print(config.write_template())
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="hearmeout", description="Meeting notes from your Linux desktop into Obsidian.")
    p.add_argument("--version", action="version", version=f"hearmeout {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def outputs(sp):
        sp.add_argument("--title", help="meeting title (default: generated from the conversation)")
        sp.add_argument("--vault", help="path to the Obsidian vault")
        sp.add_argument("--model", help="model id for notes, e.g. anthropic/claude-sonnet-5")
        sp.add_argument("--save", metavar="ITEMS",
                        help="comma-separated default items to save: " + ", ".join(obsidian.ITEMS))
        sp.add_argument("-y", "--yes", action="store_true", help="save the default items without asking")
        sp.add_argument("--no-gui", action="store_true", help="ask in the terminal instead of opening a window")

    sp = sub.add_parser("watch", help="run in the background and notice meetings automatically")
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--autostart", action="store_true", default=None, help="also start at every login")
    g.add_argument("--no-autostart", dest="autostart", action="store_false", help="stop starting at login")
    sp.set_defaults(func=cmd_watch)

    sp = sub.add_parser("record", help="record a meeting now")
    outputs(sp)
    sp.set_defaults(func=cmd_record)

    sp = sub.add_parser("process", help="make notes from an existing recording")
    sp.add_argument("path", help="audio/video file, or a folder left by an interrupted recording")
    outputs(sp)
    sp.set_defaults(func=cmd_process)

    sub.add_parser("vaults", help="list Obsidian vaults").set_defaults(func=cmd_vaults)
    sub.add_parser("doctor", help="check that everything is set up").set_defaults(func=cmd_doctor)
    sub.add_parser("init", help="create a config file").set_defaults(func=cmd_init)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except RuntimeError as e:
        _say(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
