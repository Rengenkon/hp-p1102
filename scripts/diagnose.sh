#!/usr/bin/env bash
set -u
cd "$(dirname "${BASH_SOURCE[0]}")/.."
echo '== USB ID =='
lsusb -d 03f0:002a || true
echo '== USB device permissions on host =='
for x in /dev/bus/usb/*/*; do
  [[ -e "$x" ]] || continue
  if udevadm info --query=property --name="$x" 2>/dev/null | grep -qx 'ID_VENDOR_ID=03f0'; then
    if udevadm info --query=property --name="$x" 2>/dev/null | grep -qx 'ID_MODEL_ID=002a'; then
      stat -c '%n %U:%G %a %t:%T' "$x"
    fi
  fi
done
echo '== CUPS =='
docker compose exec -T hplip lpstat -t || true
echo '== lp user groups =='
docker compose exec -T hplip id lp || true
echo '== Backend error messages =='
docker compose exec -T hplip sh -c "grep -Ei 'open device failed|stat=12|permission|plugin|backend' /var/log/cups/error_log | tail -n 35" || true
