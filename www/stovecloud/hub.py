"""Routing between browsers and hubs (Raspberry Pis), see docs/protocol.md section 5.

Every hub keeps one WebSocket to /ws/device. A browser opens /ws/client?dev=<id>
for the stove it looks at and starts an end-to-end encrypted session; this
module only forwards the opaque messages between the two and enforces who may
talk to which hub:

* "pair" sessions (adding a stove with its token): any signed-in account,
  a few attempts per account; the hub itself checks the token;
* "link" sessions: only accounts the hub has linked (table links, kept in
  sync by the hub's link add/remove messages).
"""

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time

from . import ws
from .accounts import RateLimiter, client_key

log = logging.getLogger("hub")

DEVICE_ID_RE = re.compile(r"^[A-Z2-7]{8}$")
OPEN_LIMIT = (30, 60)            # session opens per browser connection per minute
PAIR_LIMIT = (10, 600)           # pairing attempts per account per 10 minutes
REGISTER_LIMIT = (10, 3600)      # new hubs per IP (IPv6: per /64) per hour
UNLINKED_TTL = 2 * 86400         # a hub no account added is forgotten after this long offline
SESSION_CHECK = 60               # seconds between checks that a browser's sign-in still exists
MAX_CLIENTS_PER_USER = 20
MAX_C = 256 * 1024               # one encrypted message from a browser (hubs accept 1 MB)
DEVICE_QUEUE = 16 << 20          # bytes waiting for a hub before its connection is dropped
DEVICE_BACKLOG = 2 << 20         # ... and before browsers' messages are refused instead
CLIENT_QUEUE = 4 << 20


class Conn:
    """A WebSocket with a non-blocking, size-bounded send queue (ws.Sender), so
    one slow peer never blocks another; a peer that stops reading is dropped."""

    def __init__(self, sock, ip, name, max_bytes):
        self.sock = sock
        self.ip = ip
        self.sender = ws.Sender(sock, max_bytes=max_bytes, name=name)

    def send(self, obj):
        return self.sender.send(json.dumps(obj, separators=(",", ":")))

    def stop(self):
        self.sender.close()


class DeviceConn(Conn):
    def __init__(self, device_id, sock, ip):
        super().__init__(sock, ip, "device-writer", DEVICE_QUEUE)
        self.id = device_id
        self.sessions = {}                    # sid -> ClientConn


class ClientConn(Conn):
    def __init__(self, user, session_hash, device_id, sock, ip):
        super().__init__(sock, ip, "client-writer", CLIENT_QUEUE)
        self.user = user
        self.session_hash = session_hash
        self.device_id = device_id
        self.sid = None
        self.mode = None
        self.opens = RateLimiter()
        self.checked = time.monotonic()       # the sign-in was checked when the WebSocket opened


class Hub:
    def __init__(self, db, accounts, pusher, cfg):
        self.db = db
        self.accounts = accounts
        self.pusher = pusher
        self.cfg = cfg
        self.lock = threading.RLock()
        self.devices = {}                     # device id -> DeviceConn
        self.clients = {}                     # device id -> set of ClientConn
        self.limits = RateLimiter()

    # -- queries ---------------------------------------------------------------
    def online(self, device_id):
        return device_id in self.devices

    def linked_users(self, device_id):
        return [str(r["user_id"]) for r in self.db.all("SELECT user_id FROM links WHERE device_id = ?", (device_id,))]

    def is_linked(self, device_id, user_id):
        return bool(self.db.one("SELECT 1 AS x FROM links WHERE device_id = ? AND user_id = ?", (device_id, user_id)))

    def stats(self):
        with self.lock:
            return {"devices_online": len(self.devices), "browsers": sum(len(s) for s in self.clients.values()),
                    "devices_known": self.db.one("SELECT COUNT(*) AS n FROM devices")["n"]}

    # -- hubs ----------------------------------------------------------------------
    def device_auth(self, header, ip):
        """'Device <id>.<secret>' -> device id; raises PermissionError(status, text)."""
        parts = (header or "").split(" ", 1)
        if len(parts) != 2 or parts[0] != "Device" or "." not in parts[1]:
            raise PermissionError(401, "missing device credentials")
        device_id, secret = parts[1].strip().split(".", 1)
        if not DEVICE_ID_RE.match(device_id) or not 20 <= len(secret) <= 100:
            raise PermissionError(401, "malformed device credentials")
        digest = hashlib.sha256(secret.encode()).hexdigest()
        row = self.db.one("SELECT secret_hash FROM devices WHERE id = ?", (device_id,))
        if row:
            if not hmac.compare_digest(row["secret_hash"], digest):
                log.warning("device %s from %s presented a wrong secret", device_id, ip)
                raise PermissionError(401, "wrong secret for this device id")
        else:
            # trust on first use, within limits
            if self.limits.acquire([("register:" + client_key(ip), *REGISTER_LIMIT)]):
                raise PermissionError(429, "too many new devices from this address")
            if self.db.one("SELECT COUNT(*) AS n FROM devices")["n"] >= self.cfg.max_devices:
                self._prune_devices()
                if self.db.one("SELECT COUNT(*) AS n FROM devices")["n"] >= self.cfg.max_devices:
                    raise PermissionError(503, "this website accepts no more devices ([limits] max_devices)")
            self.db.run("INSERT INTO devices(id, secret_hash, created_at) VALUES(?, ?, ?)",
                        (device_id, digest, time.time()))
            log.info("new device %s registered from %s", device_id, ip)
        return device_id

    def _prune_devices(self):
        """Make room: forget the hubs no account added that are offline and were not seen for
        UNLINKED_TTL, or else the one of them seen longest ago. A real hub is online while it
        registers and is added soon after; one forgotten while offline registers again (with the
        same secret) when it reconnects. So made-up ids cannot keep real hubs out."""
        with self.lock:
            online = set(self.devices)
        idle = [r for r in self.db.all("SELECT id, COALESCE(last_seen, created_at) AS seen FROM devices "
                                       "WHERE id NOT IN (SELECT device_id FROM links) ORDER BY seen")
                if r["id"] not in online]
        doomed = [r["id"] for r in idle if r["seen"] < time.time() - UNLINKED_TTL] or [r["id"] for r in idle[:1]]
        for device_id in doomed:
            self.db.run("DELETE FROM devices WHERE id = ? AND id NOT IN (SELECT device_id FROM links)", (device_id,))

    def device_failed(self, device_id):
        """The request that presented these credentials was no WebSocket: keep no row for a hub
        that never connected."""
        self.db.run("DELETE FROM devices WHERE id = ? AND last_seen IS NULL AND "
                    "id NOT IN (SELECT device_id FROM links)", (device_id,))

    def serve_device(self, device_id, sock, ip):
        conn = DeviceConn(device_id, sock, ip)
        with self.lock:
            old = self.devices.get(device_id)
            self.devices[device_id] = conn
        if old:
            log.info("device %s reconnected, dropping the old connection", device_id)
            old.sock.close(4000, "replaced by a new connection")
        self.db.run("UPDATE devices SET last_seen = ?, last_ip = ? WHERE id = ?", (time.time(), ip, device_id))
        conn.send({"t": "welcome", "links": self.linked_users(device_id), "time": int(time.time())})
        for c in self._clients_of(device_id):
            c.send({"t": "online"})
        log.info("device %s online from %s", device_id, ip)
        try:
            while True:
                raw = sock.recv()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._from_device(conn, msg)
        finally:
            self._device_gone(conn)

    def _device_gone(self, conn):
        with self.lock:
            if self.devices.get(conn.id) is conn:
                del self.devices[conn.id]
                current = True
            else:
                current = False
            sessions, conn.sessions = list(conn.sessions.values()), {}
        conn.stop()
        conn.sock.close()
        self.db.run("UPDATE devices SET last_seen = ? WHERE id = ?", (time.time(), conn.id))
        for c in sessions:
            c.sid = None
        if current:
            log.info("device %s offline%s", conn.id, f" ({conn.sock.error})" if conn.sock.error else "")
            for c in self._clients_of(conn.id):
                c.send({"t": "offline"})

    def _from_device(self, dev, msg):
        t = msg.get("t")
        if t in ("accept", "data", "close"):
            sid = str(msg.get("sid") or "")
            with self.lock:
                client = dev.sessions.get(sid)
                if t == "close":
                    dev.sessions.pop(sid, None)
            if client is None or client.sid != sid:
                return
            if t == "accept":
                client.send({"t": "accept", "ep": msg.get("ep"), "np": msg.get("np"), "mac": msg.get("mac")})
            elif t == "data":
                client.send({"t": "data", "c": msg.get("c")})
            else:
                client.sid = None
                reason = str(msg.get("reason") or "")[:200]
                client.send({"t": "closed", "reason": reason, "code": 404 if reason in ("unknown link", "access removed")
                             else 0})
        elif t == "link":
            self._link_from_device(dev, msg)
        elif t == "push":
            self._push_from_device(dev, msg)

    def _link_from_device(self, dev, msg):
        try:
            user_id = int(msg.get("profile"))
        except (TypeError, ValueError):
            return
        if msg.get("op") == "add":
            # only an account that is adding this stove right now, i.e. has a pairing
            # session open to it (the hub answers pair.finish after this message)
            with self.lock:
                pairing = any(c.user["id"] == user_id and c.mode == "pair" for c in dev.sessions.values())
            user = self.accounts.get(user_id)
            if not pairing or not user or user["status"] != "active":
                log.warning("device %s tried to link account %s without a pairing session", dev.id, user_id)
                return
            self.db.run("INSERT INTO links(device_id, user_id, created_at) VALUES(?, ?, ?) "
                        "ON CONFLICT(device_id, user_id) DO NOTHING", (dev.id, user_id, time.time()))
            log.info("device %s linked %s", dev.id, user["username"])
        elif msg.get("op") == "remove":
            self.db.run("DELETE FROM links WHERE device_id = ? AND user_id = ?", (dev.id, user_id))
            self._end_sessions(dev.id, user_id, "access removed")

    def _push_from_device(self, dev, msg):
        sub = self.db.one("SELECT * FROM push_subs WHERE id = ?", (str(msg.get("sub") or ""),))
        if not sub or not self.is_linked(dev.id, sub["user_id"]):
            dev.send({"t": "push_result", "sub": msg.get("sub"), "ok": False, "gone": True})
            return
        if not self.pusher.available:
            return
        try:
            body = base64.b64decode(str(msg.get("body") or ""), validate=True)
        except ValueError:
            return
        if not body or len(body) > 4096:
            return

        def done(ok, gone, detail):
            if ok:
                self.db.run("UPDATE push_subs SET last_ok = ?, failures = 0 WHERE id = ?", (time.time(), sub["id"]))
            else:
                log.info("push to subscription %s failed: %s", sub["id"], detail)
                self.db.run("UPDATE push_subs SET failures = failures + 1 WHERE id = ?", (sub["id"],))
                if gone:
                    self.db.run("DELETE FROM push_subs WHERE id = ?", (sub["id"],))
            dev.send({"t": "push_result", "sub": sub["id"], "ok": ok, "gone": gone})

        self.pusher.submit(sub["endpoint"], body, min(int(msg.get("ttl") or 3600), 86400),
                           str(msg.get("urgency") or "normal"), msg.get("topic"), done)

    # -- browsers --------------------------------------------------------------------
    def _clients_of(self, device_id):
        with self.lock:
            return list(self.clients.get(device_id, ()))

    def serve_client(self, user, session_hash, device_id, sock, ip):
        client = ClientConn(user, session_hash, device_id, sock, ip)
        with self.lock:
            mine = sum(1 for s in self.clients.values() for c in s if c.user["id"] == user["id"])
            if mine >= MAX_CLIENTS_PER_USER:
                sock.close(ws.CLOSE_POLICY, "too many connections")
                client.stop()
                return
            self.clients.setdefault(device_id, set()).add(client)
        try:
            while True:
                raw = sock.recv()
                if raw is None:
                    break
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._from_client(client, msg)
        finally:
            with self.lock:
                group = self.clients.get(device_id)
                if group:
                    group.discard(client)
                    if not group:
                        del self.clients[device_id]
            self._end_client_session(client)
            client.stop()

    def _end_client_session(self, client, reason="browser left"):
        sid, client.sid = client.sid, None
        if not sid:
            return
        with self.lock:
            dev = self.devices.get(client.device_id)
            if dev:
                dev.sessions.pop(sid, None)
        if dev:
            dev.send({"t": "close", "sid": sid, "reason": reason})

    def _from_client(self, client, msg):
        t = msg.get("t")
        if t == "open":
            self._open(client, msg)
        elif t == "data":
            c = msg.get("c")
            if not client.sid or not isinstance(c, str):
                return
            if time.monotonic() - client.checked > SESSION_CHECK and not self._signed_in(client):
                return
            dev = self.devices.get(client.device_id)
            if dev is None:
                return
            # end this browser's session rather than lose a message (its nonces would
            # no longer match) or let one browser fill the hub's queue
            if len(c) > MAX_C:
                return self._refuse(client, "message too big")
            if dev.sender.pending > DEVICE_BACKLOG:
                return self._refuse(client, "the stove is busy - try again in a moment")
            dev.send({"t": "data", "sid": client.sid, "c": c})
        elif t == "close":
            self._end_client_session(client)

    def _refuse(self, client, reason):
        self._end_client_session(client, reason)
        client.send({"t": "closed", "reason": reason})

    def _signed_in(self, client):
        """The sign-in may have ended since the WebSocket was opened (signed out elsewhere, password
        changed, account disabled - also on the command line, which cannot reach these sockets)."""
        client.checked = time.monotonic()
        if self.db.one("SELECT 1 AS x FROM sessions WHERE token_hash = ? AND expires_at > ?",
                       (client.session_hash, time.time())):
            return True
        client.sock.close(4401, "signed out")
        return False

    def _open(self, client, msg):
        user = client.user
        mode = msg.get("mode")
        fields = {k: msg.get(k) for k in ("eb", "nb", "link")}
        if mode not in ("pair", "link") or not isinstance(fields["eb"], str) or len(fields["eb"]) > 200 \
                or not isinstance(fields["nb"], str) or len(fields["nb"]) > 64 \
                or (fields["link"] is not None and (not isinstance(fields["link"], str) or len(fields["link"]) > 64)):
            client.send({"t": "error", "message": "bad open request"})
            return
        if client.opens.acquire([("open", *OPEN_LIMIT)]):
            client.send({"t": "closed", "reason": "too many attempts, try again later"})
            return
        if not self._signed_in(client):
            return
        if mode == "pair":
            if self.limits.acquire([(f"pair:{user['id']}", *PAIR_LIMIT)]):
                client.send({"t": "closed", "reason": "too many attempts, try again later"})
                return
        elif not self.is_linked(client.device_id, user["id"]):
            client.send({"t": "closed", "reason": "This stove is not linked to your account.", "code": 404})
            return
        self._end_client_session(client, "new session")
        with self.lock:
            dev = self.devices.get(client.device_id)
            if dev is None:
                client.send({"t": "offline"})
                return
            sid = secrets.token_hex(8)
            client.sid, client.mode = sid, mode
            dev.sessions[sid] = client
        dev.send({"t": "open", "sid": sid, "mode": mode, "link": fields["link"], "eb": fields["eb"], "nb": fields["nb"],
                  "profile": {"id": str(user["id"]), "name": user["username"]}})

    # -- account changes on the website --------------------------------------------------
    def _end_sessions(self, device_id, user_id, reason):
        for c in self._clients_of(device_id):
            if c.user["id"] == user_id and c.sid:
                c.sid = None
                c.send({"t": "closed", "reason": reason, "code": 404})

    def unlink(self, device_id, user_id):
        """The account removed the stove on the website."""
        self.db.run("DELETE FROM links WHERE device_id = ? AND user_id = ?", (device_id, user_id))
        dev = self.devices.get(device_id)
        if dev:
            dev.send({"t": "links", "links": self.linked_users(device_id)})
        self._end_sessions(device_id, user_id, "access removed")

    def user_removed(self, user_id, device_ids):
        """An account was deleted: tell its hubs (their links to it are gone)."""
        for device_id in device_ids:
            dev = self.devices.get(device_id)
            if dev:
                dev.send({"t": "links", "links": self.linked_users(device_id)})
        self.drop_user(user_id)

    def drop_user(self, user_id, session_hash=None, keep_hash=None):
        """Close the browsers of an account (all, those of one sign-in, or all but those of one)."""
        with self.lock:
            victims = [c for s in self.clients.values() for c in s
                       if c.user["id"] == user_id and (session_hash is None or c.session_hash == session_hash)
                       and (keep_hash is None or c.session_hash != keep_hash)]
        for c in victims:
            c.sock.close(4401, "signed out")
