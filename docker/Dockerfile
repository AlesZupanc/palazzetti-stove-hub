# docker/Dockerfile - the stove master as it runs on the Raspberry Pi, in a container.
#
# Two images from one file (compose.yaml at the top of the repository uses both):
#
#   hub    the stove master, laid out exactly as pi/install.sh lays it out on the Pi:
#          Debian bookworm (what Raspberry Pi OS is built on), the system python3 and
#          python3-cryptography from apt, the code in /opt/stove-master, the config in
#          /etc/stove-master/stove.ini, the database in /var/lib/stove-master, the command
#          "stove-master", and the service running as the system user "stove".
#          docker/entrypoint.sh stands in for systemd and for the setup wizard.
#   cbox   tools/fake_cbox.py - a simulated Palazzetti Connection Box with a stove behind
#          it (ignition and cool-down phases, UDP discovery, optional latency/failures).
#
#   docker build -f docker/Dockerfile --target hub  -t stove-master .
#   docker build -f docker/Dockerfile --target cbox -t fake-cbox .
#
# The base image exists for amd64, arm64 and armv7, so the same file also builds on a Pi.

FROM debian:bookworm-slim AS base
ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 tzdata \
 && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# hub: what "sudo pi/install.sh" does on the Pi
# ---------------------------------------------------------------------------
FROM base AS hub
# ca-certificates: Raspberry Pi OS has them, the slim image does not (wss:// to the website)
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3-cryptography ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --home-dir /var/lib/stove-master --no-create-home --shell /usr/sbin/nologin stove \
 && install -d -m 750 -o stove -g stove /var/lib/stove-master \
 && install -d -m 755 /etc/stove-master /opt/stove-master

# the code, as on the Pi (/opt/stove-master/app is www/app)
COPY palazzetti.py stove_master.py stove_web.py cbox_probe.py /opt/stove-master/
COPY stovemaster/ /opt/stove-master/stovemaster/
COPY www/app/ /opt/stove-master/app/
COPY docker/stove-master /usr/local/bin/stove-master
COPY docker/entrypoint.sh /usr/local/sbin/stove-master-entrypoint
# A checkout on Windows may have CRLF line endings: python does not mind, sh does.
RUN find /opt/stove-master -name __pycache__ -prune -exec rm -rf {} + \
 && chmod -R u=rwX,go=rX /opt/stove-master \
 && sed -i 's/\r$//' /usr/local/bin/stove-master /usr/local/sbin/stove-master-entrypoint \
 && chmod 755 /usr/local/bin/stove-master /usr/local/sbin/stove-master-entrypoint

VOLUME /var/lib/stove-master
EXPOSE 80/tcp 5353/udp
# the same check pi/install.sh does after starting the service
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c 'import configparser, urllib.request; c = configparser.ConfigParser(interpolation=None); c.read("/etc/stove-master/stove.ini"); urllib.request.urlopen("http://127.0.0.1:%s/api/health" % c.get("web", "port", fallback="80"), timeout=3)' || exit 1
ENTRYPOINT ["/usr/local/sbin/stove-master-entrypoint"]
CMD ["serve"]

# ---------------------------------------------------------------------------
# cbox: the simulated Connection Box and stove
# ---------------------------------------------------------------------------
FROM base AS cbox
COPY tools/fake_cbox.py /opt/fake-cbox/fake_cbox.py
RUN chmod 644 /opt/fake-cbox/fake_cbox.py
USER nobody
EXPOSE 8081/tcp 54549/udp
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD python3 -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8081/_fake/state", timeout=2)' || exit 1
ENTRYPOINT ["python3", "/opt/fake-cbox/fake_cbox.py", "--host", "0.0.0.0"]
CMD ["--port", "8081", "--speed", "10", "--latency", "0.3", "--udp"]
