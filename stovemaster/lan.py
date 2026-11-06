"""The hub's web server on the home network: app files, /env.json and ws:// /ws.

There is no login at home (docs/protocol.md, section 2). Instead every request
must come from the home network, name the hub by an address it really has
(no DNS rebinding) and, for the WebSocket, come from a page of the hub itself
(no cross-site WebSocket hijacking).

With a reverse proxy on the Pi in front of the hub (say nginx on port 80 and
the hub on 127.0.0.1:8080, [web] trust_proxy) every connection comes from
127.0.0.1, and the visitor's address is the X-Real-IP header the proxy sets.
"""

import hashlib
import ipaddress
import json
import logging
import mimetypes
import os
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, urlapi, voice, ws
from .api import Client

log = logging.getLogger("lan")

MAX_LAN_SOCKETS = 40
CGNAT = ipaddress.ip_network("100.64.0.0/10")    # Tailscale and carrier-grade NAT
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".webmanifest": "application/manifest+json",
    ".json": "application/json",
}
SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("X-Frame-Options", "DENY"),
)


def find_app_dir():
    """The web app files: <install>/app on the Pi, www/app in the repository."""
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (os.environ.get("STOVE_APP_DIR"), os.path.join(here, "..", "app"),
                 os.path.join(here, "..", "www", "app")):
        if cand and os.path.isfile(os.path.join(cand, "index.html")):
            return os.path.abspath(cand)
    raise FileNotFoundError("the web app files (www/app) were not found")


def load_app_files(app_dir):
    """{url path: (body, etag, content type)} for every file of the web app."""
    files = {}
    for folder, dirs, names in os.walk(app_dir):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            ext = os.path.splitext(name)[1]
            if name.startswith(".") or ext not in CONTENT_TYPES:
                continue
            path = os.path.join(folder, name)
            with open(path, "rb") as f:
                body = f.read()
            url = "/" + os.path.relpath(path, app_dir).replace(os.sep, "/")
            etag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
            files[url] = (body, etag, CONTENT_TYPES.get(ext) or mimetypes.guess_type(name)[0])
    files["/"] = files["/index.html"]
    return files


def csp(ws_origin):
    return ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
            f"connect-src 'self' {ws_origin}; manifest-src 'self'; worker-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def is_loopback(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_loopback


def ip_allowed(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_private or a.is_loopback or a.is_link_local or (a.version == 4 and a in CGNAT)


def split_host(host_header):
    """'stove.local:8080' -> 'stove.local'; '[fe80::1]:80' -> 'fe80::1'."""
    host = (host_header or "").strip().lower()
    if host.startswith("["):
        return host[1:host.find("]")] if "]" in host else ""
    if host.count(":") == 1:
        host = host.split(":")[0]
    return host.rstrip(".")


class LanServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False      # open WebSockets must not keep the service from stopping

    def __init__(self, hub, cfg, app_dir=None):
        self.hub = hub
        self.cfg = cfg
        self.app_dir = app_dir or find_app_dir()
        self.files = load_app_files(self.app_dir)
        self.sockets = 0
        self.sockets_lock = threading.Lock()
        hostname = socket.gethostname().lower()
        self.names = {f"{cfg.mdns_name}.local", "localhost", hostname, hostname + ".local"} | set(cfg.allowed_hosts)
        self.local_token = urlapi.LocalToken(cfg.local_token_file)
        super().__init__((cfg.bind, cfg.port), Handler)

    def host_ok(self, host_header):
        host = split_host(host_header)
        if not host:
            return False
        try:
            ipaddress.ip_address(host)
            return True          # an IP address cannot be a rebinding attacker's name
        except ValueError:
            return host in self.names


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "stove-master"
    sys_version = ""
    timeout = 30

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_ip() or "?", fmt % args)

    def client_ip(self):
        """The visitor's address. Through a reverse proxy on this Pi ([web] trust_proxy, a connection
        from 127.0.0.1 with X-Real-IP) it is that header; one that is not an address gives "", which
        is refused. Only X-Real-IP: nginx overwrites it, X-Forwarded-For it only appends to."""
        peer = self.client_address[0]
        headers = getattr(self, "headers", None)     # not there yet for a broken request line
        if self.server.cfg.trust_proxy and headers is not None and is_loopback(peer):
            real = headers.get("X-Real-IP")
            if real is not None:
                try:
                    return str(ipaddress.ip_address(real.strip()))
                except ValueError:
                    return ""
        return peer

    def local_program(self):
        """A program on the Pi itself: from 127.0.0.1, and not through a proxy (no X-Real-IP)."""
        return is_loopback(self.client_address[0]) and "X-Real-IP" not in self.headers

    def _send(self, status, body=b"", ctype="text/plain; charset=utf-8", headers=()):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        for k, v in SECURITY_HEADERS + tuple(headers):
            self.send_header(k, v)
        if ctype:
            self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True      # the client gave up waiting; nothing to report

    def _guard(self):
        """Home network only, and only under a name that really is this hub."""
        cfg = self.server.cfg
        ip = self.client_ip()
        if not ip or (cfg.lan_only and not ip_allowed(ip)):
            self._send(403, "The stove hub only answers on the home network. From outside use the website.")
            return False
        if not self.server.host_ok(self.headers.get("Host")):
            url = self.server.hub.lan_url()
            if cfg.trust_proxy:      # a proxy in front: it decides where the Pi's IP address goes
                self._send(421, f"Unknown host name. Open {url} instead.")
            else:
                self._send(421, f"Unknown host name. Open {url} or the hub's IP address.")
            return False
        return True

    def do_GET(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path == "/ws":
            return self._websocket()
        if path == "/env.json":
            hub = self.server.hub
            env = {"mode": "local", "version": __version__,
                   "device": {"id": hub.cloud.device_id if hub.cloud else None, "name": hub.device_name},
                   "cloud_url": self.server.cfg.cloud_url or None}
            return self._send(200, json.dumps(env), "application/json", [("Cache-Control", "no-store")])
        if path == "/api/health":
            return self._send(200, json.dumps({"ok": True, "version": __version__}), "application/json",
                              [("Cache-Control", "no-store")])
        if path in ("/api/state", "/api/control", "/api/command"):
            return self._url_api(path)
        f = self.server.files.get(path)
        if f is None:
            return self._send(404, "not found")
        body, etag, ctype = f
        headers = [("ETag", etag), ("Cache-Control", "no-cache")]
        if ctype.startswith("text/html"):
            headers.append(("Content-Security-Policy", csp("ws://" + self.headers.get("Host", "").strip())))
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", None, headers)
        self._send(200, body, ctype, headers)

    do_HEAD = do_GET

    def do_POST(self):
        if not self._guard():
            return
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/api/state", "/api/control", "/api/command"):
            return self._url_api(path)
        self._send(405, "method not allowed", headers=[("Allow", "GET, HEAD")])

    def do_PUT(self):
        if self._guard():
            self._send(405, "method not allowed", headers=[("Allow", "GET, HEAD")])

    do_DELETE = do_PATCH = do_OPTIONS = do_PUT

    # -- the local API: plain URLs (Siri Shortcuts, scripts), see urlapi.py -------------
    def _url_api(self, path):
        hub = self.server.hub
        ip = self.client_ip()
        fmt = "json"
        done = []           # the commands that went through, also in an error reply
        try:
            body = b""
            if self.command == "POST":
                length = int(self.headers.get("Content-Length") or 0)
                if length > 4096:
                    raise urlapi.APIError(413, "request too large")
                body = self.rfile.read(length) if length else b""
            params = urlapi.query(self.path, body, self.headers.get("Content-Type", ""))
            fmt = "text" if params.get("format", "").lower() == "text" else "json"
            if path == "/api/state":
                state = hub.read_status()
                text = urlapi.describe_state(state, hub.probes(), hub.device_name)
                ctype, out = urlapi.render({"ok": not state.get("error"), "state": state}, text, fmt)
                return self._send(200, out, ctype, [("Cache-Control", "no-store")])
            local = self.local_program() and self.server.local_token.matches(params, self.headers)
            if not local and not urlapi.token_ok(hub.db, params, self.headers):
                log.warning("API: wrong or missing token from %s", ip)
                raise urlapi.APIError(401, "Wrong or missing secret token. It is on the local dashboard: "
                                           "Online, API (or: sudo stove-master api-token).")
            wants_state = False
            if path == "/api/command":
                # a sentence, e.g. dictated to a Siri shortcut: "turn on and set the temperature to 23"
                spoken = (params.get("text") or params.get("q") or params.get("command") or "")[:300]
                try:
                    commands, wants_state = voice.parse(spoken, lambda: hub.read_status().get("setpoint"))
                except voice.NotUnderstood as e:
                    log.info("API: did not understand %r from %s", spoken, ip)
                    raise urlapi.APIError(400, voice.sorry(spoken, e)) from None
            else:
                commands = urlapi.parse_commands(params)
            who = urlapi.local_actor(self.headers) if local else f"API ({ip})"
            client = Client("api", who, lambda obj: None, ip=ip)
            state = None
            for action, value in commands:
                reply = hub.handle(client, {"id": 1, "m": "stove.action", "p": {"action": action, "value": value}})
                if "e" in reply:
                    raise urlapi.APIError(reply.get("code") if reply.get("code") in (400, 502) else 500, reply["e"])
                state = reply["r"]
                done.append(action)
            text = urlapi.describe_done(commands) if commands else ""
            if wants_state:
                state = state or hub.read_status()
                text = (text + " " + urlapi.describe_state(state, hub.probes(), hub.device_name)).strip()
            ctype, out = urlapi.render({"ok": True, "done": done, "state": state}, text, fmt)
            self._send(200, out, ctype, [("Cache-Control", "no-store")])
        except urlapi.APIError as e:
            payload = {"ok": False, "error": str(e)}
            if path != "/api/state":
                payload["done"] = done      # e.g. ["setpoint"] when the fan then failed: those stay done
            ctype, out = urlapi.render(payload, str(e), fmt)
            self._send(e.status, out, ctype, [("Cache-Control", "no-store")])

    # -- WebSocket ---------------------------------------------------------------
    def _websocket(self):
        origin = self.headers.get("Origin")
        if origin and urllib.parse.urlsplit(origin).netloc.lower() != self.headers.get("Host", "").strip().lower():
            log.warning("refused a WebSocket from %s (page of %s)", self.client_ip(), origin)
            return self._send(403, "WebSocket only from the hub's own page")
        srv = self.server
        with srv.sockets_lock:
            if srv.sockets >= MAX_LAN_SOCKETS:
                return self._send(503, "too many connections")
            srv.sockets += 1
        try:
            sock = ws.server_upgrade(self, max_message=256 * 1024)
            if sock is not None:
                self._serve_socket(sock)
        finally:
            with srv.sockets_lock:
                srv.sockets -= 1

    def _serve_socket(self, sock):
        hub = self.server.hub
        ip = self.client_ip()
        sender = ws.Sender(sock, max_bytes=8 << 20, name="lan-writer")

        def send(obj):
            if not sender.send(json.dumps(obj, separators=(",", ":"), default=str)):
                log.debug("dropped a message to %s (connection closed or too slow)", ip)
        client = Client("lan", ip, send, ip=ip)
        hub.attach(client)
        bucket, last = 40.0, time.monotonic()
        try:
            while True:
                raw = sock.recv()
                if raw is None:
                    break
                now = time.monotonic()
                bucket = min(40.0, bucket + (now - last) * 20)   # 20 requests/s, bursts of 40
                last = now
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if bucket < 1:
                    send({"id": msg.get("id") if isinstance(msg, dict) else None,
                          "e": "Too many requests, slow down.", "code": 429})
                    continue
                bucket -= 1
                reply = hub.handle(client, msg)
                if reply is not None:
                    send(reply)
        finally:
            hub.detach(client)
            sender.close()
            sock.close()
