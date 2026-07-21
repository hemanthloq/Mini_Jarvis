"""Measure YOUR real 'jarvis' and recommend WAKE_THRESHOLD + WAKE_VAD_THRESHOLD.

The wake word must fire on 'jarvis' at your normal speaking volume, while a VAD
speech-gate rejects non-speech (grunts, coughs). Those two thresholds can only be
tuned against your actual microphone — this records you saying 'jarvis' a few
times and prints the numbers to put in .env.

Run:  .venv\\Scripts\\python.exe scripts\\calibrate_wake.py
Then say "jarvis" clearly, at NORMAL volume, ~5 times when prompted.
"""
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import config                                   # noqa: E402
from audio.wake import _Head, HEAD_PATH, PRETRAINED, ensure_models  # noqa: E402

SR = config.MIC_SAMPLE_RATE
FRAME = config.WAKE_FRAME_SAMPLES

samples: list[tuple[float, float]] = []          # (wake_score, vad_score) per frame
collecting = False


def main() -> None:
    ensure_models()
    from openwakeword.model import Model
    from openwakeword.vad import VAD
    owm = Model(wakeword_models=[PRETRAINED], inference_framework="onnx")
    head = _Head(HEAD_PATH) if HEAD_PATH.exists() else None
    vad = VAD()

    def score(frame: np.ndarray) -> tuple[float, float]:
        s = float(owm.predict(frame).get(PRETRAINED, 0.0))
        if head is not None:
            try:
                feats = owm.preprocessor.get_features(head.n_windows)
                s = max(s, head(feats))
            except Exception:
                pass
        try:
            v = float(vad.predict(frame))
        except Exception:
            v = 1.0
        return s, v

    def cb(indata, frames, t, status):
        if collecting:
            samples.append(score(indata[:, 0]))

    stream = sd.InputStream(samplerate=SR, channels=1, dtype="int16",
                            blocksize=FRAME, callback=cb)
    stream.start()

    global collecting, samples
    # IMPORTANT: say it the way you REALLY say it — casually, at normal volume,
    # not slow or over-enunciated. A deliberate "performed" jarvis scores higher
    # than everyday use, and calibrating to that is exactly what makes you have to
    # shout later. Talk as if mid-task.
    print("\nSay \"jarvis\" the way you ACTUALLY say it in normal use — casual, normal")
    print("volume, NOT slow or over-clear. Say it ~6 times with a short pause between,")
    print("as if you were just talking to it while doing something else.")
    time.sleep(3.0)
    print("\n>>> say JARVIS now, casually, ~6 times <<<   (recording 12 seconds)")
    samples = []
    collecting = True
    time.sleep(12)
    collecting = False
    stream.stop(); stream.close()

    if not samples:
        print("No audio captured — is the mic working?")
        return
    arr = np.array(samples)
    wake, vadv = arr[:, 0], arr[:, 1]

    # Segment into distinct utterances (runs above a low bar) and take EACH run's
    # peak — so we measure the score of every individual 'jarvis', including your
    # weakest one, rather than lumping all high frames together.
    peaks: list[tuple[float, float]] = []
    above = wake >= 0.4
    i = 0
    while i < len(wake):
        if above[i]:
            j = i
            while j < len(wake) and above[j]:
                j += 1
            k = i + int(np.argmax(wake[i:j]))
            peaks.append((float(wake[k]), float(vadv[k])))
            i = j
        else:
            i += 1

    print("\n" + "=" * 62)
    if len(peaks) < 2:
        print(f"  Only caught {len(peaks)} clear 'jarvis' (need a few to be reliable).")
        print(f"  Best wake score seen: {wake.max():.2f}. Speak a touch louder/closer")
        print("  or raise your Windows mic input level, then re-run.")
        print("=" * 62)
        return

    peak_wakes = sorted(p[0] for p in peaks)
    weakest = peak_wakes[0]
    weakest_vad = min(p[1] for p in peaks)

    # THE KEY FIX: casual real-use 'jarvis' scores LOWER than these deliberate
    # calibration utterances, so recommend a threshold WELL below the weakest one
    # (a real ~0.20 margin, not 0.08), and NEVER above 0.72 — anything higher is
    # the 'have to shout' zone this script exists to avoid.
    rec_wake = round(max(0.45, min(0.72, weakest - 0.20)), 2)
    rec_vad = round(max(0.05, min(0.30, weakest_vad - 0.15)), 2)

    print(f"  Detected {len(peaks)} 'jarvis' utterances.")
    print(f"  Peak wake scores: {weakest:.2f} (your weakest) .. {peak_wakes[-1]:.2f} (best)")
    print(f"  VAD speech at peaks: down to {weakest_vad:.2f}")
    print("\n  These leave a deliberate margin BELOW your weakest calibration hit,")
    print("  because casual everyday speech scores lower than a calibration run:")
    print("\n  Recommended in .env:")
    print(f"      WAKE_THRESHOLD={rec_wake}")
    print(f"      WAKE_VAD_THRESHOLD={rec_vad}")
    print("\n  Restart JARVIS. If it now wakes on noise/other speech, raise")
    print("  WAKE_THRESHOLD by ~0.05; if it ever misses you, lower it by ~0.05.")
    print("=" * 62)


if __name__ == "__main__":
    main()
