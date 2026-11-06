"""The local API: plain URLs on the home network for Siri Shortcuts, Home Assistant, scripts.

  GET http://stove.local/api/state                        what the stove does (no token needed)
  GET http://stove.local/api/control?token=TOKEN&state=on&temp=23.5&fan=3&level=4
  GET http://stove.local/api/command?token=TOKEN&text=turn on and set the temperature to 23
      a sentence, e.g. dictated to a Siri shortcut (see voice.py for what it understands)

Parameters of /api/control (any combination, at least one):
  state  on | off  (also power=on/off, 1/0, true/false)
  temp   target room temperature in °C (also setpoint, temperature), 5-40
  level  fire power 1-5 (also power=1..5)
  fan    0 (off) .. 5, high, auto (also 6 = high, 7 = auto)
Settings are sent before switching on, so "state=on&temp=22" lights it with 22 °C.
Add format=text for a sentence instead of JSON (Siri can speak it). GET and POST both work;
the secret token may also come as header X-Stove-Token or "Authorization: Bearer TOKEN"
(key= and X-Stove-Key, the first names, still work).

Why a secret token: without it, any web page opened on a phone or PC at home could switch the
stove on with a hidden link (browsers send such GET requests without asking). The token is
shown on the local dashboard (Online -> API) and with: sudo stove-master api-token.

Programs on the Pi itself (scripts, other services) have a token of their own, the local token:
a file that pi/install.sh writes once ([web] local_token_file). It is never shown and "Make a new
secret token" does not change it, and it only counts for connections from 127.0.0.1 that did not
come through a proxy (lan.py checks that), so it cannot leak to or be used from the home network.
The header X-Stove-Actor (optional) says in the Activity log who made the call.
"""

import email.parser
import email.policy
import hmac
import json
import logging
import os
import secrets
import threading
import urllib.parse

from .stove import fan_text

ON = {"on", "1", "true", "yes", "start"}
OFF = {"off", "0", "false", "no", "stop"}
FAN_WORDS = {"off": 0, "high": 6, "max": 6, "auto": 7}
TOKEN_SETTING = "url_api_key"      # its first name; kept so that existing links keep working
LOCAL_ACTOR = "Local program"      # Activity shows calls with the local token as this
ACTOR_MAX = 40

log = logging.getLogger("urlapi")


def api_token(db):
    token = db.get_setting(TOKEN_SETTING)
    if not token:
        token = new_api_token(db)
    return token


def new_api_token(db):
    token = secrets.token_urlsafe(18)
    db.set_setting(TOKEN_SETTING, token)
    return token


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def parse_commands(params):
    """Query parameters -> [(action, value)] in the order they should be sent (raises APIError)."""
    p = {k.lower(): v.strip().lower() for k, v in params.items()}
    known = {"token", "key", "format", "state", "power", "temp", "temperature", "setpoint", "level", "fan"}
    unknown = sorted(set(p) - known)
    if unknown:
        raise APIError(400, f"unknown parameter {unknown[0]!r} (use state, temp, level, fan)")
    switch = None
    level = p.get("level")
    for name in ("state", "power"):
        if name in p:
            v = p[name]
            if v in ON:
                switch = "on"
            elif v in OFF:
                switch = "off"
            elif name == "power" and v.isdigit():
                level = v
            else:
                raise APIError(400, f"{name} must be on or off")
    out = []
    temp = p.get("temp") or p.get("temperature") or p.get("setpoint")
    if temp:
        out.append(("setpoint", temp.replace(",", ".")))
    if level:
        out.append(("power", level))
    if "fan" in p:
        fan = p["fan"]
        out.append(("fan", FAN_WORDS.get(fan, fan)))
    if switch:
        out.append((switch, None))
    if not out:
        raise APIError(400, "nothing to do: give state, temp, level or fan")
    return out


def given_token(params, headers):
    given = (params.get("token") or params.get("key") or headers.get("X-Stove-Token")
             or headers.get("X-Stove-Key") or "")
    auth = headers.get("Authorization") or ""
    if not given and auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    return given


def token_ok(db, params, headers):
    given = given_token(params, headers)
    return bool(given) and hmac.compare_digest(given.encode(), api_token(db).encode())


class LocalToken:
    """The local token: read from its file when first needed and again whenever the file changes
    (a reinstall may write a new one), so the hub never needs a restart for it. No file, an
    unreadable one or a token shorter than 16 characters = no local token."""

    MIN_LENGTH = 16

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._stamp = None
        self._token = ""

    def get(self):
        if not self.path:
            return ""
        try:
            st = os.stat(self.path)
        except OSError:
            return ""
        # ctime too: chmod/chown change only that, and may make an unreadable file readable
        stamp = (st.st_mtime_ns, st.st_ctime_ns, st.st_size, st.st_ino)
        with self._lock:
            if stamp != self._stamp:
                try:
                    with open(self.path, encoding="utf-8") as f:
                        token = f.read().strip()
                except (OSError, ValueError) as e:
                    log.warning("cannot read the local API token %s: %s", self.path, e)
                    token = ""
                self._token = token if len(token) >= self.MIN_LENGTH else ""
                self._stamp = stamp
            return self._token

    def matches(self, params, headers):
        token = self.get()
        given = given_token(params, headers)
        return bool(token and given) and hmac.compare_digest(given.encode(), token.encode())


def local_actor(headers):
    """'Local program', or 'Local program (cron job)' with the X-Stove-Actor header (printable, <= 40 characters)."""
    text = " ".join("".join(c for c in headers.get("X-Stove-Actor") or "" if c.isprintable()).split())
    text = text[:ACTOR_MAX].strip()
    return f"{LOCAL_ACTOR} ({text})" if text else LOCAL_ACTOR


def room_temp(state, probes):
    temps = state.get("temps") or {}
    room = probes.get("room") or ("T1" if "T1" in temps else None)
    return temps.get(room) if room else None


def describe_state(state, probes, name="The stove"):
    """One sentence for Siri."""
    if state.get("error"):
        return f"{name} cannot be reached: {state['error']}."
    phase = state.get("phase")
    if state.get("alarm"):
        text = f"{name} reports an alarm: {state.get('status_text')}."
    elif phase == "off":
        text = f"{name} is off."
    elif phase == "shutdown":
        text = f"{name} is cooling down."
    else:
        text = f"{name} is on ({str(state.get('status_text') or 'burning').lower()}), level {state.get('power')}, " \
               f"fan {fan_text(state.get('fan')).lower()}."
    room = room_temp(state, probes)
    if room is not None:
        text += f" Room {room:g} degrees"
        text += f", target {state['setpoint']:g}." if state.get("setpoint") is not None else "."
    elif state.get("setpoint") is not None:
        text += f" Target {state['setpoint']:g} degrees."
    tank = (state.get("pellets") or {}).get("tank")
    if tank:
        if tank.get("empty"):
            text += " The pellet tank is empty."
        else:
            text += f" Pellets: about {tank['left']:g} kilos left, {tank['pct']} percent of the tank."
    return text


def describe_done(commands):
    parts = []
    for action, value in commands:
        if action == "on":
            parts.append("switching on")
        elif action == "off":
            parts.append("switching off")
        elif action == "setpoint":
            parts.append(f"target {float(value):g} degrees")
        elif action == "power":
            parts.append(f"level {value}")
        elif action == "fan":
            parts.append(f"fan {fan_text(value).lower()}")
    return "OK, " + ", ".join(parts) + "."


def render(payload, text, fmt):
    if fmt == "text":
        return "text/plain; charset=utf-8", text
    return "application/json", json.dumps(dict(payload, say=text), default=str)


def query(path_qs, body=b"", ctype=""):
    """Parameters from the URL and, for a POST, a form or JSON body."""
    params = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(path_qs).query, keep_blank_values=False))
    if body:
        if "json" in ctype:
            try:
                data = json.loads(body)
            except ValueError:
                raise APIError(400, "the body is not JSON") from None
            if isinstance(data, dict):
                params.update({str(k): str(v) for k, v in data.items()})
        elif "multipart/form-data" in ctype:
            msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                b"Content-Type: " + ctype.encode("latin-1", "replace") + b"\r\n\r\n" + body)
            for part in msg.iter_parts() if msg.is_multipart() else ():
                name = part.get_param("name", header="content-disposition")
                if name and not part.get_filename():
                    params[name] = part.get_payload(decode=True).decode("utf-8", "replace")
        else:
            params.update(urllib.parse.parse_qsl(body.decode("utf-8", "replace")))
    return params
