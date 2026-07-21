"""Weekly class schedule from data/timetable.json — "what's my next class?".

Same editable-JSON pattern as routines.json: the user maintains it by hand once
a semester, no calendar API and no OAuth. Everything here is local and offline.

Answers are computed from the real clock, never from the model's guess about
what time it is.
"""
import datetime
import json
import logging

import config

log = logging.getLogger("jarvis.timetable")

TIMETABLE_FILE = config.DATA_DIR / "timetable.json"
DEFAULT_MINUTES = 55          # assumed length when a class has no "end"
DAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

_cache: dict | None = None


def load(force: bool = False) -> dict:
    global _cache
    if _cache is not None and not force:
        return _cache
    if not TIMETABLE_FILE.exists():
        log.info("no timetable at %s", TIMETABLE_FILE)
        _cache = {}
        return _cache
    try:
        raw = json.loads(TIMETABLE_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.error("timetable.json is invalid (%s) — ignoring it", e)
        return {}
    out: dict[str, list[dict]] = {}
    for day in DAYS:
        entries = []
        for item in raw.get(day) or []:
            if not isinstance(item, dict) or not item.get("start"):
                continue
            try:
                sh, sm = (int(x) for x in str(item["start"]).split(":")[:2])
            except (ValueError, TypeError):
                log.warning("timetable: bad start time %r on %s", item.get("start"), day)
                continue
            start = sh * 60 + sm
            end = None
            if item.get("end"):
                try:
                    eh, em = (int(x) for x in str(item["end"]).split(":")[:2])
                    end = eh * 60 + em
                except (ValueError, TypeError):
                    end = None
            entries.append({"start": start, "end": end or start + DEFAULT_MINUTES,
                            "subject": str(item.get("subject") or "a class"),
                            "where": item.get("where") or ""})
        out[day] = sorted(entries, key=lambda e: e["start"])
    _cache = out
    total = sum(len(v) for v in out.values())
    log.info("loaded timetable: %d classes across the week", total)
    return out


def _fmt(minutes: int) -> str:
    """24h minutes -> a spoken clock time ('9 AM', '2:30 PM')."""
    h, m = divmod(minutes, 60)
    ampm = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return f"{h12} {ampm}" if m == 0 else f"{h12}:{m:02d} {ampm}"


def _now() -> tuple[str, int]:
    n = datetime.datetime.now()
    return DAYS[n.weekday()], n.hour * 60 + n.minute


def current_class(day: str | None = None, minute: int | None = None) -> dict | None:
    d, mnow = _now()
    day, minute = day or d, mnow if minute is None else minute
    for e in load().get(day, []):
        if e["start"] <= minute < e["end"]:
            return e
    return None


def next_class(day: str | None = None, minute: int | None = None) -> tuple[dict, str] | None:
    """(class, when) for the next one — today, else the next day that has any.
    Looks a full week ahead so a Friday evening question still answers."""
    d0, m0 = _now()
    day, minute = day or d0, m0 if minute is None else minute
    start_idx = DAYS.index(day)
    for offset in range(8):
        d = DAYS[(start_idx + offset) % 7]
        for e in load().get(d, []):
            if offset == 0 and e["start"] <= minute:
                continue
            if offset == 0:
                return e, f"at {_fmt(e['start'])}"
            when = "tomorrow" if offset == 1 else f"on {d.capitalize()}"
            return e, f"{when} at {_fmt(e['start'])}"
    return None


def _human_gap(minutes: int) -> str:
    """A spoken duration: '20 minutes', '1 hour', '2 hours and 15 minutes'.

    Deliberately article-free ('1 hour', not 'an hour'), because every caller
    prefixes it — "for another an hour 30 minutes" is what the article version
    produced. `_another()` below re-adds natural phrasing where it reads better.
    """
    if minutes < 1:
        return "less than a minute"
    if minutes < 60:
        return "1 minute" if minutes == 1 else f"{minutes} minutes"
    h, m = divmod(minutes, 60)
    hh = "1 hour" if h == 1 else f"{h} hours"
    if m == 0:
        return hh
    return f"{hh} and {'1 minute' if m == 1 else f'{m} minutes'}"


def _another(minutes: int) -> str:
    """The same duration, phrased to follow the word 'another'."""
    gap = _human_gap(minutes)
    # "another 1 hour" -> "another hour"; "another 1 hour and 30 minutes" ->
    # "another hour and 30 minutes".
    return gap[2:] if gap.startswith("1 hour") else gap


# Beyond this, a countdown stops being useful ("Computer Networks in 14 hours")
# and the day-and-time phrasing reads better.
COUNTDOWN_LIMIT = 5 * 60


def describe_next() -> str:
    """What's next — and, crucially, how long you've got.

    Asked mid-class, "what's my next class" really means two things: what is it,
    and how much of this one is left. Answering only the first is technically
    correct and practically useless, so both are given when they apply — while
    staying inside JARVIS's two-sentence limit.
    """
    nxt = next_class()
    if nxt is None:
        return "Nothing on your timetable, sir."
    e, when = nxt
    where = f" in {e['where']}" if e["where"] else ""
    _, mnow = _now()
    cur = current_class()

    # Only count down for something genuinely soon and today.
    soon = when.startswith("at") and 0 < (e["start"] - mnow) <= COUNTDOWN_LIMIT
    gap = _human_gap(e["start"] - mnow) if soon else ""

    if cur is not None:
        left = _another(cur["end"] - mnow)
        head = f"{e['subject']} {when}{where}, sir"
        return (f"{head} — you're in {cur['subject']} for another {left}."
                if soon else
                f"{head}. You're in {cur['subject']} for another {left}.")
    if soon:
        return f"{e['subject']} {when}{where}, sir — {gap} from now."
    return f"{e['subject']} {when}{where}, sir."


def describe_free() -> str:
    """'Am I free right now?' — answered from the real clock."""
    _, mnow = _now()
    cur = current_class()
    if cur is not None:
        where = f" in {cur['where']}" if cur["where"] else ""
        return (f"No, sir — {cur['subject']}{where} for another "
                f"{_another(cur['end'] - mnow)}, until {_fmt(cur['end'])}.")
    nxt = next_class()
    if nxt is None:
        return "Free, sir. Nothing left on your timetable."
    e, when = nxt
    gap = e["start"] - mnow
    if when.startswith("at") and 0 < gap <= COUNTDOWN_LIMIT:
        return f"Free for {_human_gap(gap)}, sir — {e['subject']} {when}."
    return f"Free, sir. Next is {e['subject']} {when}."


def describe_day(day: str | None = None, label: str | None = None) -> str:
    """All classes on a given day. `day` None = today.

    Also answers "what are the classes on Tuesday" and "...for tomorrow", which
    previously fell through to the LLM — which of course has no idea what your
    timetable is and said so ("my knowledge does not extend to current class
    schedules"), or worse, fuzzy-matched a media command.
    """
    today, _ = _now()
    day = (day or today).lower()
    if day not in DAYS:
        return "I don't know that day, sir."
    if label is None:
        label = ("today" if day == today else
                 "tomorrow" if DAYS[(DAYS.index(today) + 1) % 7] == day else
                 f"on {day.capitalize()}")
    classes = load().get(day, [])
    if not classes:
        return f"Nothing scheduled {label}, sir."
    parts = [f"{e['subject']} at {_fmt(e['start'])}" for e in classes]
    if len(parts) == 1:
        return f"Just {parts[0]}, {label}, sir."
    return (f"{len(parts)} {label}, sir: " + ", ".join(parts[:-1])
            + f", and {parts[-1]}.")


def describe_today() -> str:
    return describe_day()


def resolve_day(word: str) -> str | None:
    """'tuesday' / 'tomorrow' / 'today' -> a day key, or None."""
    w = (word or "").strip().lower()
    today, _ = _now()
    if w in ("today",):
        return today
    if w in ("tomorrow", "tomorow"):
        return DAYS[(DAYS.index(today) + 1) % 7]
    if w in ("yesterday",):
        return DAYS[(DAYS.index(today) - 1) % 7]
    for d in DAYS:
        if w.startswith(d[:3]):          # 'tues', 'tue', 'tuesday'
            return d
    return None
