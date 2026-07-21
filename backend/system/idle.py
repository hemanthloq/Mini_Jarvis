"""How long since the user last touched the keyboard or mouse.

Native Windows only: user32.GetLastInputInfo returns the tick count of the last
input event, system-wide. No polling of devices, no dependencies, no permissions.

Used for the proactive return greeting: JARVIS notices he has been away a while
and, on his return, mentions anything genuinely pending ONCE — rather than
staying purely reactive. The "once" matters, so the state machine here is
deliberately explicit: a greeting is armed only by a real absence, and firing it
disarms it until the next absence.
"""
import ctypes
import logging
import time
from ctypes import wintypes

import config

log = logging.getLogger("jarvis.idle")

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwTime", wintypes.DWORD)]


def idle_seconds() -> float:
    """Seconds since the last keyboard/mouse input, 0.0 if it can't be read."""
    try:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        # GetTickCount64 avoids the 49.7-day wrap of the 32-bit tick counter.
        now = _kernel32.GetTickCount64()
        return max(0.0, (now - info.dwTime) / 1000.0)
    except Exception as e:
        log.debug("idle check failed: %s", e)
        return 0.0


class ReturnWatcher:
    """Fires once when the user comes back after a real absence.

    away_after: seconds of no input that counts as "away".
    Call poll() on a timer; it returns the away-duration (seconds) exactly once
    per absence, on the first poll after input resumes, else None.
    """

    def __init__(self, away_after: float | None = None):
        self.away_after = float(
            away_after if away_after is not None
            else getattr(config, "IDLE_AWAY_SECONDS", 900))
        self._was_away = False
        self._peak_idle = 0.0

    def poll(self) -> float | None:
        idle = idle_seconds()
        if idle >= self.away_after:
            # Still away — remember how long, don't greet yet.
            self._was_away = True
            self._peak_idle = max(self._peak_idle, idle)
            return None
        if self._was_away:
            # Input has resumed after a genuine absence: greet ONCE.
            away_for = self._peak_idle
            self._was_away = False
            self._peak_idle = 0.0
            log.info("user returned after %.0f min away", away_for / 60)
            return away_for
        return None
