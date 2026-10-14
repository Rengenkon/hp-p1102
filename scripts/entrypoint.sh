#!/bin/sh
set -eu

mkdir -p /run/dbus /run/cups
# System D-Bus is used by HPLIP's helper programs; no systemd required.
dbus-uuidgen --ensure=/etc/machine-id
dbus-daemon --system --fork

/usr/sbin/cupsd -f &
cups_pid=$!
trap 'kill "$cups_pid" 2>/dev/null || true; wait "$cups_pid" 2>/dev/null || true' TERM INT

attempt=0
until lpstat -r 2>/dev/null | grep -q 'scheduler is running'; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 30 ]; then echo 'CUPS did not start' >&2; exit 1; fi
  sleep 1
done

name="${PRINTER_NAME:-HP_LaserJet_Professional_P1102}"
uri="${PRINTER_URI:?PRINTER_URI is required}"
model="${PRINTER_MODEL:-drv:///hpcups.drv/hp-laserjet_professional_p1102.ppd}"

# Idempotent: do not overwrite any existing queue or printer preferences.
if ! lpstat -p "$name" >/dev/null 2>&1; then
  echo "Creating CUPS queue $name for $uri"
  lpadmin -p "$name" -E -v "$uri" -m "$model"
fi
cupsaccept "$name"
cupsenable "$name"
lpadmin -d "$name"

wait "$cups_pid"
