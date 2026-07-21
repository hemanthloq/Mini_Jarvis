"""Run the guardrail corpus in tests/guardrails.json.

    .venv\\Scripts\\python.exe scripts\\run_guardrails.py
    .venv\\Scripts\\python.exe scripts\\run_guardrails.py -v     (show every case)

Every case here was found by hand during live testing, and several were real
shipped bugs. Run this before and after touching system/apps.py, system/drives.py,
system/indexer.py, the confidence thresholds, or the speech-hygiene filters.

Cases tagged "machine_specific" depend on files that exist on the developer's PC.
They SKIP (not fail) when the file is gone, so the corpus stays useful on another
machine — a deleted film should not read as a matcher regression.
"""
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from router import fast_path, routines, smart_path      # noqa: E402
from system import apps                                 # noqa: E402

VERBOSE = "-v" in sys.argv
CORPUS = json.loads((ROOT / "tests" / "guardrails.json").read_text(encoding="utf-8"))

passed = failed = skipped = 0
failures: list[str] = []


def check(name: str, ok: bool, detail: str, skip: bool = False) -> None:
    global passed, failed, skipped
    if skip:
        skipped += 1
        if VERBOSE:
            print(f"  skip {name}: {detail}")
        return
    if ok:
        passed += 1
        if VERBOSE:
            print(f"  ok   {name}")
    else:
        failed += 1
        failures.append(f"{name}: {detail}")
        print(f"  FAIL {name}: {detail}")


print("resolve_tier")
for c in CORPUS["resolve_tier"]:
    r = apps.resolve_tier(c["say"])
    tier = "none" if r is None else r[3]
    path = "" if r is None else r[2]
    # A machine-specific case whose file has vanished is not a regression.
    if c.get("machine_specific") and c["tier"] != "none" and r is None:
        check(c["say"], True, "target no longer on this machine", skip=True)
        continue
    ok = tier == c["tier"]
    if ok and c.get("contains"):
        ok = c["contains"].lower() in path.lower()
    check(c["say"], ok, f"want {c['tier']}{'/' + c['contains'] if c.get('contains') else ''}, "
                        f"got {tier} {path[:60]}")

print("destructive_strict")
for c in CORPUS["destructive_strict"]:
    p = apps.resolve_target_strict(c["say"])
    if c.get("machine_specific") and c["expect"] == "path" and p is None:
        check(c["say"], True, "target no longer on this machine", skip=True)
        continue
    ok = (p is not None) if c["expect"] == "path" else (p is None)
    check(c["say"], ok, f"want {c['expect']}, got {p}")

print("needs_tools")
for c in CORPUS["needs_tools"]:
    got = smart_path.needs_tools(c["say"])
    check(c["say"], got == c["expect"], f"want {c['expect']}, got {got}")

print("routines")
for c in CORPUS["routines"]:
    m = routines.match(c["say"])
    got = m.trigger if m else None
    check(c["say"][:44], got == c["expect"], f"want {c['expect']!r}, got {got!r}")

print("fast_path_no_shadow  (regex only - handlers are NOT executed)")
# Matched against normalize()d text, exactly as try_match does — writing these
# against raw speech is what let "what's my next class" fall through to the
# media "next" handler unnoticed.
_pats = [(p, fn) for p, fn in getattr(fast_path, "_PATTERNS", [])]
for c in CORPUS["fast_path_no_shadow"]:
    norm = fast_path.normalize(c["say"])
    hit = None
    for pat, fn in _pats:
        if pat.match(norm):
            hit = getattr(fn, "__name__", "?").lstrip("_")
            break
    if hit is None:
        # Mirror try_match's FIXED fallback INCLUDING the length guard, so a
        # short command key can't silently swallow a long question again.
        from rapidfuzz import fuzz, process               # noqa: E402
        words = len(norm.split())
        for cand, _score, _i in process.extract(
                norm, fast_path.FIXED.keys(), scorer=fuzz.WRatio,
                score_cutoff=fast_path.FUZZ_THRESHOLD, limit=8):
            n = len(cand.split())
            if words > n + 2 and words > n * 1.6:
                continue
            hit = f"FIXED:{cand}"
            break
    want = c["command_prefix"]
    if want is None:
        # Must not be captured by any feature pattern (media/FIXED may handle it).
        ok = hit is None or not any(k in hit for k in ("record", "class", "free",
                                                       "timetable", "today", "pesu",
                                                       "maps_route", "native_maps"))
    else:
        ok = hit is not None and (want in hit or
                                  (want == "timetable" and
                                   any(k in hit for k in ("class", "free",
                                                          "timetable", "today"))))
    check(c["say"], ok, f"want {want!r}, normalized {norm!r}, matched {hit!r}")

print("timetable_phrasing  (clock pinned, so results are deterministic)")
from system import timetable as _tt                          # noqa: E402
_real_now = _tt._now
try:
    for c in CORPUS["timetable_phrasing"]:
        hh, mm = (int(x) for x in c["at"].split(":"))
        _tt._now = lambda d=c["day"], t=hh * 60 + mm: (d, t)
        said = _tt.describe_next() if c["fn"] == "next" else _tt.describe_free()
        missing = [s for s in c.get("must_contain", []) if s not in said]
        present = [s for s in c.get("must_not_contain", []) if s in said]
        check(f"{c['day']} {c['at']} {c['fn']}", not missing and not present,
              f"missing {missing}, unwanted {present} -> {said!r}")
finally:
    _tt._now = _real_now

print("clipboard_attach  (regex only - the watcher is seeded, not started)")
smart_path.tools  # noqa: B018  (ensure module import side effects are done)
from system import clipboard as _clip                       # noqa: E402
if not _clip.WATCHER.text:
    _clip.WATCHER._store("test clipboard contents for the guardrail run")
for c in CORPUS["clipboard_attach"]:
    got = bool(smart_path._clipboard_context(c["say"]))
    check(c["say"], got == c["expect"], f"want attach={c['expect']}, got {got}")

print("speech_hygiene")
for c in CORPUS["speech_hygiene"]:
    clean, had = smart_path._strip_tool_markup(c["text"])
    ok = had == c["expect_stripped"] and (c["expect_stripped"] or clean == c["text"])
    check(c["text"][:44], ok, f"want stripped={c['expect_stripped']}, got {had} -> {clean[:50]!r}")

print("code_never_spoken")
for c in CORPUS["code_never_spoken"]:
    prose, blocks = smart_path._split_code(c["text"])
    ok = len(blocks) == c["expect_blocks"]
    if ok and c.get("must_not_speak"):
        ok = c["must_not_speak"] not in prose
    check(c["text"][:44], ok, f"want {c['expect_blocks']} block(s), got {len(blocks)}; "
                              f"spoken={prose[:50]!r}")

print(f"\n{passed} passed, {failed} failed, {skipped} skipped")
if failures:
    print("\nfailures:")
    for f in failures:
        print("  -", f)
sys.exit(1 if failed else 0)
