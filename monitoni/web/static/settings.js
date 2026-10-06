// Settings area S0–S8. Renders the status object the daemon pushes; asks the daemon for
// everything and shows its answer. `data-screen` on <body> (S0…S8 or empty) is the only
// client-side navigation state; `send()` (dev commands) comes from app.js.
"use strict";

const SCREENS = ["S0", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8"];
const TITLES = { S2: "Doors", S3: "Motor", S4: "LEDs", S5: "Audio", S6: "Network", S7: "QR codes", S8: "Events" };
const AMBER = [233, 162, 59];
const TOUCH_THROTTLE_MS = 1000;
const EVENTS_PAGE = 50;

const st = {                 // client-side state: navigation and what the daemon answered last
  screen: "",
  status: null,
  pin: "",
  lastFlowState: null,
  qrLevel: 1,
  eventsFilter: "all",
  eventRows: [],
  lastTouch: 0,
  built: false,
};

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
const pad2 = (n) => String(n).padStart(2, "0");
const clock = (iso) => { const d = new Date(iso); return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`; };
const pct = (x) => `${Math.round(x * 100)} %`;

// -- talking to the daemon ------------------------------------------------------------

async function post(name, body = {}) {
  const resp = await fetch(`/api/settings/${name}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) toast(`${resp.status} · ${data.error ?? "refused"}`);
  return { ok: resp.ok, status: resp.status, data };
}

let toastTimer = null;
function toast(text) {
  el("toast").textContent = text;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el("toast").textContent = ""; }, 4000);
}

// -- navigation -------------------------------------------------------------------------

function setScreen(id) {
  if (st.screen === id) return;
  st.screen = id;
  document.body.dataset.screen = id;
  for (const section of document.querySelectorAll("#settings section[data-screen]")) {
    section.classList.toggle("current", section.dataset.screen === id);
  }
  el("confirm").hidden = true;
  if (id === "S0") { st.pin = ""; renderPin(); }
  if (id === "S7") loadQr();
  if (id === "S8") { loadEventSummary(); loadEventList(true); }
  if (id && st.status) renderScreens(st.status);
  window.scrollTo(0, 0);
}

// called by app.js on every status push
function renderSettings(status) {
  st.status = status;
  const inSettings = status.state === "settings";
  if (inSettings && (!st.screen || st.screen === "S0")) setScreen("S1");
  else if (!inSettings && st.screen && st.screen !== "S0") setScreen("");  // the flow left
  else if (st.screen === "S0" && !["idle", "out_of_order"].includes(status.state)) setScreen("");
  if (!st.screen) return;
  if (!st.built) build(status);
  renderScreens(status);
  if (st.screen === "S8" && status.state !== st.lastFlowState) { loadEventSummary(); loadEventList(true); }
  st.lastFlowState = status.state;
}

// -- one-time structure that depends on the level count ----------------------------------

function build(status) {
  st.built = true;
  const rows = el("door_rows");
  for (let n = 1; n <= status.levels; n++) {
    rows.append(h("div", { class: "r", "data-level": n },
      h("span", { class: "lab" }, String(n), h("span", { class: "v" })),
      h("button", { class: "sb a", onclick: () => doorButton(n) }, "Unlock")));
  }
  for (const [id, onclick] of [["shelves", (n) => post("leds", { action: "level", level: n })],
                               ["qr_picker", (n) => { st.qrLevel = n; loadQr(); renderScreens(st.status); }]]) {
    const grid = el(id);
    for (let n = 1; n <= status.levels; n++) grid.append(h("button", { class: "tile", onclick: () => onclick(n) }, String(n)));
  }
  const zones = el("zones");
  (status.config_view.led.zones || []).forEach(([a, b], i) => {
    zones.append(h("div", { class: "r" }, h("span", {}, `Shelf ${i + 1}`), h("span", { class: "v" }, `px ${a} – ${b}`)));
  });
}

// -- per-screen rendering from the status object -------------------------------------------

// label, the flag, the problem text, when it flipped (local ISO time or null)
const MARK_RULES = [
  ["Doors", (s) => s.hardware.mode === "mock" ? true : s.hardware.relay_levels.connected, "DOOR RELAYS DISCONNECTED", (s) => s.hardware.relay_levels?.since],
  ["Core", (s) => s.hardware.mode === "mock" ? true : s.hardware.relay_core.connected, "CORE RELAYS DISCONNECTED", (s) => s.hardware.relay_core?.since],
  ["LEDs", (s) => s.leds.reachable, "LEDS UNREACHABLE", (s) => s.leds.since],
  ["Server", (s) => s.purchase_server.reachable, "SERVER UNREACHABLE", (s) => s.purchase_server.since],
  ["Sensor", (s) => s.hardware.mode === "mock" ? true : s.hardware.door_poll_ok, "DOOR SENSOR NOT READING", (s) => s.hardware.door_poll_since],
  ["Audio", (s) => s.audio.available, "AUDIO UNAVAILABLE", (s) => s.audio.since],
];
const markClass = (ok) => ok === true ? "m" : ok === false ? "m b" : "m w";

function renderScreens(s) {
  const hw = s.hardware;
  // S1 Home
  const strip = el("strip");
  strip.replaceChildren(...MARK_RULES.map(([label, test]) => h("div", {}, h("span", { class: markClass(test(s)) }), label)));
  const bad = MARK_RULES.find(([, test]) => test(s) === false);
  const problem = el("problem");
  problem.className = "problem";
  if (hw.door_open) problem.textContent = "DOOR OPEN · CLOSE IT TO EXIT";
  else if (bad) { const since = bad[3](s); problem.textContent = bad[2] + (since ? ` · SINCE ${clock(since).slice(0, 5)}` : ""); }
  else if (s.reason) problem.textContent = `OUT OF ORDER · ${s.reason.toUpperCase()}`;
  else if (s.settings.pin_is_default) { problem.textContent = "CHANGE THE DEFAULT PIN"; problem.className = "problem warn"; }
  else problem.textContent = "";
  el("ooo_switch").classList.toggle("on", s.settings.out_of_order);
  el("sim").hidden = s.hardware_mode !== "mock";  // Open / Close: the door sensor, mock only
  el("foot_left").textContent = `${s.machine_id} · v${s.app_version}`;
  const up = Math.floor(s.uptime_s);
  el("foot_right").textContent = `up ${Math.floor(up / 86400)} d ${Math.floor((up % 86400) / 3600)} h`;
  el("exit_btn").disabled = !!hw.door_open;
  el("exit_btn").textContent = hw.door_open ? "‹ Close the door first" : "‹ Exit";

  // S2 Doors
  const sensor = el("sensor");
  sensor.textContent = hw.door_open === true ? "OPEN" : hw.door_open === false ? "CLOSED" : "?";
  sensor.className = "big" + (hw.door_open === true ? " bad" : hw.door_open === false ? "" : " unknown");
  const channels = s.config_view.door_locks.channels;
  for (const row of el("door_rows").children) {
    const n = Number(row.dataset.level);
    const state = s.doors[String(n)];
    row.querySelector(".v").textContent = `ch ${channels[n - 1]}`;
    const b = row.querySelector("button");
    b.textContent = state === "locked" ? "Unlock" : state === "unlocked" ? "Lock" : "?";
    b.className = state === "unlocked" ? "sb x" : "sb a";
    b.disabled = state === "unknown";
  }

  // S3 Motor: the motor module's own state, the one owner of the spindle lock
  const m = s.motor;
  setDot("motor_live", m.running === true ? "ON" : m.running === false ? "OFF" : "?", m.running === true);
  setDot("spindle_live", m.spindle_open === true ? "OPEN" : m.spindle_open === false ? "CLOSED" : "?", m.spindle_open === true);
  el("spindle_btn").textContent = m.spindle_open ? "Close" : "Open";
  el("spindle_btn").className = m.spindle_open ? "sb x" : "sb a";
  const t = s.config_view.motor;
  el("timings").replaceChildren(
    ...[["Pre-delay", `${t.spindle_pre_delay_ms} ms`], ["Spin after release", `${t.spin_after_release_ms} ms`],
        ["Post-delay", `${t.spindle_post_delay_ms} ms`], ["Hard stop", `${t.max_run_s} s`]]
      .map(([k, v]) => h("div", { class: "r" }, h("span", {}, k), h("span", { class: "v" }, v))));

  // S4 LEDs
  el("wled_mark").className = markClass(s.leds.reachable);
  el("wled_ip").textContent = s.config_view.wled.ip_address;
  el("brightness_v").textContent = pct(s.leds.brightness);
  setSlider("brightness", s.leds.brightness);
  for (const [i, tile] of [...el("shelves").children].entries()) {
    tile.classList.toggle("on", s.leds.pattern === "light_level" && s.leds.level === i + 1);
  }

  // S5 Audio
  el("volume_v").textContent = pct(s.audio.volume);
  setSlider("volume", s.audio.volume);
  for (const mark of document.querySelectorAll("#sound_rows .m")) mark.className = markClass(s.audio.available);

  // S6 Network
  el("net_ip").textContent = s.ip ?? "—";
  el("net_host").textContent = s.hostname;
  const dev = (id, module, ok) => { el(id + "_host").textContent = hw.mode === "mock" ? "mock" : (hw[module]?.host ?? "—"); el(id + "_mark").className = markClass(ok); };
  dev("dev_levels", "relay_levels", hw.mode === "mock" ? true : hw.relay_levels?.connected);
  dev("dev_core", "relay_core", hw.mode === "mock" ? true : hw.relay_core?.connected);
  el("dev_wled_host").textContent = s.config_view.wled.ip_address;
  el("dev_wled_mark").className = markClass(s.leds.reachable);
  const ps = s.purchase_server;
  el("srv_url").textContent = ps.base_url;
  el("srv_mark").className = markClass(ps.reachable);
  setDot("srv_state", ps.reachable === true ? "OK" : ps.reachable === false ? "DOWN" : "?", ps.reachable === true, ps.reachable === false);
  el("srv_last_ok").textContent = ps.last_ok ? clock(ps.last_ok) : "—";
  el("srv_last_error").textContent = ps.last_error ?? "—";
  el("srv_queued").textContent = String(ps.outbox_pending);

  // S7 QR
  for (const [i, tile] of [...el("qr_picker").children].entries()) tile.classList.toggle("on", st.qrLevel === i + 1);
  el("qr_caption").textContent = `Shelf ${st.qrLevel} · as shown to customers`;
}

function setDot(id, text, ok, bad = false) {
  const node = el(id);
  node.textContent = text;
  node.className = "v dot" + (ok ? " ok" : bad ? " bad" : "");
}

function setSlider(id, value) {
  const input = el(id);
  if (document.activeElement === input) return;  // not while a finger is on it
  input.value = Math.round(value * 100);
  input.style.setProperty("--p", `${Math.round(value * 100)}%`);
}

// -- S0: the PIN ------------------------------------------------------------------------

function renderPin() {
  const dots = el("pin_dots");
  const shown = Math.max(4, st.pin.length);
  dots.replaceChildren(...Array.from({ length: shown }, (_, i) => i < st.pin.length ? h("b", {}, "●") : h("span", {}, "○")));
}

async function enterPin() {
  const { ok, status } = await post("enter", { pin: st.pin });
  if (ok) return;  // the status push switches to S1
  if (status === 403) {
    const dots = el("pin_dots");
    dots.classList.remove("shake");
    void dots.offsetWidth;  // restart the animation
    dots.classList.add("shake");
  }
  st.pin = "";
  renderPin();
}

// -- S2 / S6 / S7 / S8 helpers --------------------------------------------------------------

function doorButton(n) {
  const state = st.status?.doors[String(n)];
  if (state === "locked") post("door", { level: n, unlock: true });
  else if (state === "unlocked") post("door", { level: n, unlock: false });
}

async function testServer() {
  el("server_result").textContent = "…";
  const { ok, data } = await post("test_server");
  const r = data.test_server;
  el("server_result").textContent = !ok ? "" : r.ok ? `OK · ${r.took_ms} ms` : `${r.error} · ${r.took_ms} ms`;
}

async function loadQr() {
  el("qr_img").src = `/api/qr/${st.qrLevel}.png`;
  const info = await fetch(`/api/qr/${st.qrLevel}.json`).then((r) => r.json());
  el("qr_data").textContent = info.data;
}

async function loadEventSummary() {
  const s = await fetch("/api/events/summary").then((r) => r.json());
  el("stat_vends_today").textContent = s.vends_today;
  el("stat_vends_total").textContent = s.vends_total.toLocaleString("de-CH");
  el("stat_alarms").textContent = s.alarms_today;
  el("stat_faults").textContent = s.faults_today;
}

async function loadEventList(reset) {
  const before = reset || !st.eventRows.length ? "" : `&before=${st.eventRows[st.eventRows.length - 1].id}`;
  const rows = await fetch(`/api/events?limit=${EVENTS_PAGE}&filter=${st.eventsFilter}${before}`).then((r) => r.json());
  st.eventRows = reset ? rows : st.eventRows.concat(rows);
  el("event_rows").replaceChildren(...st.eventRows.map((r) =>
    h("div", { class: "ev" }, h("span", {}, phrase(r)), h("small", {}, clock(r.ts)))));
  el("load_more").disabled = rows.length < EVENTS_PAGE;
  for (const chip of el("chips").children) chip.classList.toggle("on", chip.dataset.filter === st.eventsFilter);
}

// the fixed phrase table: rows are data, the words are the page's
const shelf = (r) => r.level ? ` · shelf ${r.level}` : "";
const TO_PHRASES = {
  checking_purchase: (r) => `Shelf selected · ${r.level ?? "?"}`,
  door_unlocked: (r) => `Purchase permitted${shelf(r)}`,
  door_opened: (r) => `Door opened${shelf(r)}`,
  door_alarm: (r) => `Door alarm${shelf(r)}`,
  door_forced: () => "Door forced",
  completing: (r) => `Vend complete${shelf(r)}`,
  sleep: () => "Sleep",
  settings: () => "Settings opened",
  out_of_order: (r) => r.details.event === "hardware_fault" ? "Hardware fault" : r.details.event === "database_fault" ? "Report lost · out of order" : "Out of order",
  idle: (r) => r.details.from === "sleep" ? "Woke from sleep" : r.details.from === "settings" ? "Settings closed" : r.details.from === "out_of_order" ? "Back in service" : r.details.from === "door_forced" ? "Door closed" : r.details.event === "timeout" ? "Timed out · back to idle" : r.details.event === "cancel" ? "Cancelled" : "Back to idle",
};
function phrase(r) {
  const d = r.details || {};
  switch (r.kind) {
    case "transition": return (TO_PHRASES[d.to] || (() => `${d.from} → ${d.to}`))(r);
    case "network":
      if (d.component === "wled") return d.reachable ? "WLED reachable" : "WLED unreachable";
      return d.purchase_server === "reachable" ? "Server reachable" : "Server unreachable";
    case "hardware": return { door_opened: "Door sensor open", door_closed: "Door sensor closed", fault: "Hardware fault", relocked: `Shelf relocked${shelf(r)}`, outbox_failed: "Report lost", known_state: "Motor and spindle reset" }[d.event] || `Hardware · ${d.event}`;
    case "outbox": return `${d.delivered ? "Report sent" : "Report queued"} · ${d.kind}${shelf(r)}`;
    case "purchase_check": return `Purchase permitted${shelf(r)}`;
    case "timeout": return `Timeout · ${r.state.replace("_", " ")}`;
    case "rejected": return `Refused · ${d.event}`;
    case "command": return d.tool ? `Settings · ${d.tool}${d.accepted === false ? " refused" : ""}` : `Touch`;
    case "motor": return d.event === "start" ? "Motor started" : d.event === "spindle" ? `Spindle lock ${d.open ? "opened" : "closed"}` : `Motor stopped · ${d.reason.replace("_", " ")}`;
    case "daemon": return d.event === "start" ? "Daemon started" : "Daemon stopped";
    case "dev": return `Simulation · ${d.command}`;
    default: return r.kind;
  }
}

// -- wiring ----------------------------------------------------------------------------------

function wireSettings() {
  for (const b of document.querySelectorAll("#settings [data-nav]")) {
    b.onclick = () => setScreen(b.dataset.nav === "cancel" ? "" : b.dataset.nav);
  }
  for (const key of el("keypad").children) {
    key.onclick = () => {
      const k = key.dataset.key;
      if (k === "C") st.pin = "";
      else if (k === "back") st.pin = st.pin.slice(0, -1);
      else if (st.pin.length < 8) st.pin += k;
      renderPin();
    };
  }
  el("pin_enter").onclick = enterPin;
  el("exit_btn").onclick = () => post("exit");
  el("ooo_switch").onclick = () => {
    const on = !st.status.settings.out_of_order;
    el("confirm_text").textContent = on
      ? "Switch Out of order on? Customers cannot buy until it is switched off again here."
      : "Switch Out of order off? The machine sells again as soon as you leave the settings.";
    el("confirm_yes").textContent = on ? "Switch on" : "Switch off";
    el("confirm_yes").onclick = () => { el("confirm").hidden = true; post("out_of_order", { on }); };
    el("confirm").hidden = false;
  };
  el("confirm_no").onclick = () => { el("confirm").hidden = true; };
  el("sim_open").onclick = () => send({ command: "simulate_door", open: true });
  el("sim_close").onclick = () => send({ command: "simulate_door", open: false });
  el("lock_all").onclick = () => post("lock_all");
  el("spindle_btn").onclick = () => post("spindle", { open: !st.status.motor.spindle_open });
  const turn = el("turn_s3");
  const release = () => send({ command: "motor_release" });
  turn.onpointerdown = () => send({ command: "motor_press" });
  turn.onpointerup = turn.onpointercancel = turn.onpointerleave = release;
  turn.oncontextmenu = (e) => e.preventDefault();
  el("led_off").onclick = () => post("leds", { action: "off" });
  el("led_white").onclick = () => post("leds", { action: "fill", rgb: [255, 255, 255] });
  el("led_amber").onclick = () => post("leds", { action: "fill", rgb: AMBER });
  for (const [id, name] of [["brightness", "brightness"], ["volume", "volume"]]) {
    const input = el(id);
    input.oninput = () => { input.style.setProperty("--p", `${input.value}%`); el(id + "_v").textContent = `${input.value} %`; };
    input.onchange = () => post(name, { value: Number(input.value) / 100 });
  }
  for (const b of document.querySelectorAll("#sound_rows button")) b.onclick = () => post("audio", { action: "play", sound: b.dataset.sound });
  el("audio_stop").onclick = () => post("audio", { action: "stop" });
  el("test_server").onclick = testServer;
  for (const chip of el("chips").children) chip.onclick = () => { st.eventsFilter = chip.dataset.filter; loadEventList(true); };
  el("load_more").onclick = () => loadEventList(false);
  // every touch keeps the visit alive; the daemon restarts its timer (at most one a second)
  el("settings").addEventListener("pointerdown", () => {
    const now = Date.now();
    if (st.status?.state === "settings" && now - st.lastTouch > TOUCH_THROTTLE_MS) {
      st.lastTouch = now;
      send({ command: "touch" });
    }
  });
  renderPin();
}

wireSettings();
