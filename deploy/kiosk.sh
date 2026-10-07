#!/bin/sh
# The kiosk browser: waits for the daemon's page, then runs Chromium full screen under cage.
# Started by monitoni-kiosk.service, which provides tty1, the seat and RUNTIME_DIRECTORY; it
# is not meant to be run by hand. The profile is created fresh under /run at every start, so
# nothing Chromium writes survives, and the page itself loads nothing from the network.
set -eu

URL=http://127.0.0.1:8080/
PROFILE="${RUNTIME_DIRECTORY:?set by monitoni-kiosk.service}/chromium"

echo "waiting for $URL"
until curl -fs -o /dev/null "$URL"; do
  sleep 1
done

mkdir -p "$PROFILE"
# The panel is mounted upside down in the machine: inside cage, wlr-randr turns the output by
# 180° before Chromium starts (the touch follows through deploy/99-monitoni-touch.rules).
# Chromium: full screen, no bars, no gestures, nothing persists, nothing from the network.
# --app: a normal browser window is at least 500 px wide, wider than the 400 px panel, and
# would cut the page off on the right; an app window takes the panel's width.
exec cage -- /bin/sh -c 'wlr-randr --output HDMI-A-1 --transform 180 && exec "$@"' sh chromium \
  --kiosk \
  --ozone-platform=wayland \
  --user-data-dir="$PROFILE" \
  --password-store=basic \
  --no-first-run --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-pinch --overscroll-history-navigation=0 \
  --disable-features=TranslateUI \
  --disable-component-update --disable-background-networking --disable-sync \
  --no-default-browser-check --disable-breakpad --metrics-recording-only \
  --app="$URL"
