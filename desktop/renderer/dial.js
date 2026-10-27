"use strict";
// Arc slider for the target temperature. Reads the positions app.js already writes
// (marker left %) and turns them into arc angles; drag or arrow keys send one temp command.
(() => {
  const MIN = 16, MAX = 30, START = 225, SWEEP = 270;
  const $ = (id) => document.getElementById(id);
  const ribbon = $("ribbon"), room = $("marker-room"), target = $("marker-target"), text = $("target-temp");
  const block = $("target-block"), down = $("temp-down"), up = $("temp-up");
  if (!ribbon) return;
  let dragging = false, pending = null;

  const toPct = (v) => ((v - MIN) / (MAX - MIN)) * 100;
  const fromPct = (p) => Math.round(MIN + (p / 100) * (MAX - MIN));
  const current = () => { const p = parseFloat(target.style.left); return Number.isNaN(p) ? null : fromPct(p); };
  const enabled = () => !block.classList.contains("inactive") && !(down.disabled && up.disabled);

  function sync() {
    const r = parseFloat(room.style.left), t = parseFloat(target.style.left);
    if (!Number.isNaN(r)) ribbon.style.setProperty("--room", r);
    if (!Number.isNaN(t) && !dragging) { ribbon.style.setProperty("--target", t); ribbon.setAttribute("aria-valuenow", String(fromPct(t))); }
    ribbon.setAttribute("aria-disabled", String(!enabled()));
  }
  const watch = new MutationObserver(sync);
  for (const node of [room, target, block, down, up]) watch.observe(node, { attributes: true, attributeFilter: ["style", "class", "disabled"] });
  sync();

  function preview(v) {
    pending = v;
    ribbon.style.setProperty("--target", toPct(v));
    ribbon.setAttribute("aria-valuenow", String(v));
    text.textContent = v;
  }
  function commit(v) {
    pending = null;
    if (v == null || v === current()) { sync(); return; }
    if (typeof act === "function") act("temp" + v); else preview(v);
  }
  function tempAt(event) {
    const box = ribbon.getBoundingClientRect();
    const x = event.clientX - box.left - box.width / 2, y = event.clientY - box.top - box.height / 2;
    const deg = (Math.atan2(x, -y) * 180) / Math.PI;
    let rel = (((deg - START) % 360) + 360) % 360;
    if (rel > SWEEP) rel = rel > SWEEP + (360 - SWEEP) / 2 ? 0 : SWEEP;
    return fromPct((rel / SWEEP) * 100);
  }

  ribbon.addEventListener("pointerdown", (event) => {
    if (!enabled() || event.button !== 0) return;
    dragging = true; ribbon.classList.add("dragging"); ribbon.setPointerCapture(event.pointerId);
    preview(tempAt(event)); event.preventDefault();
  });
  ribbon.addEventListener("pointermove", (event) => { if (dragging) preview(tempAt(event)); });
  const end = (event) => { if (!dragging) return; dragging = false; ribbon.classList.remove("dragging"); commit(event.type === "pointerup" ? tempAt(event) : pending); };
  ribbon.addEventListener("pointerup", end);
  ribbon.addEventListener("pointercancel", end);
  ribbon.addEventListener("keydown", (event) => {
    if (!enabled()) return;
    const v = current(); if (v == null) return;
    const step = { ArrowRight: 1, ArrowUp: 1, ArrowLeft: -1, ArrowDown: -1, Home: MIN - v, End: MAX - v }[event.key];
    if (step == null) return;
    event.preventDefault(); commit(Math.min(MAX, Math.max(MIN, v + step)));
  });
})();
