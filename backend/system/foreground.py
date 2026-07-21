"""Detect a fullscreen foreground app (a game, a film) so the HUD stays out of
the way — voice keeps working exactly the same, the overlay just doesn't pop up.
"""
import ctypes
import logging
import re
from ctypes import wintypes

import psutil

log = logging.getLogger("jarvis.foreground")

_user32 = ctypes.windll.user32

# Dedicated video/media players — their on-screen dialogue is the classic source
# of wake-word false triggers, so wake sensitivity is reduced while one is focused.
_MEDIA_PROCS = {
    "vlc.exe", "mpc-hc.exe", "mpc-hc64.exe", "mpc-be.exe", "mpc-be64.exe",
    "potplayer.exe", "potplayermini64.exe", "wmplayer.exe", "smplayer.exe",
    "mpv.exe", "video.ui.exe", "kmplayer.exe", "gomplayer.exe", "mpc-qt.exe",
}
_BROWSER_PROCS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe",
                  "opera.exe", "vivaldi.exe"}


def foreground_process_name() -> str:
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        return psutil.Process(pid.value).name().lower()
    except Exception as e:
        log.debug("foreground process check failed: %s", e)
        return ""


def media_app_active() -> bool:
    """A dedicated video/media player is focused, or a browser is fullscreen (a
    proxy for fullscreen video) — the moments a wake false-trigger from on-screen
    speech is most likely."""
    name = foreground_process_name()
    if name in _MEDIA_PROCS:
        return True
    if name in _BROWSER_PROCS and fullscreen_app_active():
        return True
    return False


# ── Focus monitor: what the user is actually doing right now ────
# Builds on foreground_process_name() above rather than duplicating it. Fed to
# the smart path as one short line of context so answers can be situationally
# aware ("pause it" while Spotify is focused means something different from
# "pause it" over a video).
_APP_NAMES = {
    "code.exe": "VS Code", "devenv.exe": "Visual Studio",
    "pycharm64.exe": "PyCharm", "idea64.exe": "IntelliJ",
    "windowsterminal.exe": "Windows Terminal", "cmd.exe": "Command Prompt",
    "powershell.exe": "PowerShell", "explorer.exe": "File Explorer",
    "spotify.exe": "Spotify", "vlc.exe": "VLC", "mpv.exe": "mpv",
    "chrome.exe": "Chrome", "msedge.exe": "Edge", "firefox.exe": "Firefox",
    "brave.exe": "Brave", "notepad.exe": "Notepad", "winword.exe": "Word",
    "excel.exe": "Excel", "powerpnt.exe": "PowerPoint", "olk.exe": "Outlook",
    "steam.exe": "Steam", "discord.exe": "Discord", "whatsapp.exe": "WhatsApp",
    "soundrec.exe": "Sound Recorder", "obs64.exe": "OBS",
}
# Titles that say nothing useful, or leak more than we want into a prompt.
_TITLE_NOISE = re.compile(r"^(program manager|windows input experience|)$", re.I)
MAX_TITLE = 70


def foreground_window_title() -> str:
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return ""
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        return (buf.value or "").strip()[:MAX_TITLE]
    except Exception as e:
        log.debug("window title read failed: %s", e)
        return ""


def activity_context() -> str:
    """One short line describing what's on screen, or "" if nothing useful.

    Deliberately terse and best-effort: it is injected into every smart-path
    turn, so it must never be long, never raise, and never be worth a round trip
    on its own."""
    try:
        proc = foreground_process_name()
        if not proc:
            return ""
        app = _APP_NAMES.get(proc) or proc[:-4] if proc.endswith(".exe") else proc
        title = foreground_window_title()
        if _TITLE_NOISE.match(title or ""):
            title = ""
        if title and title.lower() != app.lower():
            return f"{app} — {title}"
        return app
    except Exception as e:
        log.debug("activity_context failed: %s", e)
        return ""


class _RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


def fullscreen_app_active() -> bool:
    """True when the foreground window covers the whole primary screen and is not
    the desktop/shell itself."""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return False

        # The desktop and shell windows are 'fullscreen' but don't count.
        shell = _user32.GetShellWindow()
        desktop = _user32.GetDesktopWindow()
        if hwnd in (shell, desktop):
            return False

        cls = ctypes.create_unicode_buffer(256)
        _user32.GetClassNameW(hwnd, cls, 256)
        if cls.value in ("Progman", "WorkerW", "Shell_TrayWnd"):
            return False

        rect = _RECT()
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False

        sw = _user32.GetSystemMetrics(0)   # SM_CXSCREEN
        sh = _user32.GetSystemMetrics(1)   # SM_CYSCREEN
        covers = (rect.left <= 0 and rect.top <= 0
                  and rect.right >= sw and rect.bottom >= sh)
        return bool(covers)
    except Exception as e:
        log.debug("foreground check failed: %s", e)
        return False
