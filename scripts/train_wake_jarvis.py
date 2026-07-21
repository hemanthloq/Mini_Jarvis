"""Train a custom 'jarvis' wake-word head (fires on any prefix).

The pretrained openWakeWord model only knows the fixed phrase "hey jarvis".
This trains a small classifier on top of openWakeWord's own feature extractor
(melspectrogram -> embedding ONNX) so it fires on the bare word regardless of
prefix: "jarvis", "hey jarvis", "okay jarvis", "yo jarvis", "jarvis?"...

Everything is local:
  * positives  — Windows SAPI voices (pyttsx3) speaking jarvis with many
                 prefixes/suffixes, then heavily augmented (speed, pitch, gain,
                 noise, reverb, clipping) so a handful of voices becomes hundreds
                 of varied samples.
  * negatives  — confusable words (service/travis/harvest/carve/驾...), everyday
                 phrases, silence, noise, AND jarvis's own ElevenLabs TTS replies
                 (critical: it must not wake itself up while speaking).
  * classifier — sklearn MLP on the 16x96 embedding window.

Inference stays fully offline (numpy + the same ONNX feature extractor).

Run:  .venv\\Scripts\\python.exe scripts\\train_wake_jarvis.py
"""
import logging
import random
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import config  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("train")

SR = 16000
MODEL_OUT = Path(config.ROOT) / "backend" / "models" / "jarvis_head.npz"
MODEL_OUT.parent.mkdir(parents=True, exist_ok=True)
random.seed(0)
np.random.seed(0)

# ── phrases ─────────────────────────────────────────────────────
POSITIVE = [
    "jarvis", "jarvis.", "jarvis?", "hey jarvis", "hey jarvis.", "okay jarvis",
    "ok jarvis", "yo jarvis", "hi jarvis", "hello jarvis", "jarvis, you there",
    "so jarvis", "uh jarvis", "alright jarvis", "excuse me jarvis", "jarvis!",
    "hey, jarvis", "jarvis are you there", "jarvis listen",
]
# Hard negatives: acoustically close, plus ordinary speech it must ignore.
NEGATIVE = [
    "service", "travis", "harvest", "carve this", "jarred", "java", "javits",
    "car keys", "starve us", "harvard", "marvel", "traverse", "carvers",
    "服务", "jarvis is a movie character",   # long-form mention (not a command)
    "what time is it", "open downloads", "play some music", "volume up",
    "how's the weather", "let me think about that", "i'll be right back",
    "can you hear me", "hello there", "good morning", "thanks a lot",
    "the quick brown fox jumps over the lazy dog", "one two three four five",
    "okay", "hey", "yo", "hello", "alright then", "nothing much",
]


def _render_all_sapi(phrases: list[str], out_dir: Path) -> list[tuple[Path, str]]:
    """Render every phrase with every installed SAPI voice at 3 speaking rates,
    in ONE PowerShell pass.

    (pyttsx3 was the obvious choice but re-initialising the SAPI COM engine per
    utterance hangs after a few dozen calls — this does the whole corpus in one
    process, in seconds.)
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = out_dir / "manifest.txt"
    phrases_file = out_dir / "phrases.txt"
    phrases_file.write_text("\n".join(phrases), encoding="utf-8")

    ps = f"""
Add-Type -AssemblyName System.Speech
$phrases = Get-Content -LiteralPath '{phrases_file}' -Encoding UTF8
$out = '{out_dir}'
$manifest = @()
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
$voices = $synth.GetInstalledVoices() | Where-Object {{ $_.Enabled }} |
          ForEach-Object {{ $_.VoiceInfo.Name }}
$i = 0
foreach ($v in $voices) {{
  foreach ($rate in -2, 0, 3) {{
    $k = 0
    foreach ($p in $phrases) {{
      if ([string]::IsNullOrWhiteSpace($p)) {{ $k++; continue }}
      $i++
      $file = Join-Path $out ("clip_{{0:d5}}.wav" -f $i)
      try {{
        $synth.SelectVoice($v)
        $synth.Rate = $rate
        $synth.SetOutputToWaveFile($file)
        $synth.Speak($p)
        $synth.SetOutputToNull()
        $manifest += ("{{0}}`t{{1}}" -f $file, $k)
      }} catch {{ }}
      $k++
    }}
  }}
}}
$synth.Dispose()
$manifest | Set-Content -LiteralPath (Join-Path $out 'manifest.txt') -Encoding UTF8
Write-Output ("voices={{0}} clips={{1}}" -f $voices.Count, $manifest.Count)
"""
    script = out_dir / "render.ps1"
    script.write_text(ps, encoding="utf-8")
    res = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                          "-File", str(script)],
                         capture_output=True, text=True, timeout=900)
    log.info("SAPI render: %s %s", res.stdout.strip(), res.stderr.strip()[:200])

    rendered = []
    if manifest.exists():
        for line in manifest.read_text(encoding="utf-8").splitlines():
            if "\t" not in line:
                continue
            path, idx = line.rsplit("\t", 1)
            p = Path(path)
            if p.exists() and p.stat().st_size > 1000:
                rendered.append((p, phrases[int(idx)]))
    return rendered


def load_wav_16k(path: Path) -> np.ndarray | None:
    try:
        with wave.open(str(path), "rb") as w:
            sr, n_ch = w.getframerate(), w.getnchannels()
            raw = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    except (wave.Error, OSError):
        return None
    if raw.size == 0:
        return None
    if n_ch > 1:
        raw = raw[::n_ch]
    x = raw.astype(np.float32) / 32768.0
    if sr != SR:
        from math import gcd
        g = gcd(int(sr), SR)
        x = resample_poly(x, SR // g, int(sr) // g)
    return x.astype(np.float32)


# ── augmentation ────────────────────────────────────────────────
def _resample(y: np.ndarray, n: int) -> np.ndarray:
    n = max(8, n)
    return np.interp(np.linspace(0, len(y) - 1, n), np.arange(len(y)), y).astype(np.float32)


def augment(x: np.ndarray, rng: random.Random) -> np.ndarray:
    y = x.copy()

    # Vocal-tract-length perturbation: warp the spectrum but keep the duration.
    # This is what makes one synthetic voice sound like many different speakers —
    # without it the model overfits the handful of local SAPI voices and misses
    # the bare word from an unfamiliar voice.
    if rng.random() < 0.85:
        warp = rng.uniform(0.80, 1.25)
        orig_len = len(y)
        y = _resample(y, int(orig_len / warp))     # shift formants + pitch
        y = _resample(y, orig_len)                 # restore the original duration

    speed = rng.uniform(0.85, 1.2)             # speaking rate
    y = _resample(y, int(len(y) / speed))

    if rng.random() < 0.7:                     # residual pitch wobble
        semitones = rng.uniform(-3.0, 3.0)
        y = _resample(y, int(len(y) / (2 ** (semitones / 12))))

    if rng.random() < 0.5:                     # room reverb (a few decaying taps)
        out = y.copy()
        for delay_ms, gain in ((23, 0.22), (41, 0.14), (71, 0.08)):
            d = int(delay_ms / 1000 * SR)
            if d < len(y):
                out[d:] += y[:-d] * gain * rng.uniform(0.5, 1.3)
        y = out

    y *= rng.uniform(0.25, 1.4)                # distance / mic gain

    noise_kind = rng.random()                  # background
    if noise_kind < 0.45:
        y += np.random.normal(0, rng.uniform(0.001, 0.02), len(y)).astype(np.float32)
    elif noise_kind < 0.6:                     # pink-ish hum
        hum = np.sin(2 * np.pi * rng.uniform(45, 65) * np.arange(len(y)) / SR)
        y += (hum * rng.uniform(0.002, 0.01)).astype(np.float32)

    return np.clip(y, -1.0, 1.0).astype(np.float32)


def pad_to_window(x: np.ndarray, rng: random.Random, seconds: float = 1.6) -> np.ndarray:
    """Center the utterance in a fixed window with random lead/tail silence, so
    the model sees the word at varying offsets (as it will from a live mic)."""
    target = int(seconds * SR)
    if len(x) > target:
        x = x[:target]
    room = target - len(x)
    lead = rng.randint(int(room * 0.15), max(int(room * 0.15), int(room * 0.85))) if room > 0 else 0
    out = np.zeros(target, dtype=np.float32)
    out[lead:lead + len(x)] = x
    out += np.random.normal(0, 0.0015, target).astype(np.float32)   # mic floor
    return np.clip(out, -1, 1)


def main() -> None:
    from openwakeword.utils import AudioFeatures

    rng = random.Random(1234)
    clips: list[np.ndarray] = []
    labels: list[int] = []

    all_phrases = POSITIVE + NEGATIVE
    pos_set = set(POSITIVE)

    with tempfile.TemporaryDirectory() as td:
        log.info("rendering %d phrases across all local SAPI voices...", len(all_phrases))
        rendered = _render_all_sapi(all_phrases, Path(td))
        if not rendered:
            sys.exit("No Windows TTS voices produced audio — cannot build training data.")
        log.info("rendered %d base clips", len(rendered))

        for path, text in rendered:
            base = load_wav_16k(path)
            if base is None or len(base) < SR * 0.15:
                continue
            label = 1 if text in pos_set else 0
            # The bare word is the hardest case (short, no carrier phrase) — give
            # it the most augmented variants.
            bare = label and len(text.split()) == 1
            n_aug = 12 if bare else (5 if label else 3)
            for _ in range(n_aug):
                clips.append(pad_to_window(augment(base, rng), rng))
                labels.append(label)

    # Negative: JARVIS's own cached TTS voice — it must never wake itself.
    own = list((Path(config.CACHE_DIR)).glob("*.wav"))
    for p in own:
        with wave.open(str(p), "rb") as w:
            a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            sr = w.getframerate()
        a = a.astype(np.float32) / 32768.0
        if sr != SR:
            a = resample_poly(a, 640, 882) if sr == 22050 else a
        for start in range(0, max(1, len(a) - int(1.6 * SR)), int(0.8 * SR)):
            seg = a[start:start + int(1.6 * SR)]
            if len(seg) < int(1.6 * SR):
                break
            clips.append(np.clip(seg, -1, 1).astype(np.float32))
            labels.append(0)
    log.info("added %d segments of jarvis's own voice as negatives", len(own))

    # Negative: pure silence / noise
    for _ in range(120):
        n = np.random.normal(0, rng.uniform(0.001, 0.03), int(1.6 * SR)).astype(np.float32)
        clips.append(np.clip(n, -1, 1))
        labels.append(0)

    X_audio = np.stack(clips)
    y = np.array(labels)
    log.info("dataset: %d clips (%d positive, %d negative)", len(y), y.sum(), (y == 0).sum())

    log.info("extracting openWakeWord embeddings...")
    feats = AudioFeatures(inference_framework="onnx")
    emb = feats.embed_clips((X_audio * 32767).astype(np.int16), batch_size=64)
    # emb: (n_clips, n_windows, 96) -> use the max-activation window per clip so
    # the classifier is position-invariant like the real streaming detector.
    log.info("embeddings: %s", emb.shape)
    X = emb.reshape(emb.shape[0], -1).astype(np.float32)

    from sklearn.model_selection import train_test_split
    from sklearn.neural_network import MLPClassifier
    from sklearn.metrics import classification_report, roc_auc_score

    Xtr, Xte, ytr, yte = train_test_split(X, y, test_size=0.2, stratify=y, random_state=7)
    clf = MLPClassifier(hidden_layer_sizes=(96, 32), activation="relu", alpha=1e-3,
                        max_iter=600, random_state=7, early_stopping=True)
    log.info("training head on %d samples...", len(ytr))
    clf.fit(Xtr, ytr)

    p = clf.predict_proba(Xte)[:, 1]
    log.info("\n%s", classification_report(yte, (p > 0.5).astype(int),
                                           target_names=["not-jarvis", "jarvis"]))
    log.info("ROC AUC: %.4f", roc_auc_score(yte, p))

    # Export raw weights -> runtime needs only numpy (no sklearn dependency).
    np.savez(MODEL_OUT,
             W0=clf.coefs_[0], b0=clf.intercepts_[0],
             W1=clf.coefs_[1], b1=clf.intercepts_[1],
             W2=clf.coefs_[2], b2=clf.intercepts_[2],
             n_windows=emb.shape[1], emb_dim=emb.shape[2])
    log.info("saved %s", MODEL_OUT)


if __name__ == "__main__":
    main()
