"""The 'Jarvis timbre': light DSP chain over raw TTS PCM.

Goal is 'processed AI voice', not 'robot'. Chain (all subtle):
  1. Low-shelf cut  (~-3 dB below 180 Hz)   -> less chesty, more speaker-like
  2. Comb resonance (~2.4 ms, 12% wet)      -> faint metallic sheen
  3. Presence boost (+2 dB around 4.5 kHz)  -> sharper consonants
  4. Short reverb tail (~90 ms, 8% wet)     -> 'suit helmet' space
Operates on int16 mono PCM; stateless per-utterance is fine because we process
in reasonably large chunks with overlap-free IIR filters carried via zi state.
"""
import numpy as np
from scipy import signal


class VoiceEffect:
    def __init__(self, sample_rate: int):
        self.sr = sample_rate
        nyq = sample_rate / 2

        # 1. low shelf cut: 2nd-order highpass blended with dry
        self._hp_sos = signal.butter(2, 180 / nyq, "highpass", output="sos")
        self._hp_zi = signal.sosfilt_zi(self._hp_sos)

        # 3. presence: bandpass around 4.5 kHz added on top
        lo, hi = 3500 / nyq, min(5500 / nyq, 0.99)
        self._bp_sos = signal.butter(2, [lo, hi], "bandpass", output="sos")
        self._bp_zi = signal.sosfilt_zi(self._bp_sos)

        # 2. comb: y[n] = x[n] + g*x[n-D]
        self._comb_delay = max(1, int(0.0024 * sample_rate))
        self._comb_buf = np.zeros(self._comb_delay, dtype=np.float32)
        self._comb_g = 0.12

        # 4. reverb: sparse decaying echo taps up to ~90 ms
        taps_ms = [23, 41, 59, 89]
        self._rev_delay = int(taps_ms[-1] / 1000 * sample_rate) + 1
        self._rev_buf = np.zeros(self._rev_delay, dtype=np.float32)
        self._rev_taps = [(int(ms / 1000 * sample_rate), 0.08 * (0.6 ** i))
                          for i, ms in enumerate(taps_ms)]

    def process(self, pcm: bytes) -> bytes:
        x = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if x.size == 0:
            return pcm

        hp, self._hp_zi = signal.sosfilt(self._hp_sos, x, zi=self._hp_zi)
        y = 0.35 * x + 0.65 * hp                      # gentle low-shelf cut

        buf = np.concatenate([self._comb_buf, y])     # comb resonance
        y = y + self._comb_g * buf[: y.size]
        self._comb_buf = buf[-self._comb_delay:]

        bp, self._bp_zi = signal.sosfilt(self._bp_sos, y, zi=self._bp_zi)
        y = y + 0.26 * bp                             # presence / transients

        rbuf = np.concatenate([self._rev_buf, y])     # short reverb tail
        for d, g in self._rev_taps:
            y = y + g * rbuf[self._rev_delay - d: self._rev_delay - d + y.size]
        self._rev_buf = rbuf[-self._rev_delay:]

        y = np.tanh(y * 1.1) * 0.95                   # soft limit
        return (y * 32767.0).astype(np.int16).tobytes()
