# HP LaserJet P1102 — reproducible Docker setup

This repository captures the working configuration established on **2026-10-14**:

- Printer: HP LaserJet Professional P1102, USB `03f0:002a`.
- Host OS in the original debugging session: Arch Linux; container: Debian 13 (Trixie).
- HPLIP: `3.22.10`; required proprietary HP plugin: `3.22.10`.
- CUPS queue: `HP_LaserJet_Professional_P1102`.
- Device URI: `hp:/usb/HP_LaserJet_Professional_P1102?serial=000000000Q8D61SNPR1a`.
- PPD: `drv:///hpcups.drv/hp-laserjet_professional_p1102.ppd`.

**Root cause of printing failure:** CUPS accepted jobs and completed rendering, but HPLIP's backend logged `open device failed stat=12` every 30 seconds. The printer was visible via `lsusb` and `hp-info`, but printing only started after granting read/write access to the USB device. Therefore access control is essential, not optional.

## New host / after reinstall

Requirements: Linux with udev, Docker Engine (rootful), Docker Compose v2 plugin, `sudo`, `lsusb` (optional for diagnosis). Rootless Docker is not covered by this setup.

1. Clone/copy this directory to the new system.
2. **Locally** obtain the matching files and put them in `plugins/`:
   - `hplip-3.22.10-plugin.run`
   - `hplip-3.22.10-plugin.run.asc`

   HP's endpoint returned **HTTP 403** in the debugging session. An alternate source is:
   `https://www.openprinting.org/download/printdriver/auxfiles/HP/plugins/`

   Check the authenticity of these files using an independently trusted signing key. The build uses HPLIP's own plugin installer, but do not treat downloading an `.asc` from the same server as independent trust verification. The HP license must be reviewed/accepted before running the automated installation. Keep the proprietary files out of public Git repositories; `.gitignore` excludes them.

3. Set up host USB group + udev rule:

   ```bash
   ./scripts/setup-host.sh
   ```

   This creates `hpprinter` (only if absent), records **its actual GID** in `.env` for Docker build, and installs `/etc/udev/rules.d/99-hp-p1102.rules`.

4. Disconnect/reconnect the printer (or power-cycle), then find its **current** bus/device number:

   ```bash
   lsusb -d 03f0:002a
   # e.g. Bus 001 Device 016, so:
   ls -l /dev/bus/usb/001/016
   ```

   Expected mode/group: `crw-rw---- root hpprinter`. The `/001/016` number is illustrative and **will change** after USB re-enumeration.

5. Build/start:

   ```bash
   docker compose up -d --build
   docker compose logs -f hplip
   ```

   `Dockerfile` checks for the expected HPLIP version. If Debian updates to a different upstream HPLIP version, the build **fails on purpose** rather than silently mixing a new HPLIP with an old plugin. Pin the Debian base/packages for long-term reproducibility if you maintain this for years.

6. Verify and print:

   ```bash
   docker compose exec hplip lpstat -t
   docker compose exec hplip hp-info -i
   docker compose exec hplip lp -d HP_LaserJet_Professional_P1102 /etc/hosts
   ```

7. For graphical applications on the **host**, configure a printer using IPP URL:

   `ipp://localhost:8631/printers/HP_LaserJet_Professional_P1102`

   Docker exposes CUPS on **host loopback only**. The host may still need a print client / CUPS or compatible IPP stack, but **not HPLIP**. If connecting from another computer, you must deliberately configure access and authentication; do not just expose port 631 publicly.

## How it works

- `udev/99-hp-p1102.rules`: sets the P1102 USB node to `root:hpprinter`, mode `0660`. No world-write permission.
- `scripts/setup-host.sh`: creates the host group, writes its numeric ID into `.env`, and installs the udev rule.
- `Dockerfile`: packages CUPS, HPLIP, hpcups PPD driver, matching HP plugin, system D-Bus, and adds the container's `lp` account to the **same numeric GID**.
- `compose.yaml`: mounts the USB bus (to survive hotplug/changed device numbers) and allows USB character device major 189 in the cgroup. No `privileged: true`.
- `scripts/entrypoint.sh`: starts D-Bus and CUPS, creates the queue only if absent (idempotently), enables it, sets it as default.
- Docker named volumes persist CUPS settings and spool between container recreations. The plugin is baked into the **image**, not hidden in a volatile writable container layer.

**Security limitation:** The container sees `/dev/bus/usb` and its cgroup rule permits major 189, i.e. other USB devices too. Ordinary DAC permissions still apply, but it is not device-level isolation. On a shared/high-trust system you may want stricter per-device mediation. `group_add` alone is not sufficient to guarantee permissions of a CUPS backend; the Dockerfile explicitly adds `lp` to the host-matching group.

## Recovery / useful commands

```bash
./scripts/diagnose.sh

docker compose logs --tail=100 hplip
docker compose exec hplip lpstat -t
docker compose exec hplip hp-check -r

docker compose exec hplip cupsctl --debug-logging
docker compose exec hplip tail -n 100 /var/log/cups/error_log
```

- **Jobs queued but no paper:** look for `open device failed stat=12` and check that the host USB node is `root:hpprinter` `0660`, then compare `id lp` inside the container against `.env`'s GID. This was the actual fix in the initial debugging session.
- **Printer changed `/001/009` to `/001/016`:** normal; never hardcode bus/device numbers.
- **`hp-setup` D-Bus traceback:** the first manual setup added a valid CUPS queue, then tried to notify the nonexistent system bus. This repo starts D-Bus and uses `lpadmin` directly; interactive `hp-setup` is not needed.
- **HPLIP `hp-check` says 21 missing dependencies:** its 3.22.10 Debian 13 detection is outdated. Diagnose actual printing; do not add all development/GUI libraries blindly.
- **Plugin says network unavailable / HP HTTP 403:** supply the two plugin files locally. Check exact version.
- **Printer USB ID changed:** the udev rule matches `03f0:002a` independently of bus and device numbers.
- **No default printer in host applications:** add the IPP URL above to host printer settings. Container queue does not automatically register itself on every host.

### Backups

The two named volumes hold the CUPS queue/settings and spool. A new host can recreate the queue automatically from the entrypoint, so volume restoration is **not required** for this specific printer. If you customized CUPS settings, back up the volumes before reinstalling. **Do not use `docker compose down -v` unless you intend to erase the saved settings/spool.**

### Important caveats

- The provided Dockerfile has **not been executed against your physical printer or built in this sandbox**; it is based on the successful manual configuration and official package behavior. The one part to verify on the first build is unattended license/plugin installation (`printf 'y\n' | hp-plugin -i -p ...`). If that installer fails, review its output and install in an interactive container, then adjust the Docker build rather than blindly bypassing signature checks.
- The USB permissions change has been tested in the conversation only with a temporary `chmod 666`; the narrower `udev` + `hpprinter` group policy still needs one test print after reconnecting the printer.
- If the printer is replaced by a different unit, update `PRINTER_URI` to its new serial. The queue is only created if absent, so either edit the existing queue with `lpadmin -v` or remove it first after backing up settings.
- Some host-level USB services (`ipp-usb`, `usblp`) can compete for the device. Investigate only if logs suggest a conflict; do not disable services preemptively.
