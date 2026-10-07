#!/bin/sh
# On any computer with internet (the laptop, or the Pi itself): download the pinned runtime
# packages as wheels for the Pi (64-bit ARM, Python 3.13) into wheels/ at the repo root, to install on a machine
# without internet from a USB stick (docs/SETUP.md §5). Needs the repo's .venv (make install).
# wheels/ is the script's own output (gitignored) and is emptied first.
#
# Compiled packages ship wheels tagged with the oldest glibc they need, and pip matches only
# the tags that are listed here (it does not expand them): manylinux2014 (glibc 2.17) for
# most, manylinux_2_28 for Pillow. Raspberry Pi OS Trixie has glibc 2.41, so both run there.
set -eu
cd "$(dirname "$0")/.."

rm -rf wheels
.venv/bin/pip download -r requirements.txt -d wheels --only-binary=:all: \
  --platform manylinux2014_aarch64 --platform manylinux_2_28_aarch64 \
  --python-version 3.13 --implementation cp
echo "wheels/: $(ls wheels | wc -l | tr -d ' ') files, $(du -sh wheels | cut -f1). Copy the folder to the stick."
