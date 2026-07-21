# JARVIS — a voice-driven Windows desktop assistant

JARVIS is a Tony Stark–style voice assistant for Windows. Say **"jarvis"** and it
opens apps and files, controls system volume, brightness and Spotify, answers
open-ended questions out loud, and can act on the machine for you (run commands,
read/write files, search the web) — all narrated back in a processed, in-character
voice, with a small always-on-top HUD overlay that shows what it's doing. Speed
comes from a two-tier router: local commands (open, play, volume, time) are matched
on-device in well under a millisecond and never touch the network, while anything
open-ended is handled by a fast LLM with real tool-calling. Destructive actions are
always spoken back for a verbal "yes" before they run.

> Windows only. It leans on Windows-specific APIs (WASAPI/pycaw audio ducking, the
> registry for user folders, SAPI voices, Task Scheduler autostart) and is not
> portable to macOS or Linux as-is.

## Tech stack

| Layer | What it uses |
|---|---|
| **Backend** | Python 3.11+, FastAPI + Uvicorn (WebSocket server driving the HUD), asyncio orchestrator |
| **Wake word** | [openWakeWord](https://github.com/dscripka/openWakeWord) (local, offline) — the pretrained `hey_jarvis` model OR-ed with a small custom-trained head that fires on bare "jarvis" with any prefix |
| **Speech-to-text** | [Deepgram](https://deepgram.com) Nova streaming STT (interim transcripts, ~300 ms endpointing) |
| **Smart path (LLM)** | [Groq](https://groq.com) OpenAI-compatible chat API — `llama-3.1-8b-instant` by default, escalating to `llama-3.3-70b-versatile` for reasoning and tool use |
| **Text-to-speech** | [ElevenLabs](https://elevenlabs.io) streaming TTS through a light DSP chain for the "AI-filtered" timbre, with **Windows SAPI as an automatic fallback** if ElevenLabs is unavailable |
| **Music** | [Spotify Web API](https://developer.spotify.com) (via spotipy) with a media-key fallback |
| **HUD** | Electron — a frameless, transparent, click-through always-on-top overlay |
| **Local matching** | rapidfuzz (fuzzy file/app resolution), psutil (real system stats) |

## Prerequisites

- **Windows 10 or 11** (required — see the note above)
- **Python 3.11+**
- **Node.js 18+** (for the Electron HUD)
- A **microphone**
- API keys (all have free tiers): **Groq**, **Deepgram**, **ElevenLabs**, and a
  **Spotify** app. Spotify playback control additionally requires **Spotify Premium**
  and the desktop app signed into the same account.

## Setup

### 1. Clone

```bash
git clone https://github.com/hemanthloq/Mini_Jarvis.git
cd Mini_Jarvis
```

### 2. Install everything

The one-time setup script creates the Python venv, installs the Python and Electron
dependencies, downloads the wake-word models, **trains the custom "jarvis" wake
word** (a few minutes), builds the file/app index, and creates your `.env`:

```bat
setup.bat
```

<details>
<summary>Or do it manually</summary>

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
cd frontend && npm install && cd ..
:: download wake models + build the file index
.venv\Scripts\python.exe -c "import sys; sys.path.insert(0,'backend'); from audio import wake; wake.ensure_models(); from system import indexer; indexer.build_index()"
:: train the custom bare-word "jarvis" wake head (one time)
.venv\Scripts\python.exe scripts\train_wake_jarvis.py
copy .env.example .env
```
</details>

### 3. Add your API keys

`setup.bat` copies `.env.example` to `.env` and opens it. Fill in each key:

| Key | Where to get it |
|---|---|
| `GROQ_API_KEY` | [console.groq.com/keys](https://console.groq.com/keys) — free tier; powers the smart path |
| `DEEPGRAM_API_KEY` | [console.deepgram.com](https://console.deepgram.com) — free credit on signup; streaming STT |
| `ELEVENLABS_API_KEY` | [elevenlabs.io → Settings → API keys](https://elevenlabs.io/app/settings/api-keys) — the voice |
| `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET` | [developer.spotify.com/dashboard](https://developer.spotify.com/dashboard) → **Create app**. Set the app's **Redirect URI** to exactly `http://127.0.0.1:8888/callback`. |
| `ANTHROPIC_API_KEY` | *(optional)* [console.anthropic.com](https://console.anthropic.com/settings/keys) — only if you switch the smart path back to Claude |

The voice defaults to ElevenLabs' "Daniel" (calm British male). Paste any voice ID
from the [voice library](https://elevenlabs.io/app/voice-library) into
`ELEVENLABS_VOICE_ID` to change it. The rest of `.env` is tuning with sensible
defaults — you can leave it alone.

### 4. Wake word

`setup.bat` already trains the custom "jarvis" head for you. It's trained locally on
openWakeWord's feature extractor using your machine's SAPI voices plus heavy
augmentation (and includes JARVIS's own TTS voice as a negative example so it can't
wake itself). To retrain at any time:

```bat
.venv\Scripts\python.exe scripts\train_wake_jarvis.py
```

If training is skipped or fails, JARVIS falls back to openWakeWord's pretrained
`hey_jarvis` phrase model — so you can still wake it with "hey jarvis" out of the box.

*(Optional but recommended)* Pre-render the stock spoken phrases so fast-path replies
are instant (needs `ELEVENLABS_API_KEY`):

```bat
.venv\Scripts\python.exe scripts\cache_phrases.py
```

### 5. Run

```bat
start.bat
```

This launches the backend and the HUD. Say **"jarvis"**, wait for the chime, then
speak. The first Spotify command opens a browser once for OAuth; the token is cached
locally afterwards.

*(Optional)* `enable_autostart.bat` registers a Windows Task Scheduler task to launch
JARVIS ~30 seconds after you log in. Remove it with `disable_autostart.bat`.

## Features

- **Wake word, fully offline** — "jarvis" with any prefix ("hey jarvis", "okay
  jarvis", or just "jarvis"). A VAD gate rejects non-speech, and sensitivity rises
  automatically while a video player is focused so on-screen dialogue doesn't false-trigger.
- **Fast local commands** (no LLM, sub-second): open apps/files/folders, `play [song]`,
  volume/mute, `set volume to N`, brightness, pause/skip/previous/stop, time/date,
  `close this`, `rebuild the index`.
- **Smart path with real tool-calling** — for anything open-ended, JARVIS can run
  shell commands, open paths, list directories, read/write files, search the web, open
  a browser, and report real system health. It never invents system stats, and it never
  speaks tool-call syntax aloud.
- **Safety first** — destructive actions (delete, overwrite, move, kill, uninstall)
  are spoken back for a verbal "yes" before running; file operations resolve and verify
  the real path rather than acting on a guessed one; and the LLM is hard-blocked from
  ever powering off the machine.
- **Spotify** — "play [song]" with or without an artist, via the Web API with a media-key
  fallback (requires Premium).
- **Voice with a fallback** — ElevenLabs streaming TTS with a "Jarvis timbre" DSP chain;
  if it's unavailable (quota, bad key, outage) JARVIS speaks with the Windows voice
  instead of going silent, and the HUD flags it.
- **Barge-in** — start talking while it's speaking and it stops and listens; it learns
  how much of its own voice the mic hears so it never interrupts itself.
- **Follow-up mode** — after a reply the mic stays open briefly so you can keep talking
  without repeating the wake word.
- **Push-to-talk & privacy mute** — a hotkey to start listening instantly, and a hard
  mic-mute that no voice command can undo.
- **User-editable routines** — map a trigger phrase to a spoken line plus a list of
  actions in `data/routines.json`, no code changes needed.
- **A HUD overlay** — a frameless orb that shows state (idle / listening / thinking /
  speaking), stays out of the way of fullscreen games, and is draggable and click-through
  when idle.
- **Extras** — timers/stopwatch, reminders, a class-timetable lookup, a morning briefing,
  and a proactive "welcome back" after you've been away.

## Configuration & routines

All tunables live in `.env` (wake sensitivity, barge-in floor, follow-up window,
hotkeys, models, weather city, backend host/port) with working defaults. To add a
routine, edit `data/routines.json`:

```json
{
  "movie mode": {
    "say": "Dimming the noise, sir.",
    "actions": ["media:pause", "volume:30"]
  }
}
```

Each key is a trigger phrase (fuzzy-matched right after the wake word, before the LLM),
and the value is a spoken response plus actions. `aliases` lets you list forms the STT
commonly mishears. See the docstring in `backend/router/routines.py` for the full action
list.

For the class-timetable lookup ("what's my next class?"), copy
[`data/timetable.example.json`](data/timetable.example.json) to `data/timetable.json`
and fill in your own weekly schedule — the file itself documents the format.

## Known limitations

- **Groq free tier is rate-limited** (per-minute token cap). JARVIS rides out `429`s by
  retrying, so a burst of long queries can add a short delay rather than failing — this
  is expected, not a bug.
- **Spotify playback control requires Premium**, and the desktop app must be signed into
  the *same* account as your API credentials. A free account or a mismatched login can't
  be controlled.
- **STT needs connectivity** — offline, fast-path commands (volume, media keys, opening
  apps/files, time/date) still work, but voice input degrades to the HUD mic button /
  typed testing until you're back online.
- The file/app index is built at setup and on demand — a file added afterwards won't be
  found until you say "rebuild the index".
- See [KNOWN_ISSUES.md](KNOWN_ISSUES.md) for deeper, deliberately-documented rough edges.

## Project layout

```
backend/
  main.py            orchestrator + FastAPI WebSocket server (state -> HUD)
  config.py          .env loading, paths, tunables
  audio/             wake word, Deepgram STT, ElevenLabs TTS, DSP, playback
  router/            fast_path (local matcher), smart_path (LLM), tools, routines
  system/            app/file index, volume, media, health, timetable, reminders, ...
frontend/            Electron HUD (frameless always-on-top overlay)
scripts/             wake-word training, phrase caching, calibration, guardrail tests
data/                index, logs, tokens, routines, timetable  (git-ignored)
tests/               guardrail corpus for the matcher
```
