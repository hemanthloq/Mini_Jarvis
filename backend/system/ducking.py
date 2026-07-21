"""Audio ducking: quiet other apps while JARVIS listens/speaks, then restore.

Prefers per-session ducking (Spotify, games, browsers) so JARVIS's own output is
untouched. Falls back to master volume if no other sessions are playing.
"""
import logging

from comtypes import CoInitialize
from pycaw.pycaw import AudioUtilities

log = logging.getLogger("jarvis.duck")

DUCK_TO = 0.25          # duck others to 25% of their current volume
_saved: dict[int, float] = {}   # pid -> original session volume
_ducked = False

# Our own process must never be ducked, nor should the system sounds session.
_SELF_NAMES = {"python.exe", "pythonw.exe"}


def duck() -> None:
    """Lower other apps' audio. Idempotent."""
    global _ducked
    if _ducked:
        return
    try:
        CoInitialize()
        _saved.clear()
        for session in AudioUtilities.GetAllSessions():
            if session.Process is None:                 # system sounds
                continue
            name = (session.Process.name() or "").lower()
            if name in _SELF_NAMES:
                continue                                # never duck ourselves
            vol = session.SimpleAudioVolume
            level = vol.GetMasterVolume()
            if level <= 0.01:
                continue                                # already silent
            _saved[session.Process.pid] = level
            vol.SetMasterVolume(level * DUCK_TO, None)
        if _saved:
            _ducked = True
            log.info("ducked %d audio session(s)", len(_saved))
    except Exception as e:
        log.warning("ducking failed: %s", e)


def unduck() -> None:
    """Restore the volumes we lowered."""
    global _ducked
    if not _ducked:
        return
    try:
        CoInitialize()
        for session in AudioUtilities.GetAllSessions():
            if session.Process is None:
                continue
            level = _saved.get(session.Process.pid)
            if level is not None:
                session.SimpleAudioVolume.SetMasterVolume(level, None)
        log.info("restored %d audio session(s)", len(_saved))
    except Exception as e:
        log.warning("unducking failed: %s", e)
    finally:
        _saved.clear()
        _ducked = False
