// Renders whatever the daemon sends. No state or timing logic lives here.
"use strict";

const FIELDS = ["machine_id", "hardware_mode", "state", "uptime_s"];
const RECONNECT_DELAY_MS = 2000;

const connection = document.getElementById("connection");

function setConnected(connected) {
  connection.textContent = connected ? "connected" : "disconnected";
  connection.className = connected ? "connected" : "disconnected";
}

function render(status) {
  for (const field of FIELDS) {
    const el = document.getElementById(field);
    if (el && field in status) {
      el.textContent = String(status[field]);
    }
  }
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

connect();
