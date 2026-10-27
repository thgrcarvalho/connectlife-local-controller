"use strict";

const { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, screen, shell } = require("electron");
const { spawn } = require("child_process");
const fs = require("fs");
const http = require("http");
const path = require("path");

const POPUP = { width: 320, height: 540 };
const MIN_HEIGHT = 540;
const MAX_HEIGHT = 820;
const DEFAULT_SETTINGS = {
  apiUrl: "http://127.0.0.1:8767",
  controllerDir: path.join(process.env.LOCALAPPDATA || app.getPath("appData"),
    "ConnectLifeLocalController", "fakecloud"),
  startController: true,
  openAtLogin: true,
};

let tray = null;
let popup = null;
let settings = DEFAULT_SETTINGS;
let lastSnapshot = null;

const settingsPath = () => path.join(app.getPath("userData"), "settings.json");

function loadSettings() {
  try {
    const stored = JSON.parse(fs.readFileSync(settingsPath(), "utf8"));
    return { ...DEFAULT_SETTINGS, ...stored };
  } catch (error) {
    try {
      fs.mkdirSync(app.getPath("userData"), { recursive: true });
      fs.writeFileSync(settingsPath(), JSON.stringify(DEFAULT_SETTINGS, null, 2));
    } catch (_) { /* first run without a writable profile: keep defaults */ }
    return { ...DEFAULT_SETTINGS };
  }
}

function icon(state) {
  const image = nativeImage.createFromPath(path.join(__dirname, "assets", `tray-${state}-16.png`));
  image.addRepresentation({
    scaleFactor: 2,
    buffer: fs.readFileSync(path.join(__dirname, "assets", `tray-${state}-32.png`)),
  });
  return image;
}

function trayState(snapshot) {
  if (!snapshot || snapshot.error || !snapshot.connected) return "off";
  if (snapshot.warnings && snapshot.warnings.length) return "warn";
  return snapshot.status && snapshot.status.t_power === "1" ? "active" : "idle";
}

const MODES = { 0: "Fan", 2: "Cool", 3: "Dry", 5: "Eco" };

function tooltip(snapshot) {
  if (!snapshot) return "AC Tray: connecting to the controller";
  if (snapshot.error) return `AC Tray: ${snapshot.error}`;
  if (!snapshot.connected) return "AC Tray: the AC is not connected";
  const s = snapshot.status || {};
  const room = s.f_temp_in ? `${s.f_temp_in}° in the room` : "room temperature unknown";
  if (s.t_power !== "1") return `AC off. ${room}`;
  const mode = MODES[s.t_work_mode] || "On";
  const target = s.t_work_mode === "0" ? "" : ` to ${s.t_temp}°`;
  return `${mode}${target}. ${room}`;
}

function positionPopup() {
  const trayBounds = tray.getBounds();
  const display = screen.getDisplayNearestPoint({ x: trayBounds.x, y: trayBounds.y });
  const area = display.workArea;
  const x = Math.round(Math.min(
    Math.max(area.x, trayBounds.x + trayBounds.width / 2 - POPUP.width / 2),
    area.x + area.width - POPUP.width));
  const below = trayBounds.y + trayBounds.height + POPUP.height <= area.y + area.height;
  const y = below ? trayBounds.y + trayBounds.height + 8 : trayBounds.y - POPUP.height - 8;
  popup.setBounds({ x, y: Math.max(area.y, y), ...POPUP });
}

function createPopup() {
  popup = new BrowserWindow({
    ...POPUP,
    show: false,
    frame: false,
    resizable: false,
    movable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    backgroundColor: "#060910",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: false,
    },
  });
  popup.loadFile(path.join(__dirname, "renderer", "index.html"));
  popup.on("blur", () => { if (!popup.webContents.isDevToolsOpened()) popup.hide(); });
  popup.on("close", (event) => { if (!app.isQuitting) { event.preventDefault(); popup.hide(); } });
}

function togglePopup() {
  if (popup.isVisible()) { popup.hide(); return; }
  positionPopup();
  popup.show();
  popup.focus();
  popup.webContents.send("popup-shown");
}

function createTray() {
  tray = new Tray(icon("off"));
  tray.setToolTip(tooltip(null));
  tray.on("click", togglePopup);
  tray.on("right-click", () => tray.popUpContextMenu(Menu.buildFromTemplate([
    { label: "Open controls", click: togglePopup },
    { label: "Open controller log", click: openLog },
    { label: "Open app log", click: () => shell.openPath(path.join(app.getPath("userData"), "renderer.log")) },
    { label: "Open settings file", click: () => shell.openPath(settingsPath()) },
    { type: "separator" },
    { label: "Quit AC Tray", click: () => { app.isQuitting = true; app.quit(); } },
  ])));
}

function openLog() {
  shell.openPath(path.join(settings.controllerDir, "captures", "controller-processes.log"));
}

function apiReachable() {
  return new Promise((resolve) => {
    const request = http.get(`${settings.apiUrl}/api/status`, { timeout: 2000 }, (response) => {
      response.resume();
      resolve(response.statusCode === 200);
    });
    request.on("error", () => resolve(false));
    request.on("timeout", () => { request.destroy(); resolve(false); });
  });
}

async function ensureController() {
  if (!settings.startController) return;
  for (let attempt = 0; attempt < 5; attempt += 1) {
    if (await apiReachable()) return;
    await new Promise((done) => setTimeout(done, 2000));
  }
  const script = path.join(settings.controllerDir, "controller_cli.py");
  if (!fs.existsSync(script)) return;
  const child = spawn("pythonw.exe", [script, "run"], {
    cwd: settings.controllerDir, detached: true, stdio: "ignore", windowsHide: true,
  });
  child.on("error", () => { /* pythonw missing: the renderer shows the API as unreachable */ });
  child.unref();
}

ipcMain.handle("get-settings", () => settings);
ipcMain.on("snapshot", (_event, snapshot) => {
  lastSnapshot = snapshot;
  tray.setImage(icon(trayState(snapshot)));
  tray.setToolTip(tooltip(snapshot));
});
ipcMain.on("hide-popup", () => popup.hide());
ipcMain.on("resize", (_event, height) => {
  const next = Math.round(Math.min(MAX_HEIGHT, Math.max(MIN_HEIGHT, Number(height) || MIN_HEIGHT)));
  if (next === POPUP.height) return;
  POPUP.height = next;
  if (popup.isVisible()) positionPopup();
});
ipcMain.on("log", (_event, line) => {
  try {
    fs.appendFileSync(path.join(app.getPath("userData"), "renderer.log"),
      `${new Date().toISOString()} ${line}\n`);
  } catch (_) { /* logging is best effort */ }
});
ipcMain.on("open-log", openLog);
ipcMain.on("quit", () => { app.isQuitting = true; app.quit(); });

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => { if (popup) togglePopup(); });
  app.whenReady().then(() => {
    settings = loadSettings();
    if (app.isPackaged) app.setLoginItemSettings({ openAtLogin: Boolean(settings.openAtLogin) });
    createTray();
    createPopup();
    ensureController();
    if (process.argv.includes("--show")) popup.webContents.once("did-finish-load", togglePopup);
  });
  app.on("window-all-closed", (event) => event.preventDefault());
}
