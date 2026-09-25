# Hear Me Out

Meeting notes from your Linux desktop into Obsidian.

Hear Me Out records your meetings (any app: Zoom, Meet, Teams, Slack, Discord…),
transcribes them with ElevenLabs Scribe, uses a model of your choice through
OpenRouter to write a summary and pull out tasks, and saves what you choose
into your local Obsidian vault as plain Markdown.

- **Notices meetings for you.** When Zoom, Teams, Slack, Discord, Google Meet
  and similar apps start using your microphone, it asks whether to record. It
  stops by itself when the call ends, or after 2 minutes with nobody talking
  (with a **Keep recording** warning first), then opens your notes for review.
- **One place for all your meetings.** The app lists recordings waiting to be saved
  and everything already in Obsidian. Read summaries, tick off to-dos (the Obsidian
  note updates too), search across transcripts, and click any line of a transcript
  to hear that moment.
- **You vs. everyone else.** Your microphone and the computer's audio are
  recorded as separate tracks, so the transcript knows what *you* said and
  "My to-dos" only lists tasks that are yours.
- **Knows who you're talking to.** When the call window names the other person
  (a Slack DM or huddle, "Call with …" in Teams, a Discord DM), the notification
  says who it's with, the transcript shows their name instead of "Them", and an
  unsaved one-on-one is listed as "Call with Richard".
- **Headset or speakers.** Recording follows the mic and speaker your call app
  actually uses (USB or Bluetooth headset), even if you switch mid-call. On laptop
  speakers, the other side's voice picked up by your mic is filtered out.
- **Knows your calendar.** Connect your Outlook calendar (Settings › Integrations) and a
  recording is named after the Outlook or Teams meeting it belongs to. Invitees'
  names help the transcript and the to-do owners, the invite's agenda helps the
  summary. Home shows your week's meetings (click one to join, or to open the notes if you
  recorded it), and you get a reminder with a **Join** button 5 minutes before each one.
- **You choose what's saved.** After each meeting, a review window lets you pick
  any of: summary, my to-dos, team tasks, transcript, audio. Untick individual
  tasks, fix the title, pick the vault, then save.
- **Local files.** Each item becomes its own note in
  `<vault>/Meetings/<date> <title>/` (pick another folder in the vault, like
  `Work/Meetings`, when saving or in Settings). Obsidian doesn't need to be open. Tasks use
  the [Tasks plugin](https://publish.obsidian.md/tasks/) format (`- [ ] … 📅 2026-09-25`),
  and every task carries the quote it came from.
- **Your keys, your machine.** No account and no server. Recordings are deleted
  once your notes are saved (unless you choose to keep the audio).

## Install

You need Linux with PipeWire (most distros since 2022) or PulseAudio, and either
[uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/) to install
Python apps. Flatpak and AppImage packages are coming.

**1. Install Hear Me Out**

```sh
uv tool install git+https://github.com/brem-21/hearmeout
# or: pipx install git+https://github.com/brem-21/hearmeout
```

**2. Add your name and API keys**

Open the app (`hearmeout`) and use **Settings** (bottom left, or Ctrl+,): your name,
your ElevenLabs and OpenRouter keys (**Check keys** tests them), the notes model,
your vault and meeting options.

Or from a terminal, create `~/.config/hearmeout/config.toml` (only you can read it)
with `hearmeout init` and fill in:

```toml
[user]
name = "Your Name"        # as people say it in meetings, so tasks for you are found
aliases = []              # nicknames, e.g. ["Kwame"]

[stt]
api_key = "sk_…"          # ElevenLabs key with Speech to Text access

[llm]
api_key = "sk-or-v1-…"    # OpenRouter key
```

Your Obsidian vault is found automatically from Obsidian's own settings (deb,
AppImage, Flatpak or Snap installs). Otherwise, set `vault` under `[obsidian]`.
Keys can also be given as environment variables (`ELEVENLABS_API_KEY`,
`OPENROUTER_API_KEY`) instead; saving Settings won't copy them into the file.

**3. Check everything works**

```sh
hearmeout doctor    # checks audio, your name, keys, vault (and calendar, once connected)
```

**4. Add it to your app menu**

```sh
hearmeout install --autostart   # app menu entry, and start quietly in the tray at login
```

Then open **Hear Me Out** from your app menu (or run `hearmeout`).

On GNOME the tray icon needs the AppIndicator extension, which Ubuntu has by default.
Elsewhere install "AppIndicator and KStatusNotifierItem Support". Without it, the
app still detects meetings and notifies you; open the window from the app menu.

### Update and uninstall

```sh
uv tool upgrade hearmeout       # or: pipx upgrade hearmeout
```

```sh
uv tool uninstall hearmeout     # or: pipx uninstall hearmeout
rm ~/.local/share/applications/io.github.brem_21.hearmeout.desktop \
   ~/.config/autostart/io.github.brem_21.hearmeout.desktop
```

Your meeting notes stay in your Obsidian vault. Settings are in
`~/.config/hearmeout/` and unsaved recordings in `~/.local/share/hearmeout/`;
delete those too to remove everything.

### Connect your Outlook / Teams calendar (optional)

No sign-in, no Azure and no admin needed. You give Hear Me Out a private link to
your calendar:

1. In [Outlook on the web](https://outlook.office.com/calendar): **Settings ›
   Calendar › Shared calendars › Publish a calendar**.
2. Choose **Calendar** and **Can view all details**, then press **Publish**.
3. Copy the **ICS** link (not the HTML one).
4. In Hear Me Out: **Settings › Integrations**, paste it and press **Connect**.

Keep the link private: anyone who has it can see your calendar. To stop sharing,
press **Unpublish** in Outlook and **Disconnect** in Hear Me Out. If there's no
**Publish a calendar** option, your organisation has turned it off; ask IT, or use
[an app registration](#your-own-microsoft-app-registration) instead.

The link is kept in `~/.config/hearmeout/config.toml` (only you can read it) and your
next few days of events in `~/.local/share/hearmeout/calendar.json`.

### Show your recent emails (optional)

Hear Me Out reads your inbox through the work account you add to GNOME, so there's
nothing to register in Azure:

1. Open **Settings › Online Accounts › Microsoft 365** (or **Open Online Accounts** on
   Home) and sign in with your work account.
2. That's it: Home lists your newest emails within a few seconds, and checks for new
   ones every 5 minutes.

GNOME asks Microsoft for access on your behalf. If your organisation hasn't approved
GNOME Online Accounts, Microsoft shows "Need admin approval"; ask IT to approve it.
Choose how many emails to show, or hide them, in **Settings › Integrations › Mail**.
The latest ones are kept in `~/.local/share/hearmeout/mail.json` (only you can read it).

## Use

### The app (recommended)

Open **Hear Me Out** from your app menu, or run `hearmeout`.

The app opens on **Home**: a greeting, **Record now**, a reminder if your name or
keys are still missing, your week from the calendar with your mail beside it, then
your recent meetings with your open to-dos beside them (tick them off right there). On
a narrow window each pair stacks, one above the other.

- **Usage** (top right): what's left on your accounts, e.g. `$4.80 · 76k credits`.
  Click it for ElevenLabs credits used and left (and when they reset), your
  OpenRouter balance and spend today / this month, and what Hear Me Out used this
  month: recordings and minutes of audio, sets of notes, tokens in / out and cost
  (by model). To see ElevenLabs credits, your key needs the **User: read**
  permission (ElevenLabs › API keys).
- **Mail** (a pane beside the week; below it on a narrow window): your 5–10 newest emails (8 by default), unread first in bold, with
  attachment and high-importance marks. Click one to read it in Outlook on the web.
  See [Show your recent emails](#show-your-recent-emails-optional).
- **The week** shows the working days, Monday to Friday, with today highlighted. **‹ ›** move between
  weeks and **Today** comes back. Hover a meeting for who's invited. Click it to join
  the call (↗), or, for a meeting you recorded, to open its notes (◆ saved,
  🎙 still to save).
- **Reminders:** a notification with **Join** a few minutes before each meeting
  (Settings › Integrations › Remind me: 5 minutes by default, or Never).
- **To-dos** (sidebar, or the to-dos tile) lists your to-dos and the team's tasks
  (with who owns each) from your saved meetings, **by date** (Overdue, Today,
  Tomorrow, Later this week, Next week, Later, No due date) or **by meeting**. Untick
  **Team tasks** for just yours, or tick **Show done** for finished ones. Ticking one
  updates its note in Obsidian. Esc or Alt+Home comes back to it.

The sidebar has **Record** (Ctrl+R), search (Ctrl+F), recordings still to save and
your saved meetings by day; Settings (Ctrl+,) is at the bottom, with buttons to jump
to each section (Appearance, You, Keys, Obsidian, Meetings, Integrations). Leaving Settings with
unsaved changes asks whether to save or discard them. A **Light | Dark** switch in
the sidebar sets the theme whatever your system uses (Settings › Appearance also has
“Match system”). While recording, a banner shows the time, live **You / Others** levels
(handy for checking your headset mic is heard) and a countdown if nobody is talking.
Closing the window keeps Hear Me Out running in the tray so it can notice
meetings; opening it again (from the app menu or the tray) brings the window back.

| In the list | What you can do |
|---|---|
| **To save**: *Making notes* | **Stop** (also in the notification and the right-click menu) stops transcribing / writing notes. The recording is kept, and so is a transcript that was already made, so **Make notes** later never pays for it twice |
| **To save**: *Ready to save* | read the summary, to-dos (with the quote each came from and **Hear it**), team tasks and transcript; rename the meeting; leave out tasks; pick what goes into Obsidian, then **Save**. A progress bar counts each file. |
| **To save**: *Failed* | see why (no internet, bad API key…), **Try again**, play or delete the recording |
| **Saved in Obsidian** (wherever you saved it, even if you rename or move the folder in Obsidian) | the same view: tick off to-dos (the note in Obsidian updates), click a transcript time to hear it, **Open in Obsidian** |

Right-click a meeting for **Open in Obsidian**, **Show folder** or **Make notes**.
Select several (or **Select everything**) and press Delete to **Move to Trash**:
recordings and their notes in the vault go to your system's Trash, so they can be
restored.

When a meeting starts you'll get a notification: **Record**, **Not now** or
**Never for this app**. While it's recording the tray icon is a red dot. When
the call ends (or you press **Stop recording**), notes are made and the app
opens on them, ready to save.

| Detected as a meeting | How |
|---|---|
| Zoom, Microsoft Teams, Slack huddles, Discord, Webex, Skype, Telegram, Signal, Element, WhatsApp… | the app uses your microphone |
| Google Meet, Teams, Zoom, Jitsi, Whereby… in a browser | the browser uses your microphone; the tab title names the meeting when it's visible (X11) |

Other apps using the mic (voice recorders, OBS) are ignored. Settings › Meetings
(and the tray menu) has detection on/off, **Record without asking**, the apps you
chose never to be asked about, and **Start at login**. On GNOME the tray icon needs the AppIndicator extension
(on by default on Ubuntu).

### With your calendar

When your calendar is connected, a recording is matched to the event it overlaps
(starting up to 15 minutes early). The model still writes the meeting's title; the
event's name is only used when there are no notes. The summary
gets a **From the invite** section (organiser, invitees, agenda, a link to Outlook),
and the frontmatter lists who was `invited`. A one-on-one shows the other person's
name in the transcript instead of "Them".

### From the terminal

```sh
hearmeout record                  # Ctrl+C when the meeting ends, then review and save
hearmeout process meeting.m4a     # notes from an existing recording
hearmeout record --no-gui         # review in the terminal
hearmeout record -y --save summary,my_todos   # save without asking
```

If you close the review window without saving, the recording is kept. The command
shows how to come back to it later.

### If something goes wrong

The app usually runs without a terminal, so unexpected errors are written to
`~/.local/share/hearmeout/app.log`. Include it when reporting a problem.

## Models

Any OpenRouter model with structured output support works (`--model` or `[llm] model`).
Tested on a sample meeting:

| Model | Notes |
|---|---|
| `google/gemini-3.1-flash-lite` | **Default.** Correct owners and dates, about 3 s, well under a cent per meeting |
| `anthropic/claude-sonnet-5` | Most careful: doesn't invent dates for vague deadlines like "next week" |
| `openai/gpt-4o-mini` | Not recommended: missed tasks assigned to other people |

To keep everything local, point `base_url` at Ollama (`http://localhost:11434/v1`).

## Your own Microsoft app registration

Instead of a calendar link, Hear Me Out can sign in to Microsoft 365 (**Settings ›
Integrations › Sign in with an app registration instead**). Microsoft only allows
this for a registered app, so someone with access to the Azure portal registers one
once. That ID can go in `BUILT_IN_CLIENT_ID` in `src/hearmeout/outlook.py` for
everyone, or be pasted in Settings:

1. [portal.azure.com](https://portal.azure.com) › **Microsoft Entra ID** ›
   **App registrations** › **New registration**
   - Name: `Hear Me Out`
   - Supported account types: **Accounts in any organizational directory (Multitenant)**
     (for your organisation's own: *this organizational directory only*)
   - Redirect URI: platform **Public client/native (mobile & desktop)**, `http://localhost`
2. **API permissions** › Add › Microsoft Graph › Delegated › **Calendars.Read**
   (User.Read is already there).
3. Copy the **Application (client) ID**. There's no client secret: it's a desktop app.
4. Maintainers: put it in `BUILT_IN_CLIENT_ID`. Adding publisher verification
   (**Branding & properties**) lets people in most organisations approve it without IT.
   For your organisation's own: **Settings › Integrations › Use my own app registration**,
   or in `config.toml`:

   ```toml
   [microsoft]
   client_id = "1a2b3c4d-…"
   tenant = "your-tenant-id"   # needed for a single-organisation registration
   ```

## Development

```sh
uv venv && uv pip install -e .
.venv/bin/hearmeout doctor
```
