# MoniToni

Control daemon for a QR-code vending machine. Raspberry Pi 5, Raspberry Pi OS
64-bit, Waveshare 7.9" HDMI touch display 400×1280 portrait. Runs unattended
for years: pinned versions, frozen OS image, no auto-updates.

## Stack

- Python 3.11, asyncio. One headless process owns all hardware, the state
  machine, config and the SQLite database. Modbus RTU frames over plain TCP are
  built by hand (no pymodbus; Waveshare transparent mode has no MBAP header).
- aiohttp: static web UI, localhost API, WebSocket. httpx: purchase server client
  and the WLED health poll. ArtDMX packets to the LED strip are built by hand
  and sent from one UDP socket (no stupidArtnet). pygame.mixer: the three sounds.
- pyyaml + pydantic v2 for config (`config/default.yaml`, overlaid by the
  gitignored `config/local.yaml`). aiosqlite for the event log.
- Front-end: plain HTML/JS/CSS, no framework, no build step. Chromium in
  Wayland kiosk mode is the renderer only.
- `python -m venv .venv` + `requirements.txt` with exact `==` pins including
  transitive packages. No Docker, uv, poetry or pre-commit.

## Commands

- `make dev` — create `.venv` if missing, install pinned requirements, run
  `python -m monitoni --mock --mock-purchase` (mock hardware, simulated
  payments). UI at http://127.0.0.1:8080/. `make test` — pytest. `make lint` — ruff.
- `python -m tests.fake_waveshare --port N --coils 8` (one per module,
  `--inputs-file` for the DI), `python -m tests.fake_purchase_server
  --port N --token T` (`GET /pay?item=N`) and `python -m tests.fake_artnet
  --port N --zones 10x12` (prints zone colour changes; the daemon reaches it
  with `--fake-artnet 127.0.0.1:N`, which also plays real sounds) — fakes for
  a laptop.

## Folder layout

- `monitoni/` — the daemon. `__main__.py` entry point, `config.py`,
  `daemon.py` (owns everything, forwards hardware events), `flow.py` (state
  machine: transition table, timeouts, entry hooks), `motor.py` (hold-to-turn
  sequence with watchdog), `purchase.py` (purchase server protocol, HTTP
  client, mock), `outbox.py` (durable reports), `eventlog.py` (SQLite
  event log), `leds.py` (pattern table, ArtNet sender, mock), `audio.py`
  (pygame, mock), `feedback.py` (state → pattern and sound), `hardware/`
  (`base.py` protocol, `modbus.py` one class per module, `real.py`,
  `mock.py`), `web/` (aiohttp routes, `static/` UI files). `assets/sounds/`:
  `success.wav`, `alarm.wav`, `error.wav`, the only sounds there are.
- `config/` — `default.yaml` (checked in), `local.yaml` (per machine). `tests/`
  — pytest. `docs/SETUP.md` — installation guide, grows with every step.

## Conventions

- Hardware, timing and state logic live in the daemon, never in the browser.
  The page renders what the daemon sends and asks before every action; the
  daemon's 409 is the answer, not a disabled button.
- One way to do each thing; prefer deleting over abstracting. No retry
  wrappers, plugin systems or abstract base classes beyond the hardware protocol.
- `default.yaml` is installation config; development always runs with `--mock`.
  Validation errors name the key. The three things a user changes on the
  machine live in `data/runtime.json` next to the database (`out_of_order`,
  `brightness`, `volume`), written atomically by the daemon, read at start.
- Door lock rule: only the `idle`/`out_of_order` entry hook locks (all doors
  at once); `unlock_door` has two callers, the `door_unlocked` hook and the
  settings Doors tool. Nothing relocks in `settings`; leaving it locks all.
- Spindle rule: `motor.py` is the one owner of the spindle lock state; the
  settings tool goes through `motor.set_spindle`, and every flow transition
  stops the motor and closes the spindle (a TURN held across Exit ends there).
- Read-back rule: every coil write is followed by a read of that coil; a
  mismatch is a `HardwareError`. Lock state is what the module reports. Relay
  ON = unlocked; relay OFF or power loss = locked.
- Error policy: a `HardwareError` in a hook or a settings tool, a lost module
  connection or a failed door poll puts the flow into `out_of_order
  (hardware)`; it returns to `idle` once the hardware has been healthy for
  `recovery_dwell_s` = 10 s. `maintenance` (the runtime switch) and `database`
  (a lost report) never clear themselves. No command is retried; the motor's
  emergency OFF is the one second write. Hardware stop switches nothing.
- Motor stop rule: the motor stops on release, after `max_run_s`, on every
  transition, when the last WebSocket closes and on daemon stop.
- Purchase server (Monitoni): `GET /api/vending/permission` polled once a
  second in `checking_purchase`; `{"HasPermission": true, "Item": N}` unlocks
  level N whatever was selected. `GET …/complete` the moment the door opens,
  `GET …/close` when it closes, both durable in the outbox and retried until
  2xx; nothing else retries. Every call carries `Monitoni-Terminal: <token>`;
  the token lives in `local.yaml` only, never logged or shown.
- Relock rule: `relock_delay_s` after the door opened the level is locked
  again, one command, no retry; a failure is a hardware fault.
- Feedback (LEDs, sound) is not safety-relevant and never affects the flow:
  `feedback.py` is the one place mapping states to patterns and sounds, every
  call guarded, no retries. The flow only announces transitions. LED zones are
  per machine (`led.zones` in `local.yaml`).
- Settings state: entered from `idle`/`out_of_order` with the PIN
  (`settings.pin` in config, checked by the daemon, never in the browser);
  no sleep, customer events rejected, door events status only; left by Exit or
  after `settings_timeout_s` untouched, refused while the door is open. On the
  way out: runtime `out_of_order` → `out_of_order (maintenance)`, hardware
  unhealthy → `(hardware)`, else `idle`. Routes `POST /api/settings/<name>`,
  one `command` row each.
- Commands come in over `POST /api/command`; status goes out over the one-way
  WebSocket (on every state change plus a 1 s heartbeat).
- Words: **shelf** on screen, `level` in code and API. Fonts are bundled under
  `static/fonts/` (OFL); nothing is loaded from the network.
- stdlib `logging` to stdout only (journald captures it). English only, code and docs.

## Kept features

- Customer screen: select level → QR → purchase verified → door unlocked →
  door monitored → idle. Sleep, out of order, door alarms, SQLite event log.
- PIN-protected settings area: status, test tools (doors, motor, LEDs, audio,
  network), QR codes, events, three runtime switches.
- Mock hardware mode for development on a laptop.
- Hardware: two Waveshare Modbus-TCP relay modules over Ethernet/PoE (30-ch
  `relay_levels` for door locks; 8-ch Module C `relay_core` for motor, spindle
  lock and the door sensor input). Gledopto ESP32 WLED via ArtNet. Audio via
  HDMI (pygame).

## Dropped features

- Kivy / KivyMD.
- Remote telemetry server and web dashboard.
- GPIO and RS485 fallback paths.
- Setup wizard (replaced by `docs/SETUP.md`).
- GSD planning framework (or any other planning framework).
