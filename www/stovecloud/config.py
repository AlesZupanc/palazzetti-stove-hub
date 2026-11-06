"""Configuration of the stove website (INI), normally /etc/stove-cloud/cloud.ini.

Written by deploy.sh (stove_cloud.py init-config / setup-mail). Every key has a
default, so a missing key is fine.
"""

import configparser
import os
import re
import urllib.parse

DEFAULT_PATH = "/etc/stove-cloud/cloud.ini"

DEFAULTS = {
    "server": {
        "bind": "127.0.0.1",
        "port": "8471",
        "public_url": "",
        "trust_proxy": "yes",
    },
    "mail": {
        "smtp_host": "",
        "smtp_port": "587",
        "smtp_security": "starttls",
        "smtp_user": "",
        "smtp_password": "",
        "from_addr": "",
    },
    "accounts": {
        "allow_registration": "yes",
        "session_days": "60",
    },
    "limits": {
        "max_devices": "100",
        "push_hosts": "",
    },
    "paths": {
        "db": "/var/lib/stove-cloud/cloud.db",
        "app": "",
    },
}

TEMPLATE = """\
# Stove website configuration (www/ on the VPS).
# After editing:  sudo systemctl restart stove-cloud

[server]
# Where the service listens; nginx forwards https://<site>/<path>/ to it.
bind = @server.bind@
port = @server.port@
# The public address of the website, e.g. https://example.com/mystove/
public_url = @server.public_url@
# Take the visitor's address from nginx's X-Real-IP header (only from 127.0.0.1).
trust_proxy = @server.trust_proxy@

[mail]
# SMTP server for account requests, approvals and password resets.
# Gmail: smtp.gmail.com, port 587, starttls, your address + an app password.
# Empty smtp_host = mails are written to the log (journalctl -u stove-cloud).
smtp_host = @mail.smtp_host@
smtp_port = @mail.smtp_port@
# starttls | ssl | none
smtp_security = @mail.smtp_security@
smtp_user = @mail.smtp_user@
smtp_password = @mail.smtp_password@
# Sender address. Empty = smtp_user.
from_addr = @mail.from_addr@

[accounts]
# Show "Create an account" (new accounts still need an administrator's approval).
allow_registration = @accounts.allow_registration@
# Sign-ins expire after this many days without use.
session_days = @accounts.session_days@

[limits]
# Most hubs (Raspberry Pis) that may register with this website.
max_devices = @limits.max_devices@
# Extra push services allowed besides Google, Apple, Mozilla and Microsoft
# (comma separated host names).
push_hosts = @limits.push_hosts@

[paths]
db = @paths.db@
# The web app files. Empty = the app/ folder next to stove_cloud.py.
app = @paths.app@
"""


def render(values=None):
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
    def __init__(self, path=None):
        cp = configparser.ConfigParser(interpolation=None)
        cp.read_dict(DEFAULTS)
        if path:
            if not os.path.exists(path):
                raise FileNotFoundError(f"config file {path} not found (run: stove_cloud.py init-config)")
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
        self.bind = g("server", "bind").strip() or "127.0.0.1"
        self.port = int(g("server", "port"))
        url = g("server", "public_url").strip()
        if url and not re.match(r"^https?://[^/\s]+(/\S*)?$", url):
            raise ValueError("server.public_url must look like https://example.com/mystove/")
        self.public_url = (url.rstrip("/") + "/") if url else f"http://localhost:{self.port}/"
        u = urllib.parse.urlsplit(self.public_url)
        self.origin = f"{u.scheme}://{u.netloc}"
        self.prefix = u.path.rstrip("/")            # "/mystove", or "" at the site root
        self.trust_proxy = _bool(g("server", "trust_proxy"))

        self.smtp_host = g("mail", "smtp_host").strip()
        self.smtp_port = int(g("mail", "smtp_port"))
        self.smtp_security = g("mail", "smtp_security").strip().lower() or "starttls"
        if self.smtp_security not in ("starttls", "ssl", "none"):
            raise ValueError("mail.smtp_security must be starttls, ssl or none")
        self.smtp_user = g("mail", "smtp_user").strip()
        self.smtp_password = g("mail", "smtp_password")
        self.mail_from = g("mail", "from_addr").strip() or self.smtp_user

        self.allow_registration = _bool(g("accounts", "allow_registration"))
        self.session_days = max(1, int(g("accounts", "session_days")))

        self.max_devices = max(1, int(g("limits", "max_devices")))
        self.push_hosts = [h.strip().lower() for h in g("limits", "push_hosts").split(",") if h.strip()]

        self.db_path = g("paths", "db").strip()
        self.app_dir = g("paths", "app").strip()


def find_config(explicit=None):
    """--config, $STOVE_CLOUD_CONFIG, /etc/stove-cloud/cloud.ini, ./cloud.ini."""
    if explicit:
        return explicit
    env = os.environ.get("STOVE_CLOUD_CONFIG")
    if env:
        return env
    if os.path.exists(DEFAULT_PATH):
        return DEFAULT_PATH
    return "cloud.ini"
