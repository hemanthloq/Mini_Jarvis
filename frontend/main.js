// JARVIS HUD — frameless, transparent, always-on-top overlay window.
const { app, BrowserWindow, screen, Tray, Menu, nativeImage, ipcMain, globalShortcut } = require("electron");
const path = require("path");

let win, tray;

function createWindow() {
  const { width, height } = screen.getPrimaryDisplay().workAreaSize;
  const W = 360, H = 520;

  win = new BrowserWindow({
    width: W,
    height: H,
    x: width - W - 24,
    y: height - H - 24,
    frame: false,
    transparent: true,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    hasShadow: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  win.setAlwaysOnTop(true, "screen-saver");
  win.loadFile(path.join(__dirname, "renderer", "index.html"));

  if (process.env.HUD_DEBUG) {
    win.webContents.on("console-message", (e, level, msg) =>
      console.log("[renderer]", msg));
  }
}

// ── Click-through (idle) ────────────────────────────────────────
// setIgnoreMouseEvents' `forward: true` mousemove forwarding proved unreliable
// on this setup (renderer received no events at all), so the main process
// polls the global cursor instead: while the assistant is idle, the window
// ignores mouse input unless the cursor is over the interactive region
// (whole HUD when expanded, just the orb when retracted).
const WIN_W = 360, WIN_H = 520;
const ORB = { x1: 290, y1: 455, x2: 360, y2: 520 };  // window-relative orb area

let hudMode = "expanded";   // expanded | retracted
let hudActive = false;      // listening / thinking / speaking
let ignoring = null;

function setIgnore(ig) {
  if (!win || win.isDestroyed() || ignoring === ig) return;
  ignoring = ig;
  if (process.env.HUD_DEBUG) console.log(`[main] ignore=${ig} (mode=${hudMode} active=${hudActive})`);
  win.setIgnoreMouseEvents(ig, { forward: true });
}

setInterval(() => {
  if (!win || win.isDestroyed()) return;
  if (dragging) { setIgnore(false); return; }   // never go click-through mid-drag
  if (hudActive) { setIgnore(false); return; }
  const p = screen.getCursorScreenPoint();
  const [wx, wy] = win.getPosition();
  const rx = p.x - wx, ry = p.y - wy;
  const inside = hudMode === "retracted"
    ? rx >= ORB.x1 && rx <= ORB.x2 && ry >= ORB.y1 && ry <= ORB.y2
    : rx >= 0 && ry >= 0 && rx <= WIN_W && ry <= WIN_H;
  setIgnore(!inside);
}, 100);

ipcMain.on("hud-status", (e, s) => {
  hudMode = s.mode;
  hudActive = s.active;
});

// ── Manual window drag ──────────────────────────────────────────
// CSS -webkit-app-region:drag swallows all mouse events (breaking both dragging
// and hover detection), so the renderer just reports mousedown/up on the drag
// handles and the main process moves the window itself.
//
// Two bugs this guards against:
//  * The window must only MOVE. setBounds is called with the width/height
//    captured at drag-start, so nothing can drift the size (Windows DIP<->pixel
//    rounding can otherwise creep the size on every call).
//  * The click-through poll must not fire mid-drag: flipping ignore=true would
//    swallow the mouseup and strand the window attached to the cursor.
let dragTimer = null;
let dragging = false;

function stopDrag() {
  clearInterval(dragTimer);
  dragTimer = null;
  dragging = false;
}

ipcMain.on("drag-start", () => {
  if (!win || win.isDestroyed()) return;
  const start = screen.getCursorScreenPoint();
  const b = win.getBounds();                       // pin the size for the whole drag
  const off = { dx: start.x - b.x, dy: start.y - b.y, w: b.width, h: b.height };
  const t0 = Date.now();

  stopDrag();
  dragging = true;
  setIgnore(false);

  dragTimer = setInterval(() => {
    if (!win || win.isDestroyed()) return stopDrag();
    if (Date.now() - t0 > 20000) return stopDrag();   // safety: never stick forever
    const p = screen.getCursorScreenPoint();
    // Keep the window on-screen: dragging it off the edge would strand the orb
    // where the cursor can't reach it.
    const area = screen.getDisplayNearestPoint(p).workArea;
    const x = Math.min(Math.max(p.x - off.dx, area.x), area.x + area.width - off.w);
    const y = Math.min(Math.max(p.y - off.dy, area.y), area.y + area.height - off.h);
    // x/y only — width/height are re-asserted from the drag-start snapshot.
    win.setBounds({ x, y, width: off.w, height: off.h });
  }, 16);
});

ipcMain.on("drag-end", () => {
  const wasDragging = dragging;
  stopDrag();
  if (wasDragging && win && !win.isDestroyed()) {
    const b = win.getBounds();
    if (b.width !== WIN_W || b.height !== WIN_H) {
      win.setBounds({ x: b.x, y: b.y, width: WIN_W, height: WIN_H });  // paranoia
    }
  }
});

// ── Quit (kills backend too) ────────────────────────────────────
async function quitAll() {
  try {
    await fetch("http://127.0.0.1:8765/quit", { signal: AbortSignal.timeout(1500) });
  } catch (_) { /* backend already gone */ }
  app.quit();
}

ipcMain.on("quit-app", quitAll);

app.whenReady().then(() => {
  createWindow();

  globalShortcut.register("Control+Alt+Q", quitAll);

  // tray icon so the overlay can be hidden/shown/quit
  tray = new Tray(nativeImage.createFromDataURL(TRAY_PNG));
  tray.setToolTip("JARVIS");
  tray.setContextMenu(Menu.buildFromTemplate([
    { label: "Show / Hide", click: () => (win.isVisible() ? win.hide() : win.show()) },
    { type: "separator" },
    { label: "Quit JARVIS (backend + HUD)", click: quitAll },
  ]));
});

app.on("will-quit", () => globalShortcut.unregisterAll());
app.on("window-all-closed", () => app.quit());

// 16x16 cyan dot
const TRAY_PNG =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAABAAAAAQCAYAAAAf8/9hAAAAgUlEQVR4nGNgGAWDHTAyMPzHJ8/IwPAfmwYWQgYwMDAwMOFTgA0zYTMdmyIWQoox4VMMk2ci1mZsLmDCJoBLDCbPRMh2dP8z4XMzNjFCriDJBmLCgOgwIDoWiEkH2NIJE4wixgvY0gGuMEBPBySlA3xhgFcRIe8w4QsDLGr+kxQGADO8IB/t9V/UAAAAAElFTkSuQmCC";
