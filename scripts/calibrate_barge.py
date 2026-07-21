"""Measure your mic and recommend a BARGE_FLOOR for barge-in.

Barge-in must fire on YOUR voice but never on JARVIS's own voice coming back
through the mic. Windows' mic-array echo cancellation removes most of the
speaker signal, so the two levels are usually far apart — this measures both on
YOUR hardware and prints the number to put in .env.

Run:  .venv\\Scripts\\python.exe scripts\\calibrate_barge.py
"""
import asyncio
import sys
import time
from pathlib import Path

import numpy as np
import sounddevice as sd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import config          # noqa: E402
from audio import tts  # noqa: E402
from audio.player import PLAYER  # noqa: E402

SR = config.MIC_SAMPLE_RATE
levels: list[float] = []
collecting = False


def _cb(indata, frames, t, status):
    if collecting:
        levels.append(float(np.sqrt(np.mean((indata[:, 0] / 32768.0) ** 2))))


def measure(seconds: float) -> tuple[float, float]:
    global collecting, levels
    levels = []
    collecting = True
    time.sleep(seconds)
    collecting = False
    if not levels:
        return 0.0, 0.0
    a = np.array(levels)
    return float(np.percentile(a, 50)), float(np.percentile(a, 95))


async def main() -> None:
    stream = sd.InputStream(samplerate=SR, channels=1, dtype="int16",
                            blocksize=config.WAKE_FRAME_SAMPLES, callback=_cb)
    stream.start()

    print("\n1/3  Measuring the room while you stay QUIET (3s)...")
    time.sleep(0.5)
    noise_med, noise_p95 = measure(3)
    print(f"     room noise:      median {noise_med:.4f}   peak {noise_p95:.4f}")

    print("\n2/3  JARVIS will now speak. Stay QUIET — this measures how much of")
    print("     its own voice the mic hears back (the echo it must ignore).")
    await asyncio.sleep(0.5)

    async def speak():
        await tts.speak("Measuring how much of my own voice returns through your "
                        "microphone, sir. Please remain quiet for a moment while "
                        "I keep talking, so we can establish the echo level.")

    task = asyncio.create_task(speak())
    await asyncio.sleep(1.0)
    echo_med, echo_p95 = measure(6)
    await task
    PLAYER.wait_done()
    print(f"     its own echo:    median {echo_med:.4f}   peak {echo_p95:.4f}")

    # Casual interruptions — NOT a deliberate loud test. Calibrating to a posed,
    # over-loud "test interruption" is exactly what makes you have to shout later.
    print("\n3/3  Now INTERRUPT me a few times, the way you naturally would mid-")
    print("     conversation — say 'stop' or 'jarvis' at your NORMAL, casual")
    print("     volume, NOT a loud deliberate test. Cut in ~5 times.")
    global collecting, levels
    time.sleep(1.5)
    print("     >>> interrupt me now, casually, ~5 times <<<  (12s)")
    levels = []
    collecting = True
    time.sleep(12)
    collecting = False
    voice_frames = np.array(levels)
    print(f"     loudest frame seen: {voice_frames.max():.4f}")

    stream.stop()
    stream.close()

    # Split into distinct interruption ATTEMPTS (runs clearly above room noise)
    # and take each attempt's SUSTAINED level (median of the run) — barge-in needs
    # a couple of consecutive frames above the floor, so the sustained level of
    # your quietest cut-in is what matters, not the single loudest spike.
    bar = max(noise_p95 * 2.0, echo_p95 * 1.2, 0.005)
    above = voice_frames > bar
    attempts, i = [], 0
    while i < len(voice_frames):
        if above[i]:
            j = i
            while j < len(voice_frames) and above[j]:
                j += 1
            if j - i >= 2:                          # ignore 1-frame clicks
                attempts.append(float(np.median(voice_frames[i:j])))
            i = j
        else:
            i += 1

    print("\n" + "=" * 62)
    if len(attempts) < 2:
        print(f"  Only caught {len(attempts)} clear interruption(s) — need a few.")
        print("  Speak a touch louder/closer, or raise your Windows mic input")
        print("  level, then re-run.")
        print("=" * 62)
        return

    weakest = min(attempts)                         # your QUIETEST genuine cut-in
    echo_ceiling = max(echo_p95 * 1.3, noise_p95 * 2.5)   # floor must clear the echo

    # THE FIX (same shape as calibrate_wake): put the floor WELL below your
    # weakest casual interruption — a real ~45% margin — so normal-volume cut-ins
    # clear it, never right beneath it. It's still lifted above JARVIS's echo so
    # it can't interrupt itself.
    floor = weakest * 0.55
    squeezed = echo_ceiling > floor
    floor = round(max(floor, echo_ceiling), 4)

    print(f"  Detected {len(attempts)} interruptions.")
    print(f"  Sustained levels: {weakest:.4f} (weakest) .. {max(attempts):.4f} (loudest)")
    print(f"  JARVIS's own echo peaks at ~{echo_p95:.4f}.")
    if squeezed:
        print("\n  NOTE: your casual cut-ins are barely above JARVIS's echo — raise")
        print("  your Windows mic input level for reliable barge-in.")
    print("\n  Sits well below your weakest casual interruption, above the echo:")
    print(f"\n      BARGE_FLOOR={floor}")
    print("\n  Restart JARVIS. If it interrupts ITSELF, raise BARGE_FLOOR a little;")
    print("  if it ever ignores you, lower it a little.")
    print("=" * 62)


if __name__ == "__main__":
    asyncio.run(main())
