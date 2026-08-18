"""JARVIS backend: orchestrator + WebSocket server for the HUD.

Flow per interaction:
  wake word ("jarvis", any prefix — local) -> chime -> Deepgram STT
    -> routines (local JSON)      -> actions + spoken line
    -> fast path (local, <300 ms) -> direct OS calls
    -> smart path (Groq + tools)  -> real tool calls, spoken reply
  ...then a follow-up window keeps the mic open so the conversation continues
  without repeating the wake word. Barge-in stops playback the moment the user
  starts talking over it.
"""
import asyncio
import contextlib
import datetime
import json
import logging
import logging.handlers
import os
import socket
import threading
import time

import numpy as np
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

import config
from audio import stt, tts, wake
from audio.player import PLAYER
from router import fast_path, refer, routines, smart_path
from state import IDLE, LISTENING, SLEEPING, SPEAKING, STATE, THINKING
from system import briefing, clipboard, ducking, foreground, health, idle, indexer
from system import media, reminders, timetable, timers
from system import weather as weather_mod

# Rotating, not a single ever-growing file. The per-frame barge/wake debug lines
# added while tuning the audio pipeline grow this fast, and an unreadable
# multi-hundred-megabyte log is a log nobody can diagnose from. 5 MB x 3 keeps
# roughly the last few sessions and caps the whole thing at 20 MB.
_file_handler = logging.handlers.RotatingFileHandler(
    config.LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-18s %(levelname)-7s %(message)s",
    handlers=[logging.StreamHandler(), _file_handler],
)
log = logging.getLogger("jarvis")

# What the HUD shows while a tool runs. Raw function names ("delete_path") look
# like leaked internals on screen; these read as JARVIS narrating himself. An
# unlisted tool falls back to its name with underscores spaced out, so adding a
# tool can never put an identifier back on the user's screen.
TOOL_STEP_LABELS = {
    "open_path":         "opening it",
    "delete_path":       "finding that file",
    "move_path":         "finding that file",
    "list_directory":    "looking in that folder",
    "read_file":         "reading it",
    "write_file":        "writing that down",
    "run_shell_command": "running that",
    "web_search":        "searching the web",
    "open_url":          "opening the browser",
    "get_system_health": "checking the machine",
    "get_top_processes": "checking what's running",
    "look_at_screen":    "looking at your screen",
    "get_timetable":     "checking your timetable",
    "remember":          "making a note",
    "forget":            "clearing that",
    "set_reminder":      "setting a reminder",
    "play_music":        "queuing that up",
    "find_file":         "searching your files",
}

# Date of the last morning briefing, so it fires once a day and not on every
# return from a coffee break.
_last_briefing_date: "datetime.date | None" = None

_wake_listener: wake.WakeListener | None = None
_history: list[dict] = []          # smart-path conversation memory
_busy = asyncio.Lock()
_barge_in = asyncio.Event()        # set when the user talks over JARVIS
_pending_offer = None              # callable to run if the next reply is "yes"
_sleeping = False                  # dormant, but the wake listener stays alive
_muted = False                     # privacy: mic fully off (wake word + button)
_last_reference: str | None = None  # the openable thing JARVIS last named, so
                                    # "open it" resolves to that — never fuzzy-
                                    # matched (which once opened 'iSCSI Initiator')
_confirming = False                 # awaiting a spoken yes/no on a destructive
                                    # action — the smart-path timeout pauses so
                                    # an interactive confirmation isn't killed
_last_spoken = ""                   # the last line JARVIS actually said, for
                                    # "repeat that" / "say that again"
_snooze_until = 0.0                 # monotonic deadline of a timed do-not-disturb
# Timer/stopwatch state now lives in system/timers.py (shared with the tool).

AFFIRMATIVE = ("yes", "yeah", "yep", "yup", "sure", "please do", "go ahead", "do it",
               "confirm", "confirmed", "affirmative", "okay", "ok", "alright", "aye")
NEGATIVE = ("no", "nope", "don't", "do not", "cancel", "stop", "never mind", "nevermind")


def _is_yes(text: str) -> bool:
    t = text.strip().lower().rstrip(".!")
    return t.startswith(AFFIRMATIVE) and not t.startswith(NEGATIVE)


@contextlib.asynccontextmanager
async def _lifespan(app):
    await _startup()
    yield


app = FastAPI(title="JARVIS backend", lifespan=_lifespan)


def _online(host="api.deepgram.com") -> bool:
    try:
        socket.create_connection((host, 443), timeout=1.5).close()
        return True
    except OSError:
        return False


def _chime(soft: bool = False) -> None:
    """Local acknowledgment blip (no TTS, no network). Soft = follow-up window."""
    sr = config.TTS_SAMPLE_RATE
    if soft:
        t1 = np.linspace(0, 0.06, int(sr * 0.06), False)
        tone = np.sin(2 * np.pi * 1100 * t1) * 0.15
    else:
        t1 = np.linspace(0, 0.07, int(sr * 0.07), False)
        t2 = np.linspace(0, 0.09, int(sr * 0.09), False)
        tone = np.concatenate([np.sin(2 * np.pi * 880 * t1) * 0.25,
                               np.sin(2 * np.pi * 1320 * t2) * 0.2])
    fade = np.minimum(1, np.linspace(0, 8, tone.size))[::-1]
    PLAYER.enqueue((tone * np.minimum(fade, 1) * 32767).astype(np.int16).tobytes())
    PLAYER.end_of_utterance()


async def _speak(text: str, cached_ok: bool = True) -> None:
    """Speak one line, honouring barge-in.

    Two guards make 'stop'/'shut up' work EVERYWHERE, not just in the smart path:
      * If the user has already interrupted (_barge_in set), return at once
        without speaking — so in a multi-line sequence (boot / morning briefing)
        the remaining lines are skipped instead of ploughing on.
      * The actual TTS runs as a cancellable child task tracked in
        _active_speech, so barge-in can tear down the ElevenLabs stream itself.
        Without this, stop_now() only flushed what was queued and the still-
        running stream kept enqueuing fresh chunks — playback resumed mid-line.
    """
    global _active_speech, _last_spoken
    if _barge_in.is_set():
        log.info("skipping speech (interrupted): %r", text[:60])
        return
    if text and text.strip():
        _last_spoken = text          # for "repeat that"
    STATE.set_state(SPEAKING)
    STATE.reply(text)
    if _wake_listener:
        _wake_listener.arm_for_barge_in()   # interruptible for the whole utterance

    async def _play() -> None:
        if not (cached_ok and tts.speak_canned(text)):
            await tts.say(text)
        await _wait_playback()

    _active_speech = asyncio.ensure_future(_play())
    try:
        await _active_speech
    except asyncio.CancelledError:
        log.info("speech interrupted mid-line: %r", text[:60])
    finally:
        _active_speech = None


async def _wait_playback() -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, PLAYER.wait_done)


# ── Confirmation for destructive tool calls ─────────────────────
def _is_no(text: str) -> bool:
    t = text.strip().lower().rstrip(".!?")
    return t.startswith(NEGATIVE)


def _describe_pending(description: str) -> str:
    """A plain, honest answer to 'what will it do?' for a pending action, built
    from the action's OWN details — no LLM call, so it can't stall the turn."""
    d = description.strip()
    if d.lower().startswith("run the command"):
        cmd = d.split(":", 1)[1].strip() if ":" in d else d
        return (f"It would run this on your PC, sir: {cmd}. I can't cleanly undo "
                f"that, which is why I'm asking first.")
    return (f"It would {d}, sir — and that can't be neatly undone, which is why "
            f"I'm asking first.")


async def _confirm(description: str) -> bool:
    """Speak what's about to happen and wait for a spoken yes/no.

    A clarifying QUESTION about the action ('what will it do?', 'is that safe?',
    'why do you need that?') is answered honestly and then re-asked — it is NOT a
    decline. Only a real 'no'/'cancel'/'stop' declines; only a real 'yes'/'go
    ahead' approves. Silence declines.
    """
    global _confirming
    log.info("awaiting confirmation: %s", description)
    await _speak(f"I'm about to {description}. Shall I go ahead, sir?", cached_ok=False)
    try:
        _confirming = True
        for _ in range(3):          # allow a couple of clarifying questions
            STATE.set_state(LISTENING)
            _chime(soft=True)
            reply = await stt.listen_once(silence_timeout=8)
            if reply and _is_yes(reply):
                log.info("confirmation %r -> APPROVED", reply)
                return True
            if not reply or _is_no(reply):
                log.info("confirmation %r -> DECLINED", reply)
                await _speak("Cancelled, sir.", cached_ok=False)
                return False
            # Neither yes nor no -> a question about the action. Answer + re-ask.
            log.info("confirmation %r -> clarifying question; explaining", reply)
            await _speak(_describe_pending(description), cached_ok=False)
            await _speak("Shall I go ahead, sir?", cached_ok=False)
        await _speak("I'll hold off for now, sir.", cached_ok=False)
        return False
    finally:
        _confirming = False


async def _shutdown_computer() -> None:
    """Explicit, confirmed OS shutdown — the only path that powers off the PC.

    Reached solely by a 'shutdown my pc'-class phrase. Makes crystal-clear this
    is the whole computer, not JARVIS, and only proceeds on an explicit yes.
    """
    STATE.event("confirming FULL COMPUTER shutdown")
    await _speak("That will shut down the entire computer, not just me, sir. "
                 "Are you sure? Say yes to shut down.", cached_ok=False)
    STATE.set_state(LISTENING)
    _chime(soft=True)
    reply = await stt.listen_once(silence_timeout=8)
    if reply and _is_yes(reply):
        log.warning("USER CONFIRMED full OS shutdown")
        await _speak("Shutting down the computer, sir.", cached_ok=False)
        await _wait_playback()
        from router import tools as _tools
        _tools.shutdown_computer()
    else:
        log.info("OS shutdown declined (%r) — cancelling cleanly", reply)
        await _speak("Cancelled, sir. Leaving everything as it is.", cached_ok=False)


# ── The interaction loop ────────────────────────────────────────
async def handle_interaction(auto_brief: bool = True) -> None:
    """Wake -> listen -> act -> respond, then keep the conversation open."""
    global _pending_offer
    async with _busy:
        try:
            _barge_in.clear()       # start fresh — no stale interrupt suppressing
            # Woken from sleep: come back up first, then carry on normally.
            if _sleeping:
                await _wake_from_sleep()

            ducking.duck()

            # First wake of the day: read the fresh briefing unprompted.
            if auto_brief and briefing.should_auto_brief():
                await _do_briefing()

            first = True
            rounds = 0
            while rounds < 8:               # hard cap: no unbounded loops
                rounds += 1
                # PRIVACY: muting must end the conversation, not merely stop the
                # NEXT wake word. Without this check the follow-up loop kept
                # opening new STT sessions after the mic was muted — a private
                # conversation continued streaming to Deepgram for over a minute
                # past the mute, which is exactly what mute exists to prevent.
                if _muted:
                    log.info("mic muted mid-interaction — ending the conversation")
                    STATE.event("microphone muted")
                    break
                _barge_in.clear()
                STATE.set_state(LISTENING)
                _chime(soft=not first)
                timeout = stt.SILENCE_TIMEOUT if first else config.FOLLOWUP_WINDOW
                text = await stt.listen_once(silence_timeout=timeout)

                if not text:
                    if first:
                        # SILENT. Measured: 11 of 33 wakes in a day were false,
                        # triggered by ambient conversation — and their acoustic
                        # scores (0.98-1.00) are indistinguishable from genuine
                        # ones, so detection cannot filter them. The actual harm
                        # was JARVIS announcing "Sorry, I didn't catch that" into
                        # a private phone call. Closing quietly makes a false wake
                        # harmless instead of embarrassing; a real wake that got
                        # no speech simply ends, which is also the right result.
                        log.info("wake fired but no speech followed — closing "
                                 "silently (likely a false wake)")
                        STATE.event("no speech detected")
                    else:
                        log.info("follow-up window closed (silence)")
                        STATE.event("follow-up window closed")
                    break

                # A pending "want the full breakdown?" style offer.
                if _pending_offer is not None:
                    offer, _pending_offer = _pending_offer, None
                    if _is_yes(text):
                        await offer()
                        first = False
                        continue

                keep_going = await respond_to(text)
                await _wait_playback()
                if not keep_going:
                    break
                first = False
                STATE.event(f"follow-up window open ({config.FOLLOWUP_WINDOW:.0f}s) — just speak")
        finally:
            ducking.unduck()
            await _wait_playback()
            # If the interaction ended by going to sleep, stay asleep.
            if not _sleeping:
                STATE.set_state(IDLE)
            if _wake_listener:
                _wake_listener.resume()


async def respond_to(text: str) -> bool:
    """Route one transcript. Returns False when the conversation should end."""
    global _last_reference, _pending_offer
    log.info("heard: %r", text)
    STATE.event(f"heard: {text}")
    loop = asyncio.get_running_loop()

    # ── a pending "did you mean...?" / "want the breakdown?" yes/no ──
    # (handle_interaction consumes this first in the voice loop; this makes it
    # work for the typed / simulated path too.)
    if _pending_offer is not None:
        offer, _pending_offer = _pending_offer, None
        if _is_yes(text):
            await offer()
            return True
        # a non-yes: drop the offer and treat `text` as a brand-new command

    # ── SELF-CORRECTION: "open chrome, actually open notepad" -> notepad ──
    corrected = refer.apply_self_correction(text)
    if corrected != text:
        log.info("self-correction: %r -> %r", text, corrected)
        text = corrected

    # ── PRONOUN: "open it" -> whatever JARVIS just named ─────────
    if refer.is_pronoun_open(text):
        if _last_reference:
            log.info("pronoun 'open it' -> last reference %r", _last_reference)
            text = f"open {_last_reference}"
        else:
            await _speak("Open what, sir?", cached_ok=False)
            return True

    # ── ROUTINES (user-editable JSON, checked before anything else) ──
    routine = routines.match(text)
    if routine is not None:
        return await _run_routine(routine)

    # ── FAST PATH: local, no LLM ────────────────────────────────
    result = await loop.run_in_executor(None, fast_path.try_match, text)
    if result is not None:
        if result.command == "briefing":
            await _do_briefing()
            return True
        if result.command == "status_report":
            await _speak(_status_report(), cached_ok=False)
            return True
        if result.command == "sleep":
            await _go_to_sleep()        # dormant, NOT a quit, NOT an OS shutdown
            return False
        if result.command == "shutdown_pc":
            await _shutdown_computer()  # the ONLY real OS-shutdown path
            return False
        if result.command == "repeat":
            if _last_spoken:
                await _speak(_last_spoken, cached_ok=False)
            else:
                await _speak("I haven't said anything yet, sir.", cached_ok=False)
            return True
        if result.command == "snooze":
            await _start_snooze(result.data.get("minutes", 30))
            return False                # go quiet — end the conversation window
        if result.command in ("timer_set", "stopwatch_start", "timer_stop"):
            await _handle_timer(result.command, result.data or {})
            return True
        if result.command == "song_maybe":
            track = result.data["track"]

            async def _play_it() -> None:
                line = await loop.run_in_executor(None, media.play_track, track)
                await _speak(line, cached_ok=False)

            _pending_offer = _play_it
            await _speak(result.reply, cached_ok=False)   # "Did you mean ...?"
            return True
        if result.command == "open_maybe":
            data = result.data or {}

            async def _open_it() -> None:
                from system import apps
                ok, line = await loop.run_in_executor(
                    None, apps.open_resolved, data.get("path"),
                    data.get("kind"), data.get("name"))
                await _speak(line, cached_ok=False)

            _pending_offer = _open_it
            await _speak(result.reply, cached_ok=False)   # "Did you mean ...?"
            return True
        await _speak(result.reply, cached_ok=result.canned)
        _last_reference = refer.extract_referent(result.reply)
        return result.command != "end_conversation"

    # ── SMART PATH: LLM (Gemini -> Cerebras -> Groq) + tools ────
    if not config.HAS_LLM or not _online():
        log.warning("smart path unavailable (no LLM key or offline)")
        STATE.reply("(offline — smart path unavailable)")
        await _speak("I'm offline at the moment, sir.")
        return False

    global _active_smart_task
    STATE.set_state(THINKING)
    _barge_in.clear()
    if _wake_listener:
        _wake_listener.arm_for_barge_in()   # cancellable by voice during THINKING
    sentences: list[str] = []
    code_blocks: list[str] = []             # shown in the HUD, never spoken
    tool_context: list[dict] = []           # tool results, for follow-ups (G)
    failed = False
    cancelled = False

    def _on_tool(name: str):
        # Surface the step in the HUD, but in JARVIS's voice rather than as a raw
        # function name: "tool-calling: delete_path" in a user-facing transcript
        # reads like leaked internals, which is precisely the thing this project
        # otherwise works hard to keep off the screen. The EXACT tool name still
        # goes to jarvis.log on the next line, so debugging loses nothing.
        STATE.event(TOOL_STEP_LABELS.get(name, name.replace("_", " ")) + "...")
        log.info("STATE thinking   -> tool      [%s]", name)

    async def _drive_reply():
        """Consume the smart-path stream. Runs as a cancellable task so a stop
        word / cancel button can kill the Groq + tool chain instantly."""
        async for sentence in smart_path.respond(text, _history, confirm_cb=_confirm,
                                                 tool_sink=tool_context, on_tool=_on_tool,
                                                 display_sink=code_blocks):
            sentences.append(sentence)
            STATE.set_state(SPEAKING)
            # Code is shown but never spoken (see smart_path._split_code).
            shown = " ".join(sentences)
            if code_blocks:
                shown += "\n\n" + "\n\n".join(code_blocks)
            STATE.reply(shown)
            if _wake_listener:
                _wake_listener.arm_for_barge_in()   # live for the whole reply
            await tts.speak(sentence)

    _active_smart_task = asyncio.ensure_future(_drive_reply())
    try:
        # Hard ceiling: a request (incl. chained tool calls) must never hang the
        # HUD on "thinking" forever. But an interactive confirmation (_confirming)
        # legitimately waits on the user — the clock pauses then, so answering
        # "what will it do?" and then "yes" isn't guillotined mid-exchange.
        while True:
            try:
                await asyncio.wait_for(asyncio.shield(_active_smart_task),
                                       timeout=config.SMART_TIMEOUT)
                break                           # finished on its own
            except asyncio.TimeoutError:
                if _active_smart_task.done():
                    break
                if _confirming:
                    continue                    # waiting on the user — don't kill it
                _active_smart_task.cancel()
                raise
    except asyncio.CancelledError:
        cancelled = True                    # user hit stop / the cancel button
        log.info("smart path cancelled by the user")
    except asyncio.TimeoutError:
        failed = True
        log.error("smart path timed out after %ss — abandoning the request",
                  config.SMART_TIMEOUT)
    except Exception as e:
        log.error("smart path failed: %s", e)
        failed = True
    finally:
        _active_smart_task = None

    # EVERY smart-path turn ends in exactly one logged outcome. Previously a run
    # that completed normally but yielded ZERO sentences fell through to the
    # "turn not stored" line: nothing spoken, nothing explaining why, and from
    # the outside indistinguishable from the process having died. Same principle
    # as the OPEN OK / NOT OPENED wrapper on open_target.
    outcome = ("cancelled" if cancelled else "failed" if failed
               else "ok" if sentences else "EMPTY")
    log.info("TURN %s: %r -> %d sentence(s), %d tool call(s)",
             outcome, text[:60], len(sentences), len(tool_context))

    if cancelled:
        # The user cut in. Return cleanly to listening — do NOT store the turn.
        PLAYER.stop_now()
        return True
    if failed:
        PLAYER.stop_now()                       # drop any half-played audio
        if not sentences:
            # If a tool actually SUCCEEDED before the turn fell over, say what it
            # did. Observed: open_maps opened the real route, then the follow-up
            # round timed out and JARVIS announced "Couldn't pin that down, sir"
            # over a browser window that was already showing the directions —
            # reporting failure for work that plainly succeeded.
            spoken = None
            for tc in reversed(tool_context):
                try:
                    say = json.loads(tc.get("result") or "{}").get("say")
                except (ValueError, AttributeError):
                    say = None
                if say:
                    spoken = say
                    break
            if spoken:
                log.info("turn failed but %s succeeded — reporting the real "
                         "outcome instead", tool_context[-1].get("name"))
                await _speak(spoken, cached_ok=False)
            else:
                await _speak("Couldn't pin that down, sir.", cached_ok=False)
    elif not sentences:
        # The model returned nothing at all. Never leave the user in silence
        # wondering whether it heard them.
        log.error("smart path produced NO reply for %r — model returned an empty "
                  "response", text[:80])
        await _speak("I didn't get anywhere with that one, sir.", cached_ok=False)

    reply = " ".join(sentences).strip()
    _last_reference = refer.extract_referent(reply)   # for a follow-up "open it"

    # A failed or markup-poisoned turn must NOT go into the conversation buffer:
    # carrying it forward made every SUBSEQUENT question fail too.
    if failed or not reply or smart_path.looks_like_markup(reply):
        if reply and smart_path.looks_like_markup(reply):
            log.error("dropping poisoned turn from memory: %r", reply[:120])
        log.info("turn not stored in conversation memory")
        return True

    # Keep the real tool outputs alongside the spoken reply so follow-ups like
    # "which one specifically?" can reason from the actual data (G).
    _history.append({"role": "user", "content": text})
    for tc in tool_context:
        _history.append({"role": "assistant", "content":
                         f"[tool {tc['name']} returned: {tc['result']}]"})
    _history.append({"role": "assistant", "content": reply})
    del _history[:-24]              # keep the last ~12 exchanges (tool results eat into this)
    return True


async def _run_routine(routine: routines.Routine) -> bool:
    """Speak the routine's line, run its actions."""
    loop = asyncio.get_running_loop()

    # "sleep" is handled here (not in run_actions) because it must speak and
    # await playback before going dormant.
    if "sleep" in routine.actions:
        if routine.say:
            await _speak(routine.say, cached_ok=True)
        await loop.run_in_executor(
            None, routines.run_actions,
            [a for a in routine.actions if a != "sleep"],
            lambda: _shutdown("routine"))
        await _go_to_sleep()
        return False

    line = routine.say
    if routine.say_dynamic == "system_health":
        # REAL numbers, phrased in character. The model never invents them.
        facts = await loop.run_in_executor(None, health.summary_line)
        try:
            line = await smart_path.phrase(
                "Greet the user and report the machine's condition.", facts)
        except Exception as e:
            log.error("dynamic phrasing failed (%s) — speaking raw facts", e)
            line = f"Welcome back, sir. {facts}."
    elif routine.say_dynamic:
        log.warning("unknown say_dynamic %r", routine.say_dynamic)

    if line:
        await _speak(line, cached_ok=bool(routine.say))

    quitting = "quit" in routine.actions
    await loop.run_in_executor(None, routines.run_actions, routine.actions,
                               lambda: _shutdown("routine"))
    return not quitting


# ── Briefing / status ───────────────────────────────────────────
async def _open_claude_desktop() -> None:
    from system import apps
    status, line, _ = await asyncio.get_running_loop().run_in_executor(
        None, apps.open_target, "claude")
    await _speak(line if status == "opened" else "I couldn't find Claude Desktop, sir.",
                 cached_ok=False)


async def _do_briefing() -> None:
    """Read today's briefing, then offer the full breakdown."""
    global _pending_offer
    text = briefing.read_text()
    if not text:
        age = briefing.age_hours()
        log.info("briefing unavailable (age=%s)", age)
        await _speak("No fresh briefing yet, sir.", cached_ok=False)
        return

    log.info("reading briefing (%d chars)", len(text))
    briefing.mark_read_today()
    try:
        line = await smart_path.phrase(
            "Deliver this morning briefing aloud to the user, in character, in a "
            "few short sentences. Keep every fact accurate; drop nothing important.",
            text)
    except Exception as e:
        log.error("briefing phrasing failed (%s) — reading it verbatim", e)
        line = text
    await _speak(line, cached_ok=False)
    if _barge_in.is_set():          # user cut the briefing off — don't dangle an
        return                      # unheard "want the breakdown?" yes/no offer
    await _speak("Want the full breakdown, sir?", cached_ok=False)
    _pending_offer = _open_claude_desktop


def _status_report() -> str:
    import datetime
    now = datetime.datetime.now()
    return (f"It's {now.strftime('%I:%M %p').lstrip('0')} on "
            f"{now.strftime('%A')}, and {briefing.status_phrase()}, sir.")


# ── Sleep / wake ────────────────────────────────────────────────
# Sleep is NOT a quit. It stops the voice, drops the HUD to dark, and stops
# processing — but the wake listener keeps running, so "jarvis" (or "get up
# jarvis") brings everything straight back with no start.bat.
# The only true quit is the HUD's X button, Ctrl+Alt+Q, or the tray menu.
async def _go_to_sleep() -> None:
    global _sleeping, _last_reference
    if _sleeping:
        return
    _last_reference = None
    log.info("SLEEP: going dormant (wake listener stays alive)")
    if not tts.speak_canned("Going to sleep, sir."):
        await tts.speak("Going to sleep, sir.")
    await _wait_playback()

    _sleeping = True
    PLAYER.stop_now()               # kill any queued speech
    ducking.unduck()                # never leave other apps turned down
    _history.clear()
    STATE.set_state(SLEEPING)
    STATE.event("asleep — say 'jarvis' to wake me")
    if _wake_listener:
        _wake_listener.resume()     # the one thing that stays awake


# ── Snooze / do-not-disturb (timed) ─────────────────────────────
async def _start_snooze(minutes: int) -> None:
    """Temporarily disable the wake word for `minutes`, then auto-resume. Distinct
    from the manual mute toggle (which has no timer)."""
    global _snooze_until
    mins = max(1, int(minutes))
    unit = "minutes" if mins != 1 else "minute"
    await _speak(f"Understood, sir. I'll go quiet for {mins} {unit} and start "
                 f"listening again after.", cached_ok=False)
    await _wait_playback()
    set_muted(True)
    deadline = time.monotonic() + mins * 60
    _snooze_until = deadline
    STATE.event(f"do-not-disturb for {mins} {unit}")
    log.info("snooze: wake word off for %d minutes", mins)

    async def _resume_after() -> None:
        await asyncio.sleep(mins * 60)
        # Only auto-resume if THIS snooze is still the active one and we're still
        # muted (the user may have unmuted, or started a new, longer snooze).
        if _muted and abs(time.monotonic() - _snooze_until) < 2:
            set_muted(False)
            log.info("snooze elapsed — listening again")
            if not tts.speak_canned("I'm listening again, sir."):
                await tts.speak("I'm listening again, sir.")
            await _wait_playback()

    if _loop:
        _loop.create_task(_resume_after())


# ── Timer / stopwatch ───────────────────────────────────────────
# State lives in system/timers.py so the fast path, the smart-path `timer` tool,
# and the HUD ticker all share one source of truth. _fmt_dur == timers.fmt_dur.
_fmt_dur = timers.fmt_dur


async def _handle_timer(cmd: str, data: dict) -> None:
    if cmd == "timer_set":
        secs = int(data.get("seconds", 60))
        timers.set_timer(secs)
        STATE.timer({"kind": "timer", "remaining": secs, "total": secs})
        await _speak(f"Timer set for {_fmt_dur(secs)}, sir.", cached_ok=False)
    elif cmd == "stopwatch_start":
        timers.start_stopwatch()
        STATE.timer({"kind": "stopwatch", "elapsed": 0})
        await _speak("Stopwatch running, sir.", cached_ok=False)
    elif cmd == "timer_stop":
        if timers.stop():
            STATE.timer({"kind": "none"})
            await _speak("Stopped, sir.", cached_ok=False)
        else:
            await _speak("Nothing's running, sir.", cached_ok=False)


def _toggle_stopwatch() -> None:
    """HUD click on the round widget: start a stopwatch, or stop the running one."""
    result = timers.toggle_stopwatch()
    if result == "started":
        STATE.timer({"kind": "stopwatch", "elapsed": 0})
    elif result == "stopped":
        STATE.timer({"kind": "none"})


async def _fire_timer() -> None:
    """Announce a completed timer in-character, interrupting idle (like reminders)."""
    async with _busy:
        if _sleeping:
            await _wake_from_sleep()
        ducking.duck()
        try:
            await _speak("Your timer's up, sir.", cached_ok=False)
            await _wait_playback()
        finally:
            ducking.unduck()
            if not _sleeping:
                STATE.set_state(IDLE)
            if _wake_listener:
                _wake_listener.resume()


async def _fire_reminder(task_text: str) -> None:
    """Speak a due reminder aloud, interrupting idle. Waits for any in-progress
    interaction (via _busy) so it never talks over an active command."""
    async with _busy:
        if _sleeping:
            await _wake_from_sleep()
        ducking.duck()
        try:
            await _speak(f"A reminder, sir — you wanted to {task_text}.",
                         cached_ok=False)
            await _wait_playback()
        finally:
            ducking.unduck()
            if not _sleeping:
                STATE.set_state(IDLE)
            if _wake_listener:
                _wake_listener.resume()


async def _wake_from_sleep() -> None:
    global _sleeping
    _sleeping = False
    log.info("SLEEP: waking up")
    STATE.set_state(IDLE)
    STATE.event("awake")
    if not tts.speak_canned("Back online, sir."):
        await tts.speak("Back online, sir.")
    await _wait_playback()


# ── Barge-in / cancel ───────────────────────────────────────────
_active_smart_task: asyncio.Task | None = None   # the in-flight LLM/tool chain
_active_speech: asyncio.Task | None = None        # the in-flight _speak() line


def _cancel_current(reason: str) -> None:
    """Stop whatever JARVIS is doing right now: playback, the in-flight LLM /
    tool-calling chain, AND the current spoken line (so a streaming TTS reply
    stops feeding the player). Safe to call from the audio thread."""
    log.info("CANCEL requested (%s)", reason)
    _barge_in.set()
    PLAYER.stop_now()
    tts.stop_fallback()          # the Windows fallback voice plays outside PLAYER
    for task in (_active_smart_task, _active_speech):
        if task is not None and not task.done() and _loop is not None:
            _loop.call_soon_threadsafe(task.cancel)
    STATE.event(f"interrupted ({reason})")


def _on_barge_in() -> None:
    """Called from the audio thread when the user talks over JARVIS (while it's
    speaking OR thinking)."""
    _cancel_current("voice stop")


# ── FastAPI surface ─────────────────────────────────────────────
async def _process_typed(text: str, source: str = "typed") -> None:
    """Run a TYPED command through the EXACT same pipeline as a spoken one —
    routines, fast path, smart path, personality, TTS. No parallel logic. Used by
    the HUD text box and the /text and /simulate endpoints."""
    text = (text or "").strip()
    if not text:
        return

    # Typing while JARVIS is listening, thinking or speaking used to be dropped
    # ON THE FLOOR — silently, with no log and no feedback, so the message simply
    # vanished. But typing IS a deliberate interruption, exactly like speaking
    # over it: if you're reaching for the keyboard mid-reply, you want the typed
    # thing, not whatever it was doing. So cancel the current turn and take it.
    if _busy.locked():
        log.info("typed input while %s — cancelling the current turn to take it: %r",
                 STATE.state, text[:60])
        _cancel_current("typed input")
        for _ in range(60):                 # up to ~6 s for the turn to unwind
            if not _busy.locked():
                break
            await asyncio.sleep(0.1)
        if _busy.locked():
            # Never fail silently — this is the exact bug being fixed.
            log.warning("typed input DROPPED: the previous turn never released "
                        "(state=%s): %r", STATE.state, text[:60])
            STATE.event("still busy — say that again?")
            return

    async with _busy:
        try:
            if _sleeping:
                await _wake_from_sleep()
            ducking.duck()
            log.info("%s command: %r", source, text)
            # Show the typed input in the transcript as a user line — this also
            # resets the reply line so JARVIS's response lands on a fresh line
            # (spoken input gets this from STT; typed input had no equivalent, so
            # typed replies were silently overwriting the previous one).
            STATE.transcript(text, final=True)
            await respond_to(text)
            await _wait_playback()
        finally:
            ducking.unduck()
            if not _sleeping:
                STATE.set_state(IDLE)
            if _wake_listener:
                _wake_listener.resume()


@app.websocket("/ws")
async def hud_socket(ws: WebSocket):
    await ws.accept()
    await STATE.register(ws)
    try:
        while True:
            msg = await ws.receive_text()
            if msg == "toggle_mic":
                _trigger_listen("HUD mic button")
            elif msg == "toggle_mute":
                set_muted(not _muted)
            elif msg == "cancel":
                _cancel_current("HUD stop button")
            elif msg == "quit":
                _shutdown("HUD close button")
            elif msg.startswith("text:"):
                # Typed into the HUD box — same pipeline as speech.
                asyncio.create_task(_process_typed(msg[5:], "HUD text"))
            elif msg == "toggle_stopwatch":
                _toggle_stopwatch()
    except WebSocketDisconnect:
        pass
    finally:
        STATE.unregister(ws)


def set_muted(muted: bool) -> None:
    """Mic mute (privacy). Hard off: the wake word AND the mic button both do
    nothing. Only a manual toggle (HUD button / Ctrl+Alt+M) or a restart clears
    it — deliberately no voice command can, since the mic is off."""
    global _muted
    _muted = muted
    # Three separate things have to stop, not one: the wake listener, any STT
    # capture already in flight (audio/stt.MUTED), and the follow-up loop that
    # would otherwise open a fresh session (checked in the listen loop).
    if muted:
        stt.MUTED.set()
    else:
        stt.MUTED.clear()
    if _wake_listener:
        _wake_listener.set_muted(muted)
    STATE.mic_muted(muted)
    STATE.event("microphone muted" if muted else "microphone live")
    log.info("mic %s", "MUTED" if muted else "unmuted")


def _trigger_listen(reason: str) -> None:
    """Start an interaction as if the wake word had fired (mic button / hotkey).
    Works while asleep too — it wakes JARVIS first."""
    if _muted:
        log.info("listen trigger ignored (%s): mic is muted", reason)
        STATE.event("mic is muted")
        return
    if STATE.state not in (IDLE, SLEEPING) or _busy.locked():
        log.info("listen trigger ignored (%s): state=%s", reason, STATE.state)
        return
    log.info("listen triggered by %s", reason)
    if _wake_listener:
        _wake_listener.pause()
    asyncio.run_coroutine_threadsafe(handle_interaction(), _loop)


def _shutdown(reason: str) -> None:
    log.info("shutdown requested (%s)", reason)
    try:
        ducking.unduck()
    except Exception:
        pass
    threading.Timer(0.4, lambda: os._exit(0)).start()


@app.get("/quit")
async def quit_endpoint():
    _shutdown("/quit endpoint")
    return {"ok": True, "bye": "Powering down."}


@app.get("/wake")
async def wake_endpoint():
    """Debug hook: behave exactly as if the wake word just fired."""
    if STATE.state != IDLE:
        return {"ok": False, "error": f"not idle (state={STATE.state})"}
    _trigger_listen("/wake endpoint")
    return {"ok": True}


@app.get("/health")
async def health_endpoint():
    """Cheap — safe to poll. (System stats are NOT sampled here: psutil's CPU
    reading blocks for 400 ms, which made polling this endpoint expensive.)"""
    return {"state": STATE.state, "online": _online(),
            "muted": _muted,
            "fullscreen_app": foreground.fullscreen_app_active(),
            "keys": {"groq": bool(config.GROQ_API_KEY),
                     "deepgram": bool(config.DEEPGRAM_API_KEY),
                     "elevenlabs": bool(config.ELEVENLABS_API_KEY)},
            "wake_alive": bool(_wake_listener and _wake_listener.is_alive())}


@app.get("/system")
async def system_endpoint():
    """Real measured system stats (samples CPU — takes ~0.4 s)."""
    return health.get_system_health()


@app.get("/cancel")
async def cancel_endpoint():
    """Debug hook: cancel whatever is in progress."""
    _cancel_current("/cancel endpoint")
    return {"ok": True}


@app.get("/mute")
async def mute_endpoint(on: bool | None = None):
    """Mute/unmute the microphone. No arg = toggle."""
    set_muted((not _muted) if on is None else on)
    return {"ok": True, "muted": _muted}


@app.get("/sleep")
async def sleep_endpoint():
    """Debug hook: go dormant (the wake word still works)."""
    if _busy.locked():
        return {"ok": False, "error": "busy"}
    async with _busy:
        await _go_to_sleep()
    return {"ok": True, "state": STATE.state}


@app.get("/simulate")
async def simulate(text: str):
    """Debug hook: run routing -> execution -> TTS on typed text (no wake/STT).

    The busy guard that used to sit here returned {"ok": false, "error": "busy"}
    and threw the text away. _process_typed now interrupts the current turn
    instead, which is both the right behaviour and the only way a test harness
    can drive this reliably — ambient room noise holding _busy silently ate
    simulated queries."""
    await _process_typed(text, "simulate")
    return {"ok": True, "state": STATE.state}


@app.get("/text")
async def text_endpoint(q: str):
    """Typed command (same as the HUD text box) through the full pipeline."""
    await _process_typed(q, "typed")
    return {"ok": True, "state": STATE.state}


# ── Startup ─────────────────────────────────────────────────────
_loop: asyncio.AbstractEventLoop = None  # type: ignore


async def _startup():
    global _wake_listener, _loop
    _loop = asyncio.get_running_loop()
    STATE.bind_loop(_loop)

    if not config.INDEX_FILE.exists():
        STATE.event("building file index (first run)...")
        _loop.run_in_executor(None, indexer.build_index)

    routines.load()

    # Voice degraded (ElevenLabs quota/auth/outage) -> tell the HUD. Never spoken:
    # the speech channel is precisely what's broken.
    tts.set_degraded_callback(
        lambda reason, quota: STATE.voice_degraded(reason, quota))

    def on_wake():
        asyncio.run_coroutine_threadsafe(handle_interaction(), _loop)

    try:
        _wake_listener = wake.WakeListener(
            on_wake=on_wake,
            on_barge_in=_on_barge_in,
            get_state=lambda: STATE.state,
            # While the Windows fallback voice is talking, the player reports 0 —
            # feed the detector a nominal level so it still predicts the echo and
            # doesn't mistake JARVIS's own voice for the user interrupting.
            get_output_rms=lambda: (tts.SAPI_NOMINAL_RMS if tts.SAPI_SPEAKING
                                    else PLAYER.output_rms),
            get_media_active=foreground.media_app_active,
        )
        _wake_listener.start()
        STATE.event("wake word ready — say 'jarvis'")
    except Exception as e:
        log.error("wake listener failed to start: %s", e)
        STATE.event(f"wake word unavailable: {e}")

    # Push-to-talk: works even in a fullscreen game with a headset.
    try:
        import keyboard
        keyboard.add_hotkey(config.PTT_HOTKEY,
                            lambda: _trigger_listen(f"hotkey {config.PTT_HOTKEY}"))
        log.info("push-to-talk hotkey: %s", config.PTT_HOTKEY)
        keyboard.add_hotkey(config.MUTE_HOTKEY, lambda: set_muted(not _muted))
        log.info("mic-mute hotkey: %s", config.MUTE_HOTKEY)
    except Exception as e:
        log.error("could not register hotkeys: %s", e)

    async def wake_watchdog():
        """Revive the mic stream only if it is REALLY dead.

        A restart tears the stream down and rebuilds it, which eats ~0.5 s of
        audio — if the watchdog fires spuriously it swallows the start of what
        you're saying. So it now requires two consecutive dead checks, never
        restarts mid-interaction, and logs every decision.
        """
        strikes = 0
        while True:
            await asyncio.sleep(5)
            if not _wake_listener:
                continue
            if _wake_listener.is_muted():
                continue
            alive = _wake_listener.is_alive()
            if alive:
                if strikes:
                    log.info("mic watchdog: stream healthy again (no restart needed)")
                strikes = 0
                continue
            if STATE.state != IDLE or _busy.locked():
                log.warning("mic watchdog: stream reads dead but an interaction is "
                            "in progress (state=%s) — NOT restarting", STATE.state)
                continue
            strikes += 1
            log.warning("mic watchdog: stream not active (strike %d/2)", strikes)
            if strikes < 2:
                continue                    # one bad read is not a dead stream
            try:
                _wake_listener.restart()
                strikes = 0
                STATE.event("wake listener recovered")
            except Exception as e:
                log.error("mic watchdog: restart failed: %s", e)

    async def overlay_watch():
        """Tell the HUD to stay retracted while a fullscreen app (game) is up."""
        last = None
        while True:
            fs = foreground.fullscreen_app_active()
            if fs != last:
                last = fs
                STATE.overlay_allowed(not fs)
                log.info("fullscreen app %s — HUD expansion %s",
                         "detected" if fs else "gone", "suppressed" if fs else "enabled")
            await asyncio.sleep(2)

    async def reminder_checker():
        """Fire due reminders aloud, even from idle. Reminders persist in
        data/reminders.json, so a restart doesn't lose them."""
        while True:
            await asyncio.sleep(15)
            try:
                fired = await _loop.run_in_executor(None, reminders.due)
            except Exception as e:
                log.error("reminder check failed: %s", e)
                continue
            for r in fired:
                log.info("reminder due: %r", r.get("text", "")[:80])
                await _fire_reminder(r.get("text", "your reminder"))

    async def _morning_briefing(pending: list[str]) -> str:
        """One compact morning status: weather, what's pending, first class.

        Assembled from REAL data only — live wttr.in, the actual reminder store,
        the actual timetable. Nothing here is generated or guessed, which is the
        same rule the say_dynamic routines follow. Kept to a few clauses because
        it is spoken, not read."""
        global _last_briefing_date
        _last_briefing_date = datetime.datetime.now().date()
        bits: list[str] = []
        try:
            w = await _loop.run_in_executor(None, weather_mod.short_phrase)
            if w:
                bits.append(w)
        except Exception as e:
            log.debug("briefing: weather unavailable (%s)", e)
        try:
            nxt = await _loop.run_in_executor(None, timetable.describe_next)
            if nxt and "Nothing on your timetable" not in nxt:
                bits.append(f"First up, {nxt.rstrip('.').removesuffix(', sir')}")
        except Exception as e:
            log.debug("briefing: timetable unavailable (%s)", e)
        if pending:
            bits.append(f"You have {', '.join(pending)}")
        if not bits:
            return "Morning, sir. Nothing pressing."
        return "Morning, sir. " + ". ".join(bits) + "."

    async def return_greeter():
        """Greet ONCE when the user comes back after a real absence, and mention
        anything genuinely pending.

        Deliberately conservative: it only speaks when there is something real to
        say, and never while JARVIS is busy, muted, sleeping or mid-conversation.
        A proactive assistant that greets an empty room, or talks over the user
        the moment they sit down, is worse than a reactive one."""
        watcher = idle.ReturnWatcher()
        while True:
            await asyncio.sleep(10)
            try:
                away_for = await _loop.run_in_executor(None, watcher.poll)
                if not away_for:
                    continue
                if STATE.state != "idle" or _busy.locked():
                    continue
                if _wake_listener and _wake_listener.is_muted():
                    continue

                pending = []
                try:
                    n = len(reminders.pending())
                    if n:
                        pending.append(f"{n} reminder{'s' if n != 1 else ''} outstanding")
                except Exception:
                    pass

                # MORNING BRIEFING: the first return of the day, in the morning,
                # after a genuinely long (overnight) gap. Deliberately narrow —
                # a briefing that fires every time you come back from the kettle
                # is noise, so it needs ALL of: morning hours, a long absence,
                # and not already given today.
                mins = int(away_for // 60)
                now = datetime.datetime.now()
                is_morning = 4 <= now.hour < 12
                first_today = _last_briefing_date != now.date()
                if is_morning and mins >= 180 and first_today:
                    line = await _morning_briefing(pending)
                elif not pending:
                    continue          # nothing worth interrupting for
                else:
                    line = (f"Welcome back, sir. You have {', '.join(pending)}."
                            if mins < 60 else
                            f"Welcome back, sir — it's been {mins // 60} hours. "
                            f"You have {', '.join(pending)}.")
                log.info("proactive return greeting after %d min: %s", mins, line)
                # Same discipline as a due reminder: hold _busy so it can never
                # talk over an active command, and don't wake a sleeping JARVIS
                # just to say hello.
                async with _busy:
                    if _sleeping:
                        continue
                    ducking.duck()
                    try:
                        await _speak(line, cached_ok=False)
                        await _wait_playback()
                    finally:
                        ducking.unduck()
            except Exception as e:
                log.error("return greeter failed: %s", e)

    async def timer_ticker():
        """Broadcast the active timer/stopwatch (from system/timers) to the HUD each
        second, and fire a completed timer's in-character alert."""
        while True:
            await asyncio.sleep(1)
            snap = timers.snapshot()
            if snap["fired"]:
                STATE.timer({"kind": "none"})
                await _fire_timer()
            elif snap["hud"]["kind"] != "none":
                STATE.timer(snap["hud"])

    async def proactive_watch():
        """Gentle, de-duped nudges: an imminent class, and a break after a long
        continuous focus stretch. Same discipline as reminders — never talks over
        an active turn, and never wakes a sleeping or muted JARVIS."""
        last_class_key = None                       # (day, start_min) already warned
        active_since = time.monotonic()
        break_nudged = False
        break_after = config.SESSION_BREAK_MINUTES * 60
        BREAK_RESET_IDLE = 300                      # a 5-min break resets the stretch
        while True:
            await asyncio.sleep(30)
            try:
                # Track continuous focus even mid-conversation; a real break resets.
                if idle.idle_seconds() >= BREAK_RESET_IDLE:
                    active_since = time.monotonic()
                    break_nudged = False

                if STATE.state != "idle" or _busy.locked():
                    continue
                if _sleeping or (_wake_listener and _wake_listener.is_muted()):
                    continue

                line = None
                # 1) A class starting soon (today only).
                try:
                    nxt = timetable.next_class()
                except Exception:
                    nxt = None
                if nxt is not None:
                    e, when = nxt
                    day, mnow = timetable._now()
                    if when.startswith("at"):
                        mins_to = e["start"] - mnow
                        key = (day, e["start"])
                        if 0 < mins_to <= config.CLASS_NUDGE_MINUTES and key != last_class_key:
                            last_class_key = key
                            where = f" in {e['where']}" if e["where"] else ""
                            line = (f"Heads up, sir — {e['subject']}{where} in about "
                                    f"{int(mins_to)} minute{'s' if int(mins_to) != 1 else ''}.")
                # 2) A long focus stretch (only if there's no class nudge this cycle).
                elapsed = time.monotonic() - active_since
                if (line is None and break_after > 0 and not break_nudged
                        and elapsed >= break_after):
                    break_nudged = True
                    line = (f"You've been heads-down for {timers.fmt_dur(int(elapsed))}, "
                            f"sir. Might be worth a breather.")
                if line is None:
                    continue

                log.info("proactive nudge: %s", line)
                async with _busy:
                    if _sleeping:
                        continue
                    ducking.duck()
                    try:
                        await _speak(line, cached_ok=False)
                        await _wait_playback()
                    finally:
                        ducking.unduck()
            except Exception as e:
                log.error("proactive watch failed: %s", e)

    _loop.create_task(wake_watchdog())
    _loop.create_task(overlay_watch())
    clipboard.WATCHER.start()      # event-driven; no polling task needed
    _loop.create_task(reminder_checker())
    _loop.create_task(return_greeter())
    _loop.create_task(timer_ticker())
    _loop.create_task(proactive_watch())

    # Warm the semantic file-search embeddings in the background so the first
    # "find that file about X" is instant. Only rebuilds when the index changes.
    if config.GOOGLE_API_KEY:
        async def _warm_filesearch():
            try:
                from system import filesearch
                await _loop.run_in_executor(None, filesearch.ensure_index)
            except Exception as e:
                log.debug("filesearch warm-up skipped: %s", e)
        _loop.create_task(_warm_filesearch())

    STATE.set_state(IDLE)
    log.info("JARVIS backend up on %s:%d", config.HOST, config.PORT)

    # Boot briefing: the cached "Systems online" opener is instant, then a live
    # line with real CPU/RAM and Bengaluru weather. Accept the small latency —
    # it's spoken once at startup.
    async def boot_briefing():
        await asyncio.sleep(0.6)
        _barge_in.clear()
        if _wake_listener:
            # Pause then arm, so the opener's own onset is ignored (~0.5 s) but
            # the user CAN talk over the whole boot briefing to cut it off.
            _wake_listener.pause()
            _wake_listener.arm_for_barge_in()
        try:
            # Goes through _speak, so 'stop'/'shut up' interrupts the boot
            # briefing exactly like any other speech — and once interrupted, the
            # weather line below is skipped instead of ploughing on.
            await _speak("Systems online, sir.", cached_ok=True)
            STATE.event("Systems online, sir.")
            log.info("boot: opener spoken")
            if _barge_in.is_set():
                return

            loop = asyncio.get_running_loop()
            h = await loop.run_in_executor(None, health.get_system_health)
            parts = [f"CPU's idling at {round(h['cpu_percent'])} percent",
                     f"memory at {round(h['ram_percent'])}"]
            if "gpu_temperature_c" in h:
                parts.append(f"GPU's at {h['gpu_temperature_c']} degrees")
            line = ", ".join(parts) + "."

            weather = await loop.run_in_executor(None, weather_mod.short_phrase)
            if weather:
                line += f" {weather.capitalize()}."
            log.info("boot briefing: %s", line)
            await _speak(line, cached_ok=False)
            STATE.event(line)
        except Exception as e:
            log.error("boot briefing (stats/weather) failed: %s", e)
        finally:
            # Barge-in pauses the wake listener; boot has no interaction loop to
            # resume it, so JARVIS would go deaf until restart. Always resume.
            if _wake_listener:
                _wake_listener.resume()
            if not _sleeping:
                STATE.set_state(IDLE)
    _loop.create_task(boot_briefing())


def _already_running() -> bool:
    """True if another backend already holds the port.

    start.bat kills previous instances by matching 'backend\\main.py' in the
    command line, but that is launch-path dependent: it misses forward-slash
    paths and `python main.py` run from the backend directory, and there are now
    three ways in (start.bat, a Desktop shortcut, and a Task Scheduler autostart
    that itself calls start.bat). A port probe is independent of ALL of that.
    Checked BEFORE uvicorn starts, because the damage from a second instance is
    two processes fighting over the microphone — which uvicorn's own bind error
    would only surface after the audio stack is already up.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.settimeout(0.6)
        return probe.connect_ex((config.HOST, config.PORT)) == 0
    except OSError:
        return False
    finally:
        probe.close()


if __name__ == "__main__":
    if _already_running():
        log.error("JARVIS is ALREADY RUNNING on %s:%d — refusing to start a "
                  "second instance (two backends fight over the microphone). "
                  "Close the existing one first, or run start.bat which stops it "
                  "for you.", config.HOST, config.PORT)
        raise SystemExit(1)
    uvicorn.run(app, host=config.HOST, port=config.PORT, log_level="warning")
