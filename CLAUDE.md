# MoniToni

Control daemon for a QR-code vending machine. Raspberry Pi 5, Raspberry Pi OS
64-bit, Waveshare 7.9" HDMI touch display 400×1280 portrait. Runs unattended
for years: pinned versions, frozen OS image, no auto-updates.

## Stack

- Python 3.11, asyncio. One headless process owns all hardware, the state
  machine, config and the SQLite database.
- aiohttp: static web UI, localhost API, WebSocket. httpx: purchase server client.
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
  `python -m monitoni --mock --mock-purchase` (mock hardware, simulated
  payments). UI at http://127.0.0.1:8080/. `make test` — pytest. `make lint` — ruff.
- `python -m tests.fake_waveshare --port N --coils 8` (one per module,
  `--inputs-file` for the DI) and `python -m tests.fake_purchase_server
  --port N` (`GET /pay?level=N`) — fakes for running real mode on a laptop.

## Folder layout

- `monitoni/` — the daemon. `__main__.py` entry point, `config.py`,
  `daemon.py` (owns everything, forwards hardware events), `flow.py` (state
  machine: transition table, timeouts, entry hooks), `motor.py` (hold-to-turn
  sequence with watchdog), `purchase.py` (purchase server protocol, HTTP
  client, mock), `outbox.py` (durable completions), `eventlog.py` (SQLite
  event log), `hardware/` (`base.py` protocol, `modbus.py` one class per
  module, `real.py`, `mock.py`), `web/` (aiohttp routes, `static/` UI files).
- `config/` — `default.yaml` (checked in) and `local.yaml` (per machine).
- `tests/` — pytest. `docs/SETUP.md` — installation guide, grows with every
  integration step.

## Conventions

- Hardware, timing and state logic live in the daemon, never in the browser.
  The page renders what the daemon sends.
- One way to do each thing. Prefer deleting over abstracting. No retry
  wrappers, no plugin systems, no abstract base classes beyond the one
  hardware protocol.
- `default.yaml` is the production configuration; development always runs with
  `--mock`. Config validation errors name the offending key.
- Door lock rule: only the `idle`/`out_of_order` entry hook locks (all doors
  at once); `unlock_door` is called from one place, the `door_unlocked` hook.
- Read-back rule: every coil write is followed by a read of that coil; a
  mismatch is a `HardwareError`. Lock state is what the module reports, never
  what was sent. Relay ON = unlocked; relay OFF or power loss = locked.
- Error policy: a `HardwareError` in an entry hook, a lost module connection
  or a failed door poll puts the flow into `out_of_order (hardware)`; it
  returns to `idle` once the hardware has been healthy for `recovery_dwell_s`
  = 10 s without a break. `maintenance` never clears itself. No command is
  retried; the motor's emergency OFF after a failed sequence is the one second
  write. Hardware commands are safety-relevant: lock state is explicit.
- Hardware stop switches nothing: relays keep their state until the next
  start locks all doors; power loss locks by wiring.
- Motor stop rule: the motor stops on release, after `max_run_s`, on leaving
  `idle`, when the last WebSocket closes and on daemon stop.
- Outbox rule: purchase completions are durable (SQLite `outbox` table),
  retried with backoff until the server answers 200; nothing else in the
  daemon retries. An unreachable purchase server never puts the machine out of
  order; a paid purchase not handed out is reported with `success: false`.
- Commands come in over `POST /api/command`; status goes out over the
  WebSocket (on every state change plus a 1 s heartbeat). The socket is
  one-way.
- stdlib `logging` to stdout only (journald captures it). English only, code
  and docs. Code stays Python 3.11 compatible.

## Kept features

- Customer screen: select level → QR code → purchase verified against
  purchase server → door unlocked → door monitored → idle.
- Sleep mode, out-of-order / maintenance mode, door alarm (incl. a door
  opened without a purchase), local SQLite event log.
- PIN-protected settings/debug area with per-component test tools: relays,
  motor, LEDs, sensors, audio, network, stats/logs.
- QR code management.
- Mock hardware mode for development on a laptop.
- Hardware: two Waveshare Modbus-TCP relay modules over Ethernet/PoE (30-ch
  `relay_levels` for door locks; 8-ch Module C `relay_core` for motor, spindle
  lock and the door sensor input). Gledopto ESP32 WLED via ArtNet for LED
  feedback. Audio via HDMI (pygame).

## Dropped features

- Kivy / KivyMD.
- Remote telemetry server and web dashboard.
- GPIO and RS485 fallback paths.
- Setup wizard (replaced by `docs/SETUP.md`).
- GSD planning framework (or any other planning framework).
