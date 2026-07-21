"""Clipboard ingestion: whatever the user last copied, available to the assistant.

Lets "explain this error" / "summarise this" refer to real copied text — the one
channel this assistant otherwise lacks, since everything else is voice or the
filesystem.

EVENT-DRIVEN, not polled. A hidden message-only window registers with
AddClipboardFormatListener and receives WM_CLIPBOARDUPDATE, so there is no timer
waking the CPU to compare strings. The listener runs on its own daemon thread
with its own message pump; nothing else in the process is touched.

Text only, by design. Images and file drops would each need their own handling
and neither is useful to a voice assistant.
"""
import ctypes
import logging
import threading
import time
from ctypes import wintypes

log = logging.getLogger("jarvis.clipboard")

WM_CLIPBOARDUPDATE = 0x031D
CF_UNICODETEXT = 13
HWND_MESSAGE = -3
MAX_CHARS = 4000          # far more than anyone dictates a question about

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

# ctypes defaults every return value to a 32-bit int. On 64-bit Windows that
# TRUNCATES handles and pointers, and the truncated HWND/pointer then causes an
# access violation deep in user32 — the process dies instantly with no Python
# traceback at all. These declarations are not optional.
_user32.CreateWindowExW.restype = wintypes.HWND
_user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
_user32.DefWindowProcW.restype = ctypes.c_longlong          # LRESULT
_user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                   wintypes.WPARAM, wintypes.LPARAM]
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.GetClipboardData.argtypes = [wintypes.UINT]
_user32.AddClipboardFormatListener.restype = wintypes.BOOL
_user32.AddClipboardFormatListener.argtypes = [wintypes.HWND]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
_kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
_kernel32.GetModuleHandleW.restype = wintypes.HMODULE
_kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]


def read_text() -> str:
    """Current clipboard text, or "" — opening the clipboard can legitimately
    fail when another process holds it, so this never raises."""
    for attempt in range(3):
        if _user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return ""
    try:
        if not _user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return ""
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return ""
        try:
            return ctypes.c_wchar_p(ptr).value or ""
        finally:
            _kernel32.GlobalUnlock(handle)
    except Exception as e:
        log.debug("clipboard read failed: %s", e)
        return ""
    finally:
        _user32.CloseClipboard()


class ClipboardWatcher:
    """Keeps the most recent clipboard TEXT, updated on change."""

    def __init__(self):
        self._text = ""
        self._at = 0.0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._hwnd = None

    # ── public ──────────────────────────────────────────────────
    @property
    def text(self) -> str:
        with self._lock:
            return self._text

    @property
    def age_seconds(self) -> float:
        with self._lock:
            return time.time() - self._at if self._at else float("inf")

    def snippet(self, limit: int = 600) -> str:
        t = self.text
        return t if len(t) <= limit else t[:limit] + " ..."

    def start(self) -> None:
        """Never allowed to take JARVIS down — a clipboard listener is a
        convenience, not a dependency."""
        if self._thread and self._thread.is_alive():
            return
        try:
            # Seed with whatever is already on the clipboard, so a reference to
            # "this" works immediately rather than only after the next copy.
            self._store(read_text())
            self._thread = threading.Thread(target=self._pump, name="clipboard",
                                            daemon=True)
            self._thread.start()
        except Exception as e:
            log.error("clipboard listener could not start (%s) — continuing "
                      "without it", e)

    # ── internals ───────────────────────────────────────────────
    def _store(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        if len(text) > MAX_CHARS:
            text = text[:MAX_CHARS]
        with self._lock:
            if text == self._text:
                return
            self._text = text
            self._at = time.time()
        log.info("clipboard: %d chars captured (%r...)", len(text), text[:48])

    def _pump(self) -> None:
        WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_longlong, wintypes.HWND,
                                     wintypes.UINT, wintypes.WPARAM,
                                     wintypes.LPARAM)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_CLIPBOARDUPDATE:
                self._store(read_text())
                return 0
            return _user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._proc = WNDPROC(wndproc)        # keep a ref or it is GC'd mid-use

        class WNDCLASS(ctypes.Structure):
            _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
                        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                        ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR)]

        try:
            wc = WNDCLASS()
            wc.lpfnWndProc = self._proc
            wc.hInstance = _kernel32.GetModuleHandleW(None)
            wc.lpszClassName = "JarvisClipboardListener"
            if not _user32.RegisterClassW(ctypes.byref(wc)):
                # Already registered (a restart in the same process) is fine.
                log.debug("clipboard window class already registered")
            self._hwnd = _user32.CreateWindowExW(
                0, wc.lpszClassName, None, 0, 0, 0, 0, 0,
                HWND_MESSAGE, None, wc.hInstance, None)     # message-only window
            if not self._hwnd:
                log.error("clipboard listener: could not create window")
                return
            if not _user32.AddClipboardFormatListener(self._hwnd):
                log.error("clipboard listener: AddClipboardFormatListener failed")
                return
            log.info("clipboard listener active (event-driven, no polling)")

            msg = wintypes.MSG()
            while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                _user32.TranslateMessage(ctypes.byref(msg))
                _user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log.error("clipboard listener died: %s", e)


WATCHER = ClipboardWatcher()
