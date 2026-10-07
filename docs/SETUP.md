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

_Performed on vm001 on 2026-10-07._

Done on the laptop, with a micro SD card in a card reader: at least 16 GB,
better 32 GB (the system with the kiosk takes about 6 GB; vm001's 8 GB card
was left with 1 GB free). The card is the **install card**. If the machine
has an SSD, it runs from the SSD in the end (§3), but the Pi needs a system on
a card first, because only a running system can make the SSD visible and copy
itself onto it. Without an SSD (vm001 has none) the machine simply runs from
the card.

1. Install Raspberry Pi Imager from https://www.raspberrypi.com/software/
   (free, for Mac, Windows and Linux) and start it.
2. **Choose Device**: Raspberry Pi 5. **Choose OS**: Raspberry Pi OS (other) →
   **Raspberry Pi OS Lite (64-bit)**, the version without a desktop (the
   kiosk brings its own display program, §9). Since autumn 2026 this is
   Debian 13 "Trixie" with Python 3.13, which is what the machine runs. Do
   not pick the entries marked **Legacy**: those are the previous release,
   Bookworm. **Choose Storage**: the card. Next.
3. "Would you like to apply OS customisation settings?" → **Edit Settings**:
   - General: tick *Set hostname* and enter `vm001` (the machine id in lower
     case; `vm002` for the next machine). Tick *Set username and password*:
     username `monitoni`, a password of your choice; write it down, it is the
     SSH login and the `sudo` password on the machine. Wi-Fi: leave the
     network name in the Wi-Fi section **empty**, which configures no Wi-Fi
     (the machine is on Ethernet). Tick *Set locale
     settings*: time zone `Europe/Zurich`; the keyboard layout does not
     matter.
   - Services: tick *Enable SSH*, choose *Use password authentication*.
   - Options: as you like (telemetry can be off).
   Save, then **Yes** (apply the settings), **Yes** (erase the card). Imager
   writes and verifies the card; a few minutes.
4. Two files on the card before the first boot. Imager ejects the card when it
   is done: take it out, put it back in, it shows up as `bootfs`. Open each
   file in a plain-text editor (on the Mac: TextEdit; if it shows formatting
   buttons, Format → Make Plain Text first):
   - `config.txt`: the last line is `[all]`; add under it
     ```
     dtparam=pciex1
     ```
     This switches on the PCIe connector the SSD hangs on; without it the SSD
     is invisible to the Pi (§3).
   - `cmdline.txt`: one long line. Add to its end, after a space, on the same
     line:
     ```
     video=HDMI-A-1:400x1280M@60 consoleblank=0 fbcon=rotate:2
     ```
     The first part sets the display to its native 400×1280 portrait mode on
     the Pi's HDMI0 port (the HDMI socket next to the USB-C power socket).
     The second keeps the text console from going black. The third turns the
     console text by 180°: the panel is mounted upside down in the machine.
     The kiosk turns its picture and the touch the same way by itself (§5).
   Save both files, eject the card (Finder: the eject symbol next to `bootfs`).

## 3. First boot and OS settings

_Performed on vm001 on 2026-10-07._

The display on HDMI0 and its USB cable (the touch panel) in the Pi, Ethernet
in, the install card in, then power. The first boot takes a minute or two
(the card is resized, the SSH keys are made, the Pi restarts once); the
display shows white console text and ends at a `vm001 login:` prompt.
Nothing to type there. **Check on the display**: the text reads upright as
the panel sits in the machine. If it is upside down, the `fbcon=rotate:2`
from §2 is missing in `cmdline.txt`.

**Log in over SSH** from the laptop, on the same network as the machine:

```
ssh monitoni@vm001.local
```

Answer `yes` to the fingerprint question, then type the password from §2
(nothing is shown while typing). If `vm001.local` is not found, the laptop's
network does not pass the name along: use the IP address instead (the
router's device list shows `vm001`), or connect the laptop to the Pi with an
Ethernet cable, which makes `vm001.local` work directly.

**OS settings** with the Pi's configuration tool (arrow keys and Enter; Tab
jumps to the buttons; Esc goes back):

```
sudo raspi-config
```

- 1 System Options → S5 Boot → **B1 Console**: a text console; the kiosk
  service (§9) takes the display over. (Lite starts like this already; this
  only confirms it.)
- 1 System Options → S6 Auto Login → **No**.
- 5 Localisation Options: set by Imager already; L2 Timezone should read
  Europe/Zurich.
- Finish. If it asks to reboot, Yes; log in again afterwards.

The machine starts without the network cable as well: nothing of MoniToni
waits for the network at boot.

**Time.** The purchase server speaks HTTPS, which needs a roughly correct
clock. Check:

```
timedatectl
```

Expected lines: `Time zone: Europe/Zurich`, `System clock synchronized: yes`,
`NTP service: active`. The Pi sets its clock from the network (the Debian
time servers, over NTP). If `synchronized: no` is still there after a few
minutes, the network blocks NTP (UDP port 123): ask the network people for a
time server and enter it as `NTP=<server>` in `/etc/systemd/timesyncd.conf`
(`sudo nano /etc/systemd/timesyncd.conf`; Ctrl+O, Enter saves, Ctrl+X
leaves), then `sudo systemctl restart systemd-timesyncd`.

**The RTC battery.** The Pi 5 has a real-time clock; with the official RTC
battery (a small rechargeable cell on the two-pin "BAT" connector between
the USB-C socket and the HDMI sockets) the time survives a power cut even
without a network, so the machine can vend right after a restart. Check the
clock reads: in the output of `timedatectl` above, the line `RTC time:`
shows the clock's time in UTC (two hours behind Zurich in summer, one in
winter). Whether a battery is fitted:

```
cat /sys/class/rtc/rtc0/battery_voltage
```

prints the battery's voltage in millionths of a volt: about 3000000 with a
charged battery, a small number (vm001: 4273) without one. Without a battery
the clock is lost at a power cut and comes back from the network (NTP) once
the machine is online; until then HTTPS to the purchase server may fail. The
official battery is charged by the Pi only
when told so: add to `/boot/firmware/config.txt`, under `[all]`, the line

```
dtparam=rtc_bbat_vchg=3000000
```

(`sudo nano /boot/firmware/config.txt`; it takes effect at the next boot).
Only with the official rechargeable cell: a non-rechargeable battery must
never be charged, so without that cell leave the line out.

**The SSD.** Check that the Pi sees it:

```
lsblk
```

Expected: `mmcblk0` (the card, with `mmcblk0p1` and `mmcblk0p2`) **and**
`nvme0n1` (the SSD). If `nvme0n1` is missing, the `dtparam=pciex1` line from
§2 is not in `/boot/firmware/config.txt` (`cat /boot/firmware/config.txt`
shows the file), or the SSD or its HAT is not seated; fix it, `sudo reboot`,
check again. If it still does not appear, the machine runs from the card:
skip the next step, everything below works the same. (vm001, 2026-10-07: the
PCIe connector comes up but nothing is on it; vm001 has no SSD and runs from
the card.)

**Install onto the SSD** (not yet performed on a machine). Copy only if the
SSD is larger than the card: `lsblk` shows both sizes. Right after a fresh `sudo reboot` and login, with
nothing else running, the whole card is copied to the SSD byte by byte, so
the SSD ends up with the same system and the same settings:

```
sudo dd if=/dev/mmcblk0 of=/dev/nvme0n1 bs=4M status=progress conv=fsync
```

It prints the progress and takes a few minutes (roughly one minute per 5 GB
of card). Then check the copied filesystem, because the card was in use
during the copy:

```
sudo blockdev --rereadpt /dev/nvme0n1
sudo e2fsck -f /dev/nvme0n1p2
```

`e2fsck` ends with a line counting files and blocks; if it fixes a small
thing or two on the way, that is fine. Shut down:

```
sudo poweroff
```

Wait until the display is dark and the green LED has stopped, take the
**install card out** and put it in a drawer: it must never be in the machine
together with the SSD again (both carry the same partition ids, and the Pi
could pick either). Power on: with no card the Pi boots from the SSD. Log
in again and check:

```
findmnt /
```

The `SOURCE` column reads `/dev/nvme0n1p2`. The copied system still thinks
it is as small as the card; give it the whole SSD:

```
sudo raspi-config
```

6 Advanced Options → A1 Expand Filesystem → Ok → Finish → Yes to reboot.
After the reboot `df -h /` shows the SSD's size.

Alternative, when the SSD can be put into a USB enclosure: write it from the
laptop with Imager exactly as in §2 (same settings, same two file edits), fit
it in the HAT and boot with no card at all. That gives a clean copy without
the `dd` step.

**Rollback.** The old system is still on the old card, untouched. Put that
card in and power on: the Pi tries the card before the SSD and boots the old
system. Take it out again to return to the new one.

## 4. Network

Static IP on the relay modules' subnet, reaching the purchase server, and what
keeps working when there is no internet.

Facts so far (no network step has been performed on a Pi yet):

- The relay modules are on the local subnet with static IPs (§6); the machine
  must reach them directly.
- From the internet the daemon needs exactly one destination:
  `https://monitoni.zhdk.ch` (`purchase_server.base_url`), for the permission
  poll and the complete/close reports. Nothing else is contacted: no updates,
  no telemetry. HTTPS verification needs a roughly correct clock; time sync and the
  RTC battery are set up in §3.
- Without the purchase server the machine keeps running: customers can browse,
  the page says "Payment server not reachable" in idle and "Payment currently
  not possible — please wait" instead of a QR code, and the reports of vends
  that already happened wait in the outbox until the server answers again.

_Configuring the Pi's network: to be written during integration._

## 5. Install the application

_Performed on vm001 on 2026-10-07._

Logged in over SSH as `monitoni` (§3). This section needs internet on the
machine for `apt` and `git`; the Python packages can come from a USB stick
instead (below).

**System packages.** cage shows one program full screen on the display,
Chromium is that program (it shows the page), wlr-randr turns the picture
(the panel is upside down), git fetches the application, python3-venv makes
the application's own Python environment (the last two are on Lite already):

```
sudo apt update
sudo apt install cage chromium wlr-randr git python3-venv
```

`Y` when asked: about 190 packages, 300 MB to download, 1 GB on the card,
a few minutes. Write down what was installed; these versions stay frozen on
the machine, `apt` is never run again except by hand:

```
apt list --installed 2>/dev/null | grep -E '^(cage|chromium|wlr-randr|git|python3-venv)/'
```

Installed on vm001 on 2026-10-07:

```
cage/stable,now 0.3.1-1~bpo13+1+rpt2 arm64
chromium/stable,now 1:154.0.8037.92-1~deb13u1+rpt1 arm64
git/stable,now 1:2.47.3-0+deb13u1 arm64
python3-venv/stable,now 3.13.5-1 arm64
wlr-randr/stable,now 0.4.1-1 arm64
```

**The application** goes to `/opt/monitoni`, owned by the user `monitoni`:

```
sudo mkdir -p /opt/monitoni
sudo chown monitoni:monitoni /opt/monitoni
git clone https://github.com/reckj/MTV-Public.git /opt/monitoni
cd /opt/monitoni
```

**The Python environment** inside it. Python 3.13 is the one Raspberry Pi OS
Trixie ships (`python3 --version` prints 3.13.x):

```
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

The second line downloads the pinned packages from the internet. **Without
internet** on the machine the packages come from a USB stick: on the laptop,
in the repository, run `deploy/wheels.sh` once (it needs the laptop's
`.venv` from `make install` and internet; it fills `wheels/` with the
packages for the Pi, 24 files, about 26 MB), copy the `wheels` folder onto a USB stick, put the stick in
the Pi, then:

```
lsblk
sudo mkdir -p /mnt/stick
sudo mount /dev/sda1 /mnt/stick
.venv/bin/pip install --no-index --find-links /mnt/stick/wheels -r requirements.txt
sudo umount /mnt/stick
```

(`lsblk` lists the stick as `sda` with its partition `sda1`; use that name in
the `mount` line.) Either way `pip` ends with `Successfully installed …`
naming the 24 packages. Check that the application runs:

```
.venv/bin/python -m monitoni --help
```

It prints the options and exits.

**The services.** One script puts the daemon and the kiosk in place as
services and makes them start at boot; it is safe to run again later (after
a change to a unit file, for example):

```
sudo /opt/monitoni/deploy/install.sh
```

It prints every step. What it does:

1. Makes sure the user `monitoni` exists (Imager created it in §2) and is in
   the groups `video`, `input`, `render` (cage: the display, the touch
   panel, the graphics chip) and `audio` (the daemon's HDMI sound).
2. Puts the daemon's options file `/etc/default/monitoni` in place (a copy
   of `deploy/monitoni.default`, only if there is none yet): one line,
   `MONITONI_OPTS=""`. §6 says what to put there on day one.
3. Copies the four unit files from `deploy/` into `/etc/systemd/system/`
   (`monitoni.service`, `monitoni-kiosk.service`,
   `monitoni-kiosk-reload.service`, `monitoni-kiosk-reload.timer`) and tells
   systemd to read them (`systemctl daemon-reload`).
4. Copies the touch rule `deploy/99-monitoni-touch.rules` into
   `/etc/udev/rules.d/` and applies it: the panel is mounted upside down, so
   the touch coordinates are turned by 180° like the picture.
5. Enables the daemon, the kiosk and the nightly reload timer
   (`systemctl enable …`), so they start at every boot.

Nothing is started yet: §6 configures the machine first, §7 and §9 start the
services. The new group memberships count for the services at once; an SSH
session only gets them after logging out and in again.

## 6. Configure

`config/local.yaml`: machine id, relay module IPs, WLED IP, purchase server
URL, settings PIN.

Keys an installer must set in `config/local.yaml` so far (copy
`config/local.yaml.example` as a start; every key not listed keeps its value
from `config/default.yaml`):

- `system.name` and `system.location` — what customers see in the top bar of
  the screen: `name` is the big wordmark (default `"Monitoni"`), `location`
  the small line under it, for example `"ZHdK · Toni-Areal"`; the default
  `""` shows no second line.
- `system.machine_id` — a label for the log and the settings screens; the
  server identifies the machine by its token, not by this.
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
  default 0.7). Both are changed on the machine in the settings area and then
  stored in `data/runtime.json`, which wins over the YAML.
- `hardware.wled.enabled` / `hardware.audio.enabled` — set to `false` on a
  machine without a strip or without a speaker; the daemon then uses the
  built-in stand-ins and shows the patterns only in the status.

Both Waveshare modules ship with the same address, 192.168.1.254. Each needs
its own static IP before the daemon can talk to both; `default.yaml` assumes
192.168.1.100 for `relay_core` and 192.168.1.101 for `relay_levels`. How to
change a module's address will be written down when it has been done.

Older `local.yaml` files: the keys `system.maintenance_mode` (the Out of order
switch in the settings area replaced it), `system.maintenance_message` (the
"Out of order" screen has fixed words now) and `qr.dir` no longer exist. The
daemon refuses to start with an unknown key and names it, for example
`system.maintenance_message: Extra inputs are not permitted`; delete that line
from `local.yaml`.

`data/runtime.json`, next to the database, is written by the daemon and holds
the three switches a user changes on the machine: `out_of_order` (true keeps
the machine on the "Out of order" screen across restarts), `brightness` and
`volume` (0 to 1). Deleting the file resets all three to the configuration;
do not edit it by hand while the daemon runs.

**The daemon's start options** live in one file, `/etc/default/monitoni`
(put there by `install.sh`, §5), with one line: `MONITONI_OPTS=""`. The
empty value is the machine's setting: the modules from `local.yaml` and the
Monitoni server with the machine's token. On day one, with no modules on the
network and no token yet, set it to the commissioning value:

```
sudo nano /etc/default/monitoni
```

and make the line read `MONITONI_OPTS="--mock --mock-purchase"` (Ctrl+O,
Enter saves, Ctrl+X leaves). The daemon then simulates the hardware and the
payments, as on the laptop (§7), whatever `hardware.mode` says. When the
token is in `local.yaml` and the modules are reachable, set the line back to
`MONITONI_OPTS=""`. Either change is applied by:

```
sudo systemctl restart monitoni
```

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

Then open http://127.0.0.1:8080/ in a browser: this is the page the kiosk
shows ("What the customer sees" below). In mock mode the payment, the door
sensor and the purchase server are simulated from a second terminal:

```
curl -X POST 127.0.0.1:8080/api/command -H 'Content-Type: application/json' -d '{"command":"simulate_payment"}'
curl -X POST 127.0.0.1:8080/api/command -H 'Content-Type: application/json' -d '{"command":"simulate_door","open":true}'
curl -X POST 127.0.0.1:8080/api/command -H 'Content-Type: application/json' -d '{"command":"simulate_server","reachable":false}'
```

The first pays for the shelf that was selected on the screen, the second
opens (`true`) or closes (`false`) the door sensor, the third makes the
purchase server unreachable (`false`) or reachable again (`true`). The
settings home screen has "Open" and "Close" for the door as a "Simulation"
card in mock mode. Walk the flow: select a shelf, simulate the payment, open
the door, close it, back to the shelves. Every step is written to
`data/monitoni.db`. Stop the daemon with Ctrl+C; it logs "shutdown requested"
and exits.

**What the customer sees.** The page is the whole kiosk: one screen per
state of the machine, nothing is decided in the browser. In the order of a
purchase:

- *Select a shelf*: the name and location (`system.name`, `system.location`)
  in the top bar, the TURN button ("HOLD TO ROTATE": hold it to turn the
  carousel, the spindle lock opens first, it stops on release and after
  `max_run_s` regardless), the caption "Select a shelf · scan to pay" and ten
  tiles, shelf 1 ("top") to 10 ("bottom"). The small gear in the top right
  corner opens the settings. While the purchase server cannot be reached,
  "Payment currently not possible" stands in amber at the bottom; the shelves
  can still be tapped.
- *Sleep*: after 60 s without a touch the screen goes black with a dim "Tap to
  wake"; any touch brings the shelves back.
- *Scan to pay*: the chosen shelf number, the QR code on a cream plate ("Scan
  with your phone" — "Pay in the app. The door unlocks by itself, nothing to
  press here."), a countdown from 2:00 and Cancel. While the purchase server
  is unreachable the plate reads "Please wait a moment. The payment server is
  not reachable." instead of the code; the countdown keeps running and the
  code appears as soon as the server answers again.
- *Door unlocked*: "Shelf N is unlocked — Open the door and take your
  product." with a countdown from 0:30; the drawing of the machine below shows
  that shelf's door in amber.
- *Take your product*: "Shelf N — Take your product — Then close the door.";
  the drawing shows the door open.
- *Close the door*: after 10 s with the door open the screen turns dark red:
  "Please close the door — The door has been open for a while."
- *Door forced*: the same red screen with "Door opened without a purchase —
  Close it. This event is recorded." when a door opens without a payment; it
  goes away when the door is closed.
- *Thank you*: "Shelf N — Thank you" for two seconds
  (`vending.timings.thank_you_s`), then back to the shelves.
- *Out of order*: "Out of order" with the gear in the corner. The line
  "Technical problem — please try again later." is added for a hardware fault
  only; while the switch in the settings is on, or after a lost report, the
  screen shows the two words alone.

While the daemon is not running, or the page has lost its connection to it,
the screen shows "Out of order" alone, without the gear; the page reconnects
by itself (after 1, 2, 5 and then every 10 seconds) and shows the current
screen as soon as the daemon is back. Reloading the page at any point shows
the current state again, countdown included.

Starting without `--mock` while `hardware.mode` is `real` refuses to run if
`config/local.yaml` is missing. With a `local.yaml` the daemon starts even if
a module is unreachable: the page shows "Out of order" with "Technical
problem — please try again later.", and `/api/status` lists both modules under
`hardware` with `connected` and `last_error`. The daemon reconnects every 2, 5, 10, then
30 seconds and returns to `idle` by itself once both modules have answered
and the door sensor has been reading for 10 seconds without a break. A module
that drops out during operation has the same effect. "Out of order" without
the second line means either that the Out of order switch in the settings
area is on (it never clears itself) or that a vend could not be recorded for
the server (the disk refused; this clears when someone opens the settings and
leaves them, or on a restart). The settings home screen names the reason
("OUT OF ORDER · HARDWARE / MAINTENANCE / DATABASE").

"Payment currently not possible" at the bottom of the shelf list, and the
plate with "The payment server is not reachable." instead of the QR code after
a shelf was selected, mean the last request to the Monitoni server failed. Check the network, then
`/api/status`: under `purchase_server`, `last_error` names the cause. `HTTP 401`
is a wrong or missing `purchase_server.token`; a connect error or timeout is
the network or the server. The messages go away with the next successful
request. "Queued reports" on the settings Network screen is the number of
`complete` and `close` reports the server has not accepted yet; they are
retried after 1, 2, 5, 15 and then every 60 seconds and survive a restart.

The LED strip and the sounds: when the daemon starts it logs `ArtNet to
<ip>:6454, universe 0, N pixels` and, after the first health poll, `WLED at
<ip> reachable`; `unreachable: <error>` means the controller does not answer
on its web port (check the IP, the cable, that WLED is up). The strip itself
should show all level zones in a dim warm colour in `idle`; if the controller
is reachable but the strip stays dark or shows WLED's own effect, the ArtNet
settings in §1 are wrong (most often "Receive UDP realtime" off or the DMX
mode not "Multi RGB"). In `/api/status`, `leds` shows `reachable`, the
current `pattern` and `level`; every change of reachability is a `network`
row `{"component": "wled", "reachable": …}` in the event log. For audio the
start log says `audio: 44100 Hz, 2 channel(s), sounds from …`; `audio unavailable,
sounds are off: <error>` means no audio device was found (on the Pi: check
that HDMI audio is enabled and the display or an amplifier is connected). A
dead strip or missing audio never stops the machine from vending.

On the Pi the daemon runs as a service (§9) whose unit sets `SDL_AUDIODRIVER=alsa`
and `AUDIODEV` to the HDMI output, so pygame goes straight to ALSA; Raspberry Pi
OS Lite has no PipeWire. The device name is what `aplay -L` lists on the machine
for the display's HDMI port (`vc4hdmi0` for HDMI0); `plughw:CARD=vc4hdmi0,DEV=0`
opens without error on vm001, but the sound has not been heard yet (the
display's audio jack is not reachable in its case), so the value in
`deploy/monitoni.service` is still marked as a placeholder.

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
on" in the confirmation → Exit. Customers now see "Out of order" and nothing
else; the switch stays on across restarts. To put it back:
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
scp monitoni@vm001.local:/opt/monitoni/data/monitoni.db ./monitoni-$(date +%F).db
```

(the machine's name from §2; the file can be opened with any SQLite tool,
table `events`). Copying while the daemon runs
is safe; the copy may miss the last second.

_Real hardware start on a Pi: to be written during integration._

## 8. Verification

Per-component tests from the settings area, and the first vend.

_To be written during integration._

## 9. Run as a service

_Performed on vm001 on 2026-10-07._

§5's `install.sh` put three services in place; once §6 is done they are
started by hand this once and come up by themselves at every boot from then
on:

```
sudo systemctl start monitoni
sudo systemctl start monitoni-kiosk
sudo systemctl start monitoni-kiosk-reload.timer
```

- `monitoni` — the daemon, `/opt/monitoni/.venv/bin/python -m monitoni` as
  the user `monitoni`, with `config/local.yaml` and the start options from
  `/etc/default/monitoni` (both §6). If it ever exits, systemd starts it
  again after 5 seconds. Its sound goes straight to the
  HDMI output (`deploy/monitoni.service` sets `SDL_AUDIODRIVER=alsa` and
  `AUDIODEV`).
- `monitoni-kiosk` — the display: cage with Chromium full screen on the Pi's
  first console (tty1), started after the daemon. `deploy/kiosk.sh` waits
  until the page answers at http://127.0.0.1:8080/, turns the picture by 180°
  (the panel is upside down; the touch follows through the rule from §5), then
  starts the browser as an app window (a normal browser window is at least
  500 px wide and would cut the 400 px page off) with a profile that is made
  fresh under `/run` at every start: nothing the browser saves survives, and
  the page loads nothing from the network. Also restarted after 5 seconds if
  it exits.
- `monitoni-kiosk-reload.timer` — restarts the kiosk every night at 04:00
  local time. The daemon is not touched; the fresh page connects and shows
  whatever state the machine is in.

After `start monitoni-kiosk` the display switches from the console text to
the "Select a shelf" screen within a few seconds. **Check on the display**:
the screen is upright, the ten shelves are centred with nothing cut off on
the right, and a tap lands under the finger: tap the gear in the top right
corner (the PIN screen opens), Cancel, then shelf 10 at the bottom (its QR
code screen opens), Cancel. A mouse arrow is shown while a keyboard with a
touchpad (the K400) is plugged in; touch does not move it. Unplug the
keyboard's receiver when the console is no longer needed.

**Looking at them:**

```
systemctl status monitoni monitoni-kiosk
systemctl list-timers monitoni-kiosk-reload.timer
```

`active (running)` is good; the timer line shows the next 04:00. The logs
(the journal; Ctrl+C leaves `-f`):

```
journalctl -u monitoni -f
journalctl -u monitoni -b
journalctl -u monitoni-kiosk -b
```

The first follows the daemon live, the second shows everything since this
boot, the third the same for the kiosk.

**After a change to `config/local.yaml`:**

```
sudo systemctl restart monitoni
```

The display shows "Out of order" for a moment while the daemon is away and
returns by itself. A mistake in the file shows up in
`journalctl -u monitoni -n 30` as `invalid configuration:` followed by the
key; the daemon keeps trying every 5 seconds until the file is fixed.

**Stopping the kiosk for debugging.** The daemon keeps running; only the
display program goes:

```
sudo systemctl stop monitoni-kiosk
```

The display goes black (no login prompt: tty1 is the kiosk's). For a login
prompt on the display with a keyboard plugged in: `sudo systemctl start
getty@tty1`. `sudo systemctl start monitoni-kiosk` brings the kiosk back and
takes tty1 over again. To keep the kiosk off across a reboot:
`sudo systemctl disable monitoni-kiosk` (and `enable` later).

**The nightly reload by hand:**

```
sudo systemctl start monitoni-kiosk-reload.service
```

The display goes black for a few seconds and comes back on the current
screen.

**The page from a laptop.** The daemon listens on the Pi itself only
(`web.host` is `127.0.0.1` on purpose: the page and its API are for the
display in front of the machine, there is no login on them). To see the same
page on a laptop, open an SSH tunnel and keep it open:

```
ssh -L 8080:127.0.0.1:8080 monitoni@vm001.local
```

then open http://127.0.0.1:8080/ in a browser on the laptop. It is a second
window onto the same daemon: it shows what the display shows, and taps on
either count. (If something on the laptop already uses port 8080, use
`-L 8081:127.0.0.1:8080` and open http://127.0.0.1:8081/.) Changing
`web.host` to reach the page without a tunnel is not supported.

**Stopping everything** (for work on the machine):

```
sudo systemctl stop monitoni-kiosk monitoni
```

`stop monitoni` stops the motor and darkens the LED strip on the way out;
`sudo systemctl start monitoni monitoni-kiosk` brings both back.

## 10. Troubleshooting

Common problems and how to check them.

_To be written during integration._
