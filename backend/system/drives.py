"""Live folder search on a named drive ("open PESU folder in D drive").

The pre-built index only covers the user profile on C:. When a command names a
drive explicitly, we walk that drive instead — bounded depth, skipping system and
hidden folders — and fuzzy-match the folder name. Results are cached briefly so
repeat requests are instant.
"""
import logging
import os
import re
import time
from pathlib import Path

from rapidfuzz import fuzz, process

from system import textnorm

log = logging.getLogger("jarvis.drives")

MAX_DEPTH = 5             # reach folders several levels down (D:\a\b\c\target)
CACHE_TTL = 300           # seconds — a drive's folder list rarely changes
MATCH_CUTOFF = 80         # stricter (was 72): a drive-walk match must be confident

# Never yield AND never descend — pure junk/noise.
SKIP_DIRS = {
    "$recycle.bin", "system volume information", "appdata",
    "node_modules", "__pycache__",
    ".git", "venv", ".venv", "env", "site-packages", "temp", "tmp", "cache",
    "recovery", "config.msi", "perflogs", "onedrivetemp",
}
# YIELD these (so "open the Windows folder" works) but DON'T descend into them —
# they're huge system trees and walking them is pointless and slow.
NO_DESCEND = {
    "windows", "program files", "program files (x86)", "programdata",
    "$windows.~bt", "$windows.~ws", "msocache", "intel", "amd", "nvidia",
}

# Recognise a drive letter in ANY natural phrasing, NOT just after a preposition.
# Matches: "d drive", "the d drive", "volume d drive", "volume d", "drive d",
# "in d drive", "on the d drive", "d:\...".  A bare letter must sit next to
# 'drive'/'volume'/':' so we don't grab a random 'd' out of ordinary speech.
_DRIVE_RE = re.compile(
    r"\b([c-z])\s+(?:drive|volume)\b"        # "d drive" / "d volume"
    r"|\b(?:drive|volume)\s+([c-z])\b"       # "drive d" / "volume d"
    r"|\b([c-z]):[\\/]?",                     # "d:" / "d:\" / "d:/"
    re.I)

_cache: dict[str, tuple[float, list[Path]]] = {}


def extract_drive(text: str) -> str | None:
    """The drive letter the user named, in any phrasing ('the volume D drive',
    'open D drive', 'in D:', 'drive D' -> 'D'). None if none present."""
    m = _DRIVE_RE.search(text or "")
    if not m:
        return None
    letter = next((g for g in m.groups() if g), "").upper()
    if not letter:
        return None
    root = Path(f"{letter}:\\")
    if not root.exists():
        log.info("drive %s: mentioned but not present", letter)
        return None
    return letter


def strip_drive_only(text: str) -> str:
    """Remove ONLY the drive phrase, keeping 'folder'/'the' for further parsing."""
    return re.sub(r"\s{2,}", " ", _DRIVE_RE.sub(" ", text or "")).strip(" .")


def strip_drive_phrase(text: str) -> str:
    """Remove the drive phrase AND container/locational filler so only the folder
    name is matched. 'windows folder present in volume C' -> 'windows' (the
    'present in' filler used to survive and wreck the match)."""
    cleaned = _DRIVE_RE.sub(" ", text or "")
    cleaned = re.sub(
        r"\b(folder|directory|drive|volume|open|launch|start|the|my|"
        r"present|located|sitting|inside|in|on|at|of|within)\b",
        " ", cleaned, flags=re.I)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def available_drives() -> list[str]:
    """Fixed drive letters that exist (C, D, ...)."""
    return [chr(c) for c in range(ord("C"), ord("Z") + 1)
            if Path(f"{chr(c)}:\\").exists()]


def find_folder_any_drive(name: str, cutoff: int = MATCH_CUTOFF) -> "Path | None":
    """Search the NON-system drives (D:, E:, ...) for a folder matching `name`,
    for 'open the PESU folder' when no drive was specified. C: is deliberately
    skipped — the C: user-profile index already covers it, and walking all of C:
    would be far too slow."""
    if not name:
        return None
    for letter in available_drives():
        if letter == "C":
            continue
        hit = find_folder(letter, name, cutoff)
        if hit is not None:
            return hit
    return None


def _walk(root: Path) -> list[Path]:
    """Folders on the drive, to a bounded depth, skipping system/hidden ones."""
    found: list[Path] = []
    root_depth = len(root.parts)
    for dirpath, dirnames, _files in os.walk(root, topdown=True):
        here = Path(dirpath)
        depth = len(here.parts) - root_depth
        if depth >= MAX_DEPTH:
            dirnames[:] = []
            continue
        keep = []
        for d in dirnames:
            dl = d.lower()
            if d.startswith(".") or dl in SKIP_DIRS:
                continue
            try:
                if (Path(dirpath) / d).stat().st_file_attributes & 0x2:   # HIDDEN
                    continue
            except (OSError, AttributeError):
                pass
            found.append(here / d)              # findable/openable
            if dl not in NO_DESCEND:
                keep.append(d)                  # ...but don't walk huge system trees
        dirnames[:] = keep
    return found


def _folders(letter: str) -> list[Path]:
    now = time.time()
    hit = _cache.get(letter)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    root = Path(f"{letter}:\\")
    t0 = time.time()
    folders = _walk(root)
    log.info("drive %s: indexed %d folders in %.1fs (depth<=%d)",
             letter, len(folders), time.time() - t0, MAX_DEPTH)
    _cache[letter] = (now, folders)
    return folders


def find_folder(letter: str, name: str, cutoff: int = MATCH_CUTOFF) -> Path | None:
    """Fuzzy-match a folder name on the given drive. Both the query and the folder
    names are run through the SAME normalisation (number words -> digits,
    'project2' -> 'project 2', hyphens -> spaces) so 'project two' reaches a
    folder literally named 'project2' and 'summer grind' reaches
    'summer-grind-2026'. A lower `cutoff` is used for the 'did you mean?' pass."""
    folders = _folders(letter)
    if not folders:
        return None
    q = textnorm.normalize(name)
    if not q:
        return None
    by_norm: dict[str, Path] = {}
    for p in folders:
        by_norm.setdefault(textnorm.normalize(p.name), p)   # first (shallowest) wins
    hit = process.extractOne(q, by_norm.keys(),
                             scorer=fuzz.WRatio, score_cutoff=cutoff)
    if not hit:
        log.info("drive %s: no folder matching %r", letter, name)
        return None
    matched = hit[0]
    # WRatio needle-in-word matches a short folder name buried in a longer query
    # ('flibbertigibbet' -> a stray 'Lib' folder at score 90). Require the query
    # and the folder name to be genuinely similar as WHOLE tokens, or one to be a
    # prefix of the other ('summer grind' -> 'summer grind 2026') — never a mid-
    # word substring.
    if fuzz.token_sort_ratio(q, matched) < 70 and not (
            matched.startswith(q) or q.startswith(matched)):
        log.info("drive %s: %r -> %r rejected (needle-in-word, not confident)",
                 letter, name, matched)
        return None
    log.info("drive %s: %r -> %s (%d)", letter, name, by_norm[matched], hit[1])
    return by_norm[matched]


# ── "open a PDF in <folder>" — filetype / name inside a specific folder ──
EXT_ALIASES = {
    "pdf": [".pdf"], "pdfs": [".pdf"],
    "word": [".docx", ".doc"], "doc": [".docx", ".doc"], "document": [".docx", ".doc"],
    # People call ANY spreadsheet an "excel sheet", and .csv is the one Excel
    # opens most often. Omitting it meant "open audit log excel sheet in <folder>"
    # failed outright on a real file named audit_log.csv: the extension filter
    # discarded the only candidate before the name was ever compared.
    "excel": [".xlsx", ".xls", ".xlsm", ".csv"],
    "spreadsheet": [".xlsx", ".xls", ".xlsm", ".csv"],
    "sheet": [".xlsx", ".xls", ".xlsm", ".csv"],
    "powerpoint": [".pptx", ".ppt"], "slides": [".pptx", ".ppt"],
    "ppt": [".pptx", ".ppt"], "presentation": [".pptx", ".ppt"],
    "image": [".png", ".jpg", ".jpeg", ".gif", ".webp"],
    "picture": [".png", ".jpg", ".jpeg", ".gif", ".webp"],
    "photo": [".png", ".jpg", ".jpeg"], "png": [".png"], "jpg": [".jpg", ".jpeg"],
    "video": [".mp4", ".mkv", ".avi", ".mov"], "movie": [".mp4", ".mkv", ".avi"],
    "song": [".mp3", ".flac", ".m4a", ".wav"], "music": [".mp3", ".flac", ".m4a"],
    "mp3": [".mp3"], "text": [".txt", ".md"], "code": [".py", ".js", ".ts", ".c", ".cpp"],
    "zip": [".zip", ".rar", ".7z"], "csv": [".csv"], "json": [".json"],
}

_FILLER = {"open", "launch", "a", "an", "any", "some", "the", "in", "my", "of",
           "file", "files", "document", "please", "for", "me"}


def parse_filetype_request(text: str) -> tuple[list[str] | None, str | None]:
    """From 'open a pdf' / 'the syllabus pdf' / 'any word document' ->
    (extensions, optional_name). Token-based so 'any' isn't split into 'a'+'ny'
    and 'word document' resolves the type without treating 'word' as a name."""
    tokens = re.findall(r"[\w']+", (text or "").lower())
    # the LAST token that names a filetype is the type
    type_idx = None
    for i, t in enumerate(tokens):
        if t.rstrip("s") in EXT_ALIASES or t in EXT_ALIASES:
            type_idx = i
    if type_idx is None:
        return None, None
    tok = tokens[type_idx]
    exts = EXT_ALIASES.get(tok) or EXT_ALIASES.get(tok.rstrip("s"))
    # the name is any meaningful words before the type token
    name_words = [t for t in tokens[:type_idx]
                  if t not in _FILLER and t not in EXT_ALIASES]
    name = " ".join(name_words) or None
    return exts, name


def _walk_files(folder: Path, exts: list[str] | None, max_depth: int = 4) -> list[Path]:
    """Files under `folder`, pruning node_modules/.git/etc and bounding depth —
    a raw rglob over a project folder with node_modules is unusably slow."""
    out: list[Path] = []
    root_depth = len(folder.parts)
    for dirpath, dirnames, filenames in os.walk(folder, topdown=True):
        here = Path(dirpath)
        if len(here.parts) - root_depth >= max_depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d.lower() not in SKIP_DIRS
                       and d.lower() not in ("node_modules", "dist", "build", "out")]
        for fn in filenames:
            if exts is None or Path(fn).suffix.lower() in exts:
                out.append(here / fn)
        if len(out) > 4000:          # safety cap
            break
    return out


def find_file_in_folder(folder: Path, exts: list[str] | None,
                        name: str | None) -> tuple[Path | None, list[Path]]:
    """Find a file inside a specific folder by type and/or name.

    Returns (best, candidates). If a name is given, fuzzy-match it. Otherwise pick
    the most recently modified file of the requested type. `candidates` lets the
    caller offer a choice when several very different files match.
    """
    if not folder.is_dir():
        return None, []
    files = _walk_files(folder, exts)
    if not files:
        return None, []

    if name:
        # Normalise BOTH sides exactly as the global index does. This path used
        # to only .lower(), so separators never lined up with speech:
        #   'audit_log'  vs spoken 'audit log'  -> 88.9 instead of 100
        #   'project2'   vs spoken 'project two'-> 73.7 instead of 100
        # None of those fell under the cutoff on their own, but they ranked a
        # correct file below a competitor, and it left this search behaving
        # differently from every other search in the app for no reason.
        by_name: dict[str, Path] = {}
        for p in files:
            by_name.setdefault(textnorm.normalize(p.stem), p)
        hit = process.extractOne(textnorm.normalize(name), by_name.keys(),
                                 scorer=fuzz.WRatio, score_cutoff=60)
        if hit:
            log.info("in-folder name %r -> %s (%d)", name, by_name[hit[0]], hit[1])
            return by_name[hit[0]], [by_name[hit[0]]]
        # name didn't match — fall through to most-recent of the type

    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0], files[:5]
