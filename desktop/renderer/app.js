"use strict";

const MODES = { 0: "fan", 2: "cool", 3: "dry", 5: "eco" };
const MIN_TEMP = 16;
const MAX_TEMP = 30;

const el = (id) => document.getElementById(id);
const app = el("app");
const ui = {
  pathLabel: el("path-label"), sleep: el("sleep"), roomTemp: el("room-temp"),
  targetBlock: el("target-block"), targetTemp: el("target-temp"), targetLabel: el("target-label"),
  tempDown: el("temp-down"), tempUp: el("temp-up"), ribbonSpan: el("ribbon-span"),
  markerRoom: el("marker-room"), markerTarget: el("marker-target"),
  modes: el("modes"), fans: el("fans"), fanHint: el("fan-hint"), power: el("power"),
  notice: el("notice"), updated: el("updated"),
};

let apiUrl = "http://127.0.0.1:8767";
let snapshot = null;
let busy = false;
let socket = null;
let reconnectDelay = 1000;
let noticeTimer = null;
let unreachable = true;

function pct(value) {
  const clamped = Math.min(MAX_TEMP, Math.max(MIN_TEMP, value));
  return ((clamped - MIN_TEMP) / (MAX_TEMP - MIN_TEMP)) * 100;
}

function setChecked(group, value) {
  for (const button of group.querySelectorAll("button")) {
    button.setAttribute("aria-checked", String(button.dataset.value === value));
  }
}

function setDisabled(group, disabled) {
  for (const button of group.querySelectorAll("button")) button.disabled = disabled;
}

function showNotice(message, kind, sticky) {
  clearTimeout(noticeTimer);
  ui.notice.textContent = message;
  ui.notice.classList.toggle("error", kind === "error");
  ui.notice.classList.toggle("info", kind === "info");
  ui.notice.hidden = !message;
  if (kind !== "warning" && !sticky) noticeTimer = setTimeout(renderNotice, 6000);
}

function renderNotice() {
  if (!snapshot) return;
  const warning = (snapshot.warnings || [])[0];
  if (warning) { showNotice(warning.message, "warning"); return; }
  ui.notice.hidden = true;
}

function ago(timestamp) {
  if (!timestamp) return "No reading yet";
  const seconds = Math.max(0, Math.round(Date.now() / 1000 - timestamp));
  if (seconds < 5) return "Updated just now";
  if (seconds < 60) return `Updated ${seconds} s ago`;
  const minutes = Math.round(seconds / 60);
  return `Updated ${minutes} min ago`;
}

function render() {
  if (!snapshot) return;
  fitWindow();
  const s = snapshot.status || {};
  const on = s.t_power === "1";
  const mode = s.t_work_mode;
  const connected = Boolean(snapshot.connected);
  const room = s.f_temp_in != null ? Number(s.f_temp_in) : null;
  const target = s.t_temp != null ? Number(s.t_temp) : null;
  const tempCapable = on && mode !== "0";

  app.dataset.path = snapshot.mode;
  app.dataset.state = unreachable ? "unreachable" : connected ? "ready" : "disconnected";
  ui.pathLabel.textContent = unreachable ? "Controller unreachable"
    : !connected ? "AC not connected"
    : snapshot.mode === "online" ? "Through the cloud" : "Local control";

  ui.roomTemp.textContent = room == null ? "--" : room;
  ui.targetTemp.textContent = target == null ? "--" : target;
  ui.targetLabel.textContent = !on ? "target when on" : mode === "0" ? "fan only" : "target";
  ui.targetBlock.classList.toggle("inactive", !tempCapable);
  ui.tempDown.disabled = !tempCapable || target == null || target <= MIN_TEMP;
  ui.tempUp.disabled = !tempCapable || target == null || target >= MAX_TEMP;

  if (room != null && target != null) {
    const a = pct(room), b = pct(target);
    // The unit only cools: the span lights up while the room is warmer than the
    // target. A target at or above the room reads dim, the AC is idling like fan mode.
    const cooling = tempCapable && room > target;
    ui.ribbonSpan.style.left = `${Math.min(a, b)}%`;
    ui.ribbonSpan.style.width = `${Math.abs(a - b)}%`;
    ui.ribbonSpan.style.opacity = cooling ? ".9" : ".25";
  }
  ui.markerRoom.classList.toggle("hidden", room == null);
  ui.markerTarget.classList.toggle("hidden", target == null);
  if (room != null) ui.markerRoom.style.left = `${pct(room)}%`;
  if (target != null) ui.markerTarget.style.left = `${pct(target)}%`;
  ui.markerTarget.style.opacity = tempCapable ? "1" : ".4";

  setChecked(ui.modes, mode);
  setDisabled(ui.modes, !on);
  const fanLocked = mode === "3";
  setChecked(ui.fans, fanLocked ? "4" : s.t_fan_speed);
  setDisabled(ui.fans, !on || fanLocked);
  ui.fanHint.textContent = !on ? "" : fanLocked ? "Dry mode keeps the fan on high"
    : s.t_fan_speed === "0" ? "Fan speed is automatic" : "";

  ui.sleep.setAttribute("aria-pressed", String(s.t_sleep === "1"));
  ui.sleep.disabled = !on || mode === "0";

  ui.power.textContent = on ? "Turn off" : "Turn on";
  ui.power.classList.toggle("on", on);

  const controlsAvailable = connected && !unreachable;
  ui.power.disabled = !controlsAvailable;
  if (!controlsAvailable) {
    setDisabled(ui.modes, true); setDisabled(ui.fans, true);
    ui.tempDown.disabled = ui.tempUp.disabled = ui.sleep.disabled = true;
  }

  ui.updated.textContent = snapshot.error ? snapshot.error : ago(snapshot.updated_at);
  renderNotice();
  window.tray.publish({ ...snapshot, error: unreachable ? "controller unreachable" : snapshot.error });
}

function applySnapshot(next, source) {
  snapshot = next;
  const s = next.status || {};
  window.tray.log(`${source} mode=${next.mode} connected=${next.connected} power=${s.t_power} `
    + `mode=${s.t_work_mode} temp=${s.t_temp} room=${s.f_temp_in} updated=${next.updated_at} error=${next.error}`);
  render();
}

async function act(action) {
  if (busy) return;
  busy = true;
  app.classList.add("busy");
  try {
    const response = await fetch(`${apiUrl}/api/actions/${action}`, { method: "POST" });
    const body = await response.json();
    if (!body.ok) throw new Error(body.error || "The controller rejected the command");
    unreachable = false;
    applySnapshot(body, `action:${action}`);
  } catch (error) {
    if (error instanceof TypeError) { unreachable = true; render(); }
    window.tray.log(`action:${action} failed: ${error.message}`);
    showNotice(error.message, "error");
  } finally {
    busy = false;
    app.classList.remove("busy");
  }
}

let reconnectTimer = null;

function connectEvents() {
  clearTimeout(reconnectTimer);
  if (socket && socket.readyState !== WebSocket.CLOSED) {
    try { socket.close(); } catch (_) { /* already closing */ }
  }
  const wsUrl = apiUrl.replace(/^http/, "ws") + "/api/events";
  const current = new WebSocket(wsUrl);
  socket = current;
  current.onopen = () => {
    if (socket !== current) return;
    window.tray.log("events: connected");
    reconnectDelay = 1000; unreachable = false; render();
  };
  current.onmessage = (event) => {
    if (socket !== current) return;
    try { applySnapshot(JSON.parse(event.data), "push"); } catch (error) { window.tray.log(`push failed: ${error.message}`); }
  };
  current.onclose = () => {
    if (socket !== current) return;
    window.tray.log(`events: closed, retry in ${reconnectDelay} ms`);
    unreachable = true;
    if (snapshot) render(); else ui.pathLabel.textContent = "Controller unreachable";
    window.tray.publish({ error: "controller unreachable", connected: false });
    clearTimeout(reconnectTimer);
    reconnectTimer = setTimeout(connectEvents, reconnectDelay);
    reconnectDelay = Math.min(15000, reconnectDelay * 2);
  };
  current.onerror = () => { if (socket === current) current.close(); };
}

function requestRefresh() {
  if (socket && socket.readyState === WebSocket.OPEN) socket.send("refresh");
}

ui.power.addEventListener("click", () => act(snapshot.status.t_power === "1" ? "off" : "on"));
ui.tempDown.addEventListener("click", () => act(`temp${Number(snapshot.status.t_temp) - 1}`));
ui.tempUp.addEventListener("click", () => act(`temp${Number(snapshot.status.t_temp) + 1}`));
ui.sleep.addEventListener("click", () => act(snapshot.status.t_sleep === "1" ? "sleepoff" : "sleep"));
for (const group of [ui.modes, ui.fans]) {
  group.addEventListener("click", (event) => {
    const button = event.target.closest("button[data-action]");
    if (button && !button.disabled) act(button.dataset.action);
  });
}
const diag = { box: el("diag"), rows: el("diag-rows"), title: el("diag-title") };

function showDiagnosis(result) {
  const categories = result.categories || { Sensors: [], Communications: [], Others: [] };
  diag.rows.replaceChildren();
  for (const [name, faults] of Object.entries(categories)) {
    const dt = document.createElement("dt"); dt.textContent = name;
    const dd = document.createElement("dd");
    dd.textContent = faults.length ? faults.join("; ") : "Normal";
    dd.classList.toggle("fault", faults.length > 0);
    diag.rows.append(dt, dd);
  }
  diag.title.textContent = result.normal ? "Self-check passed" : "Self-check found faults";
  diag.box.hidden = false;
  fitWindow();
}

function fitWindow() {
  // Measure the content's natural height: the layout stretches to the window
  // (min-height: 100vh), so lift that floor for one frame before reading it.
  requestAnimationFrame(() => {
    app.style.minHeight = "0";
    const height = app.offsetHeight;
    app.style.minHeight = "";
    window.tray.resize(height);
  });
}

async function diagnose() {
  if (busy) return;
  busy = true;
  app.classList.add("busy");
  diag.box.hidden = true;
  showNotice("Running the self-check", "info");
  try {
    const response = await fetch(`${apiUrl}/api/actions/diagnose`, { method: "POST" });
    const body = await response.json();
    if (!body.ok) throw new Error(body.error || "The self-check did not run");
    const d = body.diagnostics || {};
    window.tray.log(`diagnose normal=${d.normal} failed=${(d.failed || []).join(",")}`);
    ui.notice.hidden = true;
    showDiagnosis(d);
  } catch (error) {
    window.tray.log(`diagnose failed: ${error.message}`);
    showNotice(error.message, "error");
  } finally {
    busy = false;
    app.classList.remove("busy");
  }
}

el("diag-close").addEventListener("click", () => { diag.box.hidden = true; fitWindow(); });
el("diagnose").addEventListener("click", diagnose);
el("refresh").addEventListener("click", requestRefresh);
el("log").addEventListener("click", () => window.tray.openLog());
el("quit").addEventListener("click", () => window.tray.quit());
document.addEventListener("keydown", (event) => { if (event.key === "Escape") window.tray.hide(); });

window.tray.onShown(() => { window.tray.log("popup shown"); diag.box.hidden = true; requestRefresh(); render(); });
setInterval(() => { if (snapshot && !busy) ui.updated.textContent = snapshot.error || ago(snapshot.updated_at); }, 10000);

window.tray.settings().then((settings) => {
  apiUrl = settings.apiUrl.replace(/\/$/, "");
  connectEvents();
});
