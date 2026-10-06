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
# full screen, no bars, no gestures, nothing persists, nothing from the network; the exact set
# is checked on the machine in Milestone 8 Part B
exec cage -- chromium \
  --kiosk \
  --ozone-platform=wayland \
  --user-data-dir="$PROFILE" \
  --password-store=basic \
  --no-first-run --noerrdialogs --disable-infobars --disable-session-crashed-bubble \
  --disable-pinch --overscroll-history-navigation=0 \
  --disable-features=TranslateUI \
  --disable-component-update --disable-background-networking --disable-sync \
  --no-default-browser-check --disable-breakpad --metrics-recording-only \
  "$URL"
