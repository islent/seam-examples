// Electron host for the Seam surface.
//
// This shell exists for one reason: to show that the surface is a projection of the manifest
// and not a web app. The renderer is the *same* ui/index.html the browser loads; the only
// things Electron adds are the ones a browser cannot give:
//
//   * a configurable API base, so one window can point at any seam (local, a lab box, a
//     forwarded port on a cluster login node);
//   * direct disk access, for specimens and downloaded artifacts — post-processing inputs
//     usually live on the workstation, not on the cluster;
//   * a resident window: close the laptop lid, the seam keeps its trace.
//
// If any of these were the *reason* the surface worked, the specification would be wrong.
// They are conveniences; the manifest is the contract.

const { app, BrowserWindow, Menu, dialog, ipcMain, shell } = require("electron");
const path = require("node:path");
const fs = require("node:fs");

let win = null;
let base = process.env.SEAM_BASE || "http://127.0.0.1:8770";

const SMOKE = process.argv.includes("--smoke");
const TAB_ARG = process.argv.find((a) => a.startsWith("--seam-tab="));
const TAB = TAB_ARG ? TAB_ARG.slice("--seam-tab=".length) : "";

function createWindow() {
  win = new BrowserWindow({
    show: !SMOKE,
    width: 1280,
    height: 820,
    backgroundColor: "#fbfbfa",
    title: "Seam surface",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      additionalArguments: [`--seam-base=${base}`],
    },
  });
  // The renderer is the shared surface, loaded from disk. No bundler, no build step: the
  // same file the seam process serves over HTTP.
  // Same file the seam process serves to a browser. In smoke mode we deep-link a tab and keep
  // the window hidden, so the renderer can be inspected without a display.
  win.loadFile(path.join(__dirname, "..", "ui", "index.html"),
               TAB ? { hash: TAB } : {});
  win.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
}

function buildMenu() {
  const template = [
    {
      label: "Surface",
      submenu: [
        {
          label: "Connect to URL…",
          click: async () => {
            const answer = await promptUrl(base);
            if (answer && answer !== base) {
              base = answer;
              win.webContents.send("seam:base", base);
              win.reload();
            }
          },
        },
        { role: "reload" },
        { role: "toggleDevTools" },
      ],
    },
    {
      label: "Artifacts",
      submenu: [
        {
          label: "Open specimen folder…",
          click: async () => {
            const r = await dialog.showOpenDialog(win, { properties: ["openDirectory"] });
            if (!r.canceled && r.filePaths[0]) {
              win.webContents.send("seam:folder", r.filePaths[0]);
            }
          },
        },
      ],
    },
    { role: "fileMenu" },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// A one-field prompt without pulling in a dependency: a modal child window with an <input>.
async function promptUrl(current) {
  return new Promise((resolve) => {
    const child = new BrowserWindow({
      parent: win,
      modal: true,
      width: 520,
      height: 150,
      title: "Seam surface URL",
      webPreferences: { nodeIntegration: true, contextIsolation: false },
    });
    const html = `<!doctype html><meta charset="utf-8">
      <body style="font:13px system-ui;padding:16px;background:#fbfbfa">
      <div style="margin-bottom:8px;color:#6b6b66">Surface URL</div>
      <input id="u" style="width:100%;font:12px ui-monospace;padding:6px"
             value="${current.replace(/"/g, "&quot;")}">
      <div style="margin-top:12px;text-align:right">
        <button id="ok" style="padding:4px 14px">Connect</button>
      </div>
      <script>
        const { ipcRenderer } = require("electron");
        document.getElementById("ok").onclick = () => {
          window.close();
        };
        document.getElementById("u").addEventListener("keydown", e => {
          if (e.key === "Enter") window.close();
        });
        window.addEventListener("beforeunload", () => {
          ipcRenderer.send("seam:url-chosen", document.getElementById("u").value);
        });
      </script></body>`;
    child.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
    ipcMain.once("seam:url-chosen", (_e, url) => resolve(String(url || "").trim()));
    child.on("closed", () => resolve(null));
  });
}

// Native helpers exposed to the renderer through preload's contextBridge.
ipcMain.handle("seam:read-file", async (_e, filePath) => {
  try {
    const stat = fs.statSync(filePath);
    if (stat.size > 64 * 1024 * 1024) return { error: "file larger than 64 MB" };
    return { bytes: fs.readFileSync(filePath).toString("base64"), size: stat.size };
  } catch (e) {
    return { error: String(e.message || e) };
  }
});

ipcMain.handle("seam:list-dir", async (_e, dir) => {
  try {
    return fs.readdirSync(dir, { withFileTypes: true })
      .slice(0, 500)
      .map((d) => ({ name: d.name, dir: d.isDirectory() }));
  } catch (e) {
    return { error: String(e.message || e) };
  }
});

// ---------------------------------------------------------------------------
// smoke mode: boot the real renderer inside Chromium, read the DOM, print, exit.
// This is how the Electron host is verified without a human looking at a window.
// ---------------------------------------------------------------------------
async function smoke() {
  createWindow();
  await new Promise((resolve) => win.webContents.once("did-finish-load", resolve));
  await new Promise((resolve) => setTimeout(resolve, 5000));
  const facts = await win.webContents.executeJavaScript(`(() => {
    const q = (id) => (document.getElementById(id) || {}).textContent || null;
    const imgs = [...document.images].filter((i) => i.src.includes("/api/frame"));
    return {
      identity: q("h-name"),
      code: q("h-code"),
      transport: q("h-transport"),
      status: q("h-status"),
      profile: q("h-profile"),
      tabs: [...document.querySelectorAll("nav button")].map((b) => b.textContent),
      controls: document.querySelectorAll(".ctl").length,
      gauges: document.querySelectorAll(".gauge").length,
      groupHeaders: [...document.querySelectorAll("#left h2")].map((h) => h.textContent),
      frames: imgs.map((i) => ({ w: i.naturalWidth, h: i.naturalHeight, ok: i.complete && i.naturalWidth > 0 })),
      firstValues: [...document.querySelectorAll(".ctl .val")].slice(0, 4).map((v) => v.textContent),
      eventSource: typeof EventSource !== "undefined",
    };
  })()`);
  console.log("SMOKE " + JSON.stringify(facts));
  app.exit(0);
}

app.whenReady().then(() => {
  if (SMOKE) { smoke(); return; }
  createWindow();
  buildMenu();
  app.on("activate", () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on("window-all-closed", () => { if (process.platform !== "darwin") app.quit(); });
