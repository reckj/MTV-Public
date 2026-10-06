// The kiosk page: one WebSocket in, commands out over POST. Shared by the customer screens
// (customer.js) and the settings area (settings.js). No timers and no state logic here: the
// status object is the only input, and the daemon's answer to a command is the only truth.
"use strict";

const BACKOFF_MS = [1000, 2000, 5000, 10000];  // reconnect waits; the last one repeats
const TOUCH_THROTTLE_MS = 1000;

const el = (id) => document.getElementById(id);
const h = (tag, attrs = {}, ...children) => {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node[k] = v;
    else node.setAttribute(k, v);
  }
  node.append(...children);
  return node;
};

// -- talking to the daemon ------------------------------------------------------------

async function api(path, body) {
  const resp = await fetch(path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  return { ok: resp.ok, status: resp.status, data };
}

// customer and dev commands: a refusal is the daemon's decision, the next status shows the result
const send = (body) => api("/api/command", body);

// settings tools: a refusal is shown in the toast
async function post(name, body = {}) {
  const r = await api(`/api/settings/${name}`, body);
  if (!r.ok) toast(`${r.status} · ${r.data.error ?? "refused"}`);
  return r;
}

let toastTimer = null;
function toast(text) {
  el("toast").textContent = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el("toast").textContent = ""; }, 4000);
}

// a QR code as an inline SVG path; the page colours it and the plate gives the quiet zone
async function inlineQr(node, url) {
  node.replaceChildren();
  const resp = await fetch(url);
  if (resp.ok) node.innerHTML = await resp.text();
  return resp.ok;
}

// TURN, on C1 and S3: pointer down presses, anything that ends the hold releases
function wireTurn(button) {
  const release = () => send({ command: "motor_release" });
  button.onpointerdown = () => send({ command: "motor_press" });
  button.onpointerup = button.onpointercancel = button.onpointerleave = release;
  button.oncontextmenu = (e) => e.preventDefault();
}

// -- the status in, the screens out --------------------------------------------------------

function render(status) {
  const body = document.body;
  body.dataset.state = status.state;
  body.dataset.reason = status.reason ?? "";
  body.dataset.reachable = String(status.purchase_server.reachable);
  body.dataset.motor = status.motor.running ? "running" : status.motor.pressed ? "pressed" : "";
  renderCustomer(status);  // customer.js: C1–C9
  renderSettings(status);  // settings.js: S0–S8
}

let attempts = 0;
function connect() {
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${scheme}://${location.host}/ws`);
  ws.onopen = () => { attempts = 0; document.body.classList.remove("disconnected"); };
  ws.onmessage = (event) => render(JSON.parse(event.data));
  ws.onclose = () => {
    document.body.classList.add("disconnected");
    renderOffline();  // customer.js: the "Out of order" look until the next status
    setTimeout(connect, BACKOFF_MS[Math.min(attempts++, BACKOFF_MS.length - 1)]);
  };
  ws.onerror = () => ws.close();
}

// every touch is activity: the daemon restarts its sleep or settings timer (at most once a
// second; a TURN press is a touch of its own)
let lastTouch = 0;
document.addEventListener("pointerdown", (event) => {
  if (document.body.classList.contains("disconnected") || event.target.closest(".turn")) return;
  const now = Date.now();
  if (now - lastTouch < TOUCH_THROTTLE_MS) return;
  lastTouch = now;
  send({ command: "touch" });
});

// after customer.js and settings.js have run: the first status may arrive while a script is
// still loading (the parser yields while it fetches), and render() needs both
document.addEventListener("DOMContentLoaded", connect);
