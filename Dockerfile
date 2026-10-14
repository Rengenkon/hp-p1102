FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive
ARG HP_PRINTER_GID=987

RUN apt-get update && apt-get install -y --no-install-recommends \
    cups cups-client hplip printer-driver-hpcups \
    dbus libusb-1.0-0 usbutils ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# The HP plug-in is copyrighted software. Supply your own two matching files.
COPY plugins/hplip-3.22.10-plugin.run /tmp/hplip-3.22.10-plugin.run
COPY plugins/hplip-3.22.10-plugin.run.asc /tmp/hplip-3.22.10-plugin.run.asc

# HPLIP from Debian Trixie is 3.22.10. Fail loudly if that changes.
RUN set -eu; \
    actual="$(dpkg-query -W -f='${Version}' hplip)"; \
    case "$actual" in 3.22.10*) ;; *) echo "Unexpected HPLIP version: $actual" >&2; exit 1;; esac; \
    printf 'y\n' | hp-plugin -i -p /tmp/hplip-3.22.10-plugin.run; \
    grep -Eq '^installed[[:space:]]*=[[:space:]]*1' /var/lib/hp/hplip.state; \
    grep -Eq '^version[[:space:]]*=[[:space:]]*3\.22\.10' /var/lib/hp/hplip.state; \
    rm -f /tmp/hplip-3.22.10-plugin.run /tmp/hplip-3.22.10-plugin.run.asc

# Match host udev group ID: CUPS runs standard backends as unprivileged lp.
RUN set -eu; \
    existing="$(getent group "$HP_PRINTER_GID" | cut -d: -f1 || true)"; \
    if [ -z "$existing" ]; then groupadd -g "$HP_PRINTER_GID" hpprinter; existing=hpprinter; fi; \
    usermod -aG "$existing" lp

# Debian defaults to localhost:631; accept connections over Docker's bridge.
RUN sed -i -E 's/^[[:space:]]*Listen[[:space:]]+localhost:631[[:space:]]*$/Port 631/' /etc/cups/cupsd.conf && \
    printf '\nServerAlias *\n' >> /etc/cups/cupsd.conf

COPY scripts/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh

EXPOSE 631
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
