"""Reminders: "remind me in 10 minutes to X" / "remind me at 6pm to X".

Stored as absolute UNIX timestamps in data/reminders.json so they survive a
backend restart (as long as JARVIS is running when the time arrives). main.py
runs a checker that fires the due ones aloud, interrupting idle.
"""
import datetime
import json
import logging
import re
import time
from pathlib import Path

import config
from system import textnorm

log = logging.getLogger("jarvis.reminders")

REMINDERS_FILE: Path = config.DATA_DIR / "reminders.json"


def _load() -> list[dict]:
    if not REMINDERS_FILE.exists():
        return []
    try:
        data = json.loads(REMINDERS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(items: list[dict]) -> None:
    REMINDERS_FILE.write_text(json.dumps(items, indent=1), encoding="utf-8")


def add(text: str, at_ts: float) -> None:
    items = _load()
    items.append({"text": text, "at": at_ts, "created": time.time()})
    items.sort(key=lambda r: r["at"])
    _save(items)
    log.info("reminder set for %s: %r",
             datetime.datetime.fromtimestamp(at_ts).strftime("%H:%M"), text[:80])


def due(now: float | None = None) -> list[dict]:
    """Pop and return every reminder whose time has arrived."""
    now = now or time.time()
    items = _load()
    fired = [r for r in items if r["at"] <= now]
    if fired:
        _save([r for r in items if r["at"] > now])
    return fired


def pending() -> list[dict]:
    return _load()


# ── Natural-language time parsing ───────────────────────────────
_REL = re.compile(
    r"\bin\s+(?P<amt>[\w\s]+?)\s*(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?)\b",
    re.I)
_HALF_HOUR = re.compile(r"\bin\s+half\s+an?\s+hour\b", re.I)
_AN_HOUR = re.compile(r"\bin\s+an?\s+hour\b", re.I)
_A_MIN = re.compile(r"\bin\s+a\s+(?:minute|min)\b", re.I)
_AT = re.compile(
    r"\bat\s+(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ap>a\.?m\.?|p\.?m\.?)?\b", re.I)
_NOON = re.compile(r"\bat\s+noon\b", re.I)
_MIDNIGHT = re.compile(r"\bat\s+midnight\b", re.I)

# Strip the leading command + the trailing/leading time phrase to isolate the task
_LEAD = re.compile(r"^\s*(?:hey\s+)?(?:jarvis[,\s]*)?remind\s+me\s+", re.I)
_TO = re.compile(r"^\s*(?:to|that|about)\s+", re.I)


def _seconds_for(amt: str, unit: str) -> float | None:
    n = textnorm.parse_number(amt)
    if n is None:
        return None
    unit = unit.lower()
    if unit.startswith(("sec",)):
        return n
    if unit.startswith(("min",)):
        return n * 60
    return n * 3600          # hours


def parse(spoken: str) -> tuple[str, float] | None:
    """(task_text, absolute_ts) from a spoken reminder, or None if no time found."""
    s = (spoken or "").strip()
    body = _LEAD.sub("", s)          # drop "remind me"
    now = datetime.datetime.now()
    delay: float | None = None
    target: datetime.datetime | None = None
    time_span: tuple[int, int] | None = None

    if _HALF_HOUR.search(s):
        delay = 1800; time_span = _HALF_HOUR.search(s).span()
    elif _AN_HOUR.search(s):
        delay = 3600; time_span = _AN_HOUR.search(s).span()
    elif _A_MIN.search(s):
        delay = 60; time_span = _A_MIN.search(s).span()
    else:
        m = _REL.search(s)
        if m:
            delay = _seconds_for(m["amt"], m["unit"])
            time_span = m.span()

    if delay is None:                # try an absolute time
        if _NOON.search(s):
            target = now.replace(hour=12, minute=0, second=0, microsecond=0)
            if target <= now:                   # noon already passed -> tomorrow
                target += datetime.timedelta(days=1)
            time_span = _NOON.search(s).span()
        elif _MIDNIGHT.search(s):
            target = (now + datetime.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            time_span = _MIDNIGHT.search(s).span()
        else:
            m = _AT.search(s)
            if m:
                h = int(m["h"]); mn = int(m["m"] or 0)
                ap = (m["ap"] or "").lower().replace(".", "")
                if ap == "pm" and h != 12:
                    h += 12
                elif ap == "am" and h == 12:
                    h = 0
                target = now.replace(hour=h % 24, minute=mn, second=0, microsecond=0)
                if target <= now:               # already passed -> tomorrow
                    target += datetime.timedelta(days=1)
                time_span = m.span()

    if delay is None and target is None:
        return None
    at_ts = (time.time() + delay) if delay is not None else target.timestamp()

    # Cut the time phrase out of the body, then strip a leading "to"/"that".
    if time_span:
        # time_span is relative to `s`; map onto body by removing the phrase text
        phrase = s[time_span[0]:time_span[1]]
        body = body.replace(phrase, " ")
    task = _TO.sub("", re.sub(r"\s{2,}", " ", body).strip(" ,.")).strip(" ,.")
    return (task or "your reminder", at_ts)


def spoken_when(at_ts: float) -> str:
    """A short natural phrase for when a reminder is set for."""
    dt = datetime.datetime.fromtimestamp(at_ts)
    delta = at_ts - time.time()
    if delta < 90:
        return "in a moment"
    if delta < 3600:
        return f"in {round(delta / 60)} minutes"
    return f"at {dt.strftime('%I:%M %p').lstrip('0')}"
