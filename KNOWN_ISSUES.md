# Known issues

Deliberately unfixed, with enough detail to pick up cold. Written 2026-07-19.

---

## 1. `extractOne` tie-breaking makes wrong matches look confident

**Where:** `backend/system/apps.py`, `_best_candidate()` — the
`process.extractOne(query, table.keys(), scorer=_score, score_cutoff=MIN_SCORE)`
call in the per-kind loop.

**The bug.** `_score` saturates at 100 for many different aliases, because
`_coverage` strips stopwords and `_QUALIFIER_WORDS` before comparing. Several
unrelated files therefore tie at exactly 100.0, and `extractOne` returns
whichever the dict happened to yield first. `_best_candidate` then collapses to
**one candidate per kind**, so every other tied alias — including the right one —
is gone before any of the ranking bonuses below it can run.

The result is not a near-miss. It is a *wrong file returned at `"high"`
confidence*, which defeats the 3-tier "act / did-you-mean / fail honestly"
design: `resolve_tier` reports `high` and the caller opens it without asking.

**Live examples on this machine (2026-07-19):**

| Spoken | Returns | Should be |
|---|---|---|
| `open screen recording` | `Downloads\SDN_ARP_screenshots.zip` | a `Videos\Screen Recordings\*.mp4` |
| `open video project` | `Downloads\LAA Mini Project Titles- G Section.xlsx` | `Videos\Video Project 1.mp4` |
| `my resume` | `...\Anaconda3\Reset Spyder Settings.lnk` | nothing (no resume file exists) |

Reproduce:

```python
from rapidfuzz import process
from system import apps
idx = apps._get_index()
process.extract(apps.textnorm.normalize("video project"), idx["files"].keys(),
                scorer=apps._score, score_cutoff=apps.MIN_SCORE, limit=6)
# -> several entries all at 100.0; extractOne picks by dict order
```

**Update 2026-07-19 (later): the saturation half is fixed.** `_score` was doing
`max(WRatio, token_sort_ratio, cov)`, and `_coverage` awards a token FULL credit
at `fuzz.ratio >= 82` — a completeness measure being used as a similarity score.
That is what pinned so many aliases at exactly 100.0 and made the tie-break bite.
`cov` now gates only. Immediate effect: `open screen recording` returns the real
`Screen Recording ....mp4` instead of `SDN_ARP_screenshots.zip` (one of the three
examples below), and garbled names now score their true 80-85 instead of a fake
100. The `extractOne`-picks-one-per-kind structure is still there, so the
tie-break can still bite where scores genuinely tie — but they now tie far less.

**RESOLVED 2026-07-19 (later still) — not by tuning a threshold.** Measured over
210 real index aliases, `score` genuinely cannot separate the classes: garbled
input reaches 100.0 exactly as clean input does (clean 100.0-100.0, garbled
80.0-100.0). No value of `CONFIDENT` splits them. What *does* separate them is
corroboration — how many spoken words landed VERBATIM (clean 1.00, garbled 0.50,
unrelated 0.00). So the high tier now additionally requires
`_exact_ratio >= EXACT_CORROBORATION (0.5)`; `CONFIDENT` and
`drives.MATCH_CUTOFF` were left alone. Effect on the 210-row corpus:
clean 90 high -> 90 high (untouched), unrelated 5 none -> 5 none (untouched),
garbled 111 high -> 66 high / 45 asking. Applied in `resolve_tier`, `resolve`,
BOTH folder branches of `_open_target`, and `resolve_target_strict`.

The historical table below is kept because it explains *why* a threshold was the
wrong instrument:

| spoken | intended | score | tier | wanted |
|---|---|---|---|---|
| `python peso slides` | `python pesu slides` | 94.4 | high | high (real STT slip) |
| `my stuf` | `mystuff` | 92.3 | high | plausible |
| `mistuff` | `mystuff` | 85.7 | high | plausible |
| `jockfrit` | `jackfruit` | 82.4 | high | plausible |
| `jackfroot` | `jackfruit` | 80.0 | high | plausible |

`CONFIDENT` is 72; the garbled band is 80-92 and a *legitimate* STT slip sits at
94.4 — the ranges interleave, which is exactly why the fix had to come from a
different signal rather than a threshold.

**Accepted residual, by design.** Garbling ONE word of a multi-word name still
acts immediately, because the remaining words corroborate it — that is the same
shape as the real STT slip `"python peso slides"` -> `python pesu slides`, which
must keep working. After filler/qualifier removal both reduce to *one verbatim
word + one misheard word* (ratio 0.50), so they are structurally
indistinguishable and are treated alike. `"untitled desine"` -> `Untitled
design.mp4` acting directly is this case, and it opens the right file. The
dangerous class — nothing verbatim at all (`jockfrit`, `my stuf`, `my resume`) —
is fully caught.

**Partially fixed already — do not re-diagnose that part.** The *exact-alias*
subclass is handled: `_best_candidate` now short-circuits when `query` is
literally a key in the table, so an exact filename can no longer be discarded
before the `alias == query` +12 bonus applies. That is why `open video project 1`
works. Drop the `1` and it breaks again, because `video project` is not an exact
alias and falls back into the tie-break path.

**Why it was left.** Any real fix changes ranking for *every* lookup — apps,
folders, files, builtins, and the destructive-action resolver (`resolve()`,
used by `delete_path`/`move_path`, which must never open a guess). It needs its
own session with a proper regression corpus, not a patch.

**Sketch of a fix, when someone takes it on:**
- Replace `extractOne` with `process.extract(..., limit=N)` and carry all
  near-tied candidates into the existing bonus/ranking stage, instead of
  pre-selecting one per kind.
- Add a deterministic tie-break — prefer the alias whose token count is closest
  to the query's, or highest *bidirectional* coverage, so
  `screen recording` prefers `screen recording 2026 ...` over `sdn arp screenshots`.
- Consider demoting a tie to `"plausible"` rather than `"high"`: if several
  files score identically, "did you mean X?" is the honest answer.
- Regression-guard the existing behaviours that these thresholds encode:
  `quantum banana recipe` → None, `my tax return from 1987` → None (must not hit
  `1.pdf`), bare `it` → None, `untitled design` must not match a file named
  `untitled`, and a bare app-like name must still prefer the real app over a file.

---

## 2. Barge-in during speech is bounded by the echo term, not the floor

**Update 2026-07-19 (later):** largely addressed — see the three fixes below.
Kept here because the *diagnosis* is the reusable part.

**Where:** `backend/audio/wake.py`, the `state in ("speaking", "thinking")` branch.

The calibrated-floor bug is **fixed** (room noise can no longer silently override
`BARGE_FLOOR`; see `NOISE_CEILING`). But while JARVIS is *speaking* the actual
trigger is `max(floor, expected_echo * ECHO_MARGIN)`. Observed live: `floor=0.0100`
but `echo=0.0082`, so the effective threshold was `0.0082 * 1.6 = 0.0131`.

This mic reads the user's speech at only ~0.015, so the working margin is ~15%.
`_echo_k` decays over a session (`max(self._echo_k * 0.995, rms / ref)`), so
barge-in should get easier the longer JARVIS runs — but a cold start is the
worst case. If "have to shout to interrupt" persists *only while JARVIS is
speaking* (and not while it is thinking, where `expected_echo = 0`), this echo
term is the thing to look at, not `BARGE_FLOOR`.

---

## 3. Time-of-day greeting is invented by the LLM ("good afternoon" in the evening)

**Reported 2026-07-19.** JARVIS greeted the user with "good afternoon" during the
evening.

**Cause (diagnosed, not fixed).** There is no time-of-day greeting logic to be
wrong — grep for `good afternoon` / `good morning` across `backend/` returns
nothing. The greeting is generated by the model: `_run_routine` in `main.py` calls
`smart_path.phrase("Greet the user and report the machine's condition.", facts)`
for any routine with `say_dynamic: system_health` (`daddy's home`, `wake up
buddy`), and the boot briefing takes the same path.

`backend/router/smart_path.py` never puts the current time in the prompt — it
contains no `strftime`, `datetime.now`, or equivalent. So the model has no idea
what time it is and picks a greeting at random. It will be wrong roughly two
thirds of the time, and inconsistently.

**Fix when picked up:** inject real local time into the system prompt (or into
the `phrase()` facts string) rather than adding greeting logic — the existing
`say_dynamic` contract is "the line is generated from REAL data, never invented",
and the time is exactly such a fact. Check whether `_status_report` and the boot
briefing need the same. Cheap and low-risk; it was deferred only to keep this
pass focused on the wake/barge gates.

---

## 4. Index staleness is silent

**Update 2026-07-19:** `INDEX_VERSION` is now 4 (registry-resolved user folders),
so the stale v3 index rebuilds itself once. The general point below still stands:
a file added *after* the last build is invisible with no warning.

`data/file_index.json` is only rebuilt on version bump or an explicit
"rebuild the index". A file downloaded after the last build simply cannot be
found, and the failure looks identical to a matcher bug — that is exactly how
issue 1's `Iratta.mkv` case was originally misdiagnosed. Consider a cheap
staleness check (mtime of the indexed roots) at boot.
