"""Resolve spoken names against the alias index and open them instantly."""
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from rapidfuzz import fuzz, process

import config
from system import drives, indexer, textnorm

log = logging.getLogger("jarvis.apps")

# System/UWP apps with no Start Menu shortcut. ShellExecute (os.startfile)
# resolves bare exe names via the App Paths registry, and ms-* URIs open
# the matching Settings page / Store app.
BUILTIN_APPS = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "paint": "mspaint.exe",
    "command prompt": "cmd.exe",
    "terminal": "wt.exe",
    "file explorer": "explorer.exe",
    "explorer": "explorer.exe",
    "task manager": "taskmgr.exe",
    "settings": "ms-settings:",
    "camera": "microsoft.windows.camera:",
    "snipping tool": "ms-screenclip:",
    "spotify": "spotify:",
}

_index: dict | None = None


def _get_index() -> dict:
    global _index
    if _index is None:
        _index = indexer.load_index()
    return _index


def rebuild() -> str:
    global _index
    _index = indexer.build_index()
    return (f"Index rebuilt. {len(_index['apps'])} apps, "
            f"{len(_index['files'])} files, and "
            f"{len(_index.get('folders', {}))} folders.")


_NOISE = re.compile(r"^(the|my|a|an|file|folder|directory|app|application)\s+|"
                    r"\s+(file|folder|directory|app|application)$", re.I)

MIN_SCORE = 70          # below this we admit we couldn't find it (was 66)
MIN_COVERAGE = 60       # of a MULTI-word request, at least this much must be in
                        # the name — 'project two' must not match on 'project'
                        # alone (was 45, which let one stray word through)
CONFIDENT = 72          # the winning match's own score (before bonuses) must clear
                        # this, or we fail honestly instead of opening a guess
FOLDER_EXACT = 92       # a folder only wins if the query is basically its name

# Words people pad speech with that never appear in a filename.
_STOPWORDS = {"the", "a", "an", "my", "me", "please", "file", "document", "doc",
              "for", "of", "on", "in", "that", "this", "one", "up", "and"}

# Bare pronouns are never a searchable name — "open it" must be resolved from
# conversation context (main.py), never fuzzy-matched (which once opened the
# 'iSCSI Initiator' admin tool for "open it").
_PRONOUNS = {"it", "that", "this", "them", "those", "these", "that one", "it again"}

# Words that mean the user wants a FILE, not an app. When present a file may
# outrank an app; when ABSENT a bare app-like name prefers the app. They are
# INTENT signals, not part of the name to match ('whatsapp photo' should find a
# file called 'WhatsApp Image ...' — 'photo' must not have to match a token).
_QUALIFIER_WORDS = {
    "photo", "photos", "image", "images", "picture", "pictures", "pic", "pics",
    "screenshot", "wallpaper", "file", "files", "document", "documents", "doc",
    "pdf", "video", "videos", "movie", "clip", "song", "track", "music", "audio",
    "note", "notes", "spreadsheet", "sheet", "slide", "slides", "presentation",
    "download", "downloads", "folder", "recording", "scan",
}
_FILE_QUALIFIER = re.compile(r"\b(" + "|".join(sorted(_QUALIFIER_WORDS)) + r")\b", re.I)

# Words that say "this is an APPLICATION" without being part of its name:
# "brave browser", "spotify app". Deliberately SEPARATE from _QUALIFIER_WORDS —
# those flip the preference toward files, which is the opposite of what these
# mean. Without this, "brave browser" scored 50% coverage against the alias
# "brave" and was rejected outright, while a bare "brave" resolved fine.
_APP_WORDS = {"browser", "app", "application", "program", "software", "tool"}


def _tokens(s: str) -> list[str]:
    return [t for t in s.split() if t]


def _coverage(query: str, alias: str) -> float:
    """Fraction of the SPOKEN words that appear somewhere in the alias, fuzzily
    and in any order. Directional on purpose: filenames carry noise nobody says
    out loud ('heat 1995 1080p bluray'), so query ⊆ alias scores 100 — but
    alias ⊆ query must not (else 'untitled design' matches a file just named
    'untitled'). Filler words are ignored, and near-misses ('scheduling' vs
    'scheduler') still count, so natural speech survives.
    """
    q_tokens = [t for t in _tokens(query)
                if t not in _STOPWORDS and t not in _QUALIFIER_WORDS
                and t not in _APP_WORDS] or _tokens(query)
    a_tokens = _tokens(alias)
    if not q_tokens or not a_tokens:
        return 0.0
    hits = 0.0
    for q in q_tokens:
        best = max((fuzz.ratio(q, a) for a in a_tokens), default=0)
        # A spoken word may be the START of a filename token ("report" ->
        # "reports"), but NOT buried mid-word: 'pesu' must not match inside
        # 'udppesumate'. So prefix-match only, never substring-match — and BOTH
        # tokens must be substantial, or a stray one-letter filename token ('h'
        # from an 'h 264' movie name) would swallow any word that starts with it
        # ('heat' -> 'h'). Whichever way the prefix runs, the shorter side is the
        # one that must be >= 4 chars.
        if any((len(q) >= 4 and a.startswith(q)) or (len(a) >= 4 and q.startswith(a))
               for a in a_tokens):
            best = max(best, 100)
        if best >= 82:
            hits += 1.0
        elif best >= 68:            # a mispronounced / synonym-ish word
            hits += 0.6
    return 100.0 * hits / len(q_tokens)


# Fraction of spoken content words that appear VERBATIM in the alias. This is
# the corroboration signal that a blended similarity score cannot provide.
#
# Measured over 210 real index aliases (clean / one-word-garbled / two-word-
# garbled / real STT slips / unrelated):
#     class      score min .. max      exact ratio (median)
#     clean      100.0 .. 100.0        1.00
#     garbled     80.0 .. 100.0        0.50
#     unrelated    0.0 ..   0.0        0.00
# Garbled input reaches 100.0 just as clean input does, so NO threshold on
# `score` can separate them — the distributions genuinely overlap at the top.
# What differs is corroboration: "python peso slides" has two of three words
# exactly right ("python", "slides") confirming the one slip, while "jockfrit"
# has nothing at all matching verbatim. So the high tier additionally requires
# that at least half the spoken words landed exactly.
EXACT_CORROBORATION = 0.5


def _exact_ratio(query: str, alias: str) -> float:
    """0..1 — how much of what was SAID appears verbatim in the matched name.

    Word BOUNDARIES are not mishearings. The index splits camelCase, so the app
    "WhatsApp" is aliased 'whats app' while the user says one word, "whatsapp" —
    token-for-token that scores 0 and would be demoted to "did you mean?" for a
    perfectly exact name. So a spoken word also counts as verbatim when it spans
    consecutive alias tokens, and an entire phrase counts when the two sides are
    identical once spacing is removed ('audit log' == 'auditlog').
    """
    a_list = _tokens(alias)
    a_tokens = set(a_list)
    q_tokens = [t for t in _tokens(query)
                if t not in _STOPWORDS and t not in _QUALIFIER_WORDS
                and t not in _APP_WORDS]
    if not q_tokens:                       # the whole phrase was filler
        q_tokens = _tokens(query)
    if not q_tokens or not a_tokens:
        return 0.0
    # Identical modulo spacing — an exact match by any reasonable reading.
    if "".join(q_tokens) == "".join(a_list):
        return 1.0
    # Concatenations of consecutive alias tokens ('whats'+'app' -> 'whatsapp').
    joined = set()
    for i in range(len(a_list)):
        acc = ""
        for j in range(i, min(i + 4, len(a_list))):
            acc += a_list[j]
            joined.add(acc)
    return sum(1 for t in q_tokens if t in a_tokens or t in joined) / len(q_tokens)


def _corroborated(query: str, alias: str) -> bool:
    """May this match ACT immediately, or must it ask 'did you mean...?'

    An exact alias hit always may. Otherwise enough of the spoken words must
    have landed verbatim to corroborate whatever was misheard."""
    if alias == query:
        return True
    return _exact_ratio(query, alias) >= EXACT_CORROBORATION


def _score(query: str, alias: str, *, score_cutoff: float | None = None, **_) -> float:
    """Forgiving fuzzy match for natural speech.

    Combines several rapidfuzz views so that missing words, extra filler words,
    a different word order, or a slightly wrong word all still land on the right
    file — while an unrelated request still scores far too low to match:
      * token_sort_ratio — same words, any order
      * partial_ratio    — the spoken phrase appears inside a longer filename
      * WRatio           — general similarity / typos
      * _coverage        — directional 'did I hear all their words in this name?'
    """
    cov = _coverage(query, alias) if len(query) >= 4 else 0.0

    # If most of what the user actually said isn't in this name, it isn't the
    # file — no matter how well one stray word happens to line up. Without this,
    # "quantum banana recipe" matched a long physics PDF purely on "quantum".
    # A SINGLE spoken word must still genuinely hit a whole token (prefix/typo),
    # never a mid-string substring ('flibbertigibbet' must not match 'lib').
    content = [t for t in _tokens(query)
               if t not in _STOPWORDS and t not in _QUALIFIER_WORDS]
    if content and cov < (MIN_COVERAGE if len(content) >= 2 else 50):
        return 0.0

    # NOTE: `cov` gates above but must NOT be part of this max().
    # _coverage answers "did I hear all their words in this name?" and awards a
    # token FULL credit at fuzz.ratio >= 82 — it is a completeness measure, not a
    # similarity one. Feeding it into the score meant an 82%-similar word became
    # a 100.0 match: "jockfrit" scored a perfect 100 against "jackfruit", so a
    # badly garbled name opened something directly instead of asking "did you
    # mean...?". It also saturated scores at 100 across many aliases, which is
    # what made the extractOne tie-break in _best_candidate so damaging.
    # Similarity now comes only from the ratio scorers; cov still gates.
    s = max(fuzz.WRatio(query, alias), fuzz.token_sort_ratio(query, alias))
    # partial_ratio ONLY as needle-in-haystack: the spoken phrase inside a longer
    # filename. It is not directional on its own, so without this guard a short
    # alias matches a long query — "my tax return from 1987" scored 100 against a
    # file called "1.pdf", and "untitled design" matched a file named "untitled".
    if len(query) >= 6 and len(alias) >= len(query):
        s = max(s, fuzz.partial_ratio(query, alias))
    return s if (score_cutoff is None or s >= score_cutoff) else 0.0


RESOLVE_PLAUSIBLE = 55       # middle "did you mean?" band (below CONFIDENT=72)

# ── On-demand fallback search ───────────────────────────────────
# The index is a snapshot: anything created since the last build is invisible,
# and the failure is indistinguishable from a matcher bug (exactly how the
# Iratta.mkv case was first misdiagnosed). So when the index yields NOTHING,
# do a bounded live walk before admitting defeat.
#
# Bounded three ways, because this runs inside a spoken turn:
#   * a wall-clock budget, checked between directories — the walk stops early
#     and returns the best it found rather than blowing the response time
#   * a depth cap, since user files are rarely 7+ levels down
#   * the same SKIP_DIRS pruning the indexer uses
# It only ever runs on an index MISS, so the common path is untouched.
#
# Crucially this produces candidates scored by the SAME _score() and hands them
# back through the SAME confidence gates in resolve()/resolve_tier(), so the
# 3-tier behaviour (act / "did you mean?" / honest fail) is identical whether a
# hit came from the index or from here — the fallback widens reach, never
# loosens the bar.
LIVE_BUDGET_S = 2.0
LIVE_MAX_DEPTH = 6


def _live_candidates(query: str, qualifier: bool) -> tuple[float, str, str, str] | None:
    """Best (raw_score, kind, alias, path) from a bounded live walk, or None."""
    deadline = time.monotonic() + LIVE_BUDGET_S
    best: tuple[float, str, str, str] | None = None
    seen_dirs = 0

    for root in config.INDEX_FOLDERS:
        if time.monotonic() > deadline:
            break
        try:
            root_depth = len(root.parts)
            for here, dirs, names in os.walk(root):
                seen_dirs += 1
                if seen_dirs % 64 == 0 and time.monotonic() > deadline:
                    dirs[:] = []
                    break
                hp = Path(here)
                if len(hp.parts) - root_depth >= LIVE_MAX_DEPTH:
                    dirs[:] = []
                    continue
                dirs[:] = [d for d in dirs
                           if d not in indexer.SKIP_DIRS and not d.startswith(".")]
                for d in dirs:                       # folders
                    alias = textnorm.normalize(d)
                    s = _score(query, alias)
                    if s and (best is None or s > best[0]):
                        best = (s, "folders", alias, str(hp / d))
                for n in names:                      # files
                    if Path(n).suffix.lower() not in indexer.FILE_EXTS:
                        continue
                    alias = textnorm.normalize(Path(n).stem)
                    s = _score(query, alias)
                    if s and (best is None or s > best[0]):
                        best = (s, "files", alias, str(hp / n))
        except (OSError, PermissionError):
            continue

    if best is None:
        return None
    # Same descriptive-noise guard the index path applies, so a live hit is not
    # held to a weaker standard than an indexed one.
    if best[1] == "files" and not qualifier and best[2] != query:
        core = textnorm.normalize(speakable_name(best[2]))
        if core and _coverage(core, query) < 55:
            return None
    log.info("live search hit %r -> %s (raw %.0f, %.2fs)", query, best[3], best[0],
             LIVE_BUDGET_S - max(0.0, deadline - time.monotonic()))
    return best


def _best_candidate(spoken: str):
    """(kind, alias, path, raw_score, exact, corroborated) for the best match in
    the index, ignoring the confidence gate. None only when nothing matched the
    per-kind floors at all. resolve()/resolve_tier() layer confidence on top."""
    idx = _get_index()
    raw_query = _NOISE.sub("", spoken.strip().lower()).strip()
    query = textnorm.normalize(raw_query)          # same shape as the index keys
    if not query or query in _PRONOUNS:
        return None
    qualifier = bool(_FILE_QUALIFIER.search(raw_query))   # user wants a file?

    candidates: list[tuple[float, float, str, str, str]] = []   # total, raw, ...
    for kind, bonus in (("builtin", 6.0), ("apps", 5.0), ("files", 4.0), ("folders", 0.0)):
        table = BUILTIN_APPS if kind == "builtin" else idx.get(kind, {})
        if not table:
            continue
        if kind == "folders":
            # A folder only wins when the query IS essentially its whole name
            # ("downloads"). Plain WRatio — no token-coverage — so a query that is
            # merely a word inside the folder's name ("python" vs "python
            # notebooks") does not beat the actual file called python.
            match = process.extractOne(query, table.keys(), scorer=fuzz.WRatio,
                                       score_cutoff=FOLDER_EXACT)
            if not match:
                continue
            bonus = 7.0
        elif query in table:
            # An exact alias hit must never be discarded before the +12 exact
            # bonus below can apply. extractOne collapses to ONE winner per kind
            # and ties are broken by dict order, so 'video project 1' (score 100,
            # an exact filename) lost to 'mpca project report 1' (also 100, and
            # earlier in the index) — opening a .docx for a request that named a
            # video, at "high" confidence. Take the exact hit directly.
            match = (query, 100.0)
        else:
            match = process.extractOne(query, table.keys(), scorer=_score,
                                       score_cutoff=MIN_SCORE)
            if not match:
                continue
        alias, raw = match[0], match[1]
        # A file that merely CONTAINS the spoken words — mostly tokens the user
        # never said ('project 2' inside 'sdn arp project main 2') — is not a
        # confident match for a bare name. Drop it so a real app wins, or we fail
        # honestly and try a folder search. A qualifier ('that whatsapp photo')
        # means they DID ask for a file, so descriptive filename noise is fine.
        # Measure coverage against the CORE title, not the raw filename — a scene
        # release ('the sheep detectives 2026 web dl 1080p h 264 ...') is mostly
        # encode junk nobody says, so comparing to the whole string wrongly
        # rejected saying just 'the sheep detectives'. speakable_name strips the
        # junk so the check reflects the real title.
        if kind == "files" and not qualifier and alias != query:
            core = textnorm.normalize(speakable_name(alias))
            if core and _coverage(core, query) < 55:
                continue
        total = raw + bonus
        # An exact alias hit is unambiguous — let it beat everything else.
        if alias == query:
            total += 12.0
        # App-vs-file priority: a bare app-like name prefers the real app; a
        # file-qualified request prefers the file.
        if not qualifier and kind in ("builtin", "apps"):
            total += 8.0
        elif qualifier and kind == "files":
            total += 8.0
        candidates.append((total, raw, kind, alias, table[alias]))

    if not candidates:
        # Index miss — try the bounded live walk before giving up. Returned in
        # the same shape, so resolve()/resolve_tier() apply their usual
        # confidence gates to it unchanged.
        live = _live_candidates(query, qualifier)
        if live is None:
            return None
        raw, kind, alias, path = live
        return kind, alias, path, raw, (alias == query), _corroborated(query, alias)

    total, raw, kind, alias, path = max(candidates)
    return kind, alias, path, raw, (alias == query), _corroborated(query, alias)


def resolve(spoken: str) -> tuple[str, str, str] | None:
    """The confident (kind, display_name, path) match, or None. A bare app-like
    name prefers the real APP; a confident FILE beats the folder that merely
    contains it; a folder only wins when the query is essentially its whole name.
    None below the confidence gate — the caller must never open a low-confidence
    guess (destructive callers demand this; opens get a 'did you mean' via
    resolve_tier instead)."""
    hit = _best_candidate(spoken)
    if hit is None:
        log.info("no match for %r", spoken)
        return None
    kind, alias, path, raw, exact, corroborated = hit
    if raw < CONFIDENT and not exact:
        log.info("best for %r is low-confidence [%s] %s (raw %.0f) — failing honestly",
                 spoken, kind, path, raw)
        return None
    # A destructive caller must never act on a name that only matched fuzzily:
    # "delete jockfrit" scoring 82 against 'jackfruit' is exactly the case where
    # a confirmation prompt is not enough, because the prompt would name the
    # WRONG file confidently. Uncorroborated -> no match at all.
    if not exact and not corroborated:
        log.info("best for %r is uncorroborated (%.0f%% of the spoken words are "
                 "verbatim in %r) — failing honestly",
                 spoken, 100 * _exact_ratio(textnorm.normalize(
                     _NOISE.sub("", spoken.strip().lower())), alias), alias)
        return None
    log.info("resolved %r -> [%s] %s (raw %.0f)", spoken, kind, path, raw)
    return kind, alias, path


def resolve_tier(spoken: str):
    """(kind, alias, path, tier) where tier is 'high' | 'plausible' | 'none'.
    Powers the 3-tier open flow: act on high, ask 'did you mean?' on plausible,
    fail on none. NOT for destructive actions — those use resolve() (high only)."""
    hit = _best_candidate(spoken)
    if hit is None:
        return None
    kind, alias, path, raw, exact, corroborated = hit
    if exact:
        tier = "high"
    elif raw >= CONFIDENT:
        # Score says confident, but only ACT if enough of the spoken words are
        # verbatim. A garbled name ("jockfrit" -> jackfruit) scores as high as a
        # clean one, so score alone would open it silently; demoting it to
        # "plausible" turns that into "Did you mean jackfruit, sir?" — which is
        # what the middle tier is for. A real STT slip with the rest of the
        # phrase intact ("python peso slides") stays high.
        tier = "high" if corroborated else "plausible"
    elif raw >= RESOLVE_PLAUSIBLE:
        tier = "plausible"
    else:
        tier = "none"
    return kind, alias, path, tier


NOT_FOUND = "Couldn't find that, sir. Check the name?"

# "<what> in [the] <folder> [folder] [in <drive> drive]"
_IN_FOLDER_RE = re.compile(
    r"^(?P<what>.+?)\s+in\s+(?:the\s+|my\s+)?(?P<folder>.+?)(?:\s+folder)?"
    r"(?:\s+in\s+.*)?$", re.I)


def _index_folder(name: str, cutoff: int = 80) -> "Path | None":
    """Find a folder by name in the pre-built index (C: user profile). Both sides
    normalised so 'screen recordings' matches a folder named 'Screen Recordings'."""
    idx = _get_index()
    folders = idx.get("folders", {})
    if not name or not folders:
        return None
    q = textnorm.normalize(name)
    hit = process.extractOne(q, {textnorm.normalize(k): k for k in folders}.keys(),
                             scorer=fuzz.WRatio, score_cutoff=cutoff)
    if not hit:
        return None
    matched = hit[0]
    # Same directional guard as drives.find_folder: WRatio matches a short name
    # buried in a longer/nonsense query ('xkcd nonsense zzz' -> 'icons'). Require
    # genuine whole-token similarity or a prefix, never a needle-in-word.
    if fuzz.token_sort_ratio(q, matched) < 70 and not (
            matched.startswith(q) or q.startswith(matched)):
        return None
    for k in folders:
        if textnorm.normalize(k) == matched:
            return Path(folders[k])
    return None


def _resolve_folder(name: str, letter: str | None, cutoff: int = 80) -> "Path | None":
    """Find a folder by name: on the named drive if given, else across the C:
    index and the other drives.

    With no drive named, an EXACT folder-name match must win wherever it lives:
    'open the pesu folder' should reach D:\\pesu, not the C: index's partial
    'PESUmate-main' that merely starts with 'pesu'. So we take an exact basename
    hit from either source first, and only fall back to a fuzzy one otherwise.
    A lower `cutoff` powers the 'did you mean?' pass for garbled folder names."""
    if not name:
        return None
    if letter:
        return drives.find_folder(letter, name, cutoff)
    want = textnorm.normalize(name)
    idx_hit = _index_folder(name, cutoff)
    if idx_hit is not None and textnorm.normalize(idx_hit.name) == want:
        return idx_hit
    any_hit = drives.find_folder_any_drive(name, cutoff)
    if any_hit is not None and textnorm.normalize(any_hit.name) == want:
        return any_hit
    return idx_hit or any_hit


def _clean_folder_name(text: str) -> str:
    """Strip command/container words to leave just the folder's name."""
    t = re.sub(r"\b(open|launch|start|the|my|a|an|folder|directory|drive|volume)\b",
               " ", text, flags=re.I)
    return re.sub(r"\s{2,}", " ", t).strip()


def resolve_in_folder(spoken: str):
    """Handle 'open a pdf in the pesu folder [in D drive]' — a file OF A TYPE or
    BY NAME inside a named folder. Resolves [drive] -> folder -> file.
    Returns (path, candidates) or (None, [])."""
    letter = drives.extract_drive(spoken)
    body = drives.strip_drive_only(spoken) if letter else spoken   # keep 'folder'/'the'
    m = _IN_FOLDER_RE.match(body.strip())
    if not m or not re.search(r"\bin\b", body, re.I):
        return None, []
    what, folder_name = m["what"].strip(), m["folder"].strip()
    folder_name = _clean_folder_name(folder_name) or folder_name

    folder = _resolve_folder(folder_name, letter)
    if folder is None:
        log.info("in-folder: no folder %r (drive=%s)", folder_name, letter)
        return None, []

    exts, name = drives.parse_filetype_request(what)
    if exts is None and name is None:
        name = re.sub(r"^(?:a|an|any|the|some)\s+", "", what, flags=re.I).strip() or None
    best, cands = drives.find_file_in_folder(folder, exts, name)
    log.info("in-folder: folder=%s type=%s name=%s -> %s", folder, exts, name, best)
    return best, cands

# Opening a film/video should start at an audible level, whatever the volume was
# left at beforehand (ducked, or turned down by a routine).
MEDIA_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".m4v", ".wmv", ".flv",
              ".mpg", ".mpeg", ".mp3", ".flac", ".m4a", ".wav", ".aac", ".ogg"}

# Release-scene junk to strip before SPEAKING a filename, so JARVIS says "Opening
# The Sheep Detectives" instead of reading out "…2026.WEB-DL.1080p.H.264.Hin.Eng.
# DD5.1.640Kbps.ESub.VegaMovies…". These are the tokens that mark where a real
# title ends and the encode metadata begins.
_SCENE_WORDS = {
    "webdl", "web", "webrip", "bluray", "brrip", "bdrip", "hdrip", "dvdrip",
    "dvdscr", "hdtv", "hdcam", "cam", "camrip", "ts", "hdts", "predvd", "hc",
    "x264", "x265", "h264", "h265", "hevc", "avc", "xvid", "divx",
    "aac", "ac3", "eac3", "dts", "dd", "ddp", "dd5", "ddp5", "truehd", "atmos",
    "10bit", "8bit", "hdr", "hdr10", "sdr", "dovi",
    "esub", "esubs", "msub", "msubs", "subs", "dual", "multi",
    "hin", "eng", "tam", "tel", "kan", "mal", "hindi", "english", "tamil",
    "vegamovies", "yts", "yify", "rarbg", "galaxyrg", "galaxytv", "psa",
    "ettv", "eztv", "fgt", "evo", "ion10", "rmteam", "tigole", "qxr",
    "nf", "amzn", "dsnp", "hmax", "hulu", "remastered", "proper", "repack",
}
_SCENE_RE = re.compile(r"^\d{3,4}p$|^4k$|^(x|h)\.?26[45]$|^\d+kbps$", re.I)
# speakable_name is called on BOTH raw filenames and already-normalised index
# aliases. textnorm.normalize splits letter/digit runs, so '1080p' arrives as
# '1080 p' and '.H.264' as 'h 264' — forms _SCENE_RE (built for raw filenames)
# never matches. Without these, the cut fell through to the first _SCENE_WORD
# and 'Iratta.2023.1080p.NF...' kept a core of 'iratta 2023 1080 p', whose
# coverage against a spoken 'iratta' was 25% — so the real file was discarded
# and "open iratta" found nothing.
_RES_RE = re.compile(r"^(240|360|480|540|576|720|1080|1440|2160|4320)$")
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")


def _is_scene_token(tok: str) -> bool:
    return (tok.lower() in _SCENE_WORDS or bool(_SCENE_RE.match(tok))
            or bool(_RES_RE.match(tok)))


def speakable_name(filename: str) -> str:
    """A clean, speakable version of a filename: dots/underscores become spaces
    and release-scene metadata is dropped, so only the core title is spoken. Left
    generic — it stops at the first encode-metadata token (walking back over a
    leading year), and returns the whole cleaned name when there's no such junk,
    so ordinary names ('Untitled design', 'OS PYQs') are untouched."""
    stem = filename
    if "." in filename and not filename.startswith("."):
        stem = filename.rsplit(".", 1)[0]          # drop the extension
    s = re.sub(r"[._\-]+", " ", stem)              # dots/underscores/dashes -> space
    toks = re.sub(r"\s+", " ", s).strip().split(" ")
    cut = next((i for i, t in enumerate(toks) if _is_scene_token(t)), None)
    if cut is None:
        return " ".join(toks).strip(" -") or filename
    if cut > 0 and _YEAR_RE.match(toks[cut - 1]):  # 'Title 2026 WEB-DL' -> 'Title'
        cut -= 1
    core = " ".join(toks[:cut]).strip(" -")
    return core or " ".join(toks).strip(" -") or filename

# Shell targets that are not filesystem paths: bare exe names resolved via the
# App Paths registry, ms-*/spotify: protocol URIs, and shell:AppsFolder tokens
# (UWP / Store apps launched by AppUserModelID).
_NON_PATH = re.compile(r"^[\w.-]+\.exe$|^[a-z][\w.-]*:", re.I)


def _launch(token: str) -> None:
    """Start an app/URI token. A UWP AppUserModelID must be launched through
    Explorer ('explorer.exe shell:AppsFolder\\<AUMID>'); everything else
    (.lnk, .exe, ms-*: / spotify: URIs, real paths) goes through ShellExecute."""
    if token.lower().startswith("shell:appsfolder"):
        subprocess.Popen(["explorer.exe", token],
                         creationflags=subprocess.CREATE_NO_WINDOW)
    else:
        os.startfile(token)


def _set_media_volume_if(path: str) -> None:
    if Path(path).suffix.lower() in MEDIA_EXTS:
        try:
            from system import volume
            volume.set_volume(config.MEDIA_VOLUME)
        except Exception as e:
            log.warning("media volume set failed: %s", e)


def _do_open(target, media_ok: bool = True) -> tuple[bool, str]:
    """os.startfile a real filesystem path (file/folder/drive), verify it existed,
    set media volume. Returns (succeeded, spoken_line)."""
    p = Path(target)
    if not p.exists():
        log.error("open target does not exist: %s", p)
        return False, NOT_FOUND
    try:
        os.startfile(str(p))
    except OSError as e:
        log.error("open %s FAILED: %s", p, e)
        return False, f"I found {p.name}, sir, but Windows wouldn't open it."
    if media_ok and p.is_file():
        _set_media_volume_if(str(p))
    log.info("OPENED %s", p)
    if p.is_file():
        disp = speakable_name(p.name)
    else:
        disp = p.name or f"the {p.drive.rstrip(':/')} drive"
    return True, f"Opening {disp}."


def _launch_resolved(kind: str, name: str, path: str) -> tuple[bool, str]:
    """Open a resolved app/file/folder path (handles .lnk/.exe/UWP tokens)."""
    if not _NON_PATH.match(path) and not Path(path).exists():
        log.error("open resolved to %s which does not exist", path)
        return False, NOT_FOUND
    try:
        _launch(path)
    except OSError as e:
        log.error("launch %s FAILED: %s", path, e)
        return False, f"I found {name}, sir, but Windows wouldn't open it."
    if kind == "files":
        _set_media_volume_if(path)
    log.info("OPENED [%s] %s -> %s", kind, name, path)
    disp = speakable_name(Path(path).name) if kind == "files" else name
    return True, f"Opening {disp}."


def open_resolved(path: str, kind: str | None = None, name: str | None = None) -> tuple[bool, str]:
    """Open an already-chosen target (used by the 'did you mean? -> yes' path)."""
    if (kind in ("apps", "builtin")) or _NON_PATH.match(path or ""):
        return _launch_resolved(kind or "apps", name or Path(path).name, path)
    return _do_open(path)


def _folder_display(p: "Path") -> str:
    return f"the {p.name} folder" if p.name else f"the {p.drive.rstrip(':/')} drive"


def _strict_folder(spoken_name: str, folder: "Path | None") -> "Path | None":
    """A folder is only a valid DESTRUCTIVE target if it exists AND the spoken
    name is corroborated — folder matching runs on drives.MATCH_CUTOFF rather
    than the CONFIDENT gate, so without this a garbled name reaches a real
    delete/move target through the back door."""
    if folder is None or not folder.exists():
        return None
    if not _corroborated(textnorm.normalize(spoken_name),
                         textnorm.normalize(folder.name)):
        log.info("strict resolve rejected %r -> %s (uncorroborated)",
                 spoken_name, folder)
        return None
    return folder


def resolve_target_strict(spoken: str) -> "Path | None":
    """Resolve a spoken file/folder description to a REAL, EXISTING path at HIGH
    confidence only — for DESTRUCTIVE operations (delete/move/rename). No 'did you
    mean' tolerance: if it isn't a confident match to something that exists on
    disk, return None so the caller refuses rather than acting on a guess or a
    hallucinated path. This is the ONLY resolver destructive tools may use."""
    # 1. "<file/type> in <folder>" — resolve_in_folder only returns real files.
    best, _cands = resolve_in_folder(spoken)
    if best is not None and Path(best).exists():
        return Path(best)

    letter = drives.extract_drive(spoken)
    wants_folder = bool(re.search(r"\b(folder|directory)\b", spoken, re.I))

    # 2. a named folder / drive.
    if wants_folder or letter:
        name = drives.strip_drive_phrase(spoken) if letter else _clean_folder_name(spoken)
        if letter and not name:
            p = Path(f"{letter}:\\")
            return p if p.exists() else None
        folder = _resolve_folder(name, letter)           # high confidence only
        return _strict_folder(name, folder)

    # 3. general — a confident file match that exists (never an app/URI).
    hit = resolve(spoken)                                 # CONFIDENT gate applied
    if hit is None:
        # NOTE: this fallback bypassed resolve()'s gates entirely — it went
        # straight to drives.MATCH_CUTOFF (80), so a garbled name that resolve()
        # had just rejected as uncorroborated came back through here anyway and
        # became a destructive target. _strict_folder re-applies the rule.
        cleaned = _clean_folder_name(spoken)
        return _strict_folder(cleaned, _resolve_folder(cleaned, None))
    kind, _name, path = hit
    if kind in ("apps", "builtin"):                       # an application, not a user file
        return None
    p = Path(path)
    return p if p.exists() else None


def open_target(spoken: str):
    """Log-wrapped entry point: EVERY open attempt records a definitive outcome.

    _open_target has ~8 return paths and several of them (the bare-folder branch,
    the '<type> in <folder>' branch) returned 'failed' without logging anything.
    A spoken "open jackfruit folder" therefore produced a spoken "couldn't find
    it" with no apps-level line in jarvis.log at all, which looked exactly like
    the request never reaching the matcher. Wrapping the whole function means no
    current or future branch can fail silently.
    """
    try:
        status, reply, extra = _open_target(spoken)
    except Exception as e:
        log.exception("OPEN ERROR %r -> %s", spoken, e)
        return "failed", NOT_FOUND, None
    if status == "opened":
        log.info("OPEN OK %r", spoken)
    elif status == "ask":
        log.info("OPEN ASK %r -> %s", spoken, reply)
    else:
        log.info("NOT OPENED %r -> %s", spoken, reply)
    return status, reply, extra


def _open_target(spoken: str):
    """Resolve and open an app/file/folder by spoken name — with a 3-tier
    confidence result. Returns (status, spoken_line, extra):
      * ('opened', 'Opening X.', None)          — a confident match, acted on
      * ('ask', 'Did you mean X, sir?', extra)  — plausible; extra opens it on yes
      * ('failed', reason, None)                — nothing close enough

    `succeeded`==opened is True ONLY when a real target existed AND the OS call
    returned. Decision order: (1) '<file/type> in <folder>', (2) a bare folder or
    drive root, (3) general app/file/folder resolution. NOTE: this middle 'ask'
    tier is for non-destructive opens only — destructive callers use resolve()."""
    if textnorm.normalize(_NOISE.sub("", spoken.strip().lower())) in _PRONOUNS:
        return "failed", "Open what, sir?", None

    letter = drives.extract_drive(spoken)
    wants_folder = bool(re.search(r"\b(folder|directory)\b", spoken, re.I))

    # ── 1. "<file or type> in <folder> [in <drive>]" ────────────
    best, cands = resolve_in_folder(spoken)
    if best is not None:
        ok, reply = _do_open(best)
        return ("opened" if ok else "failed"), reply, None
    if letter or wants_folder:
        exts, _ = drives.parse_filetype_request(spoken)
        if exts is not None and re.search(r"\bin\b", spoken, re.I):
            return "failed", "Couldn't find one of those in that folder, sir.", None

    # ── 2. a bare folder, or a drive root ("open D drive") ──────
    if wants_folder or letter:
        name = drives.strip_drive_phrase(spoken) if letter else _clean_folder_name(spoken)
        if letter and not name:
            ok, reply = _do_open(Path(f"{letter}:\\"))     # "open the D drive"
            return ("opened" if ok else "failed"), reply, None
        folder = _resolve_folder(name, letter)             # high confidence
        if folder is not None:
            # Folders resolve through drives.MATCH_CUTOFF, NOT through
            # resolve_tier, so the corroboration rule has to be applied here too
            # or the two paths disagree: "open jockfrit folder" would still open
            # jackfruit outright while "open jockfrit" asked first.
            if _corroborated(textnorm.normalize(name), textnorm.normalize(folder.name)):
                log.info("open [folder] %r -> %s", name, folder)
                ok, reply = _do_open(folder)
                return ("opened" if ok else "failed"), reply, None
            log.info("open [folder-uncorroborated] %r -> %s — asking first",
                     name, folder)
            return "ask", f"Did you mean {_folder_display(folder)}, sir?", \
                   {"path": str(folder)}
        maybe = _resolve_folder(name, letter, cutoff=RESOLVE_PLAUSIBLE)   # did you mean?
        if maybe is not None and maybe.exists():
            log.info("open [folder-maybe] %r -> %s", name, maybe)
            return "ask", f"Did you mean {_folder_display(maybe)}, sir?", {"path": str(maybe)}
        where = f"the {letter} drive" if letter else "your folders or drives"
        return "failed", f"Couldn't find {name or 'that folder'} on {where}, sir.", None

    # ── 3. general resolve (apps / files / index folders) ───────
    tier = resolve_tier(spoken)
    if tier is None or tier[3] == "none":
        # maybe it's a folder (C: index OR another drive) the general resolve
        # missed — try a confident folder match, then a 'did you mean?' one.
        cleaned = _clean_folder_name(spoken)
        folder = _resolve_folder(cleaned, None)             # high confidence
        if folder is not None:
            if not _corroborated(textnorm.normalize(cleaned),
                                 textnorm.normalize(folder.name)):
                log.info("open [folder-uncorroborated] %r -> %s — asking first",
                         cleaned, folder)
                return "ask", f"Did you mean {_folder_display(folder)}, sir?", \
                       {"path": str(folder)}
            ok, reply = _do_open(folder)
            return ("opened" if ok else "failed"), reply, None
        maybe = _resolve_folder(cleaned, None, cutoff=RESOLVE_PLAUSIBLE)
        if maybe is not None and maybe.exists():
            return "ask", f"Did you mean {_folder_display(maybe)}, sir?", {"path": str(maybe)}
        log.info("open %r -> NOT FOUND", spoken)
        return "failed", NOT_FOUND, None

    kind, name, path, conf = tier
    if not _NON_PATH.match(path) and not Path(path).exists():
        return "failed", NOT_FOUND, None
    if conf == "high":
        ok, reply = _launch_resolved(kind, name, path)
        return ("opened" if ok else "failed"), reply, None
    # plausible -> ask before acting
    disp = speakable_name(Path(path).name) if kind == "files" else name
    return "ask", f"Did you mean {disp}, sir?", {"path": path, "kind": kind, "name": name}
