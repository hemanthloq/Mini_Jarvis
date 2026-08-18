"""Semantic file search — find a file by MEANING, not exact name.

The alias index matches lexically (rapidfuzz), so "find that PDF about graphs"
never surfaces "DSA_TopicWise_Notes". This embeds every index alias with Gemini
and ranks by cosine similarity, so meaning matches.

Embeddings are cached to data/file_embeddings.json keyed by a hash of the alias
set, so the slow, API-heavy build runs once and only rebuilds when the file index
changes. main.py warms it in the background at startup so the first query is fast.
Requires a Gemini key; degrades to a clear message without one.
"""
import hashlib
import json
import logging
import threading

import numpy as np

import config

log = logging.getLogger("jarvis.filesearch")

CACHE = config.DATA_DIR / "file_embeddings.json"
_URL = "https://generativelanguage.googleapis.com/v1beta/openai/embeddings"
_MODEL = "gemini-embedding-001"
_BATCH = 100

_lock = threading.Lock()          # serialize (re)builds — they're expensive
_mem: dict | None = None          # {"hash", "aliases", "paths", "mat"(np, normalized)}


def _index_map() -> dict[str, str]:
    """alias -> path, merged across the index's apps/files/folders sections."""
    try:
        d = json.loads(config.INDEX_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    out: dict[str, str] = {}
    for cat in ("apps", "files", "folders"):
        for alias, path in (d.get(cat) or {}).items():
            out[alias] = path
    return out


def _hash(aliases: list[str]) -> str:
    return hashlib.sha1("\n".join(aliases).encode("utf-8")).hexdigest()


def _embed(texts: list[str]) -> list[list[float]]:
    import httpx
    headers = {"Authorization": f"Bearer {config.GOOGLE_API_KEY}",
               "Content-Type": "application/json"}
    vecs: list[list[float]] = []
    for i in range(0, len(texts), _BATCH):
        batch = texts[i:i + _BATCH]
        r = httpx.post(_URL, headers=headers, timeout=90,
                       json={"model": _MODEL, "input": batch})
        r.raise_for_status()
        vecs.extend(item["embedding"] for item in r.json()["data"])
    return vecs


def _normalized(vecs) -> np.ndarray:
    m = np.asarray(vecs, dtype=np.float32)
    return m / (np.linalg.norm(m, axis=1, keepdims=True) + 1e-9)


def ensure_index() -> tuple[bool, str]:
    """Build/refresh the embedding cache if missing or stale. Safe to call often;
    a no-op once current. Returns (ready, note)."""
    global _mem
    if not config.GOOGLE_API_KEY:
        return False, "semantic search needs a Gemini key"
    idx = _index_map()
    if not idx:
        return False, "the file index is empty — say 'rebuild the index' first"
    aliases = list(idx.keys())
    h = _hash(aliases)

    if _mem is not None and _mem["hash"] == h:
        return True, "ready"

    with _lock:
        if _mem is not None and _mem["hash"] == h:      # built while we waited
            return True, "ready"
        # Try the on-disk cache before spending API calls.
        if CACHE.exists():
            try:
                disk = json.loads(CACHE.read_text(encoding="utf-8"))
                if disk.get("hash") == h:
                    _mem = {"hash": h, "aliases": disk["aliases"],
                            "paths": disk["paths"], "mat": _normalized(disk["vecs"])}
                    return True, "ready"
            except (OSError, json.JSONDecodeError, KeyError):
                pass
        log.info("building semantic file index (%d aliases) — one-time", len(aliases))
        try:
            vecs = _embed(aliases)
        except Exception as e:                          # noqa: BLE001
            log.error("embedding build failed: %s", e)
            return False, f"couldn't build the search index ({type(e).__name__})"
        _mem = {"hash": h, "aliases": aliases,
                "paths": [idx[a] for a in aliases], "mat": _normalized(vecs)}
        try:
            CACHE.write_text(json.dumps({"hash": h, "aliases": aliases,
                                         "paths": _mem["paths"], "vecs": vecs}),
                             encoding="utf-8")
        except OSError:
            pass
        log.info("semantic file index ready")
        return True, "built"


def search(description: str, top_k: int = 5) -> dict:
    """Top file matches for a natural-language description, by embedding cosine."""
    ready, note = ensure_index()
    if not ready:
        return {"ready": False, "note": note, "results": []}
    try:
        qv = _normalized([_embed([description])[0]])[0]
    except Exception as e:                              # noqa: BLE001
        return {"ready": False, "note": f"query embedding failed ({type(e).__name__})",
                "results": []}
    sims = _mem["mat"] @ qv
    order = np.argsort(-sims)[:top_k]
    results = [{"name": _mem["aliases"][i], "path": _mem["paths"][i],
                "score": round(float(sims[i]), 3)} for i in order]
    return {"ready": True, "results": results}
