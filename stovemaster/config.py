"""Configuration file (INI) of the stove master.

The file is normally /etc/stove-master/stove.ini and is written by
``stove_master.py setup``. Every key has a default, so a missing file or a
missing key is fine.
"""

import configparser
import os
import re

DEFAULT_PATH = "/etc/stove-master/stove.ini"

DEFAULTS = {
    "stove": {
        "box_host": "",
        "timeout": "15",
    },
    "web": {
        "bind": "0.0.0.0",
        "port": "80",
        "public_port": "",
        "trust_proxy": "no",
        "lan_only": "yes",
        "allowed_hosts": "",
        "local_token_file": "/etc/stove-master/local-api-token",
    },
    "mdns": {
        "enabled": "yes",
        "name": "stove",
        "service_name": "Palazzetti stove",
        "ip": "",
    },
    "cloud": {
        "url": "",
    },
    "scheduler": {
        "off_grace_minutes": "120",
        "start_wait_minutes": "30",
        "wait_for_time_sync": "yes",
    },
    "paths": {
        "db": "/var/lib/stove-master/stove.db",
    },
}

# Written by "stove_master.py setup"; pi/stove.ini.example is this template
# rendered with the defaults (a test keeps the two in sync).
TEMPLATE = """\
# Stove master configuration.
# After editing:  sudo systemctl restart stove-master

[stove]
# IP address (or host:port) of the Palazzetti Connection Box.
# Leave empty to find it automatically (UDP discovery, then a /24 scan).
box_host = @stove.box_host@
# Seconds to wait for one answer from the box.
timeout = @stove.timeout@

[web]
# The local dashboard: http://stove.local/ (port 80) - no login, home network only.
bind = @web.bind@
port = @web.port@
# The port in the addresses the dashboard shows, when something in front of
# the hub forwards another port to it. Empty = the same as port. With your
# own reverse proxy (e.g. nginx) on port 80 of the Pi in front of the hub:
# bind = 127.0.0.1, port = 8080, public_port = 80, trust_proxy = yes.
public_port = @web.public_port@
# Take the visitor's address from the X-Real-IP header that a reverse proxy
# on this Pi sets (only for connections from 127.0.0.1). Only with a proxy in
# front that always sets it (nginx: proxy_set_header X-Real-IP $remote_addr).
trust_proxy = @web.trust_proxy@
# Answer only clients with a private address (192.168.x.x, 10.x.x.x, ...).
lan_only = @web.lan_only@
# Extra host names the dashboard may be opened with (comma separated), for
# example a name your router gives the Pi. stove.local, the Pi's host name
# and IP addresses always work.
allowed_hosts = @web.allowed_hosts@
# Secret of the local API for programs on the Pi itself (scripts, other
# services; root and the stove group can read the file), written by
# pi/install.sh. Only accepted from 127.0.0.1 directly, never through a
# proxy; not the token shown on the dashboard. Empty or no file = off.
local_token_file = @web.local_token_file@

[mdns]
# Built-in mDNS responder: makes http://<name>.local/ work on the LAN.
enabled = @mdns.enabled@
name = @mdns.name@
# Name shown in service browsers (DNS-SD, _http._tcp).
service_name = @mdns.service_name@
# Fixed IPv4 to advertise. Empty = the address of the interface that
# received the query (recommended).
ip = @mdns.ip@

[cloud]
# Remote access: the address of the website on your VPS (www/ in the
# repository), e.g. https://example.com/mystove/ . The hub keeps a WebSocket
# open to it; add the stove to your online account with the token shown on
# the local dashboard (Online). Empty = home network only.
url = @cloud.url@

[scheduler]
# If the hub was down when a slot ended, it still switches the stove off
# when it comes back, but only within this many minutes after the end.
off_grace_minutes = @scheduler.off_grace_minutes@
# How long a slot start may wait for the stove (cooling down, alarm,
# box unreachable) before the scheduler gives up on that slot.
start_wait_minutes = @scheduler.start_wait_minutes@
# The Pi has no real-time clock: do not act on the schedule before the
# system clock is synchronised (NTP). Gives up waiting after 10 minutes.
wait_for_time_sync = @scheduler.wait_for_time_sync@

[paths]
db = @paths.db@
"""


def render(values=None):
    """Return the config file text with defaults overridden by values {(section, key): value}."""
    values = values or {}
    text = TEMPLATE
    for section, keys in DEFAULTS.items():
        for key, default in keys.items():
            value = str(values.get((section, key), default))
            if "\n" in value or "\r" in value:
                raise ValueError(f"{section}.{key}: value must be a single line")
            text = text.replace(f"@{section}.{key}@", value)
    return "\n".join(line.rstrip() for line in text.split("\n"))


def _bool(value):
    v = str(value).strip().lower()
    if v in ("1", "yes", "true", "on"):
        return True
    if v in ("0", "no", "false", "off", ""):
        return False
    raise ValueError(f"not a boolean: {value!r}")


class Config:
    """Typed view of the INI file."""

    def __init__(self, path=None):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_dict(DEFAULTS)
        if path:
            if not os.path.exists(path):
                raise FileNotFoundError(f"config file {path} not found (run: stove_master.py setup)")
            with open(path, encoding="utf-8") as f:
                cp.read_file(f)
        self.path = path
        self.raw = cp
        try:
            self._load(cp)
        except ValueError as e:
            raise ValueError(f"{path or 'config'}: {e}") from None

    def _load(self, cp):
        g = cp.get
        self.box_host = g("stove", "box_host").strip()
        self.box_timeout = float(g("stove", "timeout"))

        self.bind = g("web", "bind").strip() or "0.0.0.0"
        self.port = int(g("web", "port"))
        public = g("web", "public_port").strip()
        self.public_port_setting = int(public) if public else 0
        if not 0 <= self.public_port_setting < 65536:
            raise ValueError("web.public_port must be a port number (or empty)")
        self.trust_proxy = _bool(g("web", "trust_proxy"))
        self.lan_only = _bool(g("web", "lan_only"))
        self.allowed_hosts = [h.strip().lower().rstrip(".") for h in g("web", "allowed_hosts").split(",") if h.strip()]
        self.local_token_file = g("web", "local_token_file").strip()

        self.mdns_enabled = _bool(g("mdns", "enabled"))
        self.mdns_name = g("mdns", "name").strip().lower().rstrip(".") or "stove"
        if self.mdns_name.endswith(".local"):
            self.mdns_name = self.mdns_name[:-6]
        self.mdns_service = g("mdns", "service_name").strip() or "Palazzetti stove"
        self.mdns_ip = g("mdns", "ip").strip()

        url = g("cloud", "url").strip()
        if url and not re.match(r"^https?://[^/\s]+(/\S*)?$", url):
            raise ValueError("cloud.url must look like https://example.com/mystove/")
        self.cloud_url = url.rstrip("/") + "/" if url else ""

        self.off_grace_minutes = max(0, int(g("scheduler", "off_grace_minutes")))
        self.start_wait_minutes = max(1, int(g("scheduler", "start_wait_minutes")))
        self.wait_for_time_sync = _bool(g("scheduler", "wait_for_time_sync"))

        self.db_path = g("paths", "db").strip()

    @property
    def public_port(self):
        """The port browsers use: [web] public_port, else the port the hub listens on."""
        return self.public_port_setting or self.port


def find_config(explicit=None):
    """Config path: --config, $STOVE_MASTER_CONFIG, /etc/stove-master/stove.ini, ./stove.ini."""
    if explicit:
        return explicit
    env = os.environ.get("STOVE_MASTER_CONFIG")
    if env:
        return env
    if os.path.exists(DEFAULT_PATH):
        return DEFAULT_PATH
    return "stove.ini"
