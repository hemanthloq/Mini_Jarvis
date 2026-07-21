"""Start and stop a real recording in the Windows Sound Recorder app.

Why this is not the WhatsApp automation all over again
------------------------------------------------------
The removed WhatsApp feature needed to walk a fragile UIA tree to *find a
contact* — the tree shape varied per build and it was slow and unreliable.
This needs only two things: focus the window, and press Ctrl+R. No tree
traversal is required to perform the action.

UIA is used for VERIFICATION ONLY, and only for one element. Sound Recorder's
buttons are always present ("Stop recording" and "Pause" exist even when idle),
so button labels prove nothing. What does prove it is the app's own elapsed-time
element: while recording it ADVANCES. So `is_recording()` samples it twice and
compares. JARVIS therefore never claims to be recording on the strength of a
keystroke it merely sent — same standard as every other action here.
"""
import logging
import re
import subprocess
import time
from pathlib import Path

log = logging.getLogger("jarvis.recorder")

AUMID = "Microsoft.WindowsSoundRecorder_8wekyb3d8bbwe!App"
_TITLE_RE = r".*Sound Recorder.*"
_ELAPSED_RE = re.compile(r"current time.*?(\d+)\s*hour.*?(\d+)\s*minute.*?"
                         r"(\d+)\s*second.*?(\d+)\s*centisecond", re.I)


def recordings_dir() -> Path | None:
    """Where Sound Recorder saves — honours OneDrive redirection of Documents."""
    home = Path.home()
    for p in (home / "OneDrive" / "Documents" / "Sound recordings",
              home / "Documents" / "Sound recordings",
              home / "Music" / "Sound recordings"):
        if p.exists():
            return p
    return None


def _newest(folder: Path | None) -> tuple[Path | None, float]:
    if folder is None or not folder.exists():
        return None, 0.0
    files = [p for p in folder.iterdir() if p.is_file()]
    if not files:
        return None, 0.0
    newest = max(files, key=lambda p: p.stat().st_mtime)
    return newest, newest.stat().st_mtime


def _window(launch: bool = True, timeout: float = 12.0):
    """The Sound Recorder window, launching the app if needed. None on failure."""
    from pywinauto import Desktop
    try:
        win = Desktop(backend="uia").window(title_re=_TITLE_RE)
        if win.exists():
            return win
    except Exception:
        pass
    if not launch:
        return None
    try:
        subprocess.run(["explorer.exe", f"shell:AppsFolder\\{AUMID}"], check=False)
    except OSError as e:
        log.error("could not launch Sound Recorder: %s", e)
        return None
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            win = Desktop(backend="uia").window(title_re=_TITLE_RE)
            if win.exists():
                win.wait("visible", timeout=2)
                return win
        except Exception:
            time.sleep(0.4)
    log.error("Sound Recorder window never appeared")
    return None


def _buttons(win) -> dict[str, object]:
    """Every named Button in the window, keyed by lowercase label."""
    out: dict[str, object] = {}
    try:
        for c in win.descendants():
            try:
                if c.element_info.control_type != "Button":
                    continue
                t = (c.window_text() or "").strip().lower()
                if t:
                    out.setdefault(t, c)
            except Exception:
                continue
    except Exception as e:
        log.debug("button scan failed: %s", e)
    return out


def is_recording(win) -> bool:
    """True while a recording is actually running.

    Measured on this build, the button set is the honest signal:
        idle       -> 'Start recording', 'Play'
        recording  -> 'Stop recording', 'Pause', 'Mark recording'
    An advancing elapsed-timer is NOT reliable — it keeps advancing after a
    recording is saved, so a timer-based check reported "recording" when the app
    was idle."""
    b = _buttons(win)
    return "stop recording" in b and "start recording" not in b


def start() -> tuple[bool, str]:
    """Open Sound Recorder and actually begin recording. (ok, spoken_line)."""
    win = _window()
    if win is None:
        return False, "I couldn't open the sound recorder, sir."
    try:
        win.set_focus()
    except Exception as e:
        log.warning("could not focus Sound Recorder: %s", e)
        return False, "I opened the recorder but couldn't focus it, sir."
    time.sleep(0.8)

    if is_recording(win):
        return True, "It's already recording, sir."

    btn = _buttons(win).get("start recording")
    if btn is None:
        return False, "I opened the recorder but couldn't find its record button, sir."
    try:
        btn.click_input()
    except Exception as e:
        log.error("could not press Start recording: %s", e)
        return False, "I opened the recorder but couldn't start it, sir."

    time.sleep(1.2)
    if is_recording(win):
        log.info("recording STARTED (verified: Stop button present, Start gone)")
        return True, "Recording now, sir."
    log.warning("pressed Start but the UI never entered the recording state")
    return False, "I opened the recorder, sir, but couldn't start the recording."


def stop() -> tuple[bool, str]:
    """Stop a running recording and confirm a file was actually written."""
    win = _window(launch=False)
    if win is None:
        return False, "The sound recorder isn't open, sir."
    folder = recordings_dir()
    _before, before_m = _newest(folder)
    try:
        win.set_focus()
        time.sleep(0.5)
        if not is_recording(win):
            return False, "It isn't recording, sir."
        # Press the real Stop button. Ctrl+R must NOT be used here: on this build
        # it does not toggle — it saves the current clip and immediately starts a
        # NEW recording, which silently left the microphone running.
        btn = _buttons(win).get("stop recording")
        if btn is None:
            return False, "I couldn't find the stop button, sir."
        btn.click_input()
    except Exception as e:
        log.error("stop failed: %s", e)
        return False, "I couldn't stop the recording, sir."

    # Saving is not instant — wait for a genuinely NEWER file to appear.
    deadline = time.monotonic() + 8.0
    saved = None
    while time.monotonic() < deadline:
        newest, m = _newest(folder)
        if newest is not None and m > before_m:
            saved = newest
            break
        time.sleep(0.5)

    # Never report success while it is somehow still running.
    if is_recording(win):
        log.error("pressed Stop but the app is STILL recording")
        return False, "I couldn't stop the recording, sir — it's still running."
    if saved is None:
        log.warning("stopped but no new file appeared in %s", folder)
        return False, "I stopped it, sir, but couldn't confirm it saved."
    log.info("recording SAVED: %s", saved)
    return True, f"Saved it, sir. {saved.stem}."
