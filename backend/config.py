"""Central configuration. Loads .env from the project root once."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent          # the repo root
load_dotenv(ROOT / ".env")

# ── API keys ────────────────────────────────────────────────────
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")  # kept for the Claude path
DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "onwK4e9ZLuTAKqWW03F9")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")

# ── Models (Groq, OpenAI-compatible) ────────────────────────────
SMART_MODEL_FAST = os.getenv("SMART_MODEL_FAST", "openai/gpt-oss-20b")
SMART_MODEL_DEEP = os.getenv("SMART_MODEL_DEEP", "openai/gpt-oss-120b")

# Seconds JARVIS keeps listening for a follow-up after finishing a reply
FOLLOWUP_WINDOW = float(os.getenv("FOLLOWUP_WINDOW", "7"))

# Push-to-talk: start listening immediately, bypassing the wake word
PTT_HOTKEY = os.getenv("PTT_HOTKEY", "ctrl+alt+j")

# Mic mute (privacy): kills the wake word AND the mic button until toggled back
MUTE_HOTKEY = os.getenv("MUTE_HOTKEY", "ctrl+alt+m")

# Hard ceiling (seconds) on one smart-path request incl. all chained tool calls.
# If exceeded, the request is abandoned and JARVIS says so — never a stuck HUD.
SMART_TIMEOUT = float(os.getenv("SMART_TIMEOUT", "20"))
# Seconds of no keyboard/mouse input that counts as "away". On returning, JARVIS
# greets once and mentions anything genuinely pending (system/idle.py).
IDLE_AWAY_SECONDS = float(os.getenv("IDLE_AWAY_SECONDS", "900"))    # 15 min

# City for the boot-briefing weather line (wttr.in — no key needed)
WEATHER_CITY = os.getenv("WEATHER_CITY", "Bengaluru")

# Barge-in: how loud (mic RMS, 0..1) speech must be to interrupt JARVIS mid-reply.
# Measured on this machine: its own voice comes back at <=0.011 (Windows cancels
# most of it); a person speaking lands around 0.03+. Raise if it interrupts
# itself; lower if it ignores you. Run scripts/calibrate_barge.py to measure.
# This mic's gain is low — the user's own voice peaks at only ~0.015 in the
# stream — so the floor must stay well under that or barge-in is impossible.
# SOURCE OF TRUTH is BARGE_FLOOR in .env (the calibrated, per-machine value);
# this literal is only the fallback for a fresh clone and is kept equal to it so
# the two can never silently disagree. audio/wake.py bounds how far room noise
# may lift this at runtime (see NOISE_CEILING there).
BARGE_FLOOR = float(os.getenv("BARGE_FLOOR", "0.01"))

# Volume (0-100) forced when a film/video/track is opened, so playback starts
# audible regardless of any ducking or a routine that turned things down.
MEDIA_VOLUME = int(os.getenv("MEDIA_VOLUME", "60"))

# ── Audio ───────────────────────────────────────────────────────
MIC_SAMPLE_RATE = 16_000           # openWakeWord and Deepgram both take 16 kHz mono
WAKE_FRAME_SAMPLES = 1280          # 80 ms frames, openWakeWord's native chunk
TTS_SAMPLE_RATE = 22_050           # ElevenLabs pcm_22050 output
# Wake confidence 0..1. Raised 0.5 -> 0.55: a movie's dialogue was scoring ~0.56
# and false-waking. Genuine "jarvis" scores 0.9+, so 0.55 still triggers reliably
# while rejecting more ambient audio. Sensitivity drops further during video
# playback (see WAKE_MEDIA_BOOST / foreground.media_app_active).
WAKE_THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.55"))
# Extra confidence required while a media/video player is the focused app.
WAKE_MEDIA_BOOST = float(os.getenv("WAKE_MEDIA_BOOST", "0.25"))
# Speech-probability gate (Silero VAD, 0..1): a wake must coincide with SPEECH,
# so non-speech (grunts, sighs, coughs) can't trip it. 0 disables the gate.
# Wired in audio/wake.py (VAD created in __init__, applied in _wake_score) —
# it works WITH the acoustic WAKE_THRESHOLD above, not instead of it. A low
# value (0.05) leans on the high WAKE_THRESHOLD as the primary filter and just
# strips near-silent non-speech. Tune both with scripts/calibrate_wake.py.
# Lowered 0.05 -> 0.02 (2026-07-19). The gate was rejecting every real wake word
# at speech_prob 0.003-0.011, but the root cause was that Silero — a STREAMING
# model — was only being called on sparse frames, which corrupts its LSTM state
# (fixed in audio/wake.py; that alone raised live readings 3-4x). 0.02 still sits
# clear of the measured non-speech floor on this mic: digital silence 0.0138,
# 60 Hz hum 0.0069, 1 kHz tone 0.0110. Broadband noise DOES read speech-like to
# any VAD — WAKE_THRESHOLD plus WAKE_MEDIA_BOOST is the guard there, not this.
WAKE_VAD_THRESHOLD = float(os.getenv("WAKE_VAD_THRESHOLD", "0.02"))

# ── Server ──────────────────────────────────────────────────────
HOST = os.getenv("JARVIS_HOST", "127.0.0.1")
PORT = int(os.getenv("JARVIS_PORT", "8765"))

# ── Paths ───────────────────────────────────────────────────────
DATA_DIR = ROOT / "data"
CACHE_DIR = ROOT / "backend" / "cache"
INDEX_FILE = DATA_DIR / "file_index.json"
SPOTIFY_TOKEN_CACHE = DATA_DIR / ".spotify_token_cache"
LOG_FILE = DATA_DIR / "jarvis.log"

DATA_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

# Folders the file-alias indexer scans (recursive). Videos/Pictures/Music are
# included so their subfolders ('Screen Recordings', 'Captures', ...) resolve —
# without them, "open screen recordings folder" failed despite the folder
# existing right inside Videos.
USER = Path.home()

# Windows can REDIRECT the known user folders into OneDrive, which leaves a
# near-empty legacy directory behind at C:\Users\<user>\Desktop. Path.home() /
# "Desktop" then indexes the wrong one and JARVIS is simply blind to everything
# the user actually sees on their desktop. Measured on this machine:
#     Desktop    ~\Desktop 13 items   vs  ~\OneDrive\Desktop 33 items
#     Documents  ~\Documents 4 items  vs  ~\OneDrive\Documents 28 items
#     Pictures   ~\Pictures MISSING   vs  ~\OneDrive\Pictures 8 items
# That is why "open jackfruit folder" failed for a folder sitting in plain sight.
# The registry is the authority; both locations are indexed, because the legacy
# folder is usually not empty either (this machine has real files in both).
_SHELL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
_KNOWN_FOLDERS = {          # registry value name -> plain home-relative name
    "Desktop": "Desktop",
    "Personal": "Documents",
    "My Pictures": "Pictures",
    "My Music": "Music",
    "My Video": "Videos",
}


def _user_folders() -> list[Path]:
    """Every real user folder to index, redirected location first, deduped.

    Falls back to the plain home-relative paths if the registry is unreadable,
    so a non-Windows or locked-down machine still works."""
    out: list[Path] = []

    def add(p) -> None:
        try:
            p = Path(p)
        except (TypeError, ValueError):
            return
        if p.exists() and p not in out:
            out.append(p)

    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _SHELL_KEY) as key:
            for reg_name in _KNOWN_FOLDERS:
                try:
                    raw, _ = winreg.QueryValueEx(key, reg_name)
                    add(os.path.expandvars(raw))
                except OSError:
                    continue
    except (ImportError, OSError):
        pass                     # not Windows, or the key is unreadable

    # Plain locations too: Downloads is never redirected, and the legacy
    # Desktop/Documents usually still hold real files after a OneDrive move.
    for plain in ("Documents", "Desktop", "Downloads", "Videos", "Pictures", "Music"):
        add(USER / plain)
    return out


def _extra_drive_roots() -> list[Path]:
    """Non-system fixed drives, indexed whole.

    Audited on this machine: only C: and D: exist, both NTFS fixed. D: is the
    user's data drive (~2 950 real files once junk is pruned) so indexing it
    outright is cheap and makes 'open <anything on D:>' work without naming the
    drive. Deliberately EXCLUDED, with reasons:
      * the system drive's root  — C:\\Windows, C:\\Program Files, C:\\ProgramData
        are enormous, system-owned, and nothing there is a user document;
        installed apps are already covered by the Start-Menu/UWP app index.
      * removable and network drives — a USB stick or share that is absent at
        boot would make the index wrong, and scanning a network path can block
        for a long time. Say the drive by name ("open X in the E drive") and the
        live drive walk in system/drives.py handles it.
      * per-directory junk (node_modules, .git, .venv, $RECYCLE.BIN,
        System Volume Information, AppData) — pruned in system/indexer.py.
    """
    roots: list[Path] = []
    system = (os.environ.get("SystemDrive", "C:") or "C:").rstrip("\\").upper()
    try:
        import string
        import ctypes
        mask = ctypes.windll.kernel32.GetLogicalDrives()
        letters = [f"{c}:" for i, c in enumerate(string.ascii_uppercase) if mask >> i & 1]
        for letter in letters:
            if letter.upper() == system:
                continue
            # 3 == DRIVE_FIXED: skip removable/network/CD.
            if ctypes.windll.kernel32.GetDriveTypeW(f"{letter}\\") != 3:
                continue
            p = Path(f"{letter}\\")
            if p.exists():
                roots.append(p)
    except (ImportError, OSError, AttributeError):
        pass                     # not Windows, or the API is unavailable
    return roots


INDEX_FOLDERS = _user_folders() + _extra_drive_roots()
