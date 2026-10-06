// Dev page. Renders the status object the daemon pushes; sends commands over POST.
// No timers, no state, no transition logic here.
"use strict";

const RECONNECT_DELAY_MS = 2000;
const EVENT_LIMIT = 20;

const $ = (id) => document.getElementById(id);

async function send(body) {
  const resp = await fetch("/api/command", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({}));
    console.warn("command rejected", resp.status, err.error);
  }
}

// Settings routes (Part A): POST /api/settings/<name>; the daemon decides, the page shows the answer.
async function settings(name, body = {}) {
  const resp = await fetch(`/api/settings/${name}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  $("settings_error").textContent = resp.ok ? "" : `${name}: ${resp.status} ${data.error ?? ""}`;
  if (resp.ok && data.test_server) {
    const t = data.test_server;
    $("test_result").textContent = t.ok ? `OK · ${t.took_ms} ms` : `${t.error} · ${t.took_ms} ms`;
  }
  return data;
}

let lastState = null;

function render(status) {
  document.body.dataset.state = status.state;
  $("state_name").textContent = status.state;
  $("countdown").textContent = status.countdown_s === null ? "" : `${status.countdown_s.toFixed(0)} s`;
  for (const el of document.querySelectorAll(".selected_level")) {
    el.textContent = status.selected_level ?? "";
  }
  $("maintenance_message").textContent = status.maintenance_message;
  $("reason").textContent = status.reason ? `Reason: ${status.reason}` : "";
  document.body.dataset.reason = status.reason ?? "";
  $("purchase_id").textContent = status.purchase_id ?? "–";
  document.body.dataset.motor = status.motor.running ? "running" : status.motor.pressed ? "pressed" : "";
  document.body.dataset.hardware = status.hardware_mode;
  document.body.dataset.purchase = status.purchase_mode;
  document.body.dataset.reachable = String(status.purchase_server.reachable);

  const qr = $("qr");
  const src = status.qr_url ?? "";
  if (qr.getAttribute("src") !== src) qr.setAttribute("src", src);

  const levels = $("levels");
  if (levels.childElementCount !== status.levels) {
    levels.replaceChildren();
    for (let n = 1; n <= status.levels; n++) {
      const b = document.createElement("button");
      b.textContent = `Level ${n}`;
      b.onclick = () => send({ command: "select_level", level: n });
      levels.append(b);
    }
  }

  const dev = $("dev");
  dev.hidden = status.hardware_mode !== "mock" && status.purchase_mode !== "mock";
  if (!dev.hidden) {
    $("doors").textContent = Object.entries(status.doors)
      .map(([n, s]) => `${n}:${s === "locked" ? "🔒" : s === "unlocked" ? "🔓" : "?"}`).join(" ");
    $("motor").textContent = JSON.stringify(status.motor);
    const leds = status.leds;
    const reach = leds.reachable === null ? "unknown" : leds.reachable ? "reachable" : "unreachable";
    $("leds").textContent = `${leds.pattern} · level ${leds.level ?? "–"} · ${reach}`;
    $("audio").textContent = (status.audio.playing ?? "—") + (status.audio.available ? "" : " (unavailable)");
    $("outbox").textContent = `${status.purchase_server.outbox_pending} pending`;
    $("purchase_server").textContent = JSON.stringify(status.purchase_server);
    $("hardware").textContent = JSON.stringify(status.hardware, null, 1);
    const st = status.settings;
    $("settings_status").textContent =
      `${status.state} · out_of_order switch ${st.out_of_order ? "on" : "off"}` +
      `${st.pin_is_default ? " · default PIN" : ""} · ${status.hostname} ${status.ip ?? "no ip"}` +
      ` · v${status.app_version} · server ${status.purchase_server.base_url}`;
    $("ooo").checked = st.out_of_order;
    for (const [id, value] of [["brightness", status.leds.brightness], ["volume", status.audio.volume]]) {
      if (document.activeElement !== $(id)) $(id).value = Math.round(value * 100);
    }
    const select = $("door_level");
    if (select.childElementCount !== status.levels) {
      select.replaceChildren(...Array.from({ length: status.levels }, (_, i) => new Option(`Level ${i + 1}`, i + 1)));
    }
    if (status.state !== lastState) loadSummary();
    lastState = status.state;
    loadEvents();
  }
}

async function loadSummary() {
  const s = await fetch("/api/events/summary").then((r) => r.json());
  $("summary").textContent = `vends today ${s.vends_today} · total ${s.vends_total} · alarms ${s.alarms_today} · faults ${s.faults_today} · outbox ${s.outbox_pending}`;
}

async function loadEvents() {
  const rows = await fetch(`/api/events?limit=${EVENT_LIMIT}`).then((r) => r.json());
  $("events").replaceChildren(...rows.map((e) => {
    const li = document.createElement("li");
    const details = e.details ? JSON.stringify(e.details) : "";
    li.textContent = `${e.ts.slice(11, 23)} ${e.kind} [${e.state}] ${details}`;
    return li;
  }));
}

function setConnected(connected) {
  const el = $("connection");
  el.textContent = connected ? "connected" : "disconnected";
  el.className = connected ? "connected" : "disconnected";
}

function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/ws`);
  ws.onopen = () => setConnected(true);
  ws.onmessage = (event) => render(JSON.parse(event.data));
  ws.onclose = () => {
    setConnected(false);
    setTimeout(connect, RECONNECT_DELAY_MS);
  };
  ws.onerror = () => ws.close();
}

for (const b of document.querySelectorAll("button[data-command]")) {
  b.onclick = () => {
    const body = { command: b.dataset.command };
    if ("open" in b.dataset) body.open = b.dataset.open === "true";
    send(body);
  };
}
$("sleep").onclick = () => send({ command: "touch" });

// TURN: hold to run the motor. Pointer down presses, anything that ends the hold releases.
for (const turn of [$("turn"), $("turn_settings")]) {
  const releaseTurn = () => send({ command: "motor_release" });
  turn.onpointerdown = () => send({ command: "motor_press" });
  turn.onpointerup = releaseTurn;
  turn.onpointercancel = releaseTurn;
  turn.onpointerleave = releaseTurn;
  turn.oncontextmenu = (e) => e.preventDefault();
}

// Settings block (Part A).
for (const b of document.querySelectorAll("button[data-settings]")) {
  b.onclick = () => {
    const d = b.dataset;
    const body = {};
    if (d.settings === "enter") body.pin = $("pin").value;
    if (d.settings === "door") Object.assign(body, { level: Number($("door_level").value), unlock: d.unlock === "true" });
    if (d.settings === "spindle") body.open = d.open === "true";
    if (d.settings === "leds") {
      body.action = d.action;
      if (d.action === "fill") body.rgb = [255, 255, 255];
      if (d.action === "level") body.level = Number($("door_level").value);
    }
    if (d.settings === "audio") Object.assign(body, { action: d.action }, d.sound ? { sound: d.sound } : {});
    settings(d.settings, body);
  };
}
$("ooo").onchange = () => settings("out_of_order", { on: $("ooo").checked });
$("brightness").onchange = () => settings("brightness", { value: Number($("brightness").value) / 100 });
$("volume").onchange = () => settings("volume", { value: Number($("volume").value) / 100 });
// every touch on a settings screen keeps the visit alive (the kiosk page will do the same)
document.querySelector('section[data-state="settings"]').onpointerdown = () => send({ command: "touch" });

connect();
