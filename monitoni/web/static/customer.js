// Customer screens C1–C9, rendered from the status object; copy as on the design page.
// `data-state` on <body> (app.js) picks the screen; this file fills in what changes: the top
// bar title, the shelf number, the countdown, the QR code and the machine illustration.
"use strict";

const cs = { built: false, qrUrl: null };

function renderCustomer(s) {
  if (!cs.built) buildLevels(s.levels);
  el("brand").textContent = s.name;  // the wordmark and the small line: two config values
  el("brand_sub").textContent = s.location;
  for (const node of document.querySelectorAll("#customer .level")) {
    node.textContent = s.selected_level ?? "";
  }
  for (const node of document.querySelectorAll("#customer .cd")) {
    node.textContent = s.countdown_s === null ? "" : countdown(s.countdown_s);
  }
  // C3: the code only while the server is reachable (a payment it never sees expires unpaid)
  const wantQr = s.state === "checking_purchase" && s.purchase_server.reachable === true;
  if (wantQr && s.qr_url !== cs.qrUrl) {
    cs.qrUrl = s.qr_url;
    inlineQr(el("qr"), s.qr_url).then((ok) => { if (!ok) cs.qrUrl = null; });  // next push retries
  }
  const look = { door_unlocked: "highlight", door_opened: "open", door_alarm: "open" }[s.state];
  setMachine(look ?? "plain", look ? s.selected_level : null);
}

// while the WebSocket is down: C9's look, "Out of order" alone (customer.css hides the rest)
function renderOffline() {
  setMachine("plain", null);
}

const countdown = (seconds) => {
  const whole = Math.ceil(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
};

// the illustration: one inline SVG, the state class and the level on its root, `on` marks the
// door group the status names; customer.css draws the states
function setMachine(state, level) {
  const svg = el("machine");
  svg.setAttribute("class", `machine ${state}`);
  svg.dataset.level = level ?? "";
  for (const door of svg.querySelectorAll(".door")) {
    door.classList.toggle("on", Number(door.dataset.level) === level);
  }
}

// -- C1: ten tiles, shelf 1 at the top as in the machine ----------------------------------

function buildLevels(n) {
  cs.built = true;
  const levels = el("levels");
  for (let i = 1; i <= n; i++) {
    const caption = i === 1 ? "top" : i === n ? "bottom" : "";
    levels.append(h("button", { class: "tile lv", onclick: () => send({ command: "select_level", level: i }) },
                    String(i), h("span", { class: "cap" }, caption)));
  }
}

wireTurn(el("turn"));
el("cancel").onclick = () => send({ command: "cancel" });
for (const g of document.querySelectorAll("[data-gear]")) g.onclick = () => setScreen("S0");
