#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! command -v docker >/dev/null; then
  echo 'Docker is not installed. Install Docker Engine + Compose plugin first.' >&2
  exit 1
fi

if ! getent group hpprinter >/dev/null; then
  echo 'Creating a dedicated hpprinter group on host'
  sudo groupadd --system hpprinter
fi
gid="$(getent group hpprinter | cut -d: -f3)"

# Only a numeric group ID is stored; Compose passes it as build argument.
printf 'HP_PRINTER_GID=%s\n' "$gid" > .env

sudo install -m 0644 udev/99-hp-p1102.rules /etc/udev/rules.d/99-hp-p1102.rules
sudo udevadm control --reload-rules

echo "Configured hpprinter with GID $gid."
echo 'Disconnect and reconnect HP LaserJet P1102 (or power-cycle it).'
echo 'Then check: lsusb -d 03f0:002a and ls -l /dev/bus/usb/BBB/DDD'
echo 'When group=hpprinter and mode=crw-rw----, run docker compose up -d --build.'
