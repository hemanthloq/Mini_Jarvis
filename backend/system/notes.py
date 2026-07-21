"""Voice notes: a plain, append-only timestamped log the user dictates to.

"note to self, call the dentist" / "remember that the wifi password is hunter2"
appends a line; "what are my notes" reads them back, most recent first.
"""
import datetime
import logging
from pathlib import Path

import config

log = logging.getLogger("jarvis.notes")

NOTES_FILE: Path = config.DATA_DIR / "notes.txt"


def add(text: str) -> str:
    """Append a timestamped note. Returns the spoken confirmation."""
    text = (text or "").strip()
    if not text:
        return "There was nothing to note, sir."
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    with NOTES_FILE.open("a", encoding="utf-8") as f:
        f.write(f"[{stamp}] {text}\n")
    log.info("note added: %r", text[:80])
    return "Noted, sir."


def _entries() -> list[tuple[str, str]]:
    """(timestamp, text) for every note, oldest first."""
    if not NOTES_FILE.exists():
        return []
    out = []
    for line in NOTES_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("[") and "]" in line:
            ts, _, body = line[1:].partition("] ")
            out.append((ts, body))
        else:
            out.append(("", line))
    return out


def read_back(limit: int = 5) -> str:
    """A concise spoken summary of the notes, most recent first."""
    entries = _entries()
    if not entries:
        return "You haven't left any notes, sir."
    recent = list(reversed(entries))[:limit]
    total = len(entries)
    lead = (f"Your most recent {len(recent)} of {total} notes, sir: "
            if total > limit else
            (f"Your {total} notes, sir: " if total > 1 else "One note, sir: "))
    body = ". ".join(text for _ts, text in recent)
    return lead + body + "."


def count() -> int:
    return len(_entries())
