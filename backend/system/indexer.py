"""Builds the local alias -> path index so "open my resume" never does a live
filesystem search. Run once at first launch, re-run on demand ("rebuild index").

Two sources:
  * Apps  — Start Menu .lnk shortcuts (system + per-user)
  * Files — user's Documents / Desktop / Downloads (common document types)
"""
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import config
from system import textnorm

log = logging.getLogger("jarvis.indexer")

# Bump when the index FORMAT or scanned ROOTS change so an old on-disk index is
# rebuilt, not loaded stale. v2: normalised aliases + UWP apps. v3: Videos/
# Pictures/Music roots (so 'Screen Recordings' etc. are indexed).
# v4: user folders resolved from the registry, so OneDrive-redirected
# Desktop/Documents/Pictures are indexed instead of the empty legacy ones.
# v5: non-system fixed drive roots indexed too; system/junk dirs pruned.
INDEX_VERSION = 5

FILE_EXTS = {
    # documents
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".ppt", ".pptx",
    ".txt", ".md", ".rtf", ".odt", ".epub",
    # images
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".heic",
    # video  (.mkv/.avi/.mov were missing — movies never got indexed)
    ".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv", ".mpg", ".mpeg",
    # audio
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma",
    # archives / installers / code
    ".zip", ".rar", ".7z", ".tar", ".gz", ".exe", ".msi",
    ".py", ".ipynb", ".html", ".json", ".js", ".ts", ".css", ".step", ".stl",
}
# Pruned everywhere. The system/recycle entries matter now that whole non-system
# drive roots are indexed (config._extra_drive_roots) — without them a drive walk
# wanders into per-volume system state that is never a user document.
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "AppData",
             "$RECYCLE.BIN", "$Recycle.Bin", "System Volume Information",
             "Windows", "Program Files", "Program Files (x86)", "ProgramData",
             "OneDriveTemp", "PerfLogs", "Recovery", "site-packages", ".cache"}

START_MENUS = [
    Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs",
    Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs",
]

_SKIP_APP_WORDS = re.compile(r"uninstall|readme|website|documentation|help", re.I)


def _clean(name: str) -> str:
    """Normalize a filename into a speakable alias — the SAME normalisation the
    spoken query gets (number words -> digits, 'project2' -> 'project 2'), so the
    two can actually line up at match time."""
    return textnorm.normalize(name)


def _uwp_apps() -> dict[str, str]:
    """UWP / Microsoft Store apps (WhatsApp, Spotify, etc.) have NO Start-Menu
    .lnk, so the file loop never sees them — which is why 'open WhatsApp' used to
    fall through to a WhatsApp *image*. Get-StartApps lists them with their
    AppUserModelID; we launch those via 'shell:AppsFolder\\<AUMID>'."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=20,
            creationflags=subprocess.CREATE_NO_WINDOW).stdout
        data = json.loads(out) if out.strip() else []
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as e:
        log.warning("Get-StartApps failed (%s) — UWP apps won't be indexed", e)
        return {}
    apps: dict[str, str] = {}
    for entry in (data if isinstance(data, list) else [data]):
        name, appid = entry.get("Name"), entry.get("AppID")
        # Skip web-link "apps" (e.g. 'Steam Support Center' -> an http AppID):
        # those aren't launchable via shell:AppsFolder and aren't real apps.
        if not name or not appid or appid.lower().startswith(("http", "mailto")):
            continue
        apps.setdefault(_clean(name), f"shell:AppsFolder\\{appid}")
    log.info("indexed %d Start apps (incl. UWP) via Get-StartApps", len(apps))
    return apps


def build_index() -> dict:
    apps: dict[str, str] = {}
    files: dict[str, str] = {}
    folders: dict[str, str] = {}

    for menu in START_MENUS:
        if not menu.exists():
            continue
        for lnk in menu.rglob("*.lnk"):
            alias = _clean(lnk.stem)
            if not alias or _SKIP_APP_WORDS.search(alias):
                continue
            apps.setdefault(alias, str(lnk))

    # UWP / Store apps that have no .lnk. .lnk entries win on overlap (they carry
    # a real file path); this only fills the gaps (WhatsApp, Spotify, ...).
    for alias, token in _uwp_apps().items():
        apps.setdefault(alias, token)

    for folder in config.INDEX_FOLDERS:
        if not folder.exists():
            continue
        # The root itself — but a drive root ("D:\\") has an EMPTY .name, and an
        # empty alias key would sit in the table matching nothing sensibly.
        root_alias = _clean(folder.name)
        if root_alias:
            folders.setdefault(root_alias, str(folder))
        for root, dirs, names in os.walk(folder):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            if Path(root) == folder:                          # first-level subfolders
                for d in dirs:
                    folders.setdefault(_clean(d), str(folder / d))
            for n in names:
                p = Path(root) / n
                if p.suffix.lower() in FILE_EXTS:
                    files.setdefault(_clean(p.stem), str(p))

    # Standard user folders, always available even if not in INDEX_FOLDERS
    for name in ("Downloads", "Documents", "Desktop", "Pictures", "Music", "Videos"):
        p = Path.home() / name
        if p.exists():
            folders.setdefault(name.lower(), str(p))

    index = {"version": INDEX_VERSION, "built_at": time.time(),
             "apps": apps, "files": files, "folders": folders}
    config.INDEX_FILE.write_text(json.dumps(index, indent=1), encoding="utf-8")
    log.info("index built: %d apps, %d files, %d folders",
             len(apps), len(files), len(folders))
    return index


def load_index() -> dict:
    if config.INDEX_FILE.exists():
        try:
            idx = json.loads(config.INDEX_FILE.read_text(encoding="utf-8"))
            if idx.get("version") == INDEX_VERSION:
                return idx
            log.info("index is old format (v%s != v%s) — rebuilding",
                     idx.get("version"), INDEX_VERSION)
        except json.JSONDecodeError:
            pass
    return build_index()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    idx = build_index()
    print(f"apps:  {len(idx['apps'])}")
    print(f"files: {len(idx['files'])}")
