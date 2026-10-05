# MoniToni setup guide

Written for a technically minded installer without a deep IT background. The
machine may have no internet. Every command is given in full; nothing is
assumed.

Follow the sections in order; each one builds on the previous.

## 1. What you need

Hardware list (Pi, display, relay modules, WLED controller, PoE switch),
cables, SD card, and a laptop for imaging the card.

_To be written during integration._

## 2. Flash the OS with Raspberry Pi Imager

Which OS image to choose, and the hostname, user, SSH and locale settings to
enter in Imager before writing the card.

_To be written during integration._

## 3. First boot and OS settings

Wayland session, rotating the display to portrait, turning screen blanking
off, enabling auto-login.

_To be written during integration._

## 4. Network

Static IP on the relay modules' subnet, reaching the purchase server, and what
keeps working when there is no internet.

_To be written during integration._

## 5. Install the application

Copy or clone the repository, create the virtual environment, install the
pinned requirements, and install offline from a wheel directory when the
machine has no internet.

Get the repository onto the machine (clone from GitHub or copy from a USB
stick); exact commands will be written when this is first done on a Pi.

Steps performed so far (on a development laptop, not yet on a Pi), from inside
the repository directory:

```
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

The machine needs `requirements.txt` only. `requirements-dev.txt` (tests,
lint) is for development; `make dev` installs both and then starts the daemon
in mock mode. `python3` must be Python 3.11; Raspberry Pi OS Bookworm ships it.

_Offline installation from a wheel directory: to be written during
integration._

## 6. Configure

`config/local.yaml`: machine id, relay module IPs, WLED IP, purchase server
URL, settings PIN.

Keys an installer must set in `config/local.yaml` so far (copy
`config/local.yaml.example` as a start; every key not listed keeps its value
from `config/default.yaml`):

- `system.machine_id` — this machine's id, sent with every purchase check.
- `hardware.relay_core.host`, `hardware.relay_levels.host`,
  `hardware.wled.ip_address` — the modules' IP addresses.
- `purchase_server.base_url` — where purchases are verified.
- `qr.base_url` — the QR code for level N encodes `<qr.base_url>?level=<N>`,
  the same pattern as the old machines (no machine id). Generated PNGs land
  in `data/qr/`; delete a file there to regenerate it after changing the
  value.

On the development laptop none of these are set; mock mode runs on the
defaults. Setting them on a Pi: _to be written during integration._

## 7. First start

Run the daemon by hand, open the UI in a browser, check mock versus real
hardware mode.

Steps performed so far (on a development laptop):

```
cd monitoni
.venv/bin/python -m monitoni --mock
```

Then open http://127.0.0.1:8080/ in a browser. The page shows the current
state in the header with a green "connected" badge, level buttons 1 to 10 in
`idle`, the QR code and a cancel button after selecting a level, the door
instructions while a door is open, a red screen when the door alarm is on and
a dark screen in sleep (tap to wake). Because hardware mode is `mock`, a dev
panel at the bottom offers "Simulate payment", "Door open" and "Door close"
and lists the last 20 events. Walk the flow: select a level, simulate payment,
door open, door close, back to idle. Every step is written to
`data/monitoni.db`. Stop the daemon with Ctrl+C; it logs "shutdown requested"
and exits.

Starting without `--mock` while `hardware.mode` is `real` refuses to run if
`config/local.yaml` is missing.

_Real hardware start: to be written during integration._

## 8. Verification

Per-component tests from the settings area, and the first vend.

_To be written during integration._

## 9. Run as a service

systemd units for the daemon and the kiosk browser, and the nightly UI reload.

_To be written during integration._

## 10. Troubleshooting

Common problems and how to check them.

_To be written during integration._
