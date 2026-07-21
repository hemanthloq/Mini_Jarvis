"""Morning-briefing bridge.

An external Claude Desktop scheduled task writes a plain-text summary to
data/briefing.txt once a day. JARVIS reads it aloud on the first wake of the
day, or on demand ("what's my briefing" / "what's today looking like").
"""
import logging
import time
from datetime import datetime, date
from pathlib import Path

import config

log = logging.getLogger("jarvis.briefing")

BRIEFING_FILE = config.DATA_DIR / "briefing.txt"
STALE_HOURS = 18
_state_file = config.DATA_DIR / ".briefing_read"
MAX_CHARS = 1200


def _mtime() -> datetime | None:
    if not BRIEFING_FILE.exists():
        return None
    return datetime.fromtimestamp(BRIEFING_FILE.stat().st_mtime)


def age_hours() -> float | None:
    m = _mtime()
    return None if m is None else (time.time() - m.timestamp()) / 3600


def is_fresh() -> bool:
    a = age_hours()
    return a is not None and a <= STALE_HOURS


def status_phrase() -> str:
    """One clause describing briefing freshness, for the status report."""
    a = age_hours()
    if a is None:
        return "no briefing file yet"
    if a <= STALE_HOURS:
        return f"today's briefing is fresh, {int(a)} hours old" if a >= 1 else \
               "today's briefing just landed"
    return f"the briefing is stale, {int(a)} hours old"


def read_text() -> str | None:
    """The briefing text if it exists and is fresh, else None."""
    if not is_fresh():
        return None
    try:
        text = BRIEFING_FILE.read_text(encoding="utf-8", errors="replace").strip()
    except OSError as e:
        log.error("cannot read briefing: %s", e)
        return None
    return text[:MAX_CHARS] or None


def already_read_today() -> bool:
    if not _state_file.exists():
        return False
    try:
        return _state_file.read_text(encoding="utf-8").strip() == date.today().isoformat()
    except OSError:
        return False


def mark_read_today() -> None:
    try:
        _state_file.write_text(date.today().isoformat(), encoding="utf-8")
    except OSError as e:
        log.warning("cannot record briefing state: %s", e)


def should_auto_brief() -> bool:
    """First wake of the day, and today's briefing is sitting there unread."""
    return is_fresh() and not already_read_today()
