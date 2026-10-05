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

function render(status) {
  document.body.dataset.state = status.state;
  $("state_name").textContent = status.state;
  $("countdown").textContent = status.countdown_s === null ? "" : `${status.countdown_s.toFixed(0)} s`;
  for (const el of document.querySelectorAll(".selected_level")) {
    el.textContent = status.selected_level ?? "";
  }
  $("maintenance_message").textContent = status.maintenance_message;
  $("purchase_id").textContent = status.purchase_id ?? "–";

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
  dev.hidden = status.hardware_mode !== "mock";
  if (!dev.hidden) {
    $("doors").textContent = Object.entries(status.doors)
      .map(([n, s]) => `${n}:${s === "locked" ? "🔒" : "🔓"}`).join(" ");
    loadEvents();
  }
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

connect();
