// Preload: the only bridge between the Electron shell and the shared surface.
//
// The renderer is ui/index.html, unchanged, exactly as the browser receives it from the seam
// process. Everything Electron-specific arrives through `window.seamDesktop` and through
// window.SEAM_BASE, so the same file keeps working with no shell at all.

const { contextBridge, ipcRenderer } = require("electron");

const arg = process.argv.find((a) => a.startsWith("--seam-base="));
const base = arg ? arg.slice("--seam-base=".length) : "http://127.0.0.1:8770";

contextBridge.exposeInMainWorld("SEAM_BASE", base);

contextBridge.exposeInMainWorld("seamDesktop", {
  platform: process.platform,
  // Disk access is the one capability a browser cannot offer, and post-processing inputs
  // usually live on the workstation rather than the cluster.
  readFile: (p) => ipcRenderer.invoke("seam:read-file", p),
  listDir: (p) => ipcRenderer.invoke("seam:list-dir", p),
  onBaseChange: (cb) => ipcRenderer.on("seam:base", (_e, url) => cb(url)),
  onFolder: (cb) => ipcRenderer.on("seam:folder", (_e, dir) => cb(dir)),
});
