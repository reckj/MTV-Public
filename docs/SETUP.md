# MoniToni setup guide

Written for a technically minded installer without a deep IT background. The
machine may have no internet. Every command is given in full; nothing is
assumed.

Follow the sections in order; each one builds on the previous.

## 1. What you need

Hardware list (Pi, display, relay modules, WLED controller, PoE switch),
cables, SD card, and a laptop for imaging the card.

Facts so far (no hardware has been set up on a Pi yet):

- **LED controller**: a Gledopto ESP32 running WLED, on the same local subnet
  as the relay modules, with a static IP (`default.yaml` assumes
  192.168.1.102, key `hardware.wled.ip_address`). The daemon sends the strip's
  colours over ArtNet (UDP port 6454) and checks every 30 s that the
  controller answers `http://<ip>/json/info`. WLED is configured once, in its
  own web page, as follows. Open `http://<wled-ip>/` in a browser on the same
  network, then:
  1. **Config → LED Preferences**: set the LED count ("Length") to at least
     the number of pixels on the strip (`hardware.wled.pixel_count`). Save.
  2. **Config → Sync Interfaces**, section **Realtime**: tick "Receive UDP
     realtime"; under "Network DMX input" choose Type **Art-Net** (the Port
     field is then greyed out or gone: Art-Net always uses 6454, do not look
     for it), Multicast off, Start universe **0** (the value of
     `hardware.wled.universe`), DMX start address **1**, DMX mode **Multi
     RGB**, Timeout **2500** ms, tick **Force max brightness**, untick "Disable
     realtime gamma correction" unless the colours look wrong. Save.
  3. Leave the controller's own effect on something dark (Config → LED
     Preferences → "Turn LEDs on after power up/reset" off, or a dark default
     preset). The daemon switches the strip off when it is stopped cleanly;
     after a crash or a pulled cable the strip keeps its last frame until the
     2500 ms timeout and then shows the controller's own state, so that state
     should be dark. While the daemon runs it sends a frame at least once a
     second, which is what the timeout is for.
  "Multi RGB" means three DMX channels per pixel, red, green, blue; a strip of
  more than 170 pixels continues in the next universe, which WLED handles by
  itself. "Force max brightness" makes the daemon's `led.brightness` the only
  brightness in play.

_The rest of the list: to be written during integration._

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

Facts so far (no network step has been performed on a Pi yet):

- The relay modules are on the local subnet with static IPs (§6); the machine
  must reach them directly.
- From the internet the daemon needs exactly one destination:
  `https://monitoni.zhdk.ch` (`purchase_server.base_url`), for the permission
  poll and the complete/close reports. Nothing else is contacted: no updates,
  no telemetry. HTTPS verification needs a roughly correct clock; NTP and the
  Pi 5's RTC battery belong to the Pi milestone (_to be written_).
- Without the purchase server the machine keeps running: customers can browse,
  the page says "Payment server not reachable" in idle and "Payment currently
  not possible — please wait" instead of a QR code, and the reports of vends
  that already happened wait in the outbox until the server answers again.

_Configuring the Pi's network: to be written during integration._

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

- `system.machine_id` — a label for the log and the status page; the server
  identifies the machine by its token, not by this.
- `hardware.relay_core.host`, `hardware.relay_levels.host`,
  `hardware.wled.ip_address` — the modules' IP addresses.
- `hardware.door_locks.channels` — the `relay_levels` channel for level 1,
  2, … in order (default `[1, 2, …, 10]`). Confirm against the wiring.
- `hardware.door_sensor.di_index`, `di_active` — which digital input on
  `relay_core` the door sensor is on (0 = DI1) and which level means "door
  open" (`low` or `high`). `debounce_count` reads must agree before a change
  counts. Confirm against the wiring.
- `hardware.motor.motor_channel`, `spindle_channel` — the `relay_core`
  channels for the motor and the spindle lock. Confirm against the wiring.
- `purchase_server.token` — this machine's token, mandatory. It comes from the
  machine's device page in the Monitoni admin; Cubera creates it when the
  machine is registered. Keep `local.yaml` private, the token is the machine's
  identity. `purchase_server.base_url` normally stays at the default
  `https://monitoni.zhdk.ch`.
- `qr.base_url` — the QR code for level N encodes `<qr.base_url>?level=<N>`,
  the same pattern as the old machines (no machine id). The codes are drawn
  from this value on demand; change it, restart, done.
- `settings.pin` — the PIN for the settings area on the touchscreen, 4 to 8
  digits, written as a string in quotes (`pin: "4711"`). The default is
  `"0000"`; every start with the default logs the line `settings.pin is still
  the default 0000: set it in config/local.yaml` and the settings home screen
  shows "CHANGE THE DEFAULT PIN" in amber until it is changed.
- `hardware.wled.pixel_count` — how many pixels the LED strip has, and
  `led.zones` — which pixels belong to which level: one `[first, last]` pair
  per level, level 1 first, both numbers inclusive, pixel 0 being the one
  next to the controller. Every machine's strip is different, so count on the
  machine: in the WLED web page, open the segment editor on the main screen,
  set the segment's start and stop until exactly the pixels behind one level
  light up (stop is exclusive there, so a segment 12–24 means pixels 12 to
  23 and the zone is `[12, 23]`), write the pair down, repeat for every
  level. The ranges must not overlap and must stay below `pixel_count`; the
  daemon refuses to start otherwise and names the bad entry (`led.zones.3`
  is the fourth level). The default is ten blocks of twelve pixels.
- `led.brightness` (0..1, default 0.6) and `hardware.audio.volume` (0..1,
  default 0.7). Once the settings area exists these two are changed on the
  machine and stored in `data/runtime.json`, which then wins over the YAML.
- `hardware.wled.enabled` / `hardware.audio.enabled` — set to `false` on a
  machine without a strip or without a speaker; the daemon then uses the
  built-in stand-ins and shows the patterns only in the status.

Both Waveshare modules ship with the same address, 192.168.1.254. Each needs
its own static IP before the daemon can talk to both; `default.yaml` assumes
192.168.1.100 for `relay_core` and 192.168.1.101 for `relay_levels`. How to
change a module's address will be written down when it has been done.

Older `local.yaml` files: the key `system.maintenance_mode` no longer exists
(the Out of order switch in the settings area replaced it) and neither does
`qr.dir`. The daemon refuses to start with an unknown key and names it, for
example `system.maintenance_mode: Extra inputs are not permitted`; delete that
line from `local.yaml`.

`data/runtime.json`, next to the database, is written by the daemon and holds
the three switches a user changes on the machine: `out_of_order` (true keeps
the machine on the "Out of order" screen across restarts), `brightness` and
`volume` (0 to 1). Deleting the file resets all three to the configuration;
do not edit it by hand while the daemon runs.

On the development laptop none of these are set; mock mode runs on the
defaults. Setting them on a Pi: _to be written during integration._

## 7. First start

Run the daemon by hand, open the UI in a browser, check mock versus real
hardware mode.

Steps performed so far (on a development laptop):

```
cd monitoni
.venv/bin/python -m monitoni --mock --mock-purchase
```

`--mock` uses simulated relay modules, `--mock-purchase` simulates payments;
without the second flag the daemon talks to `purchase_server.base_url`.

Then open http://127.0.0.1:8080/ in a browser. The page shows the current
state in the header with a green "connected" badge, level buttons 1 to 10 in
`idle`, the QR code and a cancel button after selecting a level, the door
instructions while a door is open, a red screen when the door alarm is on or
a door was opened without a purchase, and a dark screen in sleep (tap to
wake). A dev panel at the bottom offers "Simulate payment" (with the mock
purchase server) and, with mock hardware, "Door open" and "Door close"; it
shows the hardware status object and lists the last 20 events. The same three
buttons sit on the settings home screen as a "Simulation" card in mock mode.
Walk the flow: select a level, simulate payment, door open, door close, back
to idle. Every step is written to `data/monitoni.db`. Stop the daemon with
Ctrl+C; it logs "shutdown requested" and exits.

In `idle` the page also shows a big TURN button: hold it to run the motor
(spindle lock opens first); it stops on release and after `max_run_s`
regardless.

Starting without `--mock` while `hardware.mode` is `real` refuses to run if
`config/local.yaml` is missing. With a `local.yaml` the daemon starts even if
a module is unreachable: the page shows "Out of order" with "Reason:
hardware", and `/api/status` lists both modules under `hardware` with
`connected` and `last_error`. The daemon reconnects every 2, 5, 10, then
30 seconds and returns to `idle` by itself once both modules have answered
and the door sensor has been reading for 10 seconds without a break. A module
that drops out during operation has the same effect. "Reason: maintenance"
means the Out of order switch in the settings area is on; it never clears
itself. "Reason: database" means a vend could not be recorded for the server
(the disk refused); it clears when someone opens the settings and leaves them,
or on a restart.

"Payment server not reachable" in `idle`, and "Payment currently not
possible — please wait" instead of the QR code after a level was selected, mean
the last request to the Monitoni server failed. Check the network, then
`/api/status`: under `purchase_server`, `last_error` names the cause. `HTTP 401`
is a wrong or missing `purchase_server.token`; a connect error or timeout is
the network or the server. The messages go away with the next successful
request. In the dev panel, "Outbox: N pending" is the number of `complete` and
`close` reports the server has not accepted yet; they are retried after 1, 2,
5, 15 and then every 60 seconds and survive a restart.

The LED strip and the sounds: when the daemon starts it logs `ArtNet to
<ip>:6454, universe 0, N pixels` and, after the first health poll, `WLED at
<ip> reachable`; `unreachable: <error>` means the controller does not answer
on its web port (check the IP, the cable, that WLED is up). The strip itself
should show all level zones in a dim warm colour in `idle`; if the controller
is reachable but the strip stays dark or shows WLED's own effect, the ArtNet
settings in §1 are wrong (most often "Receive UDP realtime" off or the DMX
mode not "Multi RGB"). In `/api/status`, `leds` shows `reachable`, the
current `pattern` and `level`; every change of reachability is a `network`
row `{"component": "wled", "reachable": …}` in the event log. The dev panel
shows the same as "LEDs: idle · level – · reachable". For audio the start
log says `audio: 44100 Hz, 2 channel(s), sounds from …`; `audio unavailable,
sounds are off: <error>` means no audio device was found (on the Pi: check
that HDMI audio is enabled and the display or an amplifier is connected). A
dead strip or missing audio never stops the machine from vending.

_HDMI audio on the Pi is not resolved yet._ On Raspberry Pi OS Bookworm sound
goes through PipeWire, which runs per logged-in user, so whether the daemon
finds the HDMI output depends on which user it runs as. The Pi milestone
decides between running the daemon as the kiosk user (the one with the
PipeWire session) and bypassing PipeWire with `SDL_AUDIODRIVER=alsa` plus
`AUDIODEV` pinned to the HDMI device in the systemd unit. Nothing about this
can be settled on the laptop.

**The settings area.** Tap the small gear in the top right corner of the
"Select a level" screen (or of the "Out of order" screen), type the PIN on the
keypad and press Enter. A wrong PIN shakes the dots and clears them; there is
no lockout. The home screen shows six dots: Doors (the door relay module),
Core (the core relay module), LEDs (the WLED controller answers), Server (the
purchase server answered the last request), Sensor (the door sensor is being
read), Audio (a sound device was found). Green is fine, red is a problem, amber
is "not known yet"; the line under the dots names the first problem and since
when (for example "SERVER UNREACHABLE · SINCE 14:02"), or reads "CHANGE THE
DEFAULT PIN" in amber while the PIN is still `0000`. Below:
the Out of order switch, the sections Doors, Motor, LEDs, Audio, Network, QR
codes and Events, and the footer with the machine id, the software version and
the uptime. "Exit" at the top left returns to the customer screen; it refuses
while the door sensor reads open ("Close the door first"). After 5 minutes
without a touch the area closes by itself.

To take the machine out of service: settings → Out of order switch → "Switch
on" in the confirmation → Exit. Customers now see "Out of order" with the
maintenance message; the switch stays on across restarts. To put it back:
gear on the "Out of order" screen → PIN → switch off → confirm → Exit.

Each section tests one component and shows its live state: Doors unlocks or
locks a shelf (no vend starts, the door may be opened without an alarm; Lock
all doors at the bottom), Motor has the TURN button and a spindle lock test
with the timings from `local.yaml`, LEDs has the brightness slider, Off /
White / Amber and a shelf to light, Audio the volume slider and the three
sounds, Network the machine's own IP, the three devices and the purchase
server with a "Test server" button (one request, the answer is "OK · N ms" or
the error), QR codes shows each shelf's code and the address it encodes, and
Events the counters (vends today and total, alarms and faults today) with the
event list (All / Vends / Hardware / Network, newest first, "Load more"). The
list has no export: to take the data off the machine copy the SQLite file.
From another computer on the same network, with the Pi's user and address:

```
scp pi@192.168.1.50:/home/pi/monitoni/data/monitoni.db ./monitoni-$(date +%F).db
```

(replace the user, the address and the path with the machine's; the file can
be opened with any SQLite tool, table `events`). Copying while the daemon runs
is safe; the copy may miss the last second.

_Real hardware start on a Pi: to be written during integration._

## 8. Verification

Per-component tests from the settings area, and the first vend.

_To be written during integration._

## 9. Run as a service

systemd units for the daemon and the kiosk browser, and the nightly UI reload.

_To be written during integration._

## 10. Troubleshooting

Common problems and how to check them.

_To be written during integration._
