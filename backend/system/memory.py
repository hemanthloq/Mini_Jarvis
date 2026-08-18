"""Long-term memory: durable facts about the user, kept across restarts.

Unlike the smart-path conversation buffer (which holds only the last few turns and
is wiped on restart), this is a small, persistent list of things worth remembering
— the user's name, preferences, ongoing projects, "I don't like X". It is injected
into every smart-path turn so JARVIS actually knows who it's talking to, and the
model writes to it via the `remember` / `forget` tools.

Stored in data/memory.json (git-ignored — it's personal). Deliberately capped, so
it stays a tight profile rather than an ever-growing log that bloats every prompt.
"""
import json
import logging
import time

import config

log = logging.getLogger("jarvis.memory")

MEMORY_FILE = config.DATA_DIR / "memory.json"
MAX_FACTS = 60          # keep the profile tight; oldest dropped past this
MAX_FACT_LEN = 200      # a "fact" is a sentence, not an essay


def _load() -> list[dict]:
    if not MEMORY_FILE.exists():
        return []
    try:
        data = json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(items: list[dict]) -> None:
    MEMORY_FILE.write_text(json.dumps(items, indent=1, ensure_ascii=False),
                           encoding="utf-8")


def add(fact: str) -> str:
    """Store one durable fact. Near-duplicates are ignored so the same thing said
    twice doesn't pile up."""
    fact = (fact or "").strip()[:MAX_FACT_LEN]
    if not fact:
        return "Nothing to remember, sir."
    items = _load()
    low = fact.lower()
    for it in items:
        if it["text"].lower() == low:
            return "Already noted, sir."
    items.append({"text": fact, "created": time.time()})
    del items[:-MAX_FACTS]                     # keep only the most recent MAX_FACTS
    _save(items)
    log.info("remembered: %r (%d total)", fact[:80], len(items))
    return f"Noted, sir: {fact}"


def forget(topic: str) -> str:
    """Drop every remembered fact that mentions `topic` (case-insensitive)."""
    topic = (topic or "").strip().lower()
    if not topic:
        return "Forget what, sir?"
    items = _load()
    kept = [it for it in items if topic not in it["text"].lower()]
    removed = len(items) - len(kept)
    if not removed:
        return f"I had nothing about {topic}, sir."
    _save(kept)
    log.info("forgot %d fact(s) matching %r", removed, topic)
    return f"Forgotten, sir — dropped {removed} note{'s' if removed != 1 else ''}."


def all_facts() -> list[str]:
    return [it["text"] for it in _load()]


def context_block() -> str:
    """The memory as a prompt fragment, or '' when empty. Injected every turn."""
    facts = all_facts()
    if not facts:
        return ""
    lines = "\n".join(f"- {f}" for f in facts)
    return ("\nWHAT YOU KNOW ABOUT THE USER (your long-term memory — use it "
            "naturally, don't recite it):\n" + lines + "\n")
