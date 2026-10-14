ARG BASE_IMAGE
FROM ${BASE_IMAGE} AS common

ENV DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
ARG DEBIAN_SNAPSHOT
ARG APT_PACKAGES
ARG PLUGIN_VERSION
ARG PLUGIN_SHA256

# The dated repository fixes transitive dependencies as well as direct packages.
RUN rm -f /etc/apt/sources.list /etc/apt/sources.list.d/* && \
    printf 'deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/%s/ trixie main\n' "$DEBIAN_SNAPSHOT" > /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends $APT_PACKAGES && \
    rm -rf /var/lib/apt/lists/*

COPY plugins/hplip-${PLUGIN_VERSION}-plugin.run /tmp/plugin.run
# This exact archive is authenticated by the repository's pinned hash. Use HP's
# bundled installer offline, rather than hp-plugin's changing network/key endpoints.
RUN echo "$PLUGIN_SHA256  /tmp/plugin.run" | sha256sum -c - && \
    test "$(python3 -c 'import configparser; c=configparser.ConfigParser(); c.read("/etc/hp/hplip.conf"); print(c["hplip"]["version"])')" = "$PLUGIN_VERSION" && \
    sh /tmp/plugin.run --noexec --target /tmp/plugin && \
    mkdir -p /etc/sane.d && touch /etc/sane.d/dll.conf && \
    cd /tmp/plugin && python3 installPlugin.py && \
    python3 -c 'import configparser,sys; c=configparser.ConfigParser(); c.read("/var/lib/hp/hplip.state"); assert c["plugin"]["installed"] == "1"; assert c["plugin"]["version"] == sys.argv[1]' "$PLUGIN_VERSION" && \
    rm -rf /tmp/plugin /tmp/plugin.run

COPY scripts/runtime.py /usr/local/lib/printer/runtime.py
COPY scripts/entrypoint.py /usr/local/bin/entrypoint.py
COPY config/cupsd.conf /usr/local/share/printer/cupsd.conf
RUN sed -i -E 's|^#?ErrorLog .*|ErrorLog /dev/stderr|; s|^#?AccessLog .*|AccessLog /dev/stdout|; s|^#?PageLog .*|PageLog /dev/stdout|' /etc/cups/cups-files.conf && \
    cp /etc/cups/cups-files.conf /usr/local/share/printer/cups-files.conf
EXPOSE 631
ENTRYPOINT ["python3", "-u", "/usr/local/bin/entrypoint.py"]

FROM common AS local
ENV PRINTER_MODE=local

FROM common AS server
ARG SERVER_APT_PACKAGES
RUN apt-get update && apt-get install -y --no-install-recommends $SERVER_APT_PACKAGES && \
    rm -rf /var/lib/apt/lists/* && python3 -m venv /opt/proxy-venv
COPY requirements-proxy.lock /usr/local/share/printer/requirements-proxy.lock
RUN /opt/proxy-venv/bin/python -m pip install --no-cache-dir --require-hashes \
    -r /usr/local/share/printer/requirements-proxy.lock && /opt/proxy-venv/bin/python -m pip check
COPY scripts/ipp.py scripts/proxy.py /usr/local/lib/printer/
COPY config/test-print.test /usr/local/share/printer/test-print.test
ENV PRINTER_MODE=server
EXPOSE 8631
