const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("hud", {
  status: (mode, active) => ipcRenderer.send("hud-status", { mode, active }),
  dragStart: () => ipcRenderer.send("drag-start"),
  dragEnd: () => ipcRenderer.send("drag-end"),
  quit: () => ipcRenderer.send("quit-app"),
});
