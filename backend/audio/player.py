"""Streaming PCM playback via sounddevice. Playback starts on the first chunk."""
import logging
import queue
import threading

import numpy as np
import sounddevice as sd

import config
from state import STATE

log = logging.getLogger("jarvis.player")


class Player:
    """One persistent output stream; chunks are queued from any thread."""

    def __init__(self, sample_rate: int = config.TTS_SAMPLE_RATE):
        self.sr = sample_rate
        self._q: queue.Queue[bytes | None] = queue.Queue()
        self._interrupt = threading.Event()
        self._playing = threading.Event()
        self._out_rms = 0.0          # what we're currently emitting (for barge-in)
        self._thread = threading.Thread(target=self._run, daemon=True, name="player")
        self._thread.start()

    @property
    def output_rms(self) -> float:
        """RMS of the audio currently going to the speakers (0 when silent)."""
        return self._out_rms if self._playing.is_set() else 0.0

    def is_playing(self) -> bool:
        return self._playing.is_set()

    def enqueue(self, pcm: bytes) -> None:
        self._q.put(pcm)

    def end_of_utterance(self) -> None:
        self._q.put(None)

    def stop_now(self) -> None:
        """Barge-in: flush everything queued."""
        self._interrupt.set()
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._q.put(None)

    def wait_done(self, timeout: float | None = None) -> None:
        self._playing.wait(0.5)          # let playback start
        while self._playing.is_set():
            threading.Event().wait(0.05)

    def _run(self) -> None:
        stream = sd.OutputStream(samplerate=self.sr, channels=1, dtype="int16")
        stream.start()
        while True:
            chunk = self._q.get()
            if chunk is None:
                self._playing.clear()
                self._interrupt.clear()
                self._out_rms = 0.0
                continue
            self._playing.set()
            if self._interrupt.is_set():
                continue
            data = np.frombuffer(chunk, dtype=np.int16)
            if data.size:
                rms = float(np.sqrt(np.mean((data / 32768.0) ** 2)))
                self._out_rms = rms          # barge-in uses this to predict the echo
                STATE.level(min(1.0, rms * 6))
            # Write in small slices so a barge-in interrupt takes effect within
            # ~40 ms instead of waiting for the whole chunk to drain.
            try:
                step = int(self.sr * 0.04)
                for i in range(0, data.size, step):
                    if self._interrupt.is_set():
                        break
                    stream.write(data[i:i + step])
            except Exception as e:
                log.error("playback error: %s", e)


PLAYER = Player()
