"""User-editable routines: trigger phrase -> actions + a spoken response.

Checked (fuzzy) right after the wake word, before anything reaches the LLM.
Edit data/routines.json — no code changes needed. See README.

Response types:
  "say"          : fixed line, spoken from the phrase cache (instant, no TTS call)
  "say_dynamic"  : keyword naming a REAL data source; the line is generated from
                   its actual output, phrased in character by the LLM.
                   Supported: "system_health"

Actions (executed in order, reusing the existing system layer):
  "open:<name>"    open an app/file/folder via the alias index
  "media:pause" | "media:next" | "media:previous" | "media:stop"
  "volume:<0-100>" | "volume:up" | "volume:down" | "mute" | "unmute"
  "brightness:<0-100>"
  "duck:on" | "duck:off"
  "launch:<name>"  alias for open
  "shell:<command>"
  "sleep"          go dormant (voice + HUD off) but KEEP the wake word listening,
                   so "jarvis" brings it straight back. This is what voice
                   commands should use.
  "quit"           hard shutdown — everything dies and needs a manual relaunch.
                   Reserved for the HUD's X button / Ctrl+Alt+Q / tray; putting
                   it in a routine means a spoken phrase can kill JARVIS outright.
"""
import json
import logging
import re
from dataclasses import dataclass, field

from rapidfuzz import fuzz, process

import config
from system import apps, ducking, media, volume

log = logging.getLogger("jarvis.routines")

ROUTINES_FILE = config.DATA_DIR / "routines.json"
MATCH_THRESHOLD = 88          # full-string similarity, not partial (see match())

DEFAULT_ROUTINES = {
    "welcome home": {
        "say_dynamic": "system_health",
        "actions": ["open:chrome", "open:spotify"],
    },
    "good morning": {
        "say_dynamic": "system_health",
        "actions": [],
    },
    "movie mode": {
        "say": "Dimming the noise, sir.",
        "actions": ["media:pause", "volume:30"],
    },
    "gaming mode": {
        "say": "Enjoy yourself, sir. I'll keep things quiet.",
        "actions": ["duck:on", "launch:steam"],
    },
    "shut up and turn it off": {
        "say": "Going to sleep, sir.",
        "actions": ["sleep"],       # dormant, not a quit — "jarvis" wakes it
    },
}


@dataclass
class Routine:
    trigger: str
    say: str | None = None
    say_dynamic: str | None = None
    actions: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)


# Speech padding that is never part of a trigger. Stripped BEFORE matching so a
# natural "jarvis, movie mode please" still scores against the bare trigger —
# this does not loosen the similarity threshold, it only removes known filler.
# (The length guard in match() rejected these outright: "movie mode please" is
# 1.7x the length of "movie mode".)
_LEAD = re.compile(r"^(?:hey|ok|okay|yo|hi|hello)?\s*jarvis\b[\s,]*", re.I)
_TRAIL = re.compile(r"\s*\b(?:please|now|for me|thanks|thank you)\b\s*$", re.I)


def _normalise(text: str) -> str:
    q = re.sub(r"[^\w\s']", " ", text.lower())
    q = re.sub(r"\s+", " ", q).strip()
    q = _LEAD.sub("", q)
    for _ in range(2):                    # "movie mode please now"
        q = _TRAIL.sub("", q)
    return q.strip()


_routines: dict[str, Routine] | None = None


def load(force: bool = False) -> dict[str, Routine]:
    global _routines
    if _routines is not None and not force:
        return _routines
    if not ROUTINES_FILE.exists():
        ROUTINES_FILE.write_text(json.dumps(DEFAULT_ROUTINES, indent=2), encoding="utf-8")
        log.info("created default %s", ROUTINES_FILE)
    try:
        raw = json.loads(ROUTINES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("routines.json is invalid (%s) — ignoring it", e)
        return {}
    out: dict[str, Routine] = {}
    for trigger, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        out[trigger.strip().lower()] = Routine(
            trigger=trigger.strip().lower(),
            say=spec.get("say"),
            say_dynamic=spec.get("say_dynamic"),
            actions=list(spec.get("actions") or []),
            # Optional extra spoken forms. Deepgram reliably mishears some
            # triggers (it transcribed "daddy's home" as "that is home"), and no
            # similarity threshold can bridge that — the two share 58% of their
            # characters. An explicit alias is the only safe fix: it stays exact
            # rather than loosening matching for every phrase.
            aliases=[a.strip().lower() for a in (spec.get("aliases") or []) if a.strip()],
        )
    _routines = out
    log.info("loaded %d routines: %s", len(out), ", ".join(out) or "-")
    return out


def match(text: str) -> Routine | None:
    """Fuzzy-match a whole utterance to a routine trigger.

    Deliberately strict. WRatio does partial matching, which once matched the
    sentence "I heard some theory about the creation of pie, what is it?" to the
    trigger "shut up and turn it off" (score 85) and shut JARVIS down mid-
    conversation. A routine must match the WHOLE utterance closely:
      * full-string similarity (fuzz.ratio / token_sort_ratio), not partial
      * and the utterance must be about as long as the trigger
    """
    table = load()
    if not table:
        return None
    query = _normalise(text)
    if not query:
        return None

    best: tuple[float, str] | None = None
    for trigger, routine in table.items():
        # Match against the trigger AND any user-declared aliases; the best
        # scoring form wins, but the routine is still keyed by its trigger.
        for form in (trigger, *routine.aliases):
            score = max(fuzz.ratio(query, form), fuzz.token_sort_ratio(query, form))
            if score < MATCH_THRESHOLD:
                continue
            # Length guard: a long sentence can never be a short trigger.
            ratio = len(query) / max(1, len(form))
            if not (0.6 <= ratio <= 1.5):
                log.debug("routine %r rejected for %r (length ratio %.2f)",
                          form, text, ratio)
                continue
            if best is None or score > best[0]:
                best = (score, trigger)

    if best is None:
        return None
    log.info("routine matched %r -> %r (%d)", text, best[1], best[0])
    return table[best[1]]


def run_actions(actions: list[str], quit_fn=None) -> None:
    """Execute a routine's actions. Reuses the same system layer the fast path uses."""
    for action in actions:
        try:
            kind, _, arg = action.partition(":")
            kind, arg = kind.strip().lower(), arg.strip()
            log.info("routine action: %s", action)

            if kind in ("open", "launch"):
                status, line, _extra = apps.open_target(arg)
                if status != "opened":
                    log.warning("routine action %r did not open anything (%s)", action, line)
            elif kind == "media":
                {"pause": media.pause_play, "play": media.pause_play,
                 "next": media.next_track, "skip": media.next_track,
                 "previous": media.previous_track, "stop": media.stop}.get(
                     arg, media.pause_play)()
            elif kind == "volume":
                if arg == "up":
                    volume.volume_up()
                elif arg == "down":
                    volume.volume_down()
                elif arg.isdigit():
                    volume.set_volume(int(arg))
            elif kind == "mute":
                volume.mute()
            elif kind == "unmute":
                volume.unmute()
            elif kind == "brightness" and arg.isdigit():
                volume.set_brightness(int(arg))
            elif kind == "duck":
                ducking.duck() if arg == "on" else ducking.unduck()
            elif kind == "shell":
                from router import tools
                tools.run_shell_command(arg, _confirmed=True)   # routines are user-authored
            elif kind == "sleep":
                pass          # handled by the orchestrator (must speak first)
            elif kind == "quit":
                if quit_fn:
                    quit_fn()
            else:
                log.warning("unknown routine action: %s", action)
        except Exception as e:
            log.error("routine action %r failed: %s", action, e)
