"""Timer + stopwatch state, shared by the fast path, the smart-path `timer` tool,
and the HUD ticker in main.py — one source of truth so all three agree.

Pure state + helpers only. The actual spoken "your timer's up" announcement stays
in main.py, because it needs the async voice pipeline; here we just report, via
snapshot(), that a timer has elapsed so main.py can fire it.
"""
import threading
import time

_lock = threading.Lock()
# {"kind":"timer","end":<monotonic>,"total":<secs>} | {"kind":"stopwatch","start":<monotonic>} | None
_state: dict | None = None


def set_timer(seconds: int) -> int:
    global _state
    seconds = max(1, int(seconds))
    with _lock:
        _state = {"kind": "timer", "end": time.monotonic() + seconds, "total": seconds}
    return seconds


def start_stopwatch() -> None:
    global _state
    with _lock:
        _state = {"kind": "stopwatch", "start": time.monotonic()}


def stop() -> bool:
    """Cancel any running timer/stopwatch. Returns whether something was running."""
    global _state
    with _lock:
        was = _state is not None
        _state = None
    return was


def toggle_stopwatch() -> str:
    """HUD click on the round widget: start a stopwatch, or stop the running one.
    Returns 'started' | 'stopped' | 'busy' (a countdown timer is running)."""
    global _state
    with _lock:
        if _state and _state.get("kind") == "stopwatch":
            _state = None
            return "stopped"
        if _state is None:
            _state = {"kind": "stopwatch", "start": time.monotonic()}
            return "started"
        return "busy"


def snapshot() -> dict:
    """{'hud': <payload for STATE.timer>, 'fired': bool}. A due countdown clears
    itself and reports fired=True so main.py can announce it once."""
    global _state
    with _lock:
        t = _state
        if not t:
            return {"hud": {"kind": "none"}, "fired": False}
        now = time.monotonic()
        if t["kind"] == "timer":
            remaining = t["end"] - now
            if remaining <= 0:
                _state = None
                return {"hud": {"kind": "none"}, "fired": True}
            return {"hud": {"kind": "timer", "remaining": int(round(remaining)),
                            "total": t.get("total", 0)}, "fired": False}
        return {"hud": {"kind": "stopwatch", "elapsed": int(now - t["start"])},
                "fired": False}


def fmt_dur(secs: int) -> str:
    """A spoken duration: '20 minutes', '1 hour', '1 hour and 15 minutes'."""
    secs = int(secs)
    if secs >= 3600:
        h, m = secs // 3600, (secs % 3600) // 60
        s = f"{h} hour" + ("s" if h != 1 else "")
        return s + (f" and {m} minutes" if m else "")
    if secs >= 60:
        m, s = secs // 60, secs % 60
        out = f"{m} minute" + ("s" if m != 1 else "")
        return out + (f" and {s} seconds" if s else "")
    return f"{secs} second" + ("s" if secs != 1 else "")
