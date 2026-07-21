"""Assistant state machine + broadcast to HUD clients over WebSocket.

States: idle -> listening -> thinking -> speaking -> idle
The HUD renders these; everything else subscribes via `on_change`.
"""
import asyncio
import json
import logging
import time

log = logging.getLogger("jarvis.state")

IDLE, LISTENING, THINKING, SPEAKING = "idle", "listening", "thinking", "speaking"
# Dormant: TTS stopped, HUD dark, nothing processed — but the wake listener stays
# alive, so saying "jarvis" brings everything back. Distinct from a real quit.
SLEEPING = "sleeping"


class StateManager:
    def __init__(self) -> None:
        self.state: str = IDLE
        self._since: float = time.time()
        self.muted: bool = False
        self.degraded: str | None = None
        self._clients: set = set()          # fastapi WebSocket objects
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # ── HUD client management ──────────────────────────────────
    async def register(self, ws) -> None:
        self._clients.add(ws)
        log.info("HUD client connected (%d total)", len(self._clients))
        await ws.send_text(json.dumps({"type": "state", "state": self.state}))
        await ws.send_text(json.dumps({"type": "mute", "muted": self.muted}))
        if self.degraded:
            await ws.send_text(json.dumps({"type": "voice", "degraded": True,
                                           "quota": True, "reason": self.degraded}))

    def unregister(self, ws) -> None:
        self._clients.discard(ws)

    # ── Broadcasting ───────────────────────────────────────────
    async def _broadcast(self, payload: dict) -> None:
        dead = []
        msg = json.dumps(payload)
        for ws in self._clients:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)

    def set_state(self, state: str, detail: str = "") -> None:
        """Thread-safe: callable from audio callback threads.

        Logs a clear transition with how long the previous state lasted, so the
        full idle -> listening -> thinking -> speaking -> idle path (and where it
        got stuck, if it did) is reconstructable from data/jarvis.log alone.
        """
        now = time.time()
        prev, held = self.state, now - self._since
        if state != prev:
            log.info("STATE %-9s -> %-9s (was %s for %.1fs)%s",
                     prev, state, prev, held, f"  [{detail}]" if detail else "")
        self.state = state
        self._since = now
        self._emit({"type": "state", "state": state, "t": now})

    def transcript(self, text: str, final: bool) -> None:
        self._emit({"type": "transcript", "text": text, "final": final})

    def reply(self, text: str) -> None:
        self._emit({"type": "reply", "text": text})

    def event(self, text: str) -> None:
        """Free-form log line for the HUD panel."""
        self._emit({"type": "log", "text": text})

    def level(self, rms: float) -> None:
        """Mic/output audio level 0..1 for waveform animation."""
        self._emit({"type": "level", "value": round(rms, 3)})

    def voice_degraded(self, reason: str | None, quota: bool = False) -> None:
        """ElevenLabs is down/out of quota — we're on the Windows voice. Shown in
        the HUD (never spoken: that channel is exactly what's broken)."""
        self.degraded = reason
        self._emit({"type": "voice", "degraded": bool(reason),
                    "quota": bool(quota), "reason": reason or ""})

    def mic_muted(self, muted: bool) -> None:
        """Mic hard-off for privacy — the HUD shows this clearly."""
        self.muted = bool(muted)
        self._emit({"type": "mute", "muted": bool(muted)})

    def timer(self, payload: dict) -> None:
        """Timer/stopwatch tick for the round HUD widget. kind: timer|stopwatch|none."""
        self._emit({"type": "timer", **payload})

    def overlay_allowed(self, allowed: bool) -> None:
        """False while a fullscreen app (game/film) is focused: the HUD stays as
        the corner orb and never pops up. Voice is unaffected."""
        self._emit({"type": "overlay", "allowed": bool(allowed)})

    def _emit(self, payload: dict) -> None:
        if self._loop is None:
            return
        asyncio.run_coroutine_threadsafe(self._broadcast(payload), self._loop)


STATE = StateManager()
