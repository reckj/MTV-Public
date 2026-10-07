#!/bin/sh
# Puts the MoniToni services in place on the Pi. Run once, as root, from the clone in
# /opt/monitoni (docs/SETUP.md §5):
#   sudo /opt/monitoni/deploy/install.sh
# Safe to run again: every step looks before it acts and says what it did. In this order:
#   1. the user monitoni (if missing) and its groups video, input, render, audio
#   2. /etc/default/monitoni, the daemon's start options, from deploy/monitoni.default if absent
#   3. the four units copied into /etc/systemd/system, then systemctl daemon-reload
#   4. the touch rule copied into /etc/udev/rules.d (the panel is upside down), then applied
#   5. systemctl enable for the daemon, the kiosk and the nightly reload timer
# Nothing is started here; SETUP §7 and §9 do that. Every line is also written out in SETUP.
set -eu

DEPLOY=$(cd "$(dirname "$0")" && pwd)
if [ "$(id -u)" -ne 0 ]; then
  echo "run as root: sudo $0" >&2
  exit 1
fi
if [ "$DEPLOY" != /opt/monitoni/deploy ]; then
  echo "note: running from $DEPLOY; the units expect the clone at /opt/monitoni"
fi

# 1. The user. On a fresh card Imager created it (SETUP §2), so it normally exists. The
#    groups: video, input and render for cage (the display, the touch panel, the GPU), audio
#    for the daemon's HDMI sound. Which of them are really needed is checked in Part B.
if id monitoni >/dev/null 2>&1; then
  echo "user monitoni: exists"
else
  useradd --create-home --shell /bin/bash monitoni
  echo "user monitoni: created without a password (set one with: passwd monitoni)"
fi
for group in video input render audio; do
  if id -nG monitoni | tr ' ' '\n' | grep -qx "$group"; then
    echo "group $group: monitoni is a member"
  else
    usermod -aG "$group" monitoni
    echo "group $group: monitoni added"
  fi
done

# 2. The daemon's start options: a copy of deploy/monitoni.default, only if there is no file
#    yet. The installer edits it (SETUP §6), so a later run never touches it.
if [ -e /etc/default/monitoni ]; then
  echo "/etc/default/monitoni: exists, left as it is"
else
  install -m 644 "$DEPLOY/monitoni.default" /etc/default/monitoni
  echo "/etc/default/monitoni: created with MONITONI_OPTS empty (SETUP §6 says what to put there)"
fi

# 3. The units. A copy, not a link, so the running system never depends on the clone being
#    where it was; run this script again after a change to a unit file.
for unit in monitoni.service monitoni-kiosk.service \
            monitoni-kiosk-reload.service monitoni-kiosk-reload.timer; do
  if cmp -s "$DEPLOY/$unit" "/etc/systemd/system/$unit"; then
    echo "$unit: unchanged"
  else
    install -m 644 "$DEPLOY/$unit" /etc/systemd/system/
    echo "$unit: copied to /etc/systemd/system"
  fi
done
systemctl daemon-reload
echo "systemd: units reloaded"

# 4. The touch rule: turns the touch panel's coordinates by 180°, like the picture. Applied to
#    the panel at once; the kiosk reads it when it starts.
rule=99-monitoni-touch.rules
if cmp -s "$DEPLOY/$rule" "/etc/udev/rules.d/$rule"; then
  echo "$rule: unchanged"
else
  install -m 644 "$DEPLOY/$rule" /etc/udev/rules.d/
  echo "$rule: copied to /etc/udev/rules.d"
fi
udevadm control --reload
udevadm trigger --subsystem-match=input --action=change
echo "udev: rules applied"

# 5. Start at boot. `enable` is quiet when the links exist already.
systemctl enable monitoni.service monitoni-kiosk.service monitoni-kiosk-reload.timer
echo "enabled at boot: monitoni, monitoni-kiosk, monitoni-kiosk-reload.timer"
echo "start them now with: sudo systemctl start monitoni monitoni-kiosk monitoni-kiosk-reload.timer"
