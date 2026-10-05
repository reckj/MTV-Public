# MoniToni

Control daemon for a QR-code vending machine. Raspberry Pi 5, Raspberry Pi OS
64-bit, Waveshare 7.9" HDMI touch display 400×1280 portrait. Runs unattended
for years: pinned versions, frozen OS image, no auto-updates.

## Stack

- Python 3.11, asyncio. One headless process owns all hardware, the state
  machine, config and the SQLite database.
- aiohttp serves the static web UI plus a localhost API and WebSocket.
- pyyaml + pydantic v2 for config (`config/default.yaml`, overlaid by the
  gitignored `config/local.yaml`). aiosqlite for the event log.
- Front-end: plain HTML/JS/CSS, no framework, no build step. Chromium in
  Wayland kiosk mode is the renderer only.
- `python -m venv .venv` + `requirements.txt` with exact `==` pins including
  transitive packages. No Docker, uv, poetry or pre-commit.

## Commands

- `make dev` — create `.venv` if missing, install pinned requirements, run
  `python -m monitoni --mock`. UI at http://127.0.0.1:8080/.
- `make test` — pytest.
- `make lint` — ruff.

## Folder layout

- `monitoni/` — the daemon. `__main__.py` entry point, `config.py`,
  `daemon.py` (owns everything, forwards hardware events), `flow.py` (state
  machine: transition table, timeouts, entry hooks), `purchase.py` (purchase
  server protocol + mock), `eventlog.py` (SQLite event log), `hardware/` (one
  implementation per mode), `web/` (aiohttp routes and `static/` UI files).
- `config/` — `default.yaml` (checked in) and `local.yaml` (per machine).
- `tests/` — pytest.
- `docs/SETUP.md` — installation guide, grows with every integration step.

## Conventions

- Hardware, timing and state logic live in the daemon, never in the browser.
  The page renders what the daemon sends.
- One way to do each thing. Prefer deleting over abstracting. No retry
  wrappers, no plugin systems, no abstract base classes beyond the one
  hardware protocol.
- Config validation errors name the offending key.
- `default.yaml` is the production configuration; development always runs with
  `--mock`.
- Door lock rule: every entry into `idle` or `out_of_order` locks all doors in
  the one entry hook; `unlock_door` is called from exactly one place
  (`door_unlocked` entry). Nothing locks "on the way".
- Open, decided in the relay milestone: hardware errors inside entry hooks.
  Today the exception propagates out of `dispatch` and the state is unchanged.
- Commands come in over `POST /api/command`; status goes out over the
  WebSocket (on every state change plus a 1 s heartbeat). The socket is
  one-way.
- stdlib `logging` to stdout only; journald captures it on the machine.
- English only, code and docs. Code stays Python 3.11 compatible.

## Kept features

- Customer screen: select level → QR code → purchase verified against
  purchase server → door unlocked → door monitored → idle.
- Sleep mode, out-of-order / maintenance mode, door alarm, local SQLite
  event log.
- PIN-protected settings/debug area with per-component test tools: relays,
  motor, LEDs, sensors, audio, network, stats/logs.
- QR code management.
- Mock hardware mode for development on a laptop.
- Hardware: two Waveshare Modbus-TCP relay modules over Ethernet/PoE
  (30-ch `relay_levels` for per-level door locks; 8-ch Module C `relay_core`
  for motor, spindle lock, digital inputs incl. door sensor; "transparent
  mode" = raw Modbus RTU frames with CRC over TCP, no MBAP). Gledopto ESP32
  WLED via ArtNet for LED feedback. Audio via HDMI (pygame).

## Dropped features

- Kivy / KivyMD.
- Remote telemetry server and web dashboard.
- GPIO and RS485 fallback paths.
- Setup wizard (replaced by `docs/SETUP.md`).
- GSD planning framework (or any other planning framework).

## Hardware safety rules

- Hardware commands are safety-relevant: no automatic retries on relay
  commands, motor must always stop on timeout, door lock state is explicit.
