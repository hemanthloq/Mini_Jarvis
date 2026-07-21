"""Fast path: local intent matcher. NO network, NO LLM — target <300 ms.

Order of matching on each final transcript:
  1. Parameterized regex commands ("play X by Y", "open X", "set volume to N")
  2. Fixed phrases via rapidfuzz against the command table
If nothing matches, the orchestrator falls through to the smart path.

Every handler returns the short spoken confirmation (str) or None to signal
"not handled after all" (e.g. unknown file name -> let Claude answer).
"""
from __future__ import annotations

import datetime
import logging
import re
from dataclasses import dataclass
from typing import Callable, Optional

from rapidfuzz import fuzz, process

from system import apps, media, notes, reminders, textnorm, volume

log = logging.getLogger("jarvis.fastpath")

FUZZ_THRESHOLD = 86  # fixed-phrase match confidence 0..100

_FILLER = re.compile(
    r"^(?:hey\s+)?(?:jarvis[,!\s]*)?(?:please\s+|can you\s+|could you\s+|would you\s+)?", re.I)
_TRAIL = re.compile(r"(?:\s*(?:please|for me|now|thanks|thank you))+[\s.!?]*$", re.I)


def normalize(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^\w\s%:]", " ", text)      # keep ':' so "3:30pm" survives
    text = _FILLER.sub("", text)
    text = _TRAIL.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class CommandResult:
    reply: str                 # short confirmation to speak
    canned: bool = True        # True -> may use a pre-rendered cached phrase
    command: str = ""          # for logging / HUD
    data: dict | None = None   # structured payload for commands main.py handles


# ── Fixed handlers ──────────────────────────────────────────────
def _time() -> str:
    return "It's " + datetime.datetime.now().strftime("%I:%M %p").lstrip("0") + "."


def _date() -> str:
    return "Today is " + datetime.datetime.now().strftime("%A, %B %d.").replace(" 0", " ")


FIXED: dict[str, Callable[[], Optional[str]]] = {
    # time / date
    "what time is it": _time,
    "what's the time": _time,
    "tell me the time": _time,
    "what day is it": _date,
    "what's the date": _date,
    "what's today's date": _date,
    # volume
    "volume up": volume.volume_up,
    "turn it up": volume.volume_up,
    "louder": volume.volume_up,
    "volume down": volume.volume_down,
    "turn it down": volume.volume_down,
    "quieter": volume.volume_down,
    "mute": volume.mute,
    "mute the volume": volume.mute,
    "unmute": volume.unmute,
    "unmute the volume": volume.unmute,
    # media transport
    "pause": media.pause_play,
    "pause the music": media.pause_play,
    "resume": media.pause_play,
    "resume the music": media.pause_play,
    "play the music": media.pause_play,
    "skip": media.next_track,
    "next": media.next_track,
    "next track": media.next_track,
    "next song": media.next_track,
    "skip this song": media.next_track,
    "previous": media.previous_track,
    "previous track": media.previous_track,
    "previous song": media.previous_track,
    "go back a song": media.previous_track,
    "stop": media.stop,
    "stop the music": media.stop,
    # window
    "close this": media.close_active_window,
    "close this window": media.close_active_window,
    "close the window": media.close_active_window,
    # maintenance
    "rebuild the index": apps.rebuild,
    "rebuild index": apps.rebuild,
    "rebuild your index": apps.rebuild,
    # notes (read-back — dynamic)
    "what are my notes": notes.read_back,
    "read my notes": notes.read_back,
    "read me my notes": notes.read_back,
    "read my notes back": notes.read_back,
    "read back my notes": notes.read_back,
    "what did i note": notes.read_back,
    # repeat last reply (handled in main — needs the last spoken text)
    "repeat that": lambda: "",
    "say that again": lambda: "",
    "say it again": lambda: "",
    "what did you say": lambda: "",
    "come again": lambda: "",
    "can you repeat that": lambda: "",
    # briefing (handled in main — reads data/briefing.txt aloud)
    "what's my briefing": lambda: "",
    "what is my briefing": lambda: "",
    "read my briefing": lambda: "",
    "my briefing": lambda: "",
    "what's today looking like": lambda: "",
    "what does today look like": lambda: "",
    "morning briefing": lambda: "",
    # status report (handled in main — time, day, briefing freshness)
    "status report": lambda: "",
    "give me a status report": lambda: "",
    "system status": lambda: "",
    # sign-off (just ends the follow-up window — JARVIS stays awake and listening)
    "that's all": lambda: "Very good, sir.",
    "that's all for now": lambda: "Very good, sir.",
    "that will be all": lambda: "Very good, sir.",
    "never mind": lambda: "Very good, sir.",
    "nevermind": lambda: "Very good, sir.",
    "thanks jarvis": lambda: "Anytime, sir.",
    "thank you jarvis": lambda: "Anytime, sir.",
    "that's it": lambda: "Very good, sir.",
    "see you later": lambda: "Until next time, sir.",
    # sleep — dormant, wake word still listening. NEVER a full quit and NEVER an
    # OS shutdown: "shut down", "power down", "go offline" all mean JARVIS sleep.
    "go to sleep": lambda: "",
    "go to sleep jarvis": lambda: "",
    "jarvis go to sleep": lambda: "",
    "shut up and turn it off": lambda: "",
    "shut down": lambda: "",
    "shutdown": lambda: "",
    "shut yourself down": lambda: "",
    "power down": lambda: "",
    "go offline": lambda: "",
    "turn yourself off": lambda: "",
    "turn it off": lambda: "",
    "turn off": lambda: "",
    "power off": lambda: "",
    "sleep": lambda: "",
    "take a nap": lambda: "",
    "goodnight jarvis": lambda: "",
    "good night jarvis": lambda: "",
    "goodnight": lambda: "",
    # OS shutdown — the ONLY phrases that power off the whole PC (with a spoken
    # yes, handled in main). Deliberately narrow: must name the machine.
    "shutdown my pc": lambda: "",
    "shut down my pc": lambda: "",
    "shut down my computer": lambda: "",
    "shutdown my computer": lambda: "",
    "shut down my laptop": lambda: "",
    "shutdown my laptop": lambda: "",
    "shut down the computer": lambda: "",
    "shut down the pc": lambda: "",
}

_END_PHRASES = {"that's all", "that's all for now", "that will be all",
                "never mind", "nevermind", "thanks jarvis", "thank you jarvis",
                "that's it", "see you later"}

# These power off the WHOLE PC. Narrow on purpose — they must name the machine.
_SHUTDOWN_PC_PHRASES = {"shutdown my pc", "shut down my pc", "shut down my computer",
                        "shutdown my computer", "shut down my laptop",
                        "shutdown my laptop", "shut down the computer",
                        "shut down the pc"}

# Everything vaguer puts JARVIS to sleep (dormant + wake word alive), not quit,
# and NEVER shuts down the machine.
_SLEEP_PHRASES = {"go to sleep", "go to sleep jarvis", "jarvis go to sleep",
                  "shut up and turn it off", "shut down", "shutdown",
                  "shut yourself down", "power down", "go offline",
                  "turn yourself off", "turn it off", "turn off", "power off",
                  "sleep", "take a nap", "goodnight jarvis", "good night jarvis",
                  "goodnight"}

_BRIEFING_PHRASES = {"what's my briefing", "what is my briefing", "read my briefing",
                     "my briefing", "what's today looking like",
                     "what does today look like", "morning briefing"}

_STATUS_PHRASES = {"status report", "give me a status report", "system status"}

# Phrases whose reply is dynamic (time, index counts, notes): never canned.
_DYNAMIC = {_time, _date, apps.rebuild, notes.read_back}

# "repeat that" / "say that again" — handled in main (re-speaks the last reply).
_REPEAT_PHRASES = {"repeat that", "say that again", "say it again",
                   "what did you say", "come again", "can you repeat that"}

# ── Parameterized patterns (checked first, in order) ────────────
_PATTERNS: list[tuple[re.Pattern, Callable[[re.Match], Optional[CommandResult]]]] = []


def _pattern(rx: str):
    def deco(fn):
        _PATTERNS.append((re.compile(rx, re.I), fn))
        return fn
    return deco


def _do_play(song: str, artist: Optional[str] = None) -> CommandResult:
    """Three-tier Spotify play: auto-play a strong match, ASK on a plausible one
    (garbled STT is common but Spotify search usually still finds it), and only
    fail outright when even the top result is a poor match."""
    track, tier = media.search_track(song, artist)
    if tier == "unavailable":
        return CommandResult("Spotify isn't set up, sir.", canned=False,
                             command="spotify_off")
    if tier == "none" or track is None:
        return CommandResult(f"I couldn't find {song} on Spotify, sir.",
                             canned=False, command="spotify_none")
    if tier == "high":
        return CommandResult(media.play_track(track), canned=False, command="spotify")
    return CommandResult(f"Did you mean {media._track_label(track)}, sir?",
                         canned=False, command="song_maybe", data={"track": track})


# "play X in [the] Y folder" is a LOCAL file open (a recording in a folder), NOT
# a Spotify search. Spotify is only for a bare "play [song]" with no local
# file/folder context. This must be checked before the generic play patterns.
@_pattern(r"^(?:play|open) (?P<target>.+\bin\b\s+.+?\b(?:folder|directory)\b.*)$")
def _play_in_folder(m) -> Optional[CommandResult]:
    status, reply, extra = apps.open_target(m["target"].strip())
    if status == "ask":
        return CommandResult(reply, canned=False, command="open_maybe", data=extra)
    return CommandResult(reply, canned=False,
                         command="open" if status == "opened" else "open_failed")


@_pattern(r"^play (?P<song>.+?) by (?P<artist>.+)$")
def _play_by(m) -> CommandResult:
    return _do_play(m["song"].strip(), m["artist"].strip())


@_pattern(r"^play (?P<song>.+)$")
def _play(m) -> Optional[CommandResult]:
    song = m["song"].strip()
    # "play the music" style transport phrases are in FIXED; skip obvious ones
    if song in ("music", "the music", "it"):
        return None
    return _do_play(song)


# ── Timer / stopwatch ───────────────────────────────────────────
@_pattern(r"^(?:set|start|create|put)\s+(?:a\s+|an\s+)?timer\s+(?:for\s+|of\s+)?"
          r"(?P<dur>.+)$")
def _timer_set(m) -> Optional[CommandResult]:
    dur = m["dur"].strip()
    n = textnorm.parse_number(dur)
    if n is None:
        return CommandResult("How long should the timer be, sir?", canned=False,
                             command="timer_no_dur")
    if re.search(r"\bhour", dur, re.I):
        secs = n * 3600
    elif re.search(r"\bsec", dur, re.I):
        secs = n
    else:
        secs = n * 60                       # default unit: minutes
    return CommandResult("", canned=False, command="timer_set", data={"seconds": secs})


@_pattern(r"^(?:start|begin)\s+(?:a\s+)?stopwatch$")
def _stopwatch(m) -> CommandResult:
    return CommandResult("", canned=False, command="stopwatch_start")


@_pattern(r"^(?:stop|cancel|clear|end)\s+(?:the\s+|my\s+)?(?:timer|stopwatch|countdown)$")
def _timer_stop(m) -> CommandResult:
    return CommandResult("", canned=False, command="timer_stop")


# ── class timetable ─────────────────────────────────────────────
# Fast path on purpose: these are answered from a local JSON file and the system
# clock, so an LLM round-trip would add latency and a chance of inventing a
# class that isn't there.
# NOTE: these are matched against normalize()d text, not raw speech. normalize()
# turns "what's" into "what s" (apostrophe -> space) and STRIPS trailing filler
# like "now", so patterns written against the spoken form silently fail:
#   "what's my next class" -> "what s my next class"   (fell through to FIXED
#                                                       fuzzy and hit "next",
#                                                       i.e. SKIPPED THE TRACK)
#   "am i free right now"  -> "am i free right"
# Deliberately generous. The first version demanded "my" and one exact verb
# form, so real speech went straight past it into the FIXED fuzzy table:
#   "what are the next class"  -> matched the media key "next" -> SKIPPED A TRACK
#   "what is the next class"   -> same risk
# A wrong answer here is worse than a missed one, so accept the whole family.
_Q = r"(?:what|when|which)\s*(?:s|is|are|was)?\s+"
_MY = r"(?:my|the|our|todays?|today\s+s)\s+"


@_pattern(rf"^(?:{_Q})?(?:{_MY})?next\s+(?:class|lecture|subject|period)$"
          rf"|^(?:{_Q})?(?:{_MY})?next\s+(?:class|lecture)\s+today$"
          rf"|^(?:{_Q})?(?:{_MY})?(?:upcoming|following)\s+(?:class|lecture)$")
def _next_class(m) -> Optional[CommandResult]:
    from system import timetable
    return CommandResult(timetable.describe_next(), canned=False,
                         command="timetable_next")


@_pattern(r"^(?:am\s+i\s+|i\s*m\s+)?free(?:\s+right)?(?:\s+now)?"
          r"(?:\s+at\s+the\s+moment)?$"
          r"|^do\s+i\s+have\s+(?:a\s+)?class(?:\s+right)?(?:\s+now)?$")
def _free_now(m) -> Optional[CommandResult]:
    from system import timetable
    return CommandResult(timetable.describe_free(), canned=False,
                         command="timetable_free")


# "what are the classes on Tuesday" / "...for tomorrow" / "what do I have
# Friday". Without the day group these went to the LLM, which answered
# "my knowledge does not extend to current class schedules, sir".
_DAYS_RE = (r"today|tomorrow|tomorow|monday|tuesday|tues|wednesday|wed|"
            r"thursday|thurs|friday|fri|saturday|sat|sunday|sun")


# Python forbids reusing a group name across alternatives, so each branch gets
# its own and the handler takes whichever matched.
@_pattern(rf"^(?:{_Q})?(?:{_MY})?(?:classes|schedule|timetable|lectures|periods)"
          rf"(?:\s+(?:for|on|this)?\s*(?P<day_a>{_DAYS_RE}))?$"
          rf"|^what\s+do\s+i\s+have\s+(?:on\s+|for\s+)?(?P<day_b>{_DAYS_RE})$"
          rf"|^(?:{_Q})?(?:{_MY})?(?P<day_c>{_DAYS_RE})\s+"
          rf"(?:classes|schedule|timetable)$")
def _classes_today(m) -> Optional[CommandResult]:
    from system import timetable
    g = m.groupdict()
    word = (g.get("day_a") or g.get("day_b") or g.get("day_c") or "").strip()
    day = timetable.resolve_day(word) if word else None
    return CommandResult(timetable.describe_day(day), canned=False,
                         command="timetable_today")


@_pattern(r"^reload (?:my )?timetable$|^refresh (?:my )?(?:timetable|schedule)$")
def _reload_timetable(m) -> Optional[CommandResult]:
    from system import timetable
    timetable.load(force=True)
    n = sum(len(v) for v in timetable.load().values())
    return CommandResult(f"Timetable reloaded, sir — {n} classes.", canned=False,
                         command="timetable_reload")


# ── maps / routes ───────────────────────────────────────────────
# Registered BEFORE _open, deliberately. "open maps and search for the shortest
# route between X and Y" starts with "open", so the file/app opener claimed it
# first and answered "Couldn't find that, sir" — it was hunting for a FILE named
# "maps and search for the shortest route...". Anything naming a map or a route
# is a maps request, not a filename, so it has to be decided here.
_ROUTE_WORDS = r"(?:route|directions?|way|path|distance|how (?:do|to) (?:i )?get)"


# The NATIVE Windows Maps app, only when explicitly asked for. "maps" alone is
# indexed as Microsoft.WindowsMaps, so a bare "open maps" used to launch that app
# instead of Google Maps in the browser. This rule is registered FIRST so an
# explicit request still reaches the app; everything else falls to _maps_route.
@_pattern(r"^(?:open|launch|start)\s+(?:the\s+)?(?:windows\s+maps|maps\s+app|"
          r"windows\s+maps\s+app)$")
def _open_native_maps(m) -> Optional[CommandResult]:
    from system import apps
    status, line, _extra = apps.open_target("maps")
    return CommandResult(line, canned=False,
                         command="open" if status == "opened" else "open_failed")


@_pattern(rf"^(?:open|show|pull up|bring up|check|find|search|look up|get|give me)"
          rf"(?:\s+(?:me|for|up))?\s+(?:the\s+|a\s+|my\s+)?"
          rf"(?:(?:shortest|fastest|quickest|best|google)\s+)*"
          rf"(?:maps?|{_ROUTE_WORDS})"
          rf"(?:\s+and\s+(?:search|look)(?:\s+for)?)?"
          rf"(?:\s+(?:for\s+|to\s+|of\s+)?(?P<rest>.+))?$"
          rf"|^(?P<rest2>.+?)\s+(?:route|directions?)\s+(?:in|on)\s+(?:google\s+)?maps?$")
def _maps_route(m) -> Optional[CommandResult]:
    """'open maps and search for the shortest route between X and Y'.

    Parses an origin/destination out of the phrasing people actually use, then
    hands it to the real open_maps tool rather than constructing a URL here.
    """
    from router import tools
    raw = (m.groupdict().get("rest") or m.groupdict().get("rest2") or "").strip()
    # Strip leading filler REPEATEDLY — one pass left "route between ballari" as
    # the origin for "open maps and search for shortest route between X and Y".
    raw = re.sub(r"\s+(?:in|on)\s+(?:google\s+)?maps?\s*$", "", raw).strip()
    while True:
        stripped = re.sub(r"^(?:shortest|fastest|quickest|best|optimi[sz]ed|the|a|"
                          r"an|route|routes|directions?|way|path|distance|for|to|"
                          r"of|in|between|from|and|search|google|maps?)\b\s*",
                          "", raw, flags=re.I).strip()
        if stripped == raw:
            break
        raw = stripped
    if not raw:
        # "open maps" / "open google maps" — no destination, so open Google Maps
        # itself. Returning None here made it fall through to _search_web, which
        # answered "Searching for maps."
        if not tools.open_url("https://www.google.com/maps").get("opened"):
            return CommandResult("I couldn't open the browser, sir.", canned=False,
                                 command="maps_failed")
        return CommandResult("Google Maps, sir.", canned=False, command="open_maps")
    origin = dest = None
    bt = re.match(r"^(?:between\s+)?(?P<a>.+?)\s+(?:and|to|-|until)\s+(?P<b>.+)$",
                  raw, re.I)
    if bt:
        origin, dest = bt["a"].strip(), bt["b"].strip()
    else:
        dest = raw
    res = tools.open_maps(dest, origin)
    if not res.get("opened"):
        return CommandResult(res.get("say", "I couldn't open maps, sir."),
                             canned=False, command="maps_failed")
    return CommandResult(res.get("say", "Maps is on screen, sir."), canned=False,
                         command="open_maps")


# ── voice recording ─────────────────────────────────────────────
# On the fast path deliberately. Sent to the LLM instead, "turn on the recorder"
# made it invent a PowerShell cmdlet (Get-AudioRecorderState), get rc=1, then hit
# a Groq 429 and blow past SMART_TIMEOUT — the user just heard "Couldn't pin that
# down, sir". This is a deterministic local action; it has no business costing an
# LLM round-trip.
@_pattern(r"^(?:can you |please )?(?:start|begin|turn on|switch on|open)\s+"
          r"(?:the\s+|a\s+|my\s+)?(?:sound\s+|voice\s+|audio\s+)?"
          r"record(?:ing|er)?(?:\s+app)?$")
def _start_recording(m) -> Optional[CommandResult]:
    from system import recorder
    ok, line = recorder.start()
    return CommandResult(line, canned=False,
                         command="record_start" if ok else "record_failed")


@_pattern(r"^(?:can you |please )?(?:stop|end|finish|turn off|switch off)\s+"
          r"(?:the\s+|my\s+)?(?:sound\s+|voice\s+|audio\s+)?"
          r"record(?:ing|er)?$")
def _stop_recording(m) -> Optional[CommandResult]:
    from system import recorder
    ok, line = recorder.stop()
    return CommandResult(line, canned=False,
                         command="record_stop" if ok else "record_failed")


# Search requests must NOT be treated as "open a file called ...". This exact
# phrasing ("open brave and search for X") used to hit the open-a-file path,
# fail, and never reach the web-search tool.
_BROWSERS = r"(?:brave|chrome|firefox|edge|browser)"


@_pattern(rf"^(?:open |launch )?{_BROWSERS}?\s*(?:and\s+)?"
          r"(?:search|google|look up|find)\s+(?:for\s+|about\s+)?(?P<q>.+)$")
def _search_web(m) -> Optional[CommandResult]:
    from router import tools
    text = m.string
    browser = next((b for b in ("brave", "chrome", "firefox", "edge")
                    if b in text), None)
    engine = "youtube" if "youtube" in text else "google"
    result = tools.web_search(m["q"].strip(), engine=engine, browser=browser)
    if not result.get("opened"):
        return CommandResult("I couldn't open the browser, sir.", canned=False,
                             command="search_failed")
    return CommandResult(f"Searching for {m['q'].strip()}.", canned=False,
                         command="web_search")


PESU_URL = "https://www.pesuacademy.com/Academy/s/studentProfilePESU"

# Well-known sites, so "open Instagram in Brave" navigates to instagram.com
# rather than opening a blank browser. Anything not listed becomes a real web
# search (also never blank).
_SITE_URLS = {
    "instagram": "https://www.instagram.com", "youtube": "https://www.youtube.com",
    "twitter": "https://twitter.com", "x": "https://x.com",
    "facebook": "https://www.facebook.com", "reddit": "https://www.reddit.com",
    "github": "https://github.com", "gmail": "https://mail.google.com",
    "google": "https://www.google.com", "maps": "https://maps.google.com",
    "google maps": "https://maps.google.com", "whatsapp": "https://web.whatsapp.com",
    "whatsapp web": "https://web.whatsapp.com", "netflix": "https://www.netflix.com",
    "amazon": "https://www.amazon.com", "linkedin": "https://www.linkedin.com",
    "spotify": "https://open.spotify.com", "chatgpt": "https://chat.openai.com",
    "chat gpt": "https://chat.openai.com", "claude": "https://claude.ai",
    "wikipedia": "https://www.wikipedia.org", "twitch": "https://www.twitch.tv",
    "discord": "https://discord.com/app", "outlook": "https://outlook.live.com",
    "prime video": "https://www.primevideo.com", "hotstar": "https://www.hotstar.com",
    "disney plus": "https://www.disneyplus.com",
    # PES University portal. Deep-linked to the student profile page rather than
    # the site root, which is where the login lands anyway.
    "pesu": PESU_URL, "pes": PESU_URL, "pesu academy": PESU_URL,
    "pes university": PESU_URL, "pes academy": PESU_URL,
    "pesu portal": PESU_URL, "pes portal": PESU_URL,
}


# PESU gets its own rule, ahead of the generic one, because it must work WITHOUT
# naming a browser ("open pes website") and must always land in Brave. The
# generic _open_site_in_browser below only fires when a browser is named, and
# without this "open pes website" fell through to the open-a-FILE path and
# failed. Registered first, so it wins.
@_pattern(r"^(?:open|launch|go to|pull up|navigate to|bring up|show me)\s+"
          r"(?:the\s+|my\s+)?pes(?:u)?(?:\s+(?:academy|university|portal|website|"
          r"site|page|profile))?(?:\s+(?:in|on|using|with)\s+(?:the\s+)?"
          r"(?:brave|chrome|firefox|edge))?$")
def _open_pesu(m) -> Optional[CommandResult]:
    from router import tools
    text = m.string
    # Brave by default; honour another browser only if explicitly asked for.
    browser = next((b for b in ("chrome", "firefox", "edge") if b in text), "brave")
    if not tools.open_url(PESU_URL, browser=browser).get("opened"):
        return CommandResult("I couldn't open the browser, sir.", canned=False,
                             command="open_url_failed")
    return CommandResult("PESU Academy, sir.", canned=False, command="open_url")


@_pattern(r"^(?:open|launch|go to|pull up|navigate to|bring up) (?P<site>.+?) "
          r"(?:in|on|using|with) (?:the )?(?P<browser>brave|chrome|firefox|edge|opera|browser)$")
def _open_site_in_browser(m) -> Optional[CommandResult]:
    """'Open Instagram in Brave' -> actually open the browser AND navigate to the
    site (or search for it), never a blank window."""
    from router import tools
    raw_site = m["site"].strip()
    key = textnorm.normalize(raw_site)
    browser = None if m["browser"] == "browser" else m["browser"]
    url = _SITE_URLS.get(key)
    if url is None and " " not in key and re.fullmatch(r"[a-z0-9]{2,}", key):
        url = f"https://www.{key}.com"                 # single word -> its .com
    if url is not None:
        if not tools.open_url(url, browser=browser).get("opened"):
            return CommandResult("I couldn't open the browser, sir.", canned=False,
                                 command="open_url_failed")
        return CommandResult(f"Opening {raw_site} in {m['browser']}.", canned=False,
                             command="open_url")
    # Unknown multi-word target -> a real search rather than a guessed domain.
    if not tools.web_search(raw_site, browser=browser).get("opened"):
        return CommandResult("I couldn't open the browser, sir.", canned=False,
                             command="search_failed")
    return CommandResult(f"Searching for {raw_site}.", canned=False, command="web_search")


# ── Reminders / notes / snooze (local, instant) ─────────────────
# "remind me what/who/when my ... is" is a RECALL question, not a reminder-to-do
# — send it to the smart path (unless it also names a time, e.g. "remind me about
# the call at 3pm").
_REMIND_QUESTION = re.compile(
    r"^(?:hey\s+)?(?:jarvis[,\s]*)?remind me\s+"
    r"(?:what|who|whom|when|where|why|how|which|of|about)\b", re.I)


@_pattern(r"^(?:hey\s+)?(?:jarvis[,\s]*)?remind me\b.+$")
def _remind(m) -> Optional[CommandResult]:
    parsed = reminders.parse(m.string)
    if parsed is None:
        if _REMIND_QUESTION.match(m.string):
            return None                 # a recall question -> let the LLM answer
        return CommandResult("When would you like me to remind you, sir?",
                             canned=False, command="reminder_no_time")
    task, at_ts = parsed
    reminders.add(task, at_ts)
    return CommandResult(f"I'll remind you to {task} {reminders.spoken_when(at_ts)}, sir.",
                         canned=False, command="reminder_set")


@_pattern(r"^(?:note to self|make a note|take a note)[,:\s]+(?P<body>.+)$")
def _note(m) -> CommandResult:
    return CommandResult(notes.add(m["body"]), canned=False, command="note")


@_pattern(r"^remember that (?P<body>.+)$")
def _remember(m) -> CommandResult:
    return CommandResult(notes.add(m["body"]), canned=False, command="note")


@_pattern(r"^(?:stop listening|don'?t disturb me|do not disturb me|leave me alone|"
          r"go quiet|snooze)\s*(?:for\s+)?(?P<dur>.+?)?$")
def _snooze(m) -> Optional[CommandResult]:
    dur = (m["dur"] or "").strip()
    if not dur:
        minutes = 30                       # a sensible default
    elif re.search(r"\bhour\b", dur, re.I):
        n = textnorm.parse_number(dur) or 1
        minutes = n * 60
    else:
        minutes = textnorm.parse_number(dur) or 15
    minutes = max(1, min(minutes, 480))
    return CommandResult("", canned=False, command="snooze", data={"minutes": minutes})


@_pattern(rf"^(?:open|launch|start) (?P<target>.+)$")
def _open(m) -> Optional[CommandResult]:
    """Open an app/file/folder. We pass the FULL target (including any 'folder'
    /'drive' words) so open_target can tell folder/drive intent. Three-tier:
    open on a confident match, ASK 'did you mean?' on a plausible one, fail
    honestly on nothing — and only ever claim success when the OS call did."""
    status, reply, extra = apps.open_target(m["target"])
    if status == "ask":
        return CommandResult(reply, canned=False, command="open_maybe", data=extra)
    return CommandResult(reply, canned=False,
                         command="open" if status == "opened" else "open_failed")


# Set the volume to a specific level, in any natural phrasing. The number may be
# spoken ('seventy five') — textnorm.parse_number turns it into 75. This MUST
# stay on the fast path: "set the volume to 40" used to fall through to the smart
# path, think for ages, and time out.
_VOL_NUM = r"(?P<n>[a-z0-9][a-z0-9\s]*?)"      # a digit or spoken-number phrase


@_pattern(r"^(?:set|change|put|adjust|make|increase|raise|bump|boost|decrease|"
          r"lower|drop|crank)\s+(?:it\s+|the\s+)?volume\s+(?:level\s+)?"
          rf"(?:up\s+|down\s+)?(?:to|at)\s+{_VOL_NUM}(?:\s*(?:percent|%))?$")
def _setvol_verb(m) -> Optional[CommandResult]:
    return _do_setvol(m["n"])


@_pattern(rf"^(?:the\s+)?volume\s+(?:level\s+)?(?:to|at)\s+{_VOL_NUM}"
          r"(?:\s*(?:percent|%))?$")
def _setvol_bare(m) -> Optional[CommandResult]:
    return _do_setvol(m["n"])


@_pattern(r"^turn\s+(?:it|up|down|the\s+volume)\s+(?:up\s+|down\s+)?(?:to|at)\s+"
          rf"{_VOL_NUM}(?:\s*(?:percent|%))?$")
def _setvol_turn(m) -> Optional[CommandResult]:
    return _do_setvol(m["n"])


def _do_setvol(spoken_n: str) -> Optional[CommandResult]:
    n = textnorm.parse_number(spoken_n)
    if n is None:
        return None                    # no number heard — let it fall through
    n = max(0, min(100, n))
    return CommandResult(volume.set_volume(n), canned=False, command="volume")


@_pattern(r"^(?:set (?:the )?brightness to|brightness) (?P<n>\d{1,3})(?:\s*(?:percent|%))?$")
def _setbright(m) -> CommandResult:
    return CommandResult(volume.set_brightness(int(m["n"])), canned=False, command="brightness")


@_pattern(r"^brightness (?P<dir>up|down)$")
def _bright(m) -> CommandResult:
    delta = 15 if m["dir"] == "up" else -15
    return CommandResult(volume.brightness_step(delta), canned=False, command="brightness")


# Two conflicting control intents in one breath ("shut down AND don't disturb me
# for two minutes") — sleep vs timed-snooze. Don't try to do both (it cycles
# asleep->awake->muted); ask which one is meant.
_SLEEP_MARKER = re.compile(
    r"\b(go to sleep|shut ?down|power down|go offline|goodnight|good night|"
    r"turn yourself off|shut yourself down)\b", re.I)
_SNOOZE_MARKER = re.compile(
    r"\b(don'?t disturb|do not disturb|stop listening|go quiet|leave me alone|"
    r"snooze)\b", re.I)


# ── Entry point ─────────────────────────────────────────────────
def try_match(raw_text: str) -> Optional[CommandResult]:
    """Return a CommandResult if this is a known command, else None."""
    text = normalize(raw_text)
    if not text:
        return None

    # Compound, conflicting control phrase -> ask instead of doing both.
    if _SLEEP_MARKER.search(text) and _SNOOZE_MARKER.search(text):
        return CommandResult("Did you want me to sleep, sir, or just go quiet for "
                             "a bit?", canned=False, command="clarify")

    for rx, handler in _PATTERNS:
        m = rx.match(text)
        if m:
            result = handler(m)
            if result is not None:
                log.info("fast path (pattern): %r -> %s", text, result.command)
                return result

    # LENGTH GUARD. WRatio does partial matching, so a SHORT command key scores
    # high against any longer sentence containing it — "what are the next class"
    # matched the one-word key "next" and SKIPPED THE MUSIC TRACK. A fixed
    # command is a whole utterance, not a word buried in a question, so the
    # spoken text must be about the same length as the phrase it matched.
    # (Same failure the routines matcher already guards against, where "I heard
    # some theory about the creation of pie" matched "shut up and turn it off".)
    spoken_words = len(text.split())
    candidates = process.extract(text, FIXED.keys(), scorer=fuzz.WRatio,
                                 score_cutoff=FUZZ_THRESHOLD, limit=8)
    match = None
    for cand, score, _ in candidates:
        n = len(cand.split())
        if spoken_words > n + 2 and spoken_words > n * 1.6:
            log.debug("fixed %r rejected for %r (%d words vs %d)",
                      cand, text, spoken_words, n)
            continue
        match = (cand, score)
        break
    if match:
        phrase = match[0]
        fn = FIXED[phrase]
        reply = fn()
        if reply is None:
            return None
        if phrase in _BRIEFING_PHRASES:
            command = "briefing"
        elif phrase in _STATUS_PHRASES:
            command = "status_report"
        elif phrase in _SHUTDOWN_PC_PHRASES:
            command = "shutdown_pc"
        elif phrase in _SLEEP_PHRASES:
            command = "sleep"
        elif phrase in _REPEAT_PHRASES:
            command = "repeat"
        elif phrase in _END_PHRASES:
            command = "end_conversation"
        else:
            command = phrase
        log.info("fast path (fuzzy %d): %r -> %r [%s]", match[1], text, phrase, command)
        if command in ("briefing", "status_report", "sleep", "shutdown_pc", "repeat"):
            return CommandResult("", canned=False, command=command)
        return CommandResult(reply, canned=fn not in _DYNAMIC, command=command)

    return None
