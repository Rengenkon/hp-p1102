
FROM debian:trixie-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      cups \
      cups-client \
      hplip \
      printer-driver-hpcups \
      libsane-hpaio \
      sane-utils \
      usbutils \
      dbus \
      ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# Разрешаем CUPS принимать IPP-соединения
# из Docker-сети.
RUN sed -i 's/^Listen localhost:631$/Port 631/' \
      /etc/cups/cupsd.conf && \
    echo 'ServerAlias *' >> /etc/cups/cupsd.conf

EXPOSE 631

CMD ["/usr/sbin/cupsd", "-f"]
