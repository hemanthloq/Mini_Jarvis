"""Tool-calling: the functions JARVIS can invoke on its own.

Groq speaks OpenAI-style tool calls, so these are exposed as JSON schemas and
dispatched here. Every call is logged (command + result) to data/jarvis.log.

SAFETY: anything destructive or hard to undo (delete, overwrite an existing
file, kill a process, uninstall, format, move a file out of its folder) does NOT
execute immediately. It returns a PendingConfirmation, which the orchestrator
speaks aloud ("I'm about to X — confirm?") and only runs after a verbal yes.
Non-destructive calls run straight away.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import urllib.parse
import webbrowser
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

import config
from system import apps, health

log = logging.getLogger("jarvis.tools")

MAX_OUTPUT = 4000          # chars of shell output handed back to the model
SHELL_TIMEOUT = 25


@dataclass
class PendingConfirmation:
    """A destructive call held back until the user says yes."""
    tool: str
    args: dict
    description: str       # spoken aloud, e.g. "delete report.pdf from Downloads"


# ── Catastrophic commands the LLM must NEVER be able to run ──────
# Powering off / rebooting / logging off the whole machine is irreversible and
# loses unsaved work in every app. The model tried to run `shutdown /h /f` from
# a casual "shut down" (meant for JARVIS, not the PC). These are HARD-BLOCKED at
# the tool boundary: no confirmation can make run_shell_command execute them.
# The only legitimate OS-shutdown path is shutdown_computer(), reached solely by
# an explicit "shutdown my pc"-class phrase in the fast path.
_OS_POWER_CMD = re.compile(
    r"\bshutdown\b|\brestart-computer\b|\bstop-computer\b|"
    r"\blogoff\b|\bshutdown\.exe\b|\brundll32.*user\.exe.*exitwindows|"
    r"\bstart-sleep\b.*-\bshutdown\b",
    re.I,
)

# ── Destructive-command detection (needs spoken confirmation) ────
_DESTRUCTIVE_CMD = re.compile(
    r"\b(rm|rmdir|del|erase|format|mkfs|fdisk|diskpart|"
    r"remove-item|clear-content|clear-item|"
    r"taskkill|kill|stop-process|stop-service|"
    r"uninstall|msiexec|reg\s+delete|rd)\b"
    r"|>\s*[^>|\s]"                       # single '>' truncates a file
    r"|\bmove\b|\bmv\b|\bmove-item\b|\bren\b|\brename-item\b",
    re.I,
)


# FILE-destructive shell verbs specifically. These must NOT run as free-text
# shell — the model was constructing hallucinated paths (C:\Peso\...). They are
# redirected to delete_path / move_path, which resolve the REAL file through the
# same matcher used for opening, verify it exists, and confirm before acting.
_FILE_DESTRUCTIVE_CMD = re.compile(
    r"\b(rm|del|erase|remove-item|rd|rmdir|move|mv|move-item|"
    r"ren|rename|rename-item|copy-item|xcopy|robocopy)\b", re.I)


def _is_destructive_command(cmd: str) -> bool:
    return bool(_DESTRUCTIVE_CMD.search(cmd))


def _is_file_destructive_command(cmd: str) -> bool:
    return bool(_FILE_DESTRUCTIVE_CMD.search(cmd))


def _is_os_power_command(cmd: str) -> bool:
    return bool(_OS_POWER_CMD.search(cmd))


# ── Tool implementations ────────────────────────────────────────
def run_shell_command(command: str, _confirmed: bool = False) -> Any:
    # HARD BLOCK: the model cannot power off / reboot / log off the machine,
    # confirmed or not. If it wanted "JARVIS off", that's sleep, not this.
    if _is_os_power_command(command):
        log.error("BLOCKED OS power command from run_shell_command: %r", command)
        return {"error": "Shutting down, restarting, or logging off the computer is "
                         "not permitted through this tool. If the user wants to stop "
                         "JARVIS, that is sleep mode, which happens elsewhere — do not "
                         "attempt it here.",
                "blocked": True}
    # Deleting / moving / renaming files must go through delete_path / move_path,
    # which resolve the REAL file — never a shell command with a model-built path.
    if _is_file_destructive_command(command):
        log.error("BLOCKED file-destructive shell command %r — must use delete_path/"
                  "move_path", command)
        return {"error": "Do NOT delete, move, rename, or copy files with a shell "
                         "command — you would be guessing the path. Use the delete_path "
                         "or move_path tool with the file's spoken NAME (e.g. 'la unit "
                         "four in the pesu folder'); it resolves the real file, checks "
                         "it exists, and confirms before acting.",
                "blocked": True}
    if _is_destructive_command(command) and not _confirmed:
        return PendingConfirmation(
            "run_shell_command", {"command": command},
            f"run the command: {command}")
    log.info("TOOL run_shell_command: %s", command)
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            capture_output=True, text=True, timeout=SHELL_TIMEOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        out = (proc.stdout or "") + (("\n[stderr] " + proc.stderr) if proc.stderr else "")
        out = out.strip()[:MAX_OUTPUT] or "(no output)"
        log.info("TOOL run_shell_command -> rc=%s, %d chars", proc.returncode, len(out))
        return {"exit_code": proc.returncode, "output": out}
    except subprocess.TimeoutExpired:
        log.warning("TOOL run_shell_command timed out: %s", command)
        return {"error": f"timed out after {SHELL_TIMEOUT}s"}
    except OSError as e:
        return {"error": str(e)}


def open_path(path: str, _confirmed: bool = False) -> Any:
    """Open a file/folder/app. Accepts a real path OR a spoken name to resolve.

    Returns {"opened": ...} ONLY when the OS actually opened a real target. On
    failure it returns an explicit error so the model cannot claim success —
    the system prompt forbids reporting actions it did not perform.
    """
    log.info("TOOL open_path: %s", path)
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if p.exists():
        try:
            os.startfile(str(p))
        except OSError as e:
            log.error("TOOL open_path FAILED %s: %s", p, e)
            return {"error": f"could not open {p}: {e}", "opened": False}
        log.info("TOOL open_path OPENED %s", p)
        return {"opened": str(p)}

    status, line, _extra = apps.open_target(path)   # fall back to the alias index
    if status == "opened":
        return {"opened": True, "detail": line}
    if status == "ask":                              # plausible — let the model ask
        log.info("TOOL open_path ASK for %r (%s)", path, line)
        return {"opened": False, "say": line}
    log.info("TOOL open_path NOT OPENED for %r (%s)", path, line)
    return {"error": f"nothing opened — no confident match for {path!r}",
            "opened": False, "say": apps.NOT_FOUND}


def list_directory(path: str, _confirmed: bool = False) -> Any:
    log.info("TOOL list_directory: %s", path)
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_dir():
        hit = apps.resolve(path)
        if hit and Path(hit[2]).is_dir():
            p = Path(hit[2])
        else:
            return {"error": f"not a directory: {path}"}
    try:
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
        items = [("[dir] " if e.is_dir() else "") + e.name for e in entries[:120]]
        return {"path": str(p), "count": len(entries), "items": items}
    except OSError as e:
        return {"error": str(e)}


def read_file(path: str, _confirmed: bool = False) -> Any:
    log.info("TOOL read_file: %s", path)
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.is_file():
        hit = apps.resolve(path)
        if hit and Path(hit[2]).is_file():
            p = Path(hit[2])
        else:
            return {"error": f"file not found: {path}"}
    try:
        text = p.read_text(encoding="utf-8", errors="replace")[:MAX_OUTPUT]
        return {"path": str(p), "content": text}
    except OSError as e:
        return {"error": str(e)}


def write_file(path: str, content: str, _confirmed: bool = False) -> Any:
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if p.exists() and not _confirmed:        # overwriting is destructive
        return PendingConfirmation(
            "write_file", {"path": str(p), "content": content},
            f"overwrite the existing file {p.name}")
    log.info("TOOL write_file: %s (%d chars)", p, len(content))
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"written": str(p), "bytes": len(content)}
    except OSError as e:
        return {"error": str(e)}


# ── Destructive FILE ops — resolved through the REAL matcher ─────
def _resolve_for_action(target: str) -> "Path | None":
    """A real, existing path for a destructive op. Accepts an already-resolved
    absolute path (the confirm step passes one back) or a spoken description,
    which is resolved STRICTLY (high confidence + must exist) — never guessed."""
    if not target:
        return None
    direct = Path(os.path.expandvars(os.path.expanduser(target)))
    if direct.is_absolute() and direct.exists():
        return direct
    return apps.resolve_target_strict(target)


def delete_path(target: str, _confirmed: bool = False) -> Any:
    """Delete a FILE (to the Recycle Bin). The target is resolved to a real file
    through the matcher and verified to EXIST before any confirmation is shown —
    a path is never constructed or guessed. Reports success only if it truly went."""
    log.info("TOOL delete_path: %s", target)
    p = _resolve_for_action(target)
    if p is None:
        return {"error": f"no confident, existing match for {target!r}",
                "opened": False, "say": apps.NOT_FOUND}
    if p.is_dir():
        return {"error": "refusing to delete a folder via voice",
                "say": f"That's a folder, sir — I'll leave removing {p.name} to you."}
    if not _confirmed:                       # verified to exist -> safe to confirm
        return PendingConfirmation(
            "delete_path", {"target": str(p)},
            f"delete {p.name} from {p.parent.name}")
    try:
        from send2trash import send2trash
        send2trash(str(p))
    except Exception as e:
        log.error("delete_path failed for %s: %s", p, e)
        return {"error": str(e), "say": f"I couldn't delete {p.name}, sir."}
    if p.exists():                           # NEVER report success unless it's gone
        log.error("delete_path: %s still exists after delete", p)
        return {"error": "file still present", "say": f"I couldn't remove {p.name}, sir."}
    log.warning("DELETED to recycle bin: %s", p)
    return {"deleted": str(p), "say": f"Moved {p.name} to the recycle bin, sir."}


def move_path(target: str, destination: str, _confirmed: bool = False) -> Any:
    """Move a real, resolved FILE into a real, resolved destination FOLDER. Both
    ends are resolved and verified before confirming; success is reported only if
    the file actually arrived."""
    log.info("TOOL move_path: %s -> %s", target, destination)
    p = _resolve_for_action(target)
    if p is None:
        return {"error": f"no existing match for {target!r}", "say": apps.NOT_FOUND}
    dest = _resolve_for_action(destination)
    if dest is None or not dest.is_dir():
        return {"error": f"no destination folder for {destination!r}",
                "say": f"I couldn't find the {destination} folder, sir."}
    if not _confirmed:
        return PendingConfirmation(
            "move_path", {"target": str(p), "destination": str(dest)},
            f"move {p.name} into {dest.name}")
    target_path = dest / p.name
    try:
        shutil.move(str(p), str(target_path))
    except Exception as e:
        log.error("move_path failed: %s", e)
        return {"error": str(e), "say": f"I couldn't move {p.name}, sir."}
    if not target_path.exists():
        return {"error": "move unconfirmed", "say": f"I couldn't move {p.name}, sir."}
    log.warning("MOVED %s -> %s", p, target_path)
    return {"moved": str(target_path), "say": f"Moved {p.name} into {dest.name}, sir."}


def get_system_health(_confirmed: bool = False) -> Any:
    return health.get_system_health()        # real measured values only


def get_top_processes(by: str = "memory", limit: int = 5,
                      _confirmed: bool = False) -> Any:
    return health.get_top_processes(by=by, limit=limit)   # aggregated by app


def shutdown_computer() -> dict:
    """Actually power off the PC. Reached ONLY from the explicit 'shutdown my pc'
    fast-path phrase, AFTER a spoken yes — never from the LLM or a shell command."""
    log.warning("SHUTDOWN COMPUTER — user explicitly confirmed a full OS shutdown")
    try:
        subprocess.Popen(["shutdown", "/s", "/t", "5"],   # 5s grace, no force flag
                         creationflags=subprocess.CREATE_NO_WINDOW)
        return {"ok": True}
    except OSError as e:
        log.error("shutdown failed: %s", e)
        return {"ok": False, "error": str(e)}


# ── Browser / web search ────────────────────────────────────────
# Without these the model invents tool calls for a capability it doesn't have
# ("<brave_search={...}>") and that markup ends up spoken aloud.
_BRAVE_CANDIDATES = [
    Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    / r"BraveSoftware\Brave-Browser\Application\brave.exe",
    Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    / r"BraveSoftware\Brave-Browser\Application\brave.exe",
    Path(os.environ.get("LOCALAPPDATA", "")) /
    r"BraveSoftware\Brave-Browser\Application\brave.exe",
]

SEARCH_ENGINES = {
    "google": "https://www.google.com/search?q={q}",
    "brave": "https://search.brave.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "youtube": "https://www.youtube.com/results?search_query={q}",
}


def _brave_path() -> str | None:
    for p in _BRAVE_CANDIDATES:
        if p and p.exists():
            return str(p)
    return None


def _open_in_browser(url: str, browser: str | None = None) -> dict:
    """Open a URL. Uses Brave when asked for (and installed), else the default."""
    want_brave = (browser or "").lower().startswith("brave")
    if want_brave:
        exe = _brave_path()
        if exe:
            try:
                subprocess.Popen([exe, url], creationflags=subprocess.CREATE_NO_WINDOW)
                log.info("TOOL opened in Brave: %s", url)
                return {"opened": url, "browser": "brave"}
            except OSError as e:
                log.warning("Brave launch failed (%s) — using the default browser", e)
        else:
            log.info("Brave not installed — using the default browser")
    try:
        webbrowser.open(url)
        log.info("TOOL opened in default browser: %s", url)
        return {"opened": url, "browser": "default"}
    except Exception as e:
        log.error("TOOL browser open failed: %s", e)
        return {"error": str(e), "opened": False}


def open_url(url: str, browser: str | None = None, _confirmed: bool = False) -> Any:
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url.lstrip("/")
    log.info("TOOL open_url: %s (browser=%s)", url, browser)
    return _open_in_browser(url, browser)


def cricket_scores(team: str | None = None, _confirmed: bool = False) -> Any:
    """Live cricket scores from ESPNcricinfo's public RSS feed.

    Source chosen after probing the alternatives: cricapi and the RapidAPI
    cricket feeds all require a key (401 without one), and Cricbuzz's internal
    JSON endpoint 404s. Cricinfo's static livescores RSS answers keyless with
    real scorelines ("England U19 320/10 v South Africa U19 460/10 & 122/6 *").

    HONEST LIMITS, deliberately surfaced rather than papered over:
      * LIVE matches only — nothing finished, nothing upcoming.
      * It is an undocumented static endpoint; if it changes shape this returns
        an explicit error instead of guessing.
      * The scoreline is a display string, not structured fields, so it is
        passed through as-is rather than parsed into runs/wickets.
    When it has nothing, say so — do NOT fall back to inventing a score.
    """
    log.info("TOOL cricket_scores: team=%r", team)
    try:
        r = httpx.get("https://static.cricinfo.com/rss/livescores.xml", timeout=10,
                      headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log.error("cricket_scores failed: %s: %s", type(e).__name__, e)
        return {"ran": False,
                "error": f"the cricket feed is unreachable ({type(e).__name__})",
                "say": "I couldn't reach the cricket feed, sir."}

    matches = [t.strip() for it in root.findall(".//item")
               if (t := it.findtext("title")) and t.strip()]
    if team:
        want = team.strip().lower()
        filtered = [m for m in matches if want in m.lower()]
        if filtered:
            matches = filtered
        else:
            return {"ran": True, "matches": [], "asked_about": team,
                    "note": f"No LIVE match involving {team}. Do not invent one."}
    log.info("TOOL cricket_scores -> %d live match(es)", len(matches))
    return {"ran": True, "matches": matches,
            "note": "These are the only matches live right now. If the list is "
                    "empty, say there's no cricket on — never invent a score."}


def open_maps(destination: str, origin: str | None = None,
              _confirmed: bool = False) -> Any:
    """Open Google Maps — a place, or directions between two places.

    Exists because the model kept SAYING "I'll open Google Maps for you, sir"
    on turns where it had no tool that could do anything of the kind. Given a
    real one, the promise becomes an action instead of a fabrication.

    Reuses the same verified browser launcher as open_url, so it honours the
    user's Brave install rather than constructing an executable path.
    """
    dest = (destination or "").strip()
    if not dest:
        return {"opened": False, "error": "no destination",
                "say": "Where to, sir?"}
    # Place names arrive lower-cased from normalize(); say them back properly.
    say_dest = dest if dest[:1].isupper() else dest.title()
    if origin and origin.strip():
        o = origin.strip()
        url = ("https://www.google.com/maps/dir/?api=1"
               f"&origin={urllib.parse.quote_plus(o)}"
               f"&destination={urllib.parse.quote_plus(dest)}")
        spoken = f"Route from {o if o[:1].isupper() else o.title()} to {say_dest}"
    else:
        url = ("https://www.google.com/maps/search/?api=1"
               f"&query={urllib.parse.quote_plus(dest)}")
        spoken = say_dest
    log.info("TOOL open_maps: %r (origin=%r)", dest, origin)
    result = _open_in_browser(url)
    if not result.get("opened"):
        return {**result, "say": "I couldn't open the browser, sir."}
    return {**result, "say": f"{spoken} is on screen, sir."}


def look_up(query: str, max_results: int | str = 5, _confirmed: bool = False) -> Any:
    """Search the web and RETURN the results as text, so the model can answer
    from real current information instead of its training data.

    This is the missing half of `web_search`, which only opens a browser window
    and hands back {"opened": url} — nothing the model can read. Asked "what
    movie is Radhika Theatre playing in Ballari", the model therefore had no way
    to find out, answered from memory, misread the cinema as a person's name,
    and said it "couldn't find any information" as though it had looked.

    Keyless (DuckDuckGo) so it needs no extra API key or billing. Network
    failures return an explicit error rather than an empty list — the caller
    must be able to tell "nothing found" apart from "the lookup didn't run".
    """
    log.info("TOOL look_up: %r", query)
    try:
        try:
            from ddgs import DDGS
        except ImportError:                      # older package name
            from duckduckgo_search import DDGS   # type: ignore
    except ImportError:
        log.error("look_up unavailable — ddgs is not installed")
        return {"error": "web lookup is unavailable (ddgs not installed)",
                "ran": False}
    try:
        n = int(max_results)          # tolerate "5" — models pass numbers as strings
    except (TypeError, ValueError):
        n = 5
    try:
        with DDGS() as ddgs:
            rows = list(ddgs.text(query, max_results=max(1, min(n, 8))))
    except Exception as e:
        log.error("look_up failed: %s: %s", type(e).__name__, e)
        return {"error": f"the web lookup failed ({type(e).__name__})", "ran": False}

    results = [{"title": r.get("title", ""),
                "snippet": (r.get("body") or "")[:400],
                "url": r.get("href") or r.get("link") or ""}
               for r in rows]
    log.info("TOOL look_up -> %d results for %r", len(results), query)
    return {"ran": True, "query": query, "results": results,
            "note": "Answer ONLY from these snippets. If they do not contain the "
                    "answer, say so plainly."}


def web_search(query: str, engine: str | None = None, browser: str | None = None,
               _confirmed: bool = False) -> Any:
    """Search the web in a real browser window."""
    engine = (engine or "google").lower()
    template = SEARCH_ENGINES.get(engine, SEARCH_ENGINES["google"])
    url = template.format(q=urllib.parse.quote_plus(query))
    log.info("TOOL web_search: %r on %s (browser=%s)", query, engine, browser)
    result = _open_in_browser(url, browser)
    result["searched"] = query
    result["engine"] = engine
    return result


def get_timetable(day: str | None = None, _confirmed: bool = False) -> Any:
    """Return the user's class schedule from their LOCAL timetable file: the
    classes on a day (default today), the class in progress right now, and the
    next upcoming class. Read-only — computed from the file and the real clock, so
    the model can answer ANY schedule question however it's phrased, instead of
    the fast path having to match an exact form."""
    from system import timetable as tt
    week = tt.load()
    if not week:
        return {"has_timetable": False, "say": "There's no timetable set up, sir."}
    today, mnow = tt._now()
    target = (tt.resolve_day(day) or today) if day else today

    classes = [{"subject": e["subject"], "start": tt._fmt(e["start"]),
                "end": tt._fmt(e["end"]), "where": e["where"] or ""}
               for e in week.get(target, [])]

    cur = tt.current_class()
    current = None if cur is None else {
        "subject": cur["subject"], "ends": tt._fmt(cur["end"]),
        "minutes_left": cur["end"] - mnow, "where": cur["where"] or ""}

    nxt = tt.next_class()
    upcoming = None
    if nxt is not None:
        e, when = nxt
        upcoming = {"subject": e["subject"], "when": when, "where": e["where"] or ""}

    log.info("TOOL get_timetable(day=%s) -> %d classes, current=%s", target,
             len(classes), bool(current))
    return {"has_timetable": True, "day": target, "classes": classes,
            "current_class": current, "next_class": upcoming,
            "note": "Answer naturally from this. minutes_left and 'when' are "
                    "already computed from the real clock; do not recompute times."}


# ── Loose-NL wrappers over the fast path's own system modules, so the LLM can
#    handle any phrasing for these (not just the fast path's exact forms). All
#    non-destructive, so none needs confirmation. ──
def set_volume(level: Any = None, change: Any = None, mute: Any = None,
               _confirmed: bool = False) -> Any:
    """Master volume: `level` absolute 0-100, `change` relative step (e.g. -10 for
    'turn it down a bit'), or `mute` true/false. None given -> report current."""
    from system import volume
    try:
        if mute is not None:
            return {"say": volume.mute() if mute else volume.unmute()}
        if level is not None:
            return {"say": volume.set_volume(int(level))}
        if change is not None:
            return {"say": volume.set_volume(volume.get_volume() + int(change))}
        return {"say": f"Volume's at {volume.get_volume()} percent, sir."}
    except (TypeError, ValueError):
        return {"error": "level/change must be numbers; mute must be true/false"}


def set_brightness(level: Any = None, change: Any = None, _confirmed: bool = False) -> Any:
    """Screen brightness: `level` absolute 0-100, or `change` relative step."""
    from system import volume
    try:
        if level is not None:
            return {"say": volume.set_brightness(int(level))}
        if change is not None:
            return {"say": volume.brightness_step(int(change))}
        return {"error": "give a brightness level (0-100) or a change amount"}
    except (TypeError, ValueError):
        return {"error": "level/change must be numbers"}
    except Exception as e:                       # sbc raises on unsupported displays
        return {"error": f"brightness control unavailable ({type(e).__name__})"}


def media_control(action: str = "", _confirmed: bool = False) -> Any:
    """Transport control for whatever is playing: pause, play, next, previous, stop."""
    from system import media
    fn = {"pause": media.pause_play, "play": media.pause_play,
          "resume": media.pause_play, "toggle": media.pause_play,
          "next": media.next_track, "skip": media.next_track, "forward": media.next_track,
          "previous": media.previous_track, "prev": media.previous_track,
          "back": media.previous_track, "stop": media.stop}.get((action or "").lower().strip())
    if fn is None:
        return {"error": f"unknown media action {action!r} — use pause/play/next/previous/stop"}
    return {"say": fn()}


def play_music(song: str = "", artist: str | None = None, _confirmed: bool = False) -> Any:
    """Play a specific song on Spotify by name (optionally by a given artist)."""
    from system import media
    if not (song or "").strip():
        return {"error": "no song specified"}
    return {"say": media.play_song(song, artist)}


def set_reminder(task: str = "", minutes_from_now: Any = None,
                 at_time: str | None = None, _confirmed: bool = False) -> Any:
    """Set a spoken reminder. Give `task` plus EITHER `minutes_from_now` (a number)
    OR `at_time` as 'HH:MM' 24-hour. It fires aloud when due."""
    import datetime
    import time as _time
    from system import reminders
    task = (task or "").strip() or "your reminder"
    at_ts = None
    if minutes_from_now is not None:
        try:
            mins = float(minutes_from_now)
        except (TypeError, ValueError):
            return {"error": "minutes_from_now must be a number"}
        if mins <= 0:
            return {"error": "minutes_from_now must be positive"}
        at_ts = _time.time() + mins * 60
    elif at_time:
        try:
            hh, mm = (int(x) for x in str(at_time).split(":")[:2])
            now = datetime.datetime.now()
            t = now.replace(hour=hh % 24, minute=mm, second=0, microsecond=0)
            if t <= now:                          # already passed today -> tomorrow
                t += datetime.timedelta(days=1)
            at_ts = t.timestamp()
        except (TypeError, ValueError):
            return {"error": "at_time must be 'HH:MM' in 24-hour form"}
    if at_ts is None:
        return {"error": "give minutes_from_now or at_time"}
    reminders.add(task, at_ts)
    return {"say": f"I'll remind you {reminders.spoken_when(at_ts)}, sir.", "task": task}


_GEMINI_VISION_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


def look_at_screen(question: str = "", _confirmed: bool = False) -> Any:
    """Screenshot the screen and ask a vision model (Gemini) about it. On-demand
    only — nothing is captured unless this is called. Requires a Gemini key."""
    import base64
    if not config.GOOGLE_API_KEY:
        return {"error": "screen vision needs a Gemini key (set GOOGLE_API_KEY)."}
    out = str(config.DATA_DIR / ".screen.png")
    script = str(config.ROOT / "scripts" / "capture_screen.ps1")
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             script, "-Out", out],
            capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW)
        with open(out, "rb") as f:
            img = f.read()
    except Exception as e:
        log.error("screen capture failed: %s", e)
        return {"error": f"couldn't capture the screen ({type(e).__name__})"}
    if not img:
        return {"error": "screen capture produced no image"}
    b64 = base64.b64encode(img).decode()
    ask = (question or "").strip() or "Describe what's on the screen."
    payload = {
        "model": "gemini-flash-latest",
        "max_tokens": 800,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": ask + " Answer in one or two spoken sentences, "
                                           "plainly, no markdown."},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
    }
    headers = {"Authorization": f"Bearer {config.GOOGLE_API_KEY}",
               "Content-Type": "application/json"}
    try:
        r = httpx.post(_GEMINI_VISION_URL, headers=headers, json=payload, timeout=45)
        if r.status_code != 200:
            log.error("vision model %s: %s", r.status_code, r.text[:200])
            return {"error": f"the vision model returned {r.status_code}"}
        text = (r.json()["choices"][0]["message"].get("content") or "").strip()
        log.info("look_at_screen -> %r", text[:120])
        return {"say": text or "I couldn't make much of the screen, sir."}
    except Exception as e:
        log.error("vision request failed: %s", e)
        return {"error": f"the vision request failed ({type(e).__name__})"}


def timer(action: str = "", minutes: Any = 0, seconds: Any = 0,
          _confirmed: bool = False) -> Any:
    """Countdown timer / stopwatch. action='set' (needs minutes and/or seconds),
    'stopwatch' (start one), or 'stop' (cancel whatever's running)."""
    from system import timers
    a = (action or "").lower().strip()
    if a in ("set", "timer", "start_timer"):
        try:
            total = int(float(minutes or 0)) * 60 + int(float(seconds or 0))
        except (TypeError, ValueError):
            return {"error": "minutes/seconds must be numbers"}
        if total <= 0:
            return {"error": "give a duration (minutes and/or seconds)"}
        timers.set_timer(total)
        return {"say": f"Timer set for {timers.fmt_dur(total)}, sir."}
    if a in ("stopwatch", "start_stopwatch", "start"):
        timers.start_stopwatch()
        return {"say": "Stopwatch running, sir."}
    if a in ("stop", "cancel", "stop_timer", "stop_stopwatch"):
        return {"say": "Stopped, sir." if timers.stop() else "Nothing's running, sir."}
    return {"error": f"unknown timer action {action!r} (use set / stopwatch / stop)"}


def remember(fact: str = "", _confirmed: bool = False) -> Any:
    """Save one durable fact about the user to long-term memory (persists across
    restarts). Use for names, preferences, ongoing projects, dislikes."""
    from system import memory
    return {"say": memory.add(fact)}


def forget(topic: str = "", _confirmed: bool = False) -> Any:
    """Drop remembered facts mentioning `topic` from long-term memory."""
    from system import memory
    return {"say": memory.forget(topic)}


REGISTRY: dict[str, Callable[..., Any]] = {
    "look_up": look_up,
    "get_timetable": get_timetable,
    "look_at_screen": look_at_screen,
    "timer": timer,
    "remember": remember,
    "forget": forget,
    "set_volume": set_volume,
    "set_brightness": set_brightness,
    "media_control": media_control,
    "play_music": play_music,
    "set_reminder": set_reminder,
    "open_maps": open_maps,
    "cricket_scores": cricket_scores,
    "run_shell_command": run_shell_command,
    "open_path": open_path,
    "list_directory": list_directory,
    "read_file": read_file,
    "write_file": write_file,
    "get_system_health": get_system_health,
    "get_top_processes": get_top_processes,
    "web_search": web_search,
    "open_url": open_url,
    "delete_path": delete_path,
    "move_path": move_path,
}

# ── OpenAI/Groq tool schemas ────────────────────────────────────
SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_timetable",
            "description": (
                "Get the user's class schedule from their LOCAL timetable: the "
                "classes on a day (default today, or a named day), the class in "
                "progress right now, and the next upcoming class. Use this for ANY "
                "question about classes, lectures, labs, periods, the schedule, or "
                "when they are free, however it is phrased ('what's coming up', "
                "'am I done for the day', 'anything this afternoon', 'when am I "
                "free'). Never guess a schedule or say you can't access it."),
            "parameters": {
                "type": "object",
                "properties": {
                    "day": {"type": "string",
                            "description": "Optional day: 'today', 'tomorrow', or a "
                                           "weekday like 'friday'. Omit for today."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "look_at_screen",
            "description": (
                "Take a screenshot and have a vision model look at it. Use for "
                "'what's on my screen', 'what does this error mean', 'summarise this "
                "page', 'help me with what I'm looking at'. Pass the user's actual "
                "question so the answer is specific."),
            "parameters": {"type": "object", "properties": {
                "question": {"type": "string",
                             "description": "What to find out about the screen."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "timer",
            "description": (
                "Countdown timer or stopwatch. action='set' to start a countdown "
                "(give minutes and/or seconds), 'stopwatch' to start a stopwatch, "
                "'stop' to cancel whatever is running. For 'wake me in 10 minutes' "
                "use set_reminder instead; this is for a visible on-screen timer."),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string", "enum": ["set", "stopwatch", "stop"]},
                "minutes": {"type": "number", "description": "Countdown minutes (action=set)."},
                "seconds": {"type": "number", "description": "Countdown seconds (action=set)."},
            }, "required": ["action"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Save ONE durable fact about the user to long-term memory (persists "
                "across restarts). Use when the user asks you to remember something, "
                "or shares a lasting preference, name, ongoing project, or dislike "
                "('remember I prefer dark mode', 'my sister's name is Anya'). Do NOT "
                "use it for one-off/transient things."),
            "parameters": {"type": "object", "properties": {
                "fact": {"type": "string", "description": "The fact, as one short sentence."},
            }, "required": ["fact"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": ("Remove remembered facts about a topic from long-term "
                            "memory ('forget what I said about my car')."),
            "parameters": {"type": "object", "properties": {
                "topic": {"type": "string", "description": "Keyword of what to forget."},
            }, "required": ["topic"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_volume",
            "description": (
                "Set the master volume. Use 'level' for an absolute 0-100 value, "
                "'change' for a relative step (-10 for 'turn it down a bit', +15 "
                "for louder), or 'mute' true/false. Omit all to report the current "
                "volume."),
            "parameters": {"type": "object", "properties": {
                "level": {"type": "integer", "description": "Absolute volume 0-100."},
                "change": {"type": "integer", "description": "Relative change, e.g. -10 or 15."},
                "mute": {"type": "boolean", "description": "true to mute, false to unmute."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_brightness",
            "description": ("Set screen brightness. 'level' = absolute 0-100, or "
                            "'change' = relative step (e.g. -20 to dim, +20 to brighten)."),
            "parameters": {"type": "object", "properties": {
                "level": {"type": "integer", "description": "Absolute brightness 0-100."},
                "change": {"type": "integer", "description": "Relative change, e.g. -20 or 20."},
            }},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "media_control",
            "description": (
                "Control whatever is playing (Spotify, video, etc.): pause, play, "
                "next, previous, or stop. Use for 'pause the music', 'skip this', "
                "'go back a song', 'stop'. To play a NAMED song use play_music."),
            "parameters": {"type": "object", "properties": {
                "action": {"type": "string",
                           "enum": ["pause", "play", "next", "previous", "stop"],
                           "description": "Transport action."},
            }, "required": ["action"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": ("Play a specific song on Spotify by name (optionally a "
                            "given artist). Use for 'play <song>', 'put on <song> by "
                            "<artist>'. For bare pause/skip/stop use media_control."),
            "parameters": {"type": "object", "properties": {
                "song": {"type": "string", "description": "Song title, as spoken."},
                "artist": {"type": "string", "description": "Optional artist name."},
            }, "required": ["song"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": (
                "Set a reminder that fires aloud when due. Provide 'task' plus "
                "EITHER 'minutes_from_now' (a number) OR 'at_time' as 'HH:MM' in "
                "24-hour form. You know the current time from context, so convert "
                "phrasings like 'in half an hour' or 'at 6pm' yourself."),
            "parameters": {"type": "object", "properties": {
                "task": {"type": "string", "description": "What to remind the user about."},
                "minutes_from_now": {"type": "number", "description": "Minutes until it fires."},
                "at_time": {"type": "string", "description": "Absolute time 'HH:MM' 24-hour."},
            }, "required": ["task"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell_command",
            "description": (
                "Run a PowerShell command on the user's Windows PC and return its "
                "output. Use for anything the other tools don't cover (system info, "
                "searching, launching processes, git, etc.). NEVER use this to "
                "delete, move, rename, or copy files — use delete_path / move_path "
                "for that (a shell path would be a guess). It is also blocked from "
                "powering off the machine."),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "PowerShell command to run."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_path",
            "description": (
                "Open a file, folder, or application. Accepts a full path, or a "
                "loose spoken name ('heat', 'downloads', 'chrome') which is resolved "
                "against the local index."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path or spoken name."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": ("List a folder's contents. Use this to find out what is "
                            "actually there before acting on files."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Folder path or spoken name."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file's contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path or spoken name."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": ("Write text to a file. Overwriting an existing file needs "
                            "the user's spoken confirmation (handled automatically)."),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cricket_scores",
            "description": (
                "Live cricket scores. Use for 'what's the score', 'any cricket on', "
                "'how are India doing'. Returns ONLY matches in progress right now. "
                "If it returns an empty list, say there is no live cricket — never "
                "invent or recall a score from memory."),
            "parameters": {
                "type": "object",
                "properties": {
                    "team": {"type": "string",
                             "description": "Optional team name to filter by, "
                                            "e.g. 'India'. Omit for all live matches."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_maps",
            "description": (
                "Open Google Maps in the browser — a place, or directions between "
                "two places. Use this whenever the user wants a map, a route, "
                "directions, or 'how do I get to X'. This is the ONLY way to open "
                "maps: never claim you are opening maps without calling it."),
            "parameters": {
                "type": "object",
                "properties": {
                    "destination": {"type": "string",
                                    "description": "Where to. A place or address."},
                    "origin": {"type": "string",
                               "description": "Optional starting point. Omit to "
                                              "just show the destination."},
                },
                "required": ["destination"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "look_up",
            "description": (
                "Search the web and READ the results back as text. Use this "
                "whenever the answer depends on CURRENT or LOCAL facts you cannot "
                "know from memory: showtimes, what's playing at a named cinema, "
                "prices, opening hours, today's news, sports scores, or any "
                "specific local business. Call this BEFORE answering such a "
                "question — never answer from memory and never say you 'couldn't "
                "find any information' unless this tool actually ran and came back "
                "empty. It does NOT open a browser; use web_search for that."),
            # DELIBERATELY only one string parameter. An optional
            # "max_results": {"type": "integer"} broke tool-calling outright:
            # Llama emits it as the STRING "5", Groq rejects the whole call with
            # `tool_use_failed - /max_results: expected integer, but got string`,
            # and after retries the turn dies as "Couldn't pin that down, sir".
            # The count is a Python-side default; the model never needs to set it.
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": "What to look up. Include the place "
                                             "name for local questions."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Open a search in a real BROWSER WINDOW for the user to look at. "
                "Use this only when they ask to open/google/search something in a "
                "browser. It returns no readable results — if YOU need the answer "
                "in order to reply, call look_up instead."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What to search for."},
                    "engine": {"type": "string",
                               "enum": ["google", "brave", "duckduckgo", "youtube"],
                               "description": "Search engine. Default google."},
                    "browser": {"type": "string",
                                "description": "Browser to open it in, e.g. 'brave'. "
                                               "Omit for the system default."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_url",
            "description": "Open a specific web page (URL) in the browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string"},
                    "browser": {"type": "string",
                                "description": "e.g. 'brave'. Omit for the default."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_health",
            "description": (
                "Get REAL measured system stats: CPU %, RAM %, disk, battery, and GPU "
                "utilization/temperature if an NVIDIA GPU is present. ALWAYS call this "
                "for any question about the machine's status, performance, temperature, "
                "load, or 'how are things running'. Never guess these numbers."),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_top_processes",
            "description": (
                "List the applications using the most RAM or CPU right now, with REAL "
                "measured numbers, aggregated per app (Chrome/VS Code etc. run many "
                "processes — these are summed). ALWAYS call this for 'what's using my "
                "RAM/CPU', 'what's the biggest memory hog', 'why is it slow'. Never "
                "guess. Report the app names and numbers it returns."),
            "parameters": {
                "type": "object",
                "properties": {
                    "by": {"type": "string", "enum": ["memory", "cpu"],
                           "description": "Rank by RAM (default) or CPU."},
                    "limit": {"type": "integer", "description": "How many, default 5."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_path",
            "description": (
                "Delete a FILE (to the Recycle Bin). Pass the file's spoken NAME as "
                "the user said it — including any folder ('la unit four in the pesu "
                "folder'). The tool resolves the REAL file itself and confirms with "
                "the user first; you must NEVER pass a full path you constructed. "
                "This is the ONLY way to delete a file — never use run_shell_command."),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string",
                               "description": "The file's spoken name/description."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_path",
            "description": (
                "Move a FILE into a folder. Pass both as spoken names; the tool "
                "resolves the real file and destination folder and confirms first. "
                "Never construct paths yourself; never use run_shell_command for this."),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "The file's spoken name."},
                    "destination": {"type": "string",
                                    "description": "The destination folder's spoken name."},
                },
                "required": ["target", "destination"],
            },
        },
    },
]


def dispatch(name: str, args: dict, confirmed: bool = False) -> Any:
    """Run a tool by name. Every dispatch is logged at entry and on any failure
    (with a traceback) so a broken/mis-routed tool is always visible in the log —
    not just the tool's own internal logging."""
    log.info("DISPATCH %s(%s) confirmed=%s", name, args, confirmed)
    fn = REGISTRY.get(name)
    if fn is None:
        log.error("DISPATCH unknown tool %r (available: %s)", name, list(REGISTRY))
        return {"error": f"unknown tool {name!r}"}
    try:
        result = fn(**args, _confirmed=confirmed)
        log.info("DISPATCH %s -> %s", name, str(result)[:200])
        return result
    except TypeError as e:
        log.error("DISPATCH %s bad arguments %s: %s", name, args, e)
        return {"error": f"bad arguments for {name}: {e}"}
    except Exception as e:                      # a tool must never kill the turn
        import traceback
        log.error("DISPATCH %s FAILED: %s\n%s", name, e, traceback.format_exc())
        return {"error": str(e)}
