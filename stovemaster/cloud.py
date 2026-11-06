"""The hub's always-on WebSocket to the VPS, and the remote sessions it carries.

The VPS routes browser sessions to this connection; each session is end-to-end
encrypted (e2e.py) with the stove's token (adding the stove to an account) or
a link key (afterwards). See docs/protocol.md, sections 3, 5 and 6.
"""

import base64
import json
import logging
import os
import secrets
import threading
import time
import urllib.parse

from . import __version__, e2e, webpush, ws
from .api import Client, RPCError

log = logging.getLogger("cloud")

CATEGORIES = ("problems", "schedule")
MAX_MESSAGE = 1024 * 1024    # from the VPS; it forwards at most 256 KB of ciphertext per message
MAX_SEALED = 768 * 1024      # to the VPS, which accepts 1 MB messages from hubs
MAX_SESSIONS = 60
MAX_PER_ACCOUNT = 12
PAIR_LIMIT = (5, 600)        # pairing attempts per account per 10 minutes
PAIR_GLOBAL_LIMIT = (20, 3600)
PUSH_TTL = 6 * 3600
SYNC_INTERVAL = 20           # seconds between looks for links removed by the command line


def device_ws_url(cloud_url):
    u = urllib.parse.urlsplit(cloud_url)
    scheme = "wss" if u.scheme == "https" else "ws"
    return urllib.parse.urlunsplit((scheme, u.netloc, u.path.rstrip("/") + "/ws/device", "", ""))


class RemoteSession:
    def __init__(self, sid, session, client, mode):
        self.sid = sid
        self.session = session
        self.client = client
        self.mode = mode
        self.finished = False     # pairing done: no second pair.finish


class CloudLink:
    def __init__(self, hub, cfg, db):
        e2e.require_crypto()
        self.hub = hub
        self.db = db
        self.url = cfg.cloud_url
        self.ws_url = device_ws_url(cfg.cloud_url)
        self.device_id = self._setting("device_id", e2e.new_device_id)
        self.device_secret = self._setting("device_secret", lambda: secrets.token_urlsafe(32))
        self._token_seen = self._setting("pair_secret", lambda: e2e.b64e(e2e.new_pair_secret()))
        self.links = {}
        self._load_links()
        self.sessions = {}
        self.lock = threading.RLock()
        self._links_lock = threading.RLock()   # sync_links never sees a link or token half changed
        self.connected = False
        self.connected_since = None
        self.last_error = ""
        self.ws = None
        self._sender = None
        self._stop = threading.Event()
        self._thread = None
        self._sync_thread = None
        self._attempts = []        # pairing attempts: (time, profile id)
        self.methods = {
            "link.leave": (self.link_leave, "remote"),
            "push.subscribe": (self.push_subscribe, "remote"),
            "push.unsubscribe": (self.push_unsubscribe, "remote"),
            "push.get": (self.push_get, "remote"),
            "push.test": (self.push_test, "remote"),
        }
        hub.cloud = self

    def _setting(self, key, make):
        value = self.db.get_setting(key)
        if not value:
            value = make()
            self.db.set_setting(key, value)
        return value

    def _load_links(self):
        self.links = {r["id"]: r for r in self.db.all("SELECT * FROM links ORDER BY created_at")}

    # -- token -----------------------------------------------------------------
    @property
    def pair_secret(self):
        return e2e.b64d(self.db.get_setting("pair_secret"))

    @property
    def token(self):
        return e2e.format_token(self.device_id, self.pair_secret)

    def regenerate(self):
        with self._links_lock:
            self.db.set_setting("pair_secret", e2e.b64e(e2e.new_pair_secret()))
            self._end_pairing()
        self.changed()
        return self.info(lan=True)

    def _end_pairing(self):
        """Sessions opened with the old token end too."""
        self._token_seen = self.db.get_setting("pair_secret")
        with self.lock:
            doomed = [s.sid for s in self.sessions.values() if s.mode == "pair"]
        for sid in doomed:
            self._close(sid, reason="the token was replaced")

    def sync_links(self):
        """Take up what another process changed in the database ("sudo stove-master links remove",
        "token --new"): end the sessions of removed links and tell the VPS, as unlink() does, and
        end pairing sessions of a replaced token. Before every new session and every SYNC_INTERVAL."""
        with self._links_lock:
            ids = {r["id"] for r in self.db.all("SELECT id FROM links")}
            for link_id in [i for i in self.links if i not in ids]:
                self.unlink(link_id, reason="removed on the command line")
            if ids != set(self.links):
                self._load_links()
            token_replaced = self.db.get_setting("pair_secret") != self._token_seen
            if token_replaced:
                self._end_pairing()
        if token_replaced:
            self.changed()

    def _sync_loop(self):
        while not self._stop.wait(SYNC_INTERVAL):
            try:
                self.sync_links()
            except Exception:
                log.exception("checking the links failed")

    # -- state for the UI ----------------------------------------------------------
    def status(self):
        return {"configured": True, "url": self.url, "connected": self.connected,
                "since": self.connected_since, "last_error": self.last_error,
                "sessions": len(self.sessions)}

    def info(self, lan=False):
        out = self.status()
        out["device"] = {"id": self.device_id, "name": self.hub.device_name}
        out["links"] = [{"id": l["id"], "account": l["profile"], "added": l["created_at"], "last_seen": l["last_seen"],
                         "online": any(s.client.link and s.client.link["id"] == l["id"] for s in self.sessions.values())}
                        for l in self.links.values()]
        if lan:
            out["token"] = self.token
            out["add_url"] = self.url + "#/add/" + self.token.replace("-", "")
        return out

    def changed(self):
        """Tell the local dashboards (name, token, links or connection changed)."""
        self.hub.broadcast("online", self.info(lan=True), lan_only=True)

    # -- connection ------------------------------------------------------------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="cloud", daemon=True)
            self._thread.start()
            self._sync_thread = threading.Thread(target=self._sync_loop, name="cloud-links", daemon=True)
            self._sync_thread.start()

    def stop(self):
        self._stop.set()
        sock = self.ws
        if sock:
            sock.close(ws.CLOSE_GOING_AWAY, "hub stopping")

    def _run(self):
        delay = 2
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self._connect_and_serve()
            except ws.HandshakeError as e:
                body = e.body.decode("utf-8", "replace").strip()[:200]
                self.last_error = f"{e}: {body}" if body else str(e)
                if e.status == 401:
                    log.error("the VPS refused this hub's device key (%s) - was the device id taken over?", body)
                elif e.status in (404, 405, 502, 503, 504):
                    self.last_error = (f"{e} - is the stove website installed and running at {self.url} ? "
                                       f"(www/deploy.sh on the VPS)")
            except (OSError, ValueError) as e:
                self.last_error = str(e) or e.__class__.__name__
            finally:
                if self.connected:
                    log.info("disconnected from %s", self.url)
                self._drop_connection()
            if self._stop.is_set():
                break
            if time.monotonic() - started > 60:
                delay = 2
            log.info("VPS not connected (%s), retrying in %d s", self.last_error, delay)
            self._stop.wait(delay)
            delay = min(delay * 2, 60)

    def _connect_and_serve(self):
        sock = ws.connect(self.ws_url, headers=[
            ("Authorization", f"Device {self.device_id}.{self.device_secret}"),
            ("User-Agent", f"stove-master/{__version__}")], max_message=MAX_MESSAGE)
        self.ws = sock
        self._sender = ws.Sender(sock, max_bytes=8 << 20, name="cloud-writer")
        self.connected, self.connected_since, self.last_error = True, time.time(), ""
        log.info("connected to %s as device %s", self.url, self.device_id)
        self.changed()
        while True:
            raw = sock.recv()
            if raw is None:
                if sock.error:
                    self.last_error = sock.error
                elif sock.close_code:
                    self.last_error = f"closed by the VPS ({sock.close_code} {sock.close_reason})".strip()
                return
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if isinstance(msg, dict):
                try:
                    self._on_message(msg)
                except Exception:
                    log.exception("message %s from the VPS failed", msg.get("t"))

    def _send(self, obj):
        sender = self._sender     # a full queue drops the connection (sock.error says why)
        return sender is not None and sender.send(json.dumps(obj, separators=(",", ":")))

    def _drop_connection(self):
        with self.lock:
            sessions, self.sessions = list(self.sessions.values()), {}
        for s in sessions:
            self.hub.detach(s.client)
        if self._sender:
            self._sender.close()          # never blocks, whatever state the writer is in
        self._sender = None
        self.ws = None
        if self.connected:
            self.connected, self.connected_since = False, None
            self.changed()

    # -- messages from the VPS ------------------------------------------------------
    def _on_message(self, msg):
        t = msg.get("t")
        if t in ("welcome", "links"):
            self._reconcile(msg.get("links") or [])
        elif t == "open":
            self._open(msg)
        elif t == "data":
            self._data(msg)
        elif t == "close":
            self._close(msg.get("sid"), tell_vps=False)
        elif t == "push_result":
            if msg.get("gone"):
                self.db.run("DELETE FROM push_subs WHERE id = ?", (str(msg.get("sub")),))
                log.info("push subscription %s expired", msg.get("sub"))
        elif t == "error":
            log.warning("VPS: %s", msg.get("message"))

    def _reconcile(self, profiles):
        """Accounts the VPS still routes to us; links removed on the website go here too."""
        self.sync_links()
        online = {str(p) for p in profiles}
        for link in list(self.links.values()):
            if link["profile_id"] not in online:
                self._delete_link(link["id"])
                self.db.log_event("hub", "system", "account removed",
                                  f"{link['profile']} removed the stove from the account online")
        for pid in online - {l["profile_id"] for l in self.links.values()}:
            self._send({"t": "link", "op": "remove", "profile": pid})
        self.changed()

    def _rate_ok(self, profile_id):
        now = time.monotonic()
        self._attempts = [(t, p) for t, p in self._attempts if now - t < PAIR_GLOBAL_LIMIT[1]]
        mine = [t for t, p in self._attempts if p == profile_id and now - t < PAIR_LIMIT[1]]
        if len(mine) >= PAIR_LIMIT[0] or len(self._attempts) >= PAIR_GLOBAL_LIMIT[0]:
            return False
        self._attempts.append((now, profile_id))
        return True

    def _open(self, msg):
        sid = str(msg.get("sid") or "")
        profile = msg.get("profile") or {}
        pid, pname = str(profile.get("id") or ""), str(profile.get("name") or "?")[:64]
        mode = msg.get("mode")
        link = None
        self.sync_links()            # never a session for a link or token the command line removed
        with self.lock:
            busy = len(self.sessions) >= MAX_SESSIONS or \
                sum(1 for s in self.sessions.values() if s.client.profile.get("id") == pid) >= MAX_PER_ACCOUNT
        if not sid or not pid or busy:
            return self._send({"t": "close", "sid": sid, "reason": "busy"})
        if mode == "pair":
            if not self._rate_ok(pid):
                return self._send({"t": "close", "sid": sid, "reason": "too many attempts, try again later"})
            psk = self.pair_secret
        elif mode == "link":
            link = self.links.get(str(msg.get("link") or ""))
            if not link or link["profile_id"] != pid:
                return self._send({"t": "close", "sid": sid, "reason": "unknown link"})
            psk = e2e.b64d(link["key"])
        else:
            return self._send({"t": "close", "sid": sid, "reason": "unknown mode"})
        try:
            accept, session = e2e.hub_accept(msg, psk, self.device_id)
        except e2e.E2EError as e:
            return self._send({"t": "close", "sid": sid, "reason": str(e)})
        rs = RemoteSession(sid, session, None, mode)
        rs.client = Client("remote", link["profile"] if link else pname, lambda obj: self._seal(rs, obj),
                           link=link, pairing=mode == "pair", profile={"id": pid, "name": pname})
        with self.lock:
            self.sessions[sid] = rs
        accept["sid"] = sid
        self._send(accept)
        self.hub.attach(rs.client)
        if link:
            self.db.run("UPDATE links SET last_seen = ? WHERE id = ?", (time.time(), link["id"]))
            link["last_seen"] = time.time()
            self.changed()

    def _seal(self, rs, obj):
        with rs.session.lock:        # nonce order must equal queue order
            try:
                c = rs.session.seal(obj, limit=MAX_SEALED)
            except e2e.E2EError as e:
                log.warning("message to %s not sent: %s", rs.client.who, e)
                if "id" not in obj:
                    return
                c = rs.session.seal({"id": obj["id"], "e": "The answer is too big.", "code": 500})
            self._send({"t": "data", "sid": rs.sid, "c": c})

    def _data(self, msg):
        rs = self.sessions.get(str(msg.get("sid") or ""))
        if rs is None:
            return
        try:
            obj = rs.session.open(msg.get("c"))
        except e2e.E2EError as e:
            log.warning("session %s (%s): %s", rs.sid, rs.client.who, e)
            return self._close(rs.sid, reason="decryption failed")
        reply = self.hub.handle(rs.client, obj)
        if reply is not None:
            rs.client.send(reply)

    def _close(self, sid, reason="", tell_vps=True):
        with self.lock:
            rs = self.sessions.pop(str(sid), None)
        if rs:
            self.hub.detach(rs.client)
            if tell_vps:
                self._send({"t": "close", "sid": rs.sid, "reason": reason})
            if rs.client.link:
                self.changed()

    # -- links -------------------------------------------------------------------------
    def pair_finish(self, client, p):
        """Called inside a pairing session, i.e. after the browser proved it has the token.
        Works once per session (the browser then closes it)."""
        with self.lock:
            rs = next((s for s in self.sessions.values() if s.client is client), None)
            if rs is None or rs.mode != "pair" or rs.finished:
                raise RPCError("This pairing session is over - start again.", 409)
            rs.finished = True
        pid, name = client.profile["id"], client.profile["name"]
        with self._links_lock:
            old = self.db.one("SELECT id FROM links WHERE profile_id = ?", (pid,))
            if old:
                self._delete_link(old["id"])
            link_id = secrets.token_hex(8)
            key = os.urandom(32)
            self.db.run("INSERT INTO links(id, profile_id, profile, key, created_at) VALUES(?, ?, ?, ?, ?)",
                        (link_id, pid, name, e2e.b64e(key), time.time()))
            self._load_links()
            self._send({"t": "link", "op": "add", "profile": pid})   # before the answer: the VPS must route it
        self.db.log_event(name, "remote", "account added", f"{name} can now reach the stove from the internet")
        self.notify("problems", "Account added", f"{name} added the stove to their online account.", "link")
        self.changed()
        return {"link": link_id, "key": e2e.b64e(key), "device": {"id": self.device_id, "name": self.hub.device_name}}

    def _delete_link(self, link_id):
        with self._links_lock:
            link = self.links.get(link_id)
            self.db.run("DELETE FROM links WHERE id = ?", (link_id,))
            self._load_links()
        with self.lock:
            doomed = [s.sid for s in self.sessions.values() if s.client.link and s.client.link["id"] == link_id]
        for sid in doomed:
            self._close(sid, reason="access removed")
        return link

    def unlink(self, link_id, reason=""):
        link = self._delete_link(link_id)
        if link:
            self._send({"t": "link", "op": "remove", "profile": link["profile_id"]})
            log.info("link %s (%s) removed: %s", link_id, link["profile"], reason)
        self.changed()

    def link_leave(self, client, p):
        name = client.link["profile"]
        self.unlink(client.link["id"], reason="removed by the account")
        self.db.log_event(name, "remote", "account removed", f"{name} removed the stove from the account")
        return {"ok": True}

    # -- push notifications ---------------------------------------------------------------
    def push_subscribe(self, client, p):
        sub = str(p.get("sub") or "")
        if not (0 < len(sub) <= 64):
            raise RPCError("missing subscription id")
        try:
            if len(e2e.b64url_decode(p.get("p256dh"))) != 65 or len(e2e.b64url_decode(p.get("auth"))) != 16:
                raise ValueError
        except (ValueError, TypeError, base64.binascii.Error):
            raise RPCError("invalid subscription keys") from None
        cats = [c for c in (p.get("categories") or ["problems"]) if c in CATEGORIES]
        self.db.run("INSERT INTO push_subs(id, link_id, p256dh, auth, categories, created_at) VALUES(?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(id) DO UPDATE SET link_id = excluded.link_id, p256dh = excluded.p256dh, "
                    "auth = excluded.auth, categories = excluded.categories",
                    (sub, client.link["id"], p["p256dh"], p["auth"], json.dumps(cats), time.time()))
        return {"subscribed": True, "categories": cats}

    def push_unsubscribe(self, client, p):
        self.db.run("DELETE FROM push_subs WHERE id = ? AND link_id = ?", (str(p.get("sub") or ""), client.link["id"]))
        return {"subscribed": False}

    def push_get(self, client, p):
        row = self.db.one("SELECT categories FROM push_subs WHERE id = ? AND link_id = ?",
                          (str(p.get("sub") or ""), client.link["id"]))
        return {"subscribed": bool(row), "categories": json.loads(row["categories"]) if row else []}

    def push_test(self, client, p):
        row = self.db.one("SELECT * FROM push_subs WHERE id = ? AND link_id = ?",
                          (str(p.get("sub") or ""), client.link["id"]))
        if not row:
            raise RPCError("This browser has no notifications from this stove yet.", 404)
        if not self._push(row, "Test notification", f"Notifications from {self.hub.device_name} work.", "test"):
            raise RPCError("The VPS is not connected.", 503)
        return {"ok": True}

    def notify(self, category, title, body, tag=None):
        for row in self.db.all("SELECT * FROM push_subs"):
            if category in json.loads(row["categories"]):
                self._push(row, title, body, tag)

    def _push(self, row, title, body, tag):
        name = self.hub.device_name
        payload = json.dumps({"title": f"{name}: {title}" if name and name != "Stove" else title, "body": body,
                              "tag": f"{self.device_id}-{tag or 'stove'}", "device": self.device_id,
                              "ts": int(time.time())}).encode("utf-8")
        try:
            data = webpush.encrypt(payload, row["p256dh"], row["auth"])
        except ValueError as e:
            log.warning("push to %s skipped: %s", row["id"], e)
            return False
        return self._send({"t": "push", "sub": row["id"], "body": e2e.b64e(data), "ttl": PUSH_TTL,
                           "urgency": "high" if tag in ("alarm", "box", "schedule-problem") else "normal",
                           "topic": (tag or "stove")[:32]})
