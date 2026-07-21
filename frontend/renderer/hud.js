/* JARVIS HUD renderer: arc-reactor canvas + backend WebSocket client. */
"use strict";

const WS_URL = "ws://127.0.0.1:8765/ws";
const CYAN = "53, 224, 255";
const RED = "255, 82, 82";          // mic muted

// ── State ───────────────────────────────────────────────────────
let state = "offline";         // offline | idle | listening | thinking | speaking
let level = 0;                 // live audio level 0..1 (mic or output)
let levelSmooth = 0;
let connected = false;
let mode = "expanded";         // expanded | retracted (mini corner orb)
let hovering = false;
let retractTimer = null;
let overlayAllowed = true;     // false while a fullscreen app is focused
let muted = false;             // mic hard-off (privacy)

const ACTIVE = new Set(["listening", "thinking", "speaking"]);
const RETRACT_AFTER_MS = 4000; // grace period once a response finishes

const canvas = document.getElementById("reactor");
const ctx = canvas.getContext("2d");
const label = document.getElementById("status-label");
const logEl = document.getElementById("log");
const connDot = document.getElementById("conn-dot");
const hudEl = document.getElementById("hud");
const reactorWrap = document.getElementById("reactor-wrap");

// ── Expand / retract + click-through ────────────────────────────
// Interaction rules (enforced by cursor polling in the MAIN process — see
// main.js; the renderer just reports mode/active):
//  * active (listening/thinking/speaking): full HUD, clicks land on us
//  * idle expanded (grace period): interactive while cursor is over the HUD
//  * idle retracted: tiny corner orb, click-through except over the orb
function sendStatus() {
  console.log(`status: mode=${mode} state=${state}`);
  if (window.hud) window.hud.status(mode, ACTIVE.has(state));
}

function applyMode(m) {
  mode = m;
  document.body.classList.toggle("retracted", m === "retracted");
  sendStatus();
}

function scheduleRetract(delay = RETRACT_AFTER_MS) {
  clearTimeout(retractTimer);
  retractTimer = setTimeout(() => {
    if (!ACTIVE.has(state) && !hovering) applyMode("retracted");
  }, delay);
}

// ── WebSocket with auto-reconnect ───────────────────────────────
let ws = null;

function connect() {
  ws = new WebSocket(WS_URL);
  ws.onopen = () => {
    connected = true;
    connDot.classList.add("ok");
  };
  ws.onclose = () => {
    connected = false;
    connDot.classList.remove("ok");
    setState("offline");
    setTimeout(connect, 1500);
  };
  ws.onerror = () => ws.close();
  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);
    switch (msg.type) {
      case "state":      setState(msg.state); break;
      case "level":      level = msg.value; break;
      case "transcript": showTranscript(msg.text, msg.final); break;
      case "reply":      showReply(msg.text); break;
      case "log":        addLine(msg.text, "sys"); break;
      case "overlay":    setOverlayAllowed(msg.allowed); break;
      case "mute":       setMuted(msg.muted); break;
      case "voice":      setVoiceDegraded(msg); break;
      case "timer":      updateTimer(msg); break;
    }
  };
}

// While a fullscreen app (a game, a film) is focused the HUD never pops up —
// it stays as the corner orb. Voice interaction is unaffected.
function setOverlayAllowed(allowed) {
  overlayAllowed = allowed;
  console.log(`overlay ${allowed ? "allowed" : "suppressed (fullscreen app)"}`);
  if (!allowed && mode === "expanded" && !hovering) applyMode("retracted");
}

const BUSY = new Set(["thinking", "speaking"]);   // stop button is active here

function setState(s) {
  state = s;
  label.textContent = s === "sleeping" ? "ASLEEP" : s.toUpperCase();
  document.body.classList.toggle("busy", BUSY.has(s));
  // Asleep = dormant, not gone: the orb goes dark and the panel drops away, but
  // the backend's wake listener is still running, so "jarvis" brings it back.
  document.body.classList.toggle("asleep", s === "sleeping");

  if (s === "sleeping") {
    clearTimeout(retractTimer);
    applyMode("retracted");
  } else if (ACTIVE.has(s)) {
    clearTimeout(retractTimer);
    if (overlayAllowed) applyMode("expanded");   // wake word / response -> full HUD
  } else {
    scheduleRetract();            // idle/offline -> shrink to orb after grace
  }
  sendStatus();
}

// Hovering pauses auto-retract (interaction itself is handled by main.js).
hudEl.addEventListener("mouseenter", () => {
  hovering = true;
  clearTimeout(retractTimer);
});
hudEl.addEventListener("mouseleave", () => {
  hovering = false;
  if (!ACTIVE.has(state)) scheduleRetract();
});

// ── Manual window dragging (reactor + top strip are handles) ────
// The window only ever MOVES (main.js pins its size). Distinguish click vs drag
// by cursor travel: a real drag must never be mistaken for a click, or grabbing
// the mini orb to reposition it would expand the HUD instead of moving it.
const DRAG_SLOP = 4;      // px of travel before it counts as a drag
let dragMoved = false;
let dragEndedAt = 0;

function attachDragHandle(el) {
  el.addEventListener("mousedown", (e) => {
    if (e.button !== 0) return;
    if (e.target.closest("button")) return;   // don't drag from ✕ / _ buttons
    dragMoved = false;
    const sx = e.screenX, sy = e.screenY;
    if (window.hud) window.hud.dragStart();

    const onMove = (me) => {
      if (Math.abs(me.screenX - sx) + Math.abs(me.screenY - sy) > DRAG_SLOP) {
        dragMoved = true;
      }
    };
    const onUp = () => {
      if (window.hud) window.hud.dragEnd();
      if (dragMoved) dragEndedAt = Date.now();
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
      window.removeEventListener("blur", onUp);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    window.addEventListener("blur", onUp);
  });
}

attachDragHandle(reactorWrap);
attachDragHandle(document.getElementById("drag-strip"));

// Clicking (NOT dragging) the mini orb re-opens the full HUD.
reactorWrap.addEventListener("click", () => {
  if (dragMoved || Date.now() - dragEndedAt < 250) return;   // it was a drag
  if (mode === "retracted") {
    applyMode("expanded");
    scheduleRetract(8000);
  }
});

// ── Minimize (manual retract) ───────────────────────────────────
document.getElementById("min-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  clearTimeout(retractTimer);
  applyMode("retracted");     // same visual state auto-retract produces
});

// ── Quit ────────────────────────────────────────────────────────
document.getElementById("close-btn").addEventListener("click", (e) => {
  e.stopPropagation();
  if (ws && ws.readyState === WebSocket.OPEN) ws.send("quit"); // stops backend
  if (window.hud) window.hud.quit();                           // stops HUD
});

// ── Transcript panel ────────────────────────────────────────────
let interimEl = null;
let replyEl = null;

function addLine(text, cls) {
  const div = document.createElement("div");
  div.className = "line " + cls;
  div.textContent = text;
  logEl.appendChild(div);
  while (logEl.children.length > 80) logEl.removeChild(logEl.firstChild);
  logEl.scrollTop = logEl.scrollHeight;
  return div;
}

function showTranscript(text, final) {
  if (!interimEl) interimEl = addLine("", "user interim");
  interimEl.textContent = text;
  logEl.scrollTop = logEl.scrollHeight;
  if (final) {
    interimEl.classList.remove("interim");
    interimEl = null;
    replyEl = null;               // next reply starts a fresh line
  }
}

function showReply(text) {
  if (!replyEl) replyEl = addLine("", "jarvis");
  replyEl.textContent = text;
  logEl.scrollTop = logEl.scrollHeight;
}

// ── Mic mute (privacy) ──────────────────────────────────────────
// Hard off: wake word AND the mic button both do nothing. Persists until this
// button (or Ctrl+Alt+M) is used again — no voice command can undo it.
function setMuted(m) {
  muted = m;
  document.body.classList.toggle("muted", m);
  console.log(`mic ${m ? "MUTED" : "live"}`);
}

document.getElementById("mute-btn").addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send("toggle_mute");
});

// Stop/cancel the current request (same effect as saying "stop").
document.getElementById("stop-btn").addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send("cancel");
});

// ── Voice degraded (ElevenLabs quota/outage -> Windows voice) ────
// Shown, never spoken: speech is exactly the channel that's broken.
function setVoiceDegraded(msg) {
  const el = document.getElementById("voice-label");
  document.body.classList.toggle("voice-degraded", !!msg.degraded);
  if (msg.degraded) {
    el.textContent = msg.quota ? "▲ VOICE: QUOTA" : "▲ VOICE: FALLBACK";
    el.title = `Using the Windows voice — ${msg.reason || "ElevenLabs unavailable"}`;
    addLine(msg.quota
      ? "ElevenLabs quota exhausted — using the Windows voice"
      : "ElevenLabs unavailable — using the Windows voice", "sys");
  }
}

// ── Controls ────────────────────────────────────────────────────
document.getElementById("mic-btn").addEventListener("click", () => {
  if (muted) return;                       // muted: the mic button is inert
  if (ws && ws.readyState === WebSocket.OPEN) ws.send("toggle_mic");
});

document.getElementById("log-toggle").addEventListener("click", (e) => {
  logEl.classList.toggle("collapsed");
  e.target.textContent = logEl.classList.contains("collapsed") ? "▸" : "▾";
});

// ── Typed-command box (same pipeline as speech) ─────────────────
const textBox = document.getElementById("text-box");
const textInput = document.getElementById("text-input");
function sendTyped() {
  const v = textInput.value.trim();
  if (!v) return;
  if (ws && ws.readyState === WebSocket.OPEN) ws.send("text:" + v);
  textInput.value = "";
}
document.getElementById("kbd-btn").addEventListener("click", (e) => {
  textBox.classList.toggle("collapsed");
  const open = !textBox.classList.contains("collapsed");
  e.currentTarget.classList.toggle("active", open);
  if (open) textInput.focus();
});
document.getElementById("text-send").addEventListener("click", sendTyped);

// ── Round timer / stopwatch widget ──────────────────────────────
const timerWidget = document.getElementById("timer-widget");
const timerKind = document.getElementById("timer-kind");
const timerValue = document.getElementById("timer-value");
function fmtClock(s) {
  s = Math.max(0, Math.round(s));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  const mm = h ? String(m).padStart(2, "0") : String(m);
  return (h ? h + ":" : "") + mm + ":" + String(sec).padStart(2, "0");
}
function updateTimer(msg) {
  if (!msg.kind || msg.kind === "none") { timerWidget.classList.add("hidden"); return; }
  timerWidget.classList.remove("hidden");
  timerWidget.classList.toggle("running", msg.kind === "stopwatch");
  if (msg.kind === "timer") {
    timerKind.textContent = "TIMER";
    timerValue.textContent = fmtClock(msg.remaining);
    // subtle urgency in the last 10s
    timerWidget.classList.toggle("urgent", msg.remaining <= 10);
  } else {
    timerKind.textContent = "STOPWATCH";
    timerValue.textContent = fmtClock(msg.elapsed);
    timerWidget.classList.remove("urgent");
  }
}
timerWidget.addEventListener("click", () => {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send("toggle_stopwatch");
});
textInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); sendTyped(); }
  else if (e.key === "Escape") { textBox.classList.add("collapsed"); }
});

// ── Arc reactor animation ───────────────────────────────────────
const CX = 110, CY = 110;
let t = 0;

function ring(r, width, alpha, start = 0, end = Math.PI * 2) {
  ctx.beginPath();
  ctx.arc(CX, CY, r, start, end);
  ctx.strokeStyle = `rgba(${CYAN}, ${alpha})`;
  ctx.lineWidth = width;
  ctx.stroke();
}

function segments(r, count, span, offset, width, alpha) {
  for (let i = 0; i < count; i++) {
    const a = offset + (i / count) * Math.PI * 2;
    ring(r, width, alpha, a, a + span);
  }
}

function draw() {
  t += 1 / 60;
  levelSmooth += (level - levelSmooth) * 0.25;
  level *= 0.96; // decay if no updates arrive

  ctx.clearRect(0, 0, 220, 220);

  const idlePulse = 0.5 + 0.5 * Math.sin(t * 1.4);
  let coreAlpha, coreR, glow;

  switch (state) {
    case "listening": {
      // waveform ring reacting to mic level
      coreAlpha = 0.9; glow = 26 + levelSmooth * 40; coreR = 20 + levelSmooth * 10;
      segments(74, 48, 0.06, t * 0.6, 2 + levelSmooth * 8, 0.8);
      ring(58, 1.5, 0.9);
      break;
    }
    case "thinking": {
      // spinning broken rings
      coreAlpha = 0.75; glow = 20; coreR = 20;
      segments(74, 3, 1.2, t * 2.4, 2.5, 0.85);
      segments(62, 4, 0.8, -t * 1.7, 1.5, 0.5);
      ring(58, 1, 0.35);
      break;
    }
    case "speaking": {
      // output waveform
      coreAlpha = 1; glow = 30 + levelSmooth * 50; coreR = 22 + levelSmooth * 12;
      segments(74, 64, 0.05, 0, 2 + levelSmooth * 14, 0.9);
      ring(58, 1.5, 0.9);
      break;
    }
    case "offline": {
      coreAlpha = 0.18; glow = 4; coreR = 18;
      ring(74, 1, 0.12);
      ring(58, 1, 0.1);
      break;
    }
    case "sleeping": {
      // barely-there heartbeat: dormant, but still listening for "jarvis"
      const breath = 0.5 + 0.5 * Math.sin(t * 0.7);
      coreAlpha = 0.08 + breath * 0.07;
      glow = 2 + breath * 4;
      coreR = 17;
      ring(58, 1, 0.06 + breath * 0.04);
      break;
    }
    default: { // idle — slow ambient pulse
      coreAlpha = 0.35 + idlePulse * 0.3;
      glow = 10 + idlePulse * 14;
      coreR = 19 + idlePulse * 2;
      segments(74, 12, 0.34, t * 0.15, 1.5, 0.4 + idlePulse * 0.2);
      ring(58, 1, 0.4);
    }
  }

  // static geometry
  ring(88, 1, 0.22);
  segments(88, 24, 0.02, 0, 3, 0.3);
  ring(98, 0.5, 0.12);

  // triangular tick marks (idle rotation)
  segments(50, 6, 0.16, -t * 0.35, 2, 0.35);

  // core — red while the mic is muted, so it's obvious even as the corner orb
  const coreCol = muted ? RED : CYAN;
  ctx.save();
  ctx.shadowColor = `rgba(${coreCol}, 0.9)`;
  ctx.shadowBlur = glow;
  ctx.beginPath();
  ctx.arc(CX, CY, coreR, 0, Math.PI * 2);
  ctx.fillStyle = `rgba(${coreCol}, ${coreAlpha})`;
  ctx.fill();
  ctx.restore();

  ctx.beginPath();
  ctx.arc(CX, CY, coreR + 6, 0, Math.PI * 2);
  ctx.strokeStyle = `rgba(${coreCol}, ${coreAlpha * 0.5})`;
  ctx.lineWidth = 1;
  ctx.stroke();

  if (muted) {          // slash through the core: universally "mic off"
    ctx.beginPath();
    ctx.moveTo(CX - coreR - 4, CY + coreR + 4);
    ctx.lineTo(CX + coreR + 4, CY - coreR - 4);
    ctx.strokeStyle = `rgba(${RED}, 0.9)`;
    ctx.lineWidth = 2.5;
    ctx.stroke();
  }

  requestAnimationFrame(draw);
}

console.log(`hud bridge available: ${Boolean(window.hud)}${window.hud ? "" : " — preload missing, click-through/drag/quit disabled"}`);
sendStatus();
connect();
draw();
scheduleRetract();   // if the backend never connects, still shrink out of the way
