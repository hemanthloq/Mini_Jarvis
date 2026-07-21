"""Wake word + barge-in on one always-on local mic stream (fully offline).

Two detectors run per 80 ms frame:
  * the custom-trained "jarvis" head (backend/models/jarvis_head.npz) — fires on
    the bare word with any prefix: "jarvis", "hey jarvis", "okay jarvis", "yo
    jarvis". Trained by scripts/train_wake_jarvis.py on openWakeWord's own
    feature extractor.
  * openWakeWord's pretrained "hey_jarvis" model, OR-ed in as a safety net.

While JARVIS is SPEAKING the same stream does barge-in detection: the wake word
always interrupts, and so does sustained speech that is clearly louder than the
echo of JARVIS's own voice coming back through the mic (the coupling coefficient
is learned live, so it doesn't interrupt itself).
"""
import logging
import threading
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd

import config

log = logging.getLogger("jarvis.wake")

PRETRAINED = "hey_jarvis"
HEAD_PATH = Path(config.ROOT) / "backend" / "models" / "jarvis_head.npz"

WAKE_CONSECUTIVE = 2      # frames above threshold before we believe it (debounce)
WAKE_WINDOW = 4           # ...counted over this many recent frames, not in a row
                          # (see the debounce comment in the idle branch)
VAD_WINDOW = 4            # frames of speech-probability history kept (~320 ms)

BARGE_FRAMES = 3          # hot frames required within BARGE_WINDOW
                          # Raised 2->3 (window 4->6) on 2026-07-20: barge-in was
                          # firing on ambient conversation not addressed to
                          # JARVIS. Measured that day — interrupts fired at rms
                          # 0.0119-0.0388 while ambient-while-speaking ran median
                          # 0.0019, p90 0.0065, max 0.0154, so the quietest
                          # "interrupts" sat INSIDE the ambient range. Raising the
                          # floor instead would land on the user's own ~0.015-0.02
                          # speech and bring back "have to shout". But only 0.3%
                          # of 625 ambient samples ever crossed the trigger — they
                          # are isolated peaks, not sustained — so demanding more
                          # hot frames in a slightly longer window separates a
                          # deliberate "stop" from a stray peak WITHOUT making
                          # anything harder to reach by volume.
# ...but counted over a WINDOW, not consecutively. The old code did
# `+1 if over else max(0, n-1)`, which with BARGE_FRAMES=2 is identical to a hard
# reset: a single dip wipes the single banked frame, so the counter oscillates
# 1->0->1->0 and never arrives. Real speech is not flat — a short word like
# "stop" crosses the threshold on its vowel and dips on the consonants — so at
# conversational volume it logged "hearing you" over and over and never fired.
# 2-of-the-last-4 frames (~320 ms) tolerates that dip; the echo threshold below
# is what actually keeps JARVIS from interrupting itself.
BARGE_WINDOW = 6          # ~480 ms. 3-of-6, not 2-of-4 (see BARGE_FRAMES).
ECHO_MARGIN = 1.6         # must be this much louder than the predicted echo
                          # (was 2.5, which pushed the threshold above this
                          # low-gain mic's own speech level)
# Hard cap on the learned coupling. _echo_k ratchets UP via max() but decays only
# 0.5%/frame, so anything that feeds it a large ratio poisons the threshold for
# ~10 s. See the learning guard in the speaking branch.
ECHO_K_MAX = 0.35

# Measured on this machine: while JARVIS speaks at 65% volume the mic reads only
# rms 0.002-0.011, because Windows' mic-array echo cancellation strips out the
# speaker signal. A person talking is NOT cancelled and lands far above that
# (~0.03+). So the barge floor sits between the two. Tune with BARGE_FLOOR in
# .env: raise it if JARVIS interrupts itself, lower it if it ignores you.
BARGE_FLOOR = config.BARGE_FLOOR

# The floor also adapts to room noise, but that term used to be able to override
# the calibrated BARGE_FLOOR outright: `max(self._noise * 4.0, BARGE_FLOOR)`.
# Two things were wrong with it.
#   1. _noise is SEEDED at 0.004 and only refined while idle, so 0.004 * 4 =
#      0.016 beat a calibrated 0.01 on the very first frame in ANY room, however
#      quiet. Calibration was dead on arrival — the measured value never applied.
#   2. With this mic's low gain the user's own speech reads ~0.015, so a 0.016
#      floor made barge-in impossible: the "have to shout to interrupt" problem.
# Now the adaptive term may only RAISE the floor within a bounded band above the
# calibrated value, so a genuinely noisy room still suppresses false triggers but
# the calibrated number can never become meaningless. The seed is derived from
# BARGE_FLOOR so an unmeasured room starts exactly at the calibrated value.
NOISE_MULT = 4.0
NOISE_CEILING = 1.25      # floor stays within [BARGE_FLOOR, 1.25 * BARGE_FLOOR]

# While speaking, its own voice can still make the detector "hear" the wake word,
# so a barge-in on the wake word needs high confidence AND real loudness — an
# echo, by definition, cannot be louder than the echo estimate.
BARGE_WAKE_THRESHOLD = 0.9
BARGE_WAKE_LOUDNESS = 1.6


def ensure_models() -> None:
    """Download the pretrained openWakeWord models on first run (one-time)."""
    import openwakeword
    from openwakeword.utils import download_models
    model_dir = Path(openwakeword.__file__).parent / "resources" / "models"
    if not list(model_dir.glob("hey_jarvis*.onnx")):
        log.info("downloading openWakeWord models...")
        download_models()


class _Head:
    """The custom 'jarvis' classifier: a tiny MLP over openWakeWord embeddings.
    Pure numpy at inference — no sklearn/torch needed at runtime."""

    def __init__(self, path: Path):
        d = np.load(path)
        self.W = [d["W0"], d["W1"], d["W2"]]
        self.b = [d["b0"], d["b1"], d["b2"]]
        self.n_windows = int(d["n_windows"])
        self.emb_dim = int(d["emb_dim"])

    def __call__(self, features: np.ndarray) -> float:
        x = np.asarray(features, dtype=np.float32).reshape(1, -1)
        if x.shape[1] != self.W[0].shape[0]:
            return 0.0
        for W, b in zip(self.W[:-1], self.b[:-1]):
            x = np.maximum(0.0, x @ W + b)          # relu
        # .ravel()[0] — float() on a (1,1) array raises on numpy 2.x, which made
        # the head fail silently on every frame in the live listener.
        z = float((x @ self.W[-1] + self.b[-1]).ravel()[0])
        return 1.0 / (1.0 + np.exp(-z))             # sigmoid


class WakeListener:
    def __init__(self, on_wake, on_barge_in=None, get_state=None, get_output_rms=None,
                 get_media_active=None):
        """on_wake(): user said the wake word while idle.
        on_barge_in(): user started talking while JARVIS was speaking.
        get_state(): current assistant state string.
        get_output_rms(): RMS of what we're currently playing (0 when silent).
        get_media_active(): True when a video/media player is focused — wake
        confidence is raised then, since on-screen dialogue false-triggers."""
        ensure_models()
        from openwakeword.model import Model
        self._owm = Model(wakeword_models=[PRETRAINED], inference_framework="onnx")

        self._head = None
        if HEAD_PATH.exists():
            try:
                self._head = _Head(HEAD_PATH)
                log.info("custom 'jarvis' wake head loaded (any prefix)")
            except Exception as e:
                log.error("custom head failed to load (%s) — using 'hey jarvis' only", e)
        else:
            log.warning("no custom head at %s — run scripts/train_wake_jarvis.py; "
                        "falling back to the fixed phrase 'hey jarvis'", HEAD_PATH)

        # Speech-probability gate: rejects non-speech (grunts, coughs, ambient)
        # for BOTH detectors below, since the custom head bypasses the pretrained
        # model's own VAD. Best-effort — never breaks wake detection if it fails.
        self._vad = None
        if config.WAKE_VAD_THRESHOLD > 0:
            try:
                from openwakeword.vad import VAD
                self._vad = VAD()
                log.info("wake VAD gate on (>= %.2f speech) — non-speech rejected",
                         config.WAKE_VAD_THRESHOLD)
            except Exception as e:
                log.warning("VAD init failed (%s) — non-speech gating off", e)

        self._on_wake = on_wake
        self._on_barge_in = on_barge_in
        self._get_state = get_state or (lambda: "idle")
        self._get_output_rms = get_output_rms or (lambda: 0.0)
        self._get_media_active = get_media_active or (lambda: False)
        self._media_active = False       # cached; refreshed ~1/s (psutil is slow)

        self._paused = threading.Event()
        self._muted = threading.Event()     # privacy: no audio is processed at all
        self._stream: sd.InputStream | None = None
        self._cooldown = 0
        self._frame_n = 0

        # Seeded so an unmeasured room yields exactly BARGE_FLOOR (see NOISE_MULT
        # above) — a hardcoded 0.004 seed silently overrode calibration.
        self._noise = BARGE_FLOOR / NOISE_MULT
        self._echo_k = 0.05        # learned mic-vs-speaker coupling (worst case)
        self._out_hold = 0.0       # decaying hold of our own output level
        self._speech_frames = 0
        self._barge_hist: deque[bool] = deque(maxlen=BARGE_WINDOW)
        self._barge_rms: deque[float] = deque(maxlen=BARGE_WINDOW)
        self._barge_run = 0        # peak hot-frame count of the current run
        self._wake_peak = 0.0          # near-miss instrumentation (see run loop)
        self._wake_peak_rms = 0.0
        self._hot_hist: deque[bool] = deque(maxlen=WAKE_WINDOW)
        # Rolling speech probability, maintained every frame so Silero's
        # streaming state stays valid (see _cb).
        self._vad_recent: deque[float] = deque(maxlen=VAD_WINDOW)
        self._vad_broken = False
        self._hot_hist.clear()
        self._head_broken = False

    # ── lifecycle ───────────────────────────────────────────────
    def start(self) -> None:
        self._stream = sd.InputStream(
            samplerate=config.MIC_SAMPLE_RATE, channels=1, dtype="int16",
            blocksize=config.WAKE_FRAME_SAMPLES, callback=self._cb)
        self._stream.start()
        log.info("wake listener running (threshold %.2f, custom head: %s)",
                 config.WAKE_THRESHOLD, "yes" if self._head else "no")

    def is_alive(self) -> bool:
        return self._stream is not None and self._stream.active

    def restart(self) -> None:
        log.warning("wake mic stream dead — restarting")
        try:
            if self._stream is not None:
                self._stream.close()
        except Exception:
            pass
        self._owm.reset()
        self._cooldown = 8
        self.start()

    # ── mic mute (privacy) ──────────────────────────────────────
    # Hard off: no wake word, no barge-in, no audio inspected at all. Survives
    # until it is manually toggled back — no voice command can undo it, because
    # the whole point is that JARVIS isn't listening.
    def set_muted(self, muted: bool) -> None:
        if muted:
            self._muted.set()
            self._owm.reset()
            self._hot_hist.clear()
            self._speech_frames = 0
            log.info("MIC MUTED — not processing any audio")
        else:
            self._muted.clear()
            self._owm.reset()
            self._cooldown = 8
            log.info("mic unmuted — listening for the wake word again")

    def is_muted(self) -> bool:
        return self._muted.is_set()

    def arm_for_barge_in(self) -> None:
        """Re-arm the mic for the whole time JARVIS is speaking.

        The listener is paused when the wake word fires (so it can't re-trigger
        during STT), and used to stay paused until the interaction ended — which
        meant barge-in was DEAD for the entire reply. Speaking paths call this so
        the user can interrupt at any point, right through a long answer.

        Idempotent, and only a short cooldown: calling it once per sentence must
        not keep resetting a long cooldown, or a burst of short sentences would
        leave no window in which the user could actually cut in.
        """
        if self._paused.is_set():
            self._paused.clear()
            self._reset_vad()
            self._owm.reset()
            self._cooldown = 6          # ~0.5 s: ignore the start of our own audio
            self._speech_frames = 0
            self._barge_hist.clear()
            self._barge_rms.clear()
            self._barge_run = 0
            self._hot_hist.clear()
            log.info("mic armed for barge-in during speech")

    def pause(self) -> None:
        # Logged because a listener that is paused and never resumed looks
        # EXACTLY like a broken wake word: the mic is live, no error is raised,
        # and nothing at all reaches the detector. Pair each PAUSED with the
        # matching RESUMED in the log; an unmatched PAUSED is the bug.
        log.info("wake listener PAUSED — wake word inactive until resumed")
        self._paused.set()
        self._owm.reset()

    def _reset_vad(self) -> None:
        """Drop Silero's streaming state after a gap in the audio it saw.

        pause() stops feeding it mid-stream, so on the way back its LSTM state
        describes audio from before the gap. Starting clean is correct — and is
        exactly the staleness that made the gated version unreliable."""
        self._vad_recent.clear()
        if self._vad is not None:
            try:
                self._vad.reset_states()
            except Exception:
                pass

    def resume(self) -> None:
        self._reset_vad()
        self._owm.reset()
        self._cooldown = 12         # ~1 s ignored so we don't hear our own tail
        self._speech_frames = 0
        self._barge_hist.clear()
        self._barge_rms.clear()
        self._barge_run = 0
        self._hot_hist.clear()
        self._out_hold = 0.0
        self._paused.clear()
        log.info("wake listener RESUMED — listening for 'jarvis' again")

    # ── detection ───────────────────────────────────────────────
    def _wake_score(self, frame: np.ndarray) -> float:
        """Max of the pretrained phrase model and the custom bare-word head,
        gated by VAD so non-speech can't trip it."""
        score = float(self._owm.predict(frame).get(PRETRAINED, 0.0))
        if self._head is not None:
            try:
                feats = self._owm.preprocessor.get_features(self._head.n_windows)
                score = max(score, self._head(feats))
            except Exception as e:
                # Loudly, once — a silently broken head means the bare word
                # "jarvis" quietly stops working and nobody notices.
                if not self._head_broken:
                    self._head_broken = True
                    log.error("custom wake head is failing (%s) — falling back to "
                              "the 'hey jarvis' phrase model only", e)
        # VAD gate — only when already near a hit (VAD isn't free): a wake must
        # coincide with real speech, so a grunt/cough/sigh that briefly spikes the
        # acoustic model is rejected here.
        # Read the CONTINUOUSLY-maintained speech probability (see _cb) rather
        # than calling predict() here on an isolated frame, which corrupted the
        # streaming state. Take the max over the short recent window: the speech
        # peak and the acoustic peak rarely land on the same 80 ms frame.
        if self._vad is not None and score >= 0.3 and self._vad_recent:
            speech = max(self._vad_recent)
            if speech < config.WAKE_VAD_THRESHOLD:
                # Previously a silent `return 0.0`. A VAD that rejects real
                # speech makes the wake word look totally dead with NOTHING
                # in the log to explain it — make the rejection visible.
                log.info("wake REJECTED by VAD: acoustic score %.2f but speech "
                         "prob %.3f < WAKE_VAD_THRESHOLD %.3f",
                         score, speech, config.WAKE_VAD_THRESHOLD)
                return 0.0
        return score

    def _cb(self, indata, frames, t, status) -> None:
        # An exception here would abort the PortAudio stream permanently, which
        # is exactly how wake detection died silently before.
        try:
            # Muted: drop the audio on the floor before anything looks at it.
            if self._muted.is_set():
                return
            if self._paused.is_set():
                return

            frame = indata[:, 0]
            rms = float(np.sqrt(np.mean((frame / 32768.0) ** 2)))
            state = self._get_state()

            # Silero is a STREAMING model: it carries LSTM state (_h/_c) between
            # calls and only produces meaningful probabilities when fed a
            # CONTINUOUS stream. It used to be called only when the acoustic score
            # already cleared 0.3 ("VAD isn't free"), i.e. on sparse, isolated
            # frames with state left over from whenever it last fired — which
            # returns near-garbage. Measured on this machine: the same real speech
            # reads 0.99 fed contiguously and 0.009 fed 1-frame-in-12, and real
            # speech was scoring BELOW digital silence (0.039). That is what
            # rejected every "hey jarvis" at speech_prob 0.003-0.011.
            # The saving was illusory: predict() costs 0.475 ms per 80 ms frame,
            # 0.59% of one core. So feed it every frame and keep a short rolling
            # window, since the speech peak can sit a frame either side of the
            # acoustic peak.
            if self._vad is not None:
                try:
                    self._vad_recent.append(float(self._vad.predict(frame)))
                except Exception as e:
                    if not self._vad_broken:
                        self._vad_broken = True
                        log.error("VAD is failing (%s) — speech gating disabled "
                                  "rather than silently rejecting every wake", e)
                    self._vad = None

            self._frame_n += 1
            if self._frame_n % 4 == 0:
                from state import STATE
                STATE.level(min(1.0, rms * 8))

            if self._cooldown > 0:
                self._cooldown -= 1
                return

            # ── barge-in / cancel while JARVIS is SPEAKING or THINKING ──
            # Speaking: subtract our own echo. Thinking: we're silent, so any real
            # speech (or the wake word) is a "stop" — that's how the user cancels
            # a long LLM/tool call, not just TTS playback.
            if state in ("speaking", "thinking"):
                if state == "speaking":
                    # The player's level briefly reads 0 between chunks while the
                    # sound card drains — hold it so the echo estimate doesn't
                    # collapse mid-sentence (JARVIS used to interrupt its own tail).
                    self._out_hold = max(self._get_output_rms(), self._out_hold * 0.85)
                    ref = self._out_hold
                    # Only learn the coupling from frames that are plausibly PURE
                    # echo. The old guard (rms < ref * 1.2) admitted frames where
                    # the user was clearly talking, and because _echo_k takes a
                    # max() those frames ratcheted the estimate up — the user's own
                    # voice raised the bar against them mid-barge, then decayed
                    # back only over ~10 s. That is why `echo` was observed at
                    # 0.02-0.03 here but 0.008 on an idle boot.
                    if ref > 0.02 and rms < ref * 0.5:
                        self._echo_k = min(ECHO_K_MAX,
                                           max(self._echo_k * 0.995, rms / ref))
                    expected_echo = self._echo_k * ref
                else:
                    expected_echo = 0.0      # thinking: nothing playing, no echo

                # Adaptive, but bounded: room noise can lift the floor above the
                # calibrated BARGE_FLOOR only as far as NOISE_CEILING allows.
                adaptive = self._noise * NOISE_MULT
                floor = min(max(adaptive, BARGE_FLOOR), BARGE_FLOOR * NOISE_CEILING)
                if adaptive > BARGE_FLOOR * NOISE_CEILING and self._frame_n % 100 == 0:
                    # Visible rather than silent: if the room really is this loud,
                    # BARGE_FLOOR itself needs re-calibrating.
                    log.warning("room noise %.4f wants a floor of %.4f — clamped to "
                                "%.4f (%.2fx BARGE_FLOOR). Re-run calibrate_barge.py "
                                "if barge-in misfires.",
                                self._noise, adaptive, floor, NOISE_CEILING)
                trigger = max(floor, expected_echo * ECHO_MARGIN)
                over = rms > trigger
                self._barge_hist.append(over)
                self._barge_rms.append(rms)
                hits = sum(self._barge_hist)

                # NEAR-MISS: a run of hot frames that decayed away WITHOUT firing.
                # This is the other half of the evidence — to judge whether 3-of-6
                # is right we need the patterns that were rejected, not only the
                # ones that fired. Logged once per run, when it goes cold.
                if over:
                    self._barge_run = max(self._barge_run, hits)
                elif self._barge_run and hits == 0:
                    if self._barge_run >= 1:
                        log.info("barge near-miss while %s: peaked at %d/%d hot "
                                 "frames in %d — did NOT interrupt | frames %s | "
                                 "trigger=%.4f rms seq: %s",
                                 state, self._barge_run, BARGE_FRAMES, BARGE_WINDOW,
                                 "".join("#" if h else "." for h in self._barge_hist),
                                 trigger,
                                 " ".join(f"{r:.4f}" for r in self._barge_rms))
                    self._barge_run = 0
                if over and hits == 1:            # first frame over — visible,
                    log.info("barge: hearing you over %s (rms=%.4f > trigger=%.4f "
                             "[floor=%.4f echo=%.4f k=%.3f]) — %d of last %d frames "
                             "to interrupt",
                             state, rms, trigger, floor, expected_echo,
                             self._echo_k, BARGE_FRAMES, BARGE_WINDOW)

                wake_score = self._wake_score(frame)
                above_echo = rms > max(floor, expected_echo * BARGE_WAKE_LOUDNESS)
                said_wake = wake_score >= BARGE_WAKE_THRESHOLD and above_echo

                if self._frame_n % 20 == 0:      # periodic level readout for tuning
                    log.info("barge watch [%s]: rms=%.4f trigger=%.4f floor=%.4f "
                             "echo=%.4f k=%.3f ref=%.4f hits=%d/%d",
                             state, rms, trigger, floor, expected_echo,
                             self._echo_k, self._out_hold, hits, BARGE_WINDOW)
                if said_wake or hits >= BARGE_FRAMES:
                    # Log the ACTUAL frame pattern, not just the firing rms.
                    # The 3-of-6 window was chosen from an estimate because the
                    # old log only recorded the rms at the moment of firing, so
                    # there was no way to see whether real interrupts sustain and
                    # ambient peaks don't. '#' = over trigger, '.' = under, oldest
                    # first; rms values are the matching per-frame levels.
                    pattern = "".join("#" if h else "." for h in self._barge_hist)
                    levels = " ".join(f"{r:.4f}" for r in self._barge_rms)
                    log.info("barge-in/cancel while %s (%s, rms=%.4f, wake=%.2f) "
                             "| frames %s (%d/%d of last %d) | rms seq: %s",
                             state, "wake word" if said_wake else "speech", rms,
                             wake_score, pattern, hits, BARGE_FRAMES, BARGE_WINDOW,
                             levels)
                    self._barge_hist.clear()
                    self._barge_rms.clear()
                    self._barge_run = 0
                    self._barge_rms.clear()
                    self.pause()
                    if self._on_barge_in:
                        self._on_barge_in()
                return

            # "sleeping" is dormant, not off: the wake word still works.
            if state not in ("idle", "sleeping"):   # listening -> not ours
                return

            # ── idle: noise floor + wake word ───────────────────
            self._noise = 0.995 * self._noise + 0.005 * rms
            score = self._wake_score(frame)

            # Refresh the (slow) media-app check about once a second, not every
            # 80 ms frame. While a video player is focused, on-screen dialogue is
            # the main false-trigger source — so demand higher confidence AND an
            # extra hot frame before believing it's really the wake word.
            if self._frame_n % 12 == 0:
                try:
                    self._media_active = bool(self._get_media_active())
                except Exception:
                    self._media_active = False
            threshold = config.WAKE_THRESHOLD + (config.WAKE_MEDIA_BOOST
                                                 if self._media_active else 0.0)
            need = WAKE_CONSECUTIVE + (1 if self._media_active else 0)

            # NEAR-MISS INSTRUMENTATION. Until now the ONLY wake log was the
            # success line below, so a wake word that never fires left the log
            # completely empty — there was no grep pattern that could show why.
            # Track the peak score and report it periodically whenever there was
            # any acoustic activity at all, so a failed "hey jarvis" is visible
            # with its actual score. NOTE: peak rms is DIAGNOSTIC ONLY — nothing
            # in the wake path gates on loudness, only on `score` and `hot`.
            self._wake_peak = max(self._wake_peak, score)
            self._wake_peak_rms = max(self._wake_peak_rms, rms)
            if self._frame_n % 25 == 0:                  # ~2 s
                if self._wake_peak >= 0.15:
                    log.info("wake near-miss: peak score %.2f (threshold %.2f%s), "
                             "peak rms %.4f, speech prob %.3f (need %.3f), "
                             "hot frames needed %d — NOT triggered",
                             self._wake_peak, threshold,
                             ", media" if self._media_active else "",
                             self._wake_peak_rms,
                             max(self._vad_recent) if self._vad_recent else -1.0,
                             config.WAKE_VAD_THRESHOLD, need)
                self._wake_peak = 0.0
                self._wake_peak_rms = 0.0

            # Debounce: the spoken word spans several 80 ms frames, so a genuine
            # "jarvis" stays hot for 2+ frames. One-frame spikes are music, a
            # video, or room noise clipping the threshold — those caused a string
            # of false wakes at 0.51-0.58 while real speech scores 0.9+.
            #
            # Counted over a WINDOW, not consecutively. This was `self._hot_frames
            # = 0` on any miss — a HARD RESET, and therefore an even stronger form
            # of the bug already fixed in the barge-in path: a real "hey jarvis"
            # whose score dips for a single 80 ms frame (the gap between "hey" and
            # "jarvis", or a consonant) could never bank 2 in a row, no matter how
            # high it peaked. A logged near-miss of 0.82 against a 0.55 threshold
            # is exactly that failure. 2-of-the-last-4 keeps the anti-spike
            # guarantee (one isolated frame still cannot wake it) while tolerating
            # the dip. Media mode still demands `need`+1 hits from the same window,
            # so it gets stricter, not looser.
            self._hot_hist.append(score >= threshold)
            if sum(self._hot_hist) >= need:
                # Speech-prob is logged on the SUCCESS line too, not just on
                # rejections. Without it there is no way to tell afterwards
                # whether a false wake (ambient conversation) looked different
                # from a real one — and acoustic score alone does not separate
                # them: measured false wakes reach 0.98-1.00, same as genuine.
                log.info("wake word detected (%.2f, threshold %.2f%s, speech prob "
                         "%.3f, rms %.4f)", score, threshold,
                         ", media" if self._media_active else "",
                         max(self._vad_recent) if self._vad_recent else -1.0, rms)
                self._hot_hist.clear()
                self.pause()
                self._on_wake()
        except Exception as e:
            log.error("wake callback error (suppressed): %s", e)
