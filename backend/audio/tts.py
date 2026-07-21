"""ElevenLabs streaming TTS + pre-rendered stock phrase cache.

- speak(): streams PCM chunks from ElevenLabs, runs each through the effects
  chain, and hands them to the player as they arrive (first-chunk latency).
- speak_canned(): plays a cached WAV with ZERO network latency; falls back to
  live TTS if the phrase isn't cached yet.
"""
import asyncio
import hashlib
import logging
import queue
import threading
import wave
from pathlib import Path

import httpx

import config
from audio.effects import VoiceEffect
from audio.player import PLAYER

log = logging.getLogger("jarvis.tts")

_URL = ("https://api.elevenlabs.io/v1/text-to-speech/{voice}/stream"
        "?output_format=pcm_22050")

# Stock phrases pre-rendered by scripts/cache_phrases.py
STOCK_PHRASES = [
    "Yes, sir.", "Right away.", "On it.", "Done.", "Of course.",
    "As you wish.", "Working on it.", "One moment.", "Muted.", "Unmuted.",
    "Skipping.", "Going back.", "Stopped.", "Closed.",
    "Sorry, I didn't catch that.", "I'm offline at the moment, sir.",
    "Welcome back, sir.", "Very good, sir.", "Anytime, sir.",
    "Systems online, sir.", "Goodnight, sir.", "Until next time, sir.",
    "Dimming the noise, sir.", "Enjoy yourself, sir. I'll keep things quiet.",
    "Shutting down, sir.", "Cancelled, sir.",
    "Going to sleep, sir.", "Back online, sir.",
    "Couldn't pin that down, sir.", "Couldn't do that one, sir.",
    "Cancelled, sir. Leaving everything as it is.",
]


def _cache_path(text: str) -> Path:
    h = hashlib.sha1(f"{config.ELEVENLABS_VOICE_ID}:{text}".encode()).hexdigest()[:16]
    return config.CACHE_DIR / f"{h}.wav"


async def synth_to_pcm(text: str, apply_fx: bool = True) -> bytes:
    """Full (non-streaming collect) synthesis — used by the cache builder."""
    fx = VoiceEffect(config.TTS_SAMPLE_RATE)
    out = bytearray()
    async for chunk in _stream_raw(text):
        out.extend(fx.process(chunk) if apply_fx else chunk)
    return bytes(out)


class TTSDegraded(RuntimeError):
    """ElevenLabs is unusable (quota exhausted, bad key, outage)."""

    def __init__(self, reason: str, quota: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.quota = quota


# Set when ElevenLabs fails, so the HUD can show that the voice is degraded and
# we stop hammering a dead API on every single utterance.
DEGRADED: TTSDegraded | None = None
_on_degraded = None          # callback(reason, quota) -> tell the HUD


def set_degraded_callback(fn) -> None:
    global _on_degraded
    _on_degraded = fn


def _mark_degraded(reason: str, quota: bool) -> None:
    global DEGRADED
    first = DEGRADED is None
    DEGRADED = TTSDegraded(reason, quota)
    if first:
        log.error("VOICE DEGRADED — falling back to Windows speech. Reason: %s", reason)
        if _on_degraded:
            try:
                _on_degraded(reason, quota)
            except Exception as e:
                log.warning("degraded callback failed: %s", e)


def clear_degraded() -> None:
    global DEGRADED
    if DEGRADED is not None:
        log.info("ElevenLabs is working again — restoring the normal voice")
    DEGRADED = None
    if _on_degraded:
        try:
            _on_degraded(None, False)
        except Exception:
            pass


async def _stream_raw(text: str):
    headers = {"xi-api-key": config.ELEVENLABS_API_KEY,
               "Content-Type": "application/json"}
    body = {
        "text": text,
        "model_id": "eleven_turbo_v2_5",     # lowest latency tier
        "voice_settings": {"stability": 0.55, "similarity_boost": 0.8,
                           "style": 0.2, "use_speaker_boost": True},
    }
    url = _URL.format(voice=config.ELEVENLABS_VOICE_ID)
    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream("POST", url, headers=headers, json=body) as r:
            if r.status_code != 200:
                detail = (await r.aread()).decode("utf-8", "replace")[:300]
                # 401 = bad key, 429 = rate limit, 402/quota_exceeded = out of
                # characters. Any of these used to leave JARVIS silently mute.
                quota = (r.status_code in (401, 402, 429)
                         or "quota" in detail.lower()
                         or "exceeded" in detail.lower())
                raise TTSDegraded(f"ElevenLabs {r.status_code}: {detail}", quota=quota)
            async for chunk in r.aiter_bytes(chunk_size=4096):
                if chunk:
                    yield chunk


# ── Windows SAPI fallback (always available, offline) ───────────
# True while the Windows voice is talking. The barge-in detector needs this:
# SAPI plays through its own audio path, so our player reports zero output level
# and the echo estimate would go blind — and JARVIS would interrupt itself.
SAPI_SPEAKING = False
# Stand-in output level while the Windows voice is talking, so the barge-in echo
# estimator has a non-zero reference. Lowered 0.09 -> 0.03: at 0.09 the estimator
# predicted an echo of ~0.018 when the real (echo-cancelled) echo is only ~0.003,
# and the safety margin on that inflated value pushed the barge threshold above
# the user's own voice — so "stop" could never interrupt the Windows voice.
SAPI_NOMINAL_RMS = 0.03


class _Sapi:
    """The Windows voice on a dedicated COM thread so it can be INTERRUPTED.

    The old pyttsx3 fallback called runAndWait(), which blocks uninterruptibly
    through the entire sentence — so barge-in ('stop', 'shut up') did nothing
    whenever JARVIS was on this voice (ElevenLabs quota exhausted / no key). Here
    we speak ASYNC and poll; a stop() purges the current utterance immediately.
    """
    _ASYNC = 1                # SVSFlagsAsync — Speak returns at once
    _PURGE = 2                # SVSFPurgeBeforeSpeak — drop what's playing/queued

    def __init__(self):
        self._q: "queue.Queue[tuple[str, threading.Event]]" = queue.Queue()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._voice = None
        self._ok = False
        threading.Thread(target=self._run, daemon=True, name="sapi-voice").start()
        self._ready.wait(timeout=5)

    def _run(self) -> None:
        global SAPI_SPEAKING
        try:
            import pythoncom
            import win32com.client
            pythoncom.CoInitialize()
            self._voice = win32com.client.Dispatch("SAPI.SpVoice")
            self._voice.Rate = 0
            self._ok = True
        except Exception as e:
            log.error("SAPI init failed: %s", e)
            self._ready.set()
            return
        self._ready.set()
        while True:
            text, done = self._q.get()
            self._stop.clear()
            SAPI_SPEAKING = True
            try:
                self._voice.Speak(text, self._ASYNC)
                while not self._voice.WaitUntilDone(50):
                    if self._stop.is_set():
                        self._voice.Speak("", self._PURGE)   # interrupt now
                        log.info("SAPI voice interrupted (barge-in)")
                        break
            except Exception as e:
                log.error("SAPI speak failed: %s", e)
            finally:
                SAPI_SPEAKING = False
                done.set()

    def speak(self, text: str) -> threading.Event:
        done = threading.Event()
        if not self._ok:
            done.set()
        else:
            self._q.put((text, done))
        return done

    def stop(self) -> None:
        self._stop.set()


_sapi_player: "_Sapi | None" = None


def _sapi() -> _Sapi:
    global _sapi_player
    if _sapi_player is None:
        _sapi_player = _Sapi()
    return _sapi_player


def stop_fallback() -> None:
    """Interrupt the Windows fallback voice mid-sentence (called on barge-in)."""
    if _sapi_player is not None:
        _sapi_player.stop()


async def speak_fallback(text: str) -> None:
    loop = asyncio.get_running_loop()
    log.info("speaking via Windows SAPI fallback: %r", text[:60])
    done = _sapi().speak(text)
    await loop.run_in_executor(None, done.wait)


async def speak(text: str) -> None:
    """Stream TTS for one utterance; playback begins on the first chunk.

    JARVIS is NEVER silently mute: if ElevenLabs fails (quota, bad key, outage)
    we log the real reason and speak with the Windows voice instead.
    """
    if not config.ELEVENLABS_API_KEY:
        log.warning("no ElevenLabs key — using the Windows voice")
        await speak_fallback(text)
        return

    if DEGRADED is not None:            # already known-bad: don't stall every line
        await speak_fallback(text)
        return

    fx = VoiceEffect(config.TTS_SAMPLE_RATE)
    pending = b""                        # keep int16 alignment across chunks
    spoke_anything = False
    try:
        async for chunk in _stream_raw(text):
            data = pending + chunk
            cut = len(data) - (len(data) % 2)
            pending = data[cut:]
            if cut:
                spoke_anything = True
                PLAYER.enqueue(fx.process(data[:cut]))
    except TTSDegraded as e:
        _mark_degraded(e.reason, e.quota)
        PLAYER.end_of_utterance()
        await speak_fallback(text)
        return
    except Exception as e:
        log.error("TTS stream failed (%s) — using the Windows voice", e)
        PLAYER.end_of_utterance()
        await speak_fallback(text)
        return
    finally:
        PLAYER.end_of_utterance()

    if not spoke_anything:
        log.error("ElevenLabs returned no audio — using the Windows voice")
        await speak_fallback(text)


def speak_canned(text: str) -> bool:
    """Play a cached phrase instantly. Returns False if not cached."""
    path = _cache_path(text)
    if not path.exists():
        return False
    try:
        with wave.open(str(path), "rb") as w:
            PLAYER.enqueue(w.readframes(w.getnframes()))
        PLAYER.end_of_utterance()
        return True
    except Exception as e:
        log.error("canned playback failed: %s", e)
        return False


async def say(text: str) -> None:
    """Cached if available, else live stream (with the SAPI fallback behind it)."""
    if not speak_canned(text):
        await speak(text)


def save_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(config.TTS_SAMPLE_RATE)
        w.writeframes(pcm)


async def build_phrase_cache() -> int:
    """Pre-render all stock phrases (called by scripts/cache_phrases.py)."""
    n = 0
    for phrase in STOCK_PHRASES:
        path = _cache_path(phrase)
        if path.exists():
            continue
        pcm = await synth_to_pcm(phrase)
        save_wav(path, pcm)
        log.info("cached %r -> %s", phrase, path.name)
        n += 1
        await asyncio.sleep(0.3)  # be polite to the free tier
    return n
