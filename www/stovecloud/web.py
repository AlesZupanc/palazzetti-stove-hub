"""HTTP side of the website: the app files, env.json, the account API and the two
WebSocket endpoints (/ws/device for hubs, /ws/client for browsers).

Runs behind nginx (TLS) on 127.0.0.1. All paths live under the prefix of the
public URL, e.g. /mystove/.
"""

import datetime as dt
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__, ws
from .updates import UpdateError, Updates
from .accounts import SETUP_TTL, AuthError, clean_secret, token_hash, verify_auth

log = logging.getLogger("web")

COOKIE = "stove_session"
MAX_BODY = 128 * 1024
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
    ("X-Robots-Tag", "noindex, nofollow"),
)
USER_ACTIONS = ("approve", "reject", "disable", "enable", "make_admin", "make_user", "delete")


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def local_time(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else None


def find_app_dir(configured=""):
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (configured, os.path.join(here, "..", "app")):
        if cand and os.path.isfile(os.path.join(cand, "index.html")):
            return os.path.abspath(cand)
    raise FileNotFoundError("the web app files (app/ next to stove_cloud.py) were not found")


def load_app_files(app_dir):
    files, digest = {}, hashlib.sha256()
    for folder, dirs, names in sorted(os.walk(app_dir)):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            ext = os.path.splitext(name)[1]
            if name.startswith(".") or ext not in CONTENT_TYPES:
                continue
            path = os.path.join(folder, name)
            with open(path, "rb") as f:
                body = f.read()
            url = "/" + os.path.relpath(path, app_dir).replace(os.sep, "/")
            digest.update(url.encode() + body)
            files[url] = body
    version = digest.hexdigest()[:12]
    out = {}
    for url, body in files.items():
        if url == "/sw.js":          # the service worker re-installs whenever any app file changes
            body = body.replace(b"__APP_VERSION__", version.encode())
        etag = '"' + hashlib.sha256(body).hexdigest()[:20] + '"'
        out[url] = (body, etag, CONTENT_TYPES[os.path.splitext(url)[1]])
    out["/"] = out["/index.html"]
    return out, version


class CloudServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False      # open WebSockets must not keep the service from stopping

    def __init__(self, cfg, db, accounts, mailer, pusher, hub):
        self.cfg = cfg
        self.db = db
        self.accounts = accounts
        self.mailer = mailer
        self.pusher = pusher
        self.hub = hub
        self.app_dir = find_app_dir(cfg.app_dir)
        self.files, self.app_version = load_app_files(self.app_dir)
        self.started = time.time()
        self.updates = Updates(os.path.dirname(os.path.abspath(cfg.db_path)),
                               "/etc/systemd/system/stove-cloud-update.path")
        p = r"(\d{1,12})"
        routes = [
            ("GET", "me", self.me, None),
            ("POST", "prelogin", self.prelogin, None),
            ("POST", "login", self.login, None),
            ("POST", "logout", self.logout, None),
            ("POST", "register", self.register, None),
            ("POST", "approval", self.approval_info, None),
            ("POST", "approval/decide", self.approval_decide, None),
            ("POST", "setup", self.setup, None),
            ("POST", "password/forgot", self.forgot, None),
            ("POST", "password/reset", self.reset, None),
            ("POST", "password/change", self.change_password, "user"),
            ("POST", "sessions/revoke", self.revoke_sessions, "user"),
            ("PUT", "vault", self.put_vault, "user"),
            ("GET", "devices", self.devices, "user"),
            ("DELETE", r"devices/([A-Z2-7]{8})", self.device_delete, "user"),
            ("POST", "push/subscriptions", self.push_add, "user"),
            ("DELETE", r"push/subscriptions/([A-Za-z0-9_-]{8,64})", self.push_delete, "user"),
            ("GET", "users", self.users, "admin"),
            ("POST", f"users/{p}", self.user_action, "admin"),
            ("GET", "system", self.system, "admin"),
            ("POST", "system/update", self.system_update, "admin"),
        ]
        self.routes = [(m, re.compile("/api/" + path + "$"), fn, level) for m, path, fn, level in routes]
        super().__init__((cfg.bind, cfg.port), Handler)

    # -- helpers -----------------------------------------------------------------
    def link(self, fragment=""):
        return self.cfg.public_url + fragment

    def notify_admins(self, subject, body):
        admins = self.accounts.admins()
        if not admins:
            log.warning("no active administrator to tell: %s", subject)
        for a in admins:
            self.mailer.send(a["email"], subject, body)

    def push_subject(self):
        admins = self.accounts.admins()
        return f"mailto:{admins[0]['email']}" if admins else self.cfg.public_url

    def env(self):
        return {"mode": "cloud", "version": __version__, "app": self.app_version,
                "registration": self.cfg.allow_registration, "mail": self.mailer.configured,
                "push_key": self.pusher.public_key if self.pusher.available else None}

    # -- accounts -------------------------------------------------------------------
    def me(self, req, user):
        if not user:
            return {"user": None}
        return {"user": self.accounts.public(user), "salt": user["salt"], "iterations": user["iterations"],
                "vault": {"blob": user["vault"], "version": user["vault_version"]}}

    def prelogin(self, req, user):
        return self.accounts.prelogin(req.json().get("login"))

    def login(self, req, user):
        data = req.json()
        u = self.accounts.authenticate(data.get("login"), data.get("auth"), req.ip)
        token = self.accounts.create_session(u["id"], req.ip, req.header("user-agent"))
        log.info("%s signed in from %s", u["username"], req.ip)
        u = self.accounts.get(u["id"])
        return req.json_response({"user": self.accounts.public(u), "vault": {"blob": u["vault"], "version": u["vault_version"]}},
                                 cookie=(token, self.cfg.session_days * 86400))

    def logout(self, req, user):
        for token in req.cookies(COOKIE):
            self.accounts.revoke_session(token)
            if user:
                self.hub.drop_user(user["id"], token_hash(token))
        return req.json_response({"ok": True}, cookie=("", 0))

    def revoke_sessions(self, req, user):
        self.accounts.revoke_sessions(user["id"])
        self.hub.drop_user(user["id"])
        return req.json_response({"ok": True}, cookie=("", 0))

    def register(self, req, user):
        if not self.cfg.allow_registration:
            raise HTTPError(403, "Registration is closed. Ask the administrator for an account.")
        d = req.json()
        new, token = self.accounts.register(d.get("username"), d.get("email"), d.get("salt"), d.get("auth"),
                                            d.get("iterations"), req.ip)
        log.info("account request from %s (%s)", new["username"], req.ip)
        self.notify_admins(
            f"Stove: new account request from {new['username']}",
            f"Someone asked for an account on your stove website.\n\n"
            f"  Username: {new['username']}\n  E-mail:   {new['email']}\n  From:     {req.ip}\n"
            f"  Time:     {local_time(new['created_at'])}\n\n"
            f"Approve or reject it here (link valid for 7 days):\n{self.link('#/approve/' + token)}\n\n"
            f"An approved account can then add stoves with their tokens.\n")
        return {"ok": True, "message": "Thanks! The administrator has been asked to approve your account. "
                + ("You get an e-mail as soon as it is approved." if self.mailer.configured else "")}

    def _pending(self, req, consume=False):
        token = req.json().get("token")
        u = (self.accounts.consume_token if consume else self.accounts.peek_token)("approve", token)
        if not u or u["status"] != "pending":
            raise HTTPError(404, "This approval link is invalid, has expired or was already used.")
        return u

    def approval_info(self, req, user):
        u = self._pending(req)
        return {"user": {"username": u["username"], "email": u["email"], "created": local_time(u["created_at"]),
                         "ip": u["reg_ip"]}}

    def approval_decide(self, req, user):
        decision = req.json().get("decision")
        if decision not in ("approve", "reject"):
            raise HTTPError(400, "decision must be approve or reject")
        u = self._pending(req, consume=True)
        if decision == "approve":
            self._approve(u)
            return {"ok": True, "message": f"{u['username']} can now sign in."}
        self.accounts.delete(u["id"])
        return {"ok": True, "message": f"The request from {u['username']} was rejected and deleted."}

    def _approve(self, u):
        self.accounts.set_status(u["id"], "active")
        self.mailer.send(u["email"], "Your stove account was approved",
                         f"Hello {u['username']},\n\nyour account was approved. Sign in here:\n\n  {self.link()}\n\n"
                         f"To add your stove, open its page at home (http://stove.local -> Online) and follow the "
                         f"steps there.\n")

    def setup(self, req, user):
        """First password of an account created on the command line (the administrator)."""
        d = req.json()
        u = self.accounts.consume_token("setup", d.get("token"))
        if not u:
            raise HTTPError(404, "This link is invalid, has expired or was already used.")
        self.accounts.set_secret(u["id"], d.get("salt"), d.get("auth"), d.get("iterations"), vault=None)
        self.accounts.set_status(u["id"], "active")
        return {"ok": True, "message": "Password set. You can sign in now.", "username": u["username"]}

    def forgot(self, req, user):
        if not self.mailer.configured:
            raise HTTPError(400, "Password reset by e-mail is not set up. Ask the administrator.")
        u, token = self.accounts.request_reset(req.json().get("login"), req.ip)
        if u:
            self.mailer.send(u["email"], "Stove: reset your password",
                             f"Hello {u['username']},\n\nsomeone (hopefully you) asked to reset your password.\n"
                             f"Choose a new one here within one hour:\n\n{self.link('#/reset/' + token)}\n\n"
                             f"Your stoves are stored encrypted with your password, so after the reset you add "
                             f"them again with their tokens.\nIf it wasn't you, ignore this e-mail.\n")
        return {"ok": True, "message": "If that account exists, an e-mail with a reset link is on its way."}

    def reset(self, req, user):
        d = req.json()
        u = self.accounts.peek_token("reset", d.get("token"))
        if not u or u["status"] != "active":
            raise HTTPError(400, "This reset link is invalid or has expired. Request a new one.")
        clean_secret(d.get("salt"), d.get("auth"), d.get("iterations"))   # check before using the link up
        if not self.accounts.consume_token("reset", d.get("token")):
            raise HTTPError(400, "This reset link was already used.")
        devices = [r["device_id"] for r in self.db.all("SELECT device_id FROM links WHERE user_id = ?", (u["id"],))]
        self.accounts.set_secret(u["id"], d["salt"], d["auth"], d.get("iterations"), vault=None)
        for device_id in devices:            # the stoves' keys were in the vault, which is gone now
            self.hub.unlink(device_id, u["id"])
        self.hub.drop_user(u["id"])
        return {"ok": True, "message": "Password changed. Sign in and add your stoves again with their tokens."}

    def change_password(self, req, user):
        d = req.json()
        key = f"pwchange:{user['id']}"
        if self.accounts.limits.acquire([(key, 5, 900)]):
            raise HTTPError(429, "Too many attempts, try again later.")
        if not verify_auth(str(d.get("old_auth") or ""), user["auth_hash"]):
            raise HTTPError(400, "Your current password is wrong.")
        self.accounts.limits.refund(key)
        vault = d.get("vault")
        if vault is None:
            raise HTTPError(400, "the vault (re-encrypted with the new password) is missing")
        self.accounts.set_secret(user["id"], d.get("salt"), d.get("auth"), d.get("iterations"), vault=vault,
                                 keep_session=req.cookie(COOKIE))
        u = self.accounts.get(user["id"])
        return {"ok": True, "message": "Password changed. Other devices were signed out.",
                "vault": {"blob": u["vault"], "version": u["vault_version"]}}

    def put_vault(self, req, user):
        d = req.json()
        try:
            version = int(d.get("version"))
        except (TypeError, ValueError):
            raise HTTPError(400, "version missing") from None
        new = self.accounts.put_vault(user["id"], d.get("blob"), version)
        if new is None:
            u = self.accounts.get(user["id"])
            return req.json_response({"error": "Your stoves were changed on another device meanwhile.",
                                      "vault": {"blob": u["vault"], "version": u["vault_version"]}}, 409)
        return {"version": new}

    # -- stoves ------------------------------------------------------------------------
    def devices(self, req, user):
        rows = self.db.all("SELECT l.device_id, l.created_at, d.last_seen FROM links l JOIN devices d "
                           "ON d.id = l.device_id WHERE l.user_id = ?", (user["id"],))
        return {"devices": [{"id": r["device_id"], "online": self.hub.online(r["device_id"]),
                             "linked": r["created_at"], "last_seen": r["last_seen"]} for r in rows]}

    def device_delete(self, req, user, device_id):
        self.hub.unlink(device_id, user["id"])
        return {"ok": True}

    def push_add(self, req, user):
        if not self.pusher.available:
            raise HTTPError(503, "Push notifications are not available on this server.")
        try:
            endpoint = self.pusher.check_endpoint(req.json().get("endpoint"))
        except Exception as e:
            raise HTTPError(400, str(e)) from None
        row = self.db.one("SELECT id, user_id FROM push_subs WHERE endpoint = ?", (endpoint,))
        if row:
            if row["user_id"] != user["id"]:   # the browser now belongs to another account
                self.db.run("UPDATE push_subs SET user_id = ? WHERE id = ?", (user["id"], row["id"]))
            return {"sub": row["id"]}
        if self.db.one("SELECT COUNT(*) AS n FROM push_subs WHERE user_id = ?", (user["id"],))["n"] >= 20:
            raise HTTPError(400, "Too many devices with notifications on this account.")
        sub = secrets.token_urlsafe(12)
        self.db.run("INSERT INTO push_subs(id, user_id, endpoint, created_at) VALUES(?, ?, ?, ?)",
                    (sub, user["id"], endpoint, time.time()))
        return {"sub": sub}

    def push_delete(self, req, user, sub):
        self.db.run("DELETE FROM push_subs WHERE id = ? AND user_id = ?", (sub, user["id"]))
        return {"ok": True}

    # -- administrators --------------------------------------------------------------------
    def users(self, req, user):
        out = []
        for u in self.accounts.list():
            pub = self.accounts.public(u)
            pub.update(created=local_time(u["created_at"]), last=local_time(u["last_login"]), ip=u["reg_ip"],
                       me=u["id"] == user["id"], has_password=bool(u["auth_hash"]),
                       stoves=self.db.one("SELECT COUNT(*) AS n FROM links WHERE user_id = ?", (u["id"],))["n"])
            out.append(pub)
        return {"users": out}

    def user_action(self, req, user, user_id):
        action = req.json().get("action")
        if action not in USER_ACTIONS:
            raise HTTPError(400, "unknown action")
        target = self.accounts.get(int(user_id))
        if not target:
            raise HTTPError(404, "user not found")
        risky = action in ("disable", "delete", "make_user")
        if risky and target["id"] == user["id"]:
            raise HTTPError(400, "You cannot do that to your own account.")
        if risky and target["role"] == "admin" and target["status"] == "active" and self.accounts.active_admin_count() <= 1:
            raise HTTPError(400, "At least one active administrator is required.")
        if action in ("approve", "reject") and target["status"] != "pending":
            raise HTTPError(400, f"{target['username']} is not waiting for approval.")
        if action == "approve":
            self._approve(target)
        elif action == "reject":
            self.accounts.delete(target["id"])
        elif action == "disable":
            self.accounts.set_status(target["id"], "disabled")
            self.hub.drop_user(target["id"])
        elif action == "enable":
            self.accounts.set_status(target["id"], "active")
        elif action == "make_admin":
            self.accounts.set_role(target["id"], "admin")
        elif action == "make_user":
            self.accounts.set_role(target["id"], "user")
        elif action == "delete":
            devices = [r["device_id"] for r in self.db.all("SELECT device_id FROM links WHERE user_id = ?",
                                                            (target["id"],))]
            self.accounts.delete(target["id"])
            self.hub.user_removed(target["id"], devices)
        log.info("%s: %s %s", user["username"], action, target["username"])
        return self.users(req, user)

    def system(self, req, user):
        return dict(self.hub.stats(), version=__version__, app=self.app_version,
                    uptime_s=int(time.time() - self.started), public_url=self.cfg.public_url,
                    mail={"configured": self.mailer.configured, "sender": self.cfg.mail_from or None},
                    push=self.pusher.available, registration=self.cfg.allow_registration,
                    users=self.db.one("SELECT COUNT(*) AS n FROM users")["n"], update=self.updates.info())

    def system_update(self, req, user):
        """Ask systemd to run tools/auto-update.sh now (the website restarts if there is something new)."""
        try:
            info = self.updates.request()
        except UpdateError as e:
            raise HTTPError(409, str(e)) from None
        log.info("%s: asked for an update from GitHub", user["username"])
        return info



def create_setup_link(cfg, accounts, username, email):
    """Command line: create (or re-invite) an administrator who sets the password in the browser."""
    u = accounts.find(username) or accounts.find(email)
    if u is None:
        uid = accounts.create(username, email, role="admin", status="pending")
    else:
        uid = u["id"]
        accounts.set_role(uid, "admin")
    return cfg.public_url + "#/setup/" + accounts.create_token("setup", uid, SETUP_TTL)


class Request:
    """What a route function sees of the HTTP request."""

    def __init__(self, handler, body):
        self.h = handler
        self.body = body
        self.ip = handler.client_ip()

    def header(self, name, default=""):
        return self.h.headers.get(name, default)

    def cookies(self, name):
        out = []
        for part in self.header("Cookie").split(";"):
            k, sep, v = part.strip().partition("=")
            if sep and k == name:
                out.append(v.strip().strip('"'))
        return out

    def cookie(self, name):
        values = self.cookies(name)
        return values[0] if values else None

    def json(self):
        if not self.body:
            return {}
        if self.header("Content-Type").split(";")[0].strip().lower() != "application/json":
            raise HTTPError(415, "expected a JSON body")
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HTTPError(400, "invalid JSON") from None
        if not isinstance(data, dict):
            raise HTTPError(400, "expected a JSON object")
        return data

    def json_response(self, obj, status=200, cookie=None):
        return (status, obj, cookie)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "stove-cloud"
    sys_version = ""
    timeout = 60

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.client_ip(), fmt % args)

    def client_ip(self):
        peer = self.client_address[0]
        if self.server.cfg.trust_proxy and peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            fwd = (self.headers.get("X-Real-IP") or "").strip()
            try:
                return str(ipaddress.ip_address(fwd))
            except ValueError:
                pass
        return peer

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
            self.wfile.write(body)

    def _json(self, status, obj, cookie=None):
        headers = [("Cache-Control", "no-store")]
        if cookie is not None:
            token, max_age = cookie
            parts = [f"{COOKIE}={token}", f"Path={self.server.cfg.prefix}/", f"Max-Age={max_age}", "HttpOnly",
                     "SameSite=Strict"]
            if self.server.cfg.public_url.startswith("https:"):
                parts.append("Secure")
            headers.append(("Set-Cookie", "; ".join(parts)))
        self._send(status, json.dumps(obj, separators=(",", ":")), "application/json", headers)

    def _dispatch(self):
        cfg = self.server.cfg
        u = urllib.parse.urlsplit(self.path)
        path = u.path
        if path == cfg.prefix and cfg.prefix:
            return self._send(301, b"", None, [("Location", cfg.prefix + "/" + (f"?{u.query}" if u.query else ""))])
        if not path.startswith(cfg.prefix + "/"):
            return self._send(404, "not found")
        sub = path[len(cfg.prefix):]
        if sub == "/ws/device":
            return self._device_socket()
        if sub == "/ws/client":
            return self._client_socket(urllib.parse.parse_qs(u.query))
        if sub.startswith("/api/"):
            return self._api(sub)
        if self.command not in ("GET", "HEAD"):
            return self._send(405, "method not allowed", headers=[("Allow", "GET, HEAD")])
        if sub == "/env.json":
            return self._json(200, self.server.env())
        if sub == "/_health":
            return self._json(200, dict(self.server.hub.stats(), ok=True, version=__version__))
        f = self.server.files.get(sub)
        if f is None:
            return self._send(404, "not found")
        body, etag, ctype = f
        headers = [("ETag", etag), ("Cache-Control", "no-cache")]
        if ctype.startswith("text/html"):
            headers.append(("Content-Security-Policy",
                            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                            f"connect-src 'self' {cfg.origin.replace('http', 'ws', 1)}; manifest-src 'self'; "
                            "worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"))
        if self.headers.get("If-None-Match") == etag:
            return self._send(304, b"", None, headers)
        self._send(200, body, ctype, headers)

    do_GET = do_POST = do_PUT = do_DELETE = do_HEAD = do_PATCH = do_OPTIONS = _dispatch

    # -- API ---------------------------------------------------------------------
    def _api(self, sub):
        srv = self.server
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True
            return self._json(413, {"error": "request too large"})
        body = self.rfile.read(length) if length else b""
        req = Request(self, body)
        try:
            for method, rx, fn, level in srv.routes:
                m = rx.match(sub)
                if not m:
                    continue
                if self.command != method:
                    continue
                if method != "GET" and req.header("X-Requested-With") != "stove":
                    raise HTTPError(403, "missing X-Requested-With header")
                user = self._session_user(req)
                if level and user is None:
                    raise HTTPError(401, "Please sign in.")
                if level == "admin" and user["role"] != "admin":
                    raise HTTPError(403, "Administrators only.")
                result = fn(req, user, *m.groups())
                if isinstance(result, tuple):
                    return self._json(*result)
                return self._json(200, result)
            if any(rx.match(sub) for _, rx, _, _ in srv.routes):
                raise HTTPError(405, "method not allowed")
            raise HTTPError(404, "not found")
        except (HTTPError, AuthError) as e:
            return self._json(e.status, {"error": e.message})
        except Exception:
            log.exception("error in %s %s", self.command, sub)
            return self._json(500, {"error": "internal error"})

    def _session_user(self, req):
        for token in req.cookies(COOKIE):
            user = self.server.accounts.session_user(token)
            if user:
                req.session = token
                return user
        return None

    # -- WebSockets ----------------------------------------------------------------
    def _device_socket(self):
        hub = self.server.hub
        ip = self.client_ip()
        try:
            device_id = hub.device_auth(self.headers.get("Authorization"), ip)
        except PermissionError as e:
            status, text = e.args
            return self._send(status, text)
        sock = ws.server_upgrade(self, max_message=1024 * 1024, ping_interval=25, idle_timeout=80)
        if sock:
            hub.serve_device(device_id, sock, ip)

    def _client_socket(self, query):
        srv = self.server
        origin = self.headers.get("Origin")
        if origin and origin.rstrip("/") != srv.cfg.origin:
            return self._send(403, "wrong origin")
        device_id = (query.get("dev") or [""])[0]
        req = Request(self, b"")
        user = self._session_user(req)
        sock = ws.server_upgrade(self, max_message=1024 * 1024, ping_interval=25, idle_timeout=80)
        if not sock:
            return
        if user is None:
            sock.close(4401, "signed out")
            return
        if not re.match(r"^[A-Z2-7]{8}$", device_id):
            sock.close(ws.CLOSE_POLICY, "bad device id")
            return
        srv.hub.serve_client(user, token_hash(req.session), device_id, sock, req.ip)

