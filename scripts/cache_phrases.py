"""Pre-render JARVIS's stock phrases to WAV so fast-path confirmations play
with zero TTS latency. Run once after setting ELEVENLABS_API_KEY in .env:

    .venv\\Scripts\\python.exe scripts\\cache_phrases.py
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import config  # noqa: E402
from audio import tts  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")

if not config.ELEVENLABS_API_KEY:
    sys.exit("ELEVENLABS_API_KEY is not set in .env — fill it in first.")

n = asyncio.run(tts.build_phrase_cache())
print(f"done — {n} new phrase(s) rendered, "
      f"{len(tts.STOCK_PHRASES)} total in {config.CACHE_DIR}")
