# Hear Me Out

Meeting notes from your Linux desktop into Obsidian.

Hear Me Out records your meetings (any app: Zoom, Meet, Teams, Slack, Discord…),
transcribes them with ElevenLabs Scribe, uses a model of your choice through
OpenRouter to write a summary and pull out tasks, and saves what you choose
into your local Obsidian vault as plain Markdown.

- **Notices meetings for you.** When Zoom, Teams, Slack, Discord, Google Meet
  and similar apps start using your microphone, it asks whether to record. It
  stops by itself when the call ends, then opens your notes for review.
- **You vs. everyone else.** Your microphone and the computer's audio are
  recorded as separate tracks, so the transcript knows what *you* said and
  "My to-dos" only lists tasks that are yours.
- **You choose what's saved.** After each meeting, a review window lets you pick
  any of: summary, my to-dos, team tasks, transcript, audio. Untick individual
  tasks, fix the title, pick the vault, then save.
- **Local files.** Each item becomes its own note in
  `<vault>/Meetings/<date> <title>/`. Obsidian doesn't need to be open. Tasks use
  the [Tasks plugin](https://publish.obsidian.md/tasks/) format (`- [ ] … 📅 2026-09-25`),
  and every task carries the quote it came from.
- **Your keys, your machine.** No account and no server. Recordings are deleted
  once your notes are saved (unless you choose to keep the audio).

## Install

```sh
pipx install git+https://github.com/brem-21/hearmeout    # Flatpak and AppImage coming
```

Needs PipeWire (most distros since 2022) or PulseAudio.

## Set up

```sh
hearmeout init      # creates ~/.config/hearmeout/config.toml
hearmeout doctor    # checks audio, keys and vault
```

Put your name in the config file (so tasks addressed to you are found) and add your keys,
either in the file or as environment variables:

```sh
export ELEVENLABS_API_KEY=...   # needs Speech to Text access
export OPENROUTER_API_KEY=...
```

Your vault is found automatically from Obsidian's own settings (deb, AppImage,
Flatpak or Snap installs). Otherwise, set `vault` in the config.

## Use

### Automatically (recommended)

```sh
hearmeout watch --autostart     # runs in the tray now and at every login
```

When a meeting starts you'll get a notification: **Record**, **Not now** or
**Never for this app**. While it's recording the tray icon is a red dot. When
the call ends (or you press **Stop recording**), notes are made and the review
window opens.

| Detected as a meeting | How |
|---|---|
| Zoom, Microsoft Teams, Slack huddles, Discord, Webex, Skype, Telegram, Signal, Element, WhatsApp… | the app uses your microphone |
| Google Meet, Teams, Zoom, Jitsi, Whereby… in a browser | the browser uses your microphone; the tab title names the meeting when it's visible (X11) |

Other apps using the mic (voice recorders, OBS) are ignored. The tray menu has
**Record now**, **Unsaved recordings**, detection on/off, **Record without asking**
and **Start at login**. On GNOME the tray icon needs the AppIndicator extension
(on by default on Ubuntu).

### By hand

```sh
hearmeout record                  # Ctrl+C when the meeting ends, then review and save
hearmeout process meeting.m4a     # notes from an existing recording
hearmeout record --no-gui         # review in the terminal
hearmeout record -y --save summary,my_todos   # save without asking
```

If you close the review window without saving, the recording is kept. The command
shows how to come back to it later.

## Models

Any OpenRouter model with structured output support works (`--model` or `[llm] model`).
Tested on a sample meeting:

| Model | Notes |
|---|---|
| `google/gemini-3.1-flash-lite` | **Default.** Correct owners and dates, about 3 s, well under a cent per meeting |
| `anthropic/claude-sonnet-5` | Most careful: doesn't invent dates for vague deadlines like "next week" |
| `openai/gpt-4o-mini` | Not recommended: missed tasks assigned to other people |

To keep everything local, point `base_url` at Ollama (`http://localhost:11434/v1`).

## Development

```sh
uv venv && uv pip install -e .
.venv/bin/hearmeout doctor
```
