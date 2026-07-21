"""Deepgram streaming STT over a raw websocket (Nova model).

We open the socket only after the wake word fires, stream 16 kHz PCM from the
mic, and surface interim + final transcripts through callbacks. Endpointing at
300 ms gives fast finals so the router can act almost as the user stops talking.
"""
import asyncio
import json
import logging
import queue
import threading
import time

import numpy as np
import sounddevice as sd
import websockets

import config
from state import STATE

# Set by main.set_muted(). Checked inside the mic callback so muting kills an
# in-flight capture immediately, rather than only preventing the next one.
MUTED = threading.Event()

log = logging.getLogger("jarvis.stt")

_DG_URL = (
    "wss://api.deepgram.com/v1/listen"
    "?model=nova-2&language=en&encoding=linear16&sample_rate=16000&channels=1"
    "&interim_results=true&endpointing=300&vad_events=true&punctuate=true"
)

SILENCE_TIMEOUT = 6.0    # give up if the user says nothing after the wake word
MAX_UTTERANCE = 20.0     # hard cap on one listening session


async def listen_once(silence_timeout: float = SILENCE_TIMEOUT) -> str | None:
    """Capture one utterance; return the final transcript or None."""
    if not config.DEEPGRAM_API_KEY:
        log.error("no Deepgram key configured")
        return None

    audio_q: queue.Queue[bytes] = queue.Queue()
    stats = {"frames": 0, "sent": 0, "peak": 0.0, "overflows": 0}

    def _mic_cb(indata, frames, t, status):
        if status:
            # input overflow => we dropped audio; this is exactly the kind of
            # thing that made "it didn't hear me" impossible to diagnose
            stats["overflows"] += 1
            log.warning("STT mic status: %s", status)
        # PRIVACY: muting must stop an IN-FLIGHT session, not just the next wake
        # word. This callback kept queueing audio after the mic was muted, so a
        # session already running went on streaming to Deepgram — measured at 15
        # seconds past the mute, then four more sessions after it. Dropping the
        # audio here means muted frames are never queued, never sent, and the
        # session ends at its own timeout with nothing transcribed.
        if MUTED.is_set():
            return
        pcm = (indata[:, 0] * 32767).astype(np.int16).tobytes()
        audio_q.put(pcm)
        rms = float(np.sqrt(np.mean(indata[:, 0] ** 2)))
        stats["frames"] += 1
        stats["peak"] = max(stats["peak"], rms)
        STATE.level(min(1.0, rms * 8))

    transcript_parts: list[str] = []
    got_speech = asyncio.Event()

    # Start capturing BEFORE the websocket connects: anything the user says
    # while Deepgram is still handshaking is buffered in audio_q and sent as
    # backlog, so fast talkers don't get their first words clipped.
    stream = sd.InputStream(samplerate=config.MIC_SAMPLE_RATE, channels=1,
                            dtype="float32", blocksize=1600,  # 100 ms
                            callback=_mic_cb)
    stream.start()

    t0 = time.monotonic()
    try:
        async with websockets.connect(
            _DG_URL,
            additional_headers={"Authorization": f"Token {config.DEEPGRAM_API_KEY}"},
            open_timeout=5,
        ) as ws:
            log.info("STT: Deepgram connected in %.0f ms", (time.monotonic() - t0) * 1000)
            loop = asyncio.get_running_loop()

            async def sender():
                try:
                    while True:
                        pcm = await loop.run_in_executor(None, audio_q.get)
                        await ws.send(pcm)
                        stats["sent"] += 1
                except asyncio.CancelledError:
                    pass
                except websockets.ConnectionClosed as e:
                    # This used to pass silently: Deepgram would drop the socket,
                    # we'd stop sending audio, and it looked like "it just didn't
                    # hear me" with nothing in the log.
                    log.error("STT: Deepgram closed the connection mid-utterance "
                              "(%s) — audio after this point was NOT transcribed", e)

            send_task = asyncio.create_task(sender())
            final_text: str | None = None
            try:
                deadline = loop.time() + silence_timeout
                hard_stop = loop.time() + MAX_UTTERANCE
                while True:
                    timeout = max(0.1, min(deadline, hard_stop) - loop.time())
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                    except asyncio.TimeoutError:
                        break
                    msg = json.loads(raw)
                    if msg.get("type") != "Results":
                        continue
                    alt = msg["channel"]["alternatives"][0]
                    text = alt.get("transcript", "")
                    if not text:
                        continue
                    got_speech.set()
                    deadline = loop.time() + silence_timeout
                    if msg.get("is_final"):
                        transcript_parts.append(text)
                        STATE.transcript(" ".join(transcript_parts), final=False)
                        if msg.get("speech_final"):
                            final_text = " ".join(transcript_parts).strip()
                            break
                    else:
                        interim = (" ".join(transcript_parts + [text])).strip()
                        STATE.transcript(interim, final=False)
            finally:
                send_task.cancel()

            if final_text is None and transcript_parts:
                final_text = " ".join(transcript_parts).strip()
            if final_text:
                STATE.transcript(final_text, final=True)

            # Always leave evidence: if it "didn't hear me", this line says
            # whether the mic captured anything, whether it reached Deepgram, and
            # how loud it was — rather than leaving us to guess.
            log.info("STT session: %.1fs, %d mic frames (%d sent), peak level %.3f, "
                     "overflows %d, transcript %r",
                     time.monotonic() - t0, stats["frames"], stats["sent"],
                     stats["peak"], stats["overflows"], final_text or "")
            if stats["frames"] == 0:
                log.error("STT: the mic produced NO audio at all this session")
            elif stats["peak"] < 0.01 and not final_text:
                log.warning("STT: mic audio was essentially silent (peak %.4f) — "
                            "check the input device / Windows mic privacy setting",
                            stats["peak"])
            return final_text or None
    except Exception as e:
        log.error("STT session failed after %.1fs (%d frames captured): %s",
                  time.monotonic() - t0, stats["frames"], e)
        return None
    finally:
        if not stream.closed:
            stream.stop()
            stream.close()
