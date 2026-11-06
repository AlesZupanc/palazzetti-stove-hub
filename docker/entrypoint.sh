#!/bin/sh
# docker/entrypoint.sh - starts the stove master in a container the way systemd starts it on
# the Pi (pi/stove-master.service): as the user "stove", with /etc/stove-master/stove.ini.
#
# A container has no setup wizard, so when /etc/stove-master/stove.ini does not exist yet it
# is written from these environment variables (compose.yaml sets the important ones). Every
# setting of pi/stove.ini.example has one; unset means the Pi's default:
#
#   STOVE_BOX_HOST             [stove]     box_host      fake-cbox:8081 in compose.yaml
#   STOVE_BOX_TIMEOUT                      timeout
#   STOVE_WEB_BIND             [web]       bind
#   STOVE_WEB_PORT                         port
#   STOVE_WEB_PUBLIC_PORT                  public_port
#   STOVE_TRUST_PROXY                      trust_proxy
#   STOVE_LAN_ONLY                         lan_only
#   STOVE_ALLOWED_HOSTS                    allowed_hosts
#   STOVE_LOCAL_TOKEN_FILE                 local_token_file
#   STOVE_MDNS_ENABLED         [mdns]      enabled
#   STOVE_MDNS_NAME                        name
#   STOVE_MDNS_SERVICE_NAME                service_name
#   STOVE_MDNS_IP                          ip
#   STOVE_CLOUD_URL            [cloud]     url
#   STOVE_OFF_GRACE_MINUTES    [scheduler] off_grace_minutes
#   STOVE_START_WAIT_MINUTES               start_wait_minutes
#   STOVE_WAIT_FOR_TIME_SYNC               wait_for_time_sync
#
# An existing stove.ini is kept, so you can bind-mount your own to /etc/stove-master/stove.ini
# or change it inside the container ("docker compose exec stove-master stove-master setup",
# then restart). The file is written again when the container is recreated, which is what
# "docker compose up" does after a change in compose.yaml.
set -eu

CONFIG=/etc/stove-master/stove.ini
DATA=/var/lib/stove-master

if [ "$(id -u)" = 0 ]; then
    # a fresh database volume may belong to root: give it to the service user, as install.sh does
    chown stove:stove "$DATA" 2>/dev/null || echo "warning: cannot chown $DATA (bind mount?)" >&2

    if [ ! -f "$CONFIG" ]; then
        python3 - "$CONFIG" <<'PY'
import os
import sys

sys.path.insert(0, "/opt/stove-master")
from stovemaster import config as cfgmod   # noqa: E402  (the same template the setup wizard writes)

ENV = {
    "STOVE_BOX_HOST": ("stove", "box_host"),
    "STOVE_BOX_TIMEOUT": ("stove", "timeout"),
    "STOVE_WEB_BIND": ("web", "bind"),
    "STOVE_WEB_PORT": ("web", "port"),
    "STOVE_WEB_PUBLIC_PORT": ("web", "public_port"),
    "STOVE_TRUST_PROXY": ("web", "trust_proxy"),
    "STOVE_LAN_ONLY": ("web", "lan_only"),
    "STOVE_ALLOWED_HOSTS": ("web", "allowed_hosts"),
    "STOVE_LOCAL_TOKEN_FILE": ("web", "local_token_file"),
    "STOVE_MDNS_ENABLED": ("mdns", "enabled"),
    "STOVE_MDNS_NAME": ("mdns", "name"),
    "STOVE_MDNS_SERVICE_NAME": ("mdns", "service_name"),
    "STOVE_MDNS_IP": ("mdns", "ip"),
    "STOVE_CLOUD_URL": ("cloud", "url"),
    "STOVE_OFF_GRACE_MINUTES": ("scheduler", "off_grace_minutes"),
    "STOVE_START_WAIT_MINUTES": ("scheduler", "start_wait_minutes"),
    "STOVE_WAIT_FOR_TIME_SYNC": ("scheduler", "wait_for_time_sync"),
}
values = {key: os.environ[var].strip() for var, key in ENV.items() if var in os.environ}
path = sys.argv[1]
with open(path + ".new", "w", encoding="utf-8") as f:
    f.write(cfgmod.render(values))
os.replace(path + ".new", path)
cfgmod.Config(path)   # fail now, with a clear message, rather than in the service
PY
        chown root:stove "$CONFIG"
        chmod 640 "$CONFIG"
        echo "wrote $CONFIG from the environment" >&2
    fi
fi

exec stove-master "$@"
