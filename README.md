# Hear Me Out

Meeting notes from your Linux desktop into Obsidian.

Hear Me Out records your meetings (any app: Zoom, Meet, Teams, Slack, Discord…),
transcribes them with ElevenLabs Scribe, uses a model of your choice through
OpenRouter to write a summary and pull out tasks, and saves what you choose
into your local Obsidian vault as plain Markdown.

- **Notices meetings for you.** When Zoom, Teams, Slack, Discord, Google Meet
  and similar apps start using your microphone, it asks whether to record. It
  stops by itself when the call ends, then opens your notes for review.
- **One place for all your meetings.** The app lists recordings waiting to be saved
  and everything already in Obsidian. Read summaries, tick off to-dos (the Obsidian
  note updates too), search across transcripts, and click any line of a transcript
  to hear that moment.
- **You vs. everyone else.** Your microphone and the computer's audio are
  recorded as separate tracks, so the transcript knows what *you* said and
  "My to-dos" only lists tasks that are yours.
- **Headset or speakers.** Recording follows the mic and speaker your call app
  actually uses (USB or Bluetooth headset), even if you switch mid-call. On laptop
  speakers, the other side's voice picked up by your mic is filtered out.
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

### The app (recommended)

```sh
hearmeout install --autostart   # adds Hear Me Out to your app menu and starts it at login
hearmeout                       # open the app
```

The app has a **Record** button, the list of meetings, and a **Settings** menu.
Closing the window keeps Hear Me Out running in the tray so it can notice
meetings; opening it again (from the app menu or the tray) brings the window back.

| In the list | What you can do |
|---|---|
| **To save**: *Ready to save* | pick what goes into Obsidian (summary, my to-dos, team tasks, transcript, audio), untick tasks, fix the title, then **Save**. A progress bar counts each file. |
| **To save**: *Failed* | see why (no internet, bad API key…), **Try again**, play or delete the recording |
| **Saved in Obsidian** | summary, to-dos you can tick off, team tasks, transcript; **Open in Obsidian**; play the audio if you kept it |

When a meeting starts you'll get a notification: **Record**, **Not now** or
**Never for this app**. While it's recording the tray icon is a red dot. When
the call ends (or you press **Stop recording**), notes are made and the app
opens on them, ready to save.

| Detected as a meeting | How |
|---|---|
| Zoom, Microsoft Teams, Slack huddles, Discord, Webex, Skype, Telegram, Signal, Element, WhatsApp… | the app uses your microphone |
| Google Meet, Teams, Zoom, Jitsi, Whereby… in a browser | the browser uses your microphone; the tab title names the meeting when it's visible (X11) |

Other apps using the mic (voice recorders, OBS) are ignored. The Settings menu
(also in the tray) has detection on/off, **Record without asking**, the apps you
chose never to be asked about, and **Start at login**. On GNOME the tray icon needs the AppIndicator extension
(on by default on Ubuntu).

### From the terminal

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
