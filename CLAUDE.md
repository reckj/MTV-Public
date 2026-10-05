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
- Modbus RTU frames over plain TCP built by hand, no pymodbus (Waveshare
  transparent mode has no MBAP header).
- Front-end: plain HTML/JS/CSS, no framework, no build step. Chromium in
  Wayland kiosk mode is the renderer only.
- `python -m venv .venv` + `requirements.txt` with exact `==` pins including
  transitive packages. No Docker, uv, poetry or pre-commit.

## Commands

- `make dev` — create `.venv` if missing, install pinned requirements, run
  `python -m monitoni --mock`. UI at http://127.0.0.1:8080/.
- `make test` — pytest.
- `make lint` — ruff.
- `.venv/bin/python -m tests.fake_waveshare --port 15020 --coils 8` — a fake
  module for running real mode on a laptop (start one per module).

## Folder layout

- `monitoni/` — the daemon. `__main__.py` entry point, `config.py`,
  `daemon.py` (owns everything, forwards hardware events), `flow.py` (state
  machine: transition table, timeouts, entry hooks), `motor.py` (hold-to-turn
  sequence with watchdog), `purchase.py` (purchase server protocol + mock),
  `eventlog.py` (SQLite event log), `hardware/` (`base.py` protocol,
  `modbus.py` one class per module, `real.py`, `mock.py`), `web/` (aiohttp
  routes and `static/` UI files).
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
- Read-back rule: every coil write is followed by a read of that coil; a
  mismatch is a `HardwareError`. Lock state is what the module reports, never
  what was sent. Relay ON = unlocked; relay OFF or power loss = locked.
- Error policy: a `HardwareError` in an entry hook, a lost module connection
  or a failed door poll puts the flow into `out_of_order (hardware)`; it
  returns to `idle` by itself once both modules are connected and the door
  sensor reads. `maintenance` never clears itself. No command is retried; the
  motor's emergency OFF after a failed sequence is the one second write.
- Motor stop rule: the motor stops on release, after `max_run_s`, on leaving
  `idle`, when the last WebSocket closes and on daemon stop.
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
