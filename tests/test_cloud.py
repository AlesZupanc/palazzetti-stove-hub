"""The website on the VPS (www/stovecloud) together with a real hub: accounts without
passwords on the server, the encrypted vault, adding a stove with its token,
end-to-end encrypted control, unlinking, and push notifications."""

import base64
import configparser
import hashlib
import http.client
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest

from helpers import ROOT, FakeStove, make_config, temp_db

sys.path.insert(0, os.path.join(ROOT, "www"))

from cryptography.hazmat.backends import default_backend  # noqa: E402
from cryptography.hazmat.primitives import hashes, serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: E402

from stovecloud import accounts as accmod  # noqa: E402
from stovecloud import config as cloudcfg  # noqa: E402
from stovecloud.accounts import Accounts  # noqa: E402
from stovecloud.db import DB as CloudDB  # noqa: E402
from stovecloud.hub import Hub as CloudHub  # noqa: E402
from stovecloud.mailer import Mailer  # noqa: E402
from stovecloud.push import Pusher  # noqa: E402
from stovecloud.updates import Updates  # noqa: E402
from stovecloud.web import CloudServer, create_setup_link  # noqa: E402

from stovemaster import e2e, ws  # noqa: E402
from stovemaster.api import Hub  # noqa: E402
from stovemaster.cloud import CloudLink  # noqa: E402
from stovemaster.scheduler import Scheduler  # noqa: E402

ITER = 100000


def derive(password, salt_b64, iterations=ITER):
    """What crypto.js Account.derive does in the browser."""
    master = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.b64decode(salt_b64), iterations)
    auth = e2e.hkdf(master, b"stove-account-v1", b"stove auth", 32)
    vault_key = e2e.hkdf(master, b"stove-account-v1", b"stove vault", 32)
    return base64.b64encode(auth).decode(), vault_key


def seal_vault(key, obj):
    iv = os.urandom(12)
    return base64.b64encode(iv + AESGCM(key).encrypt(iv, json.dumps(obj).encode(), None)).decode()


def open_vault(key, blob):
    raw = base64.b64decode(blob)
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None))


class FakePushService:
    """Stands in for urllib's opener: records what would be posted to FCM/Apple/Mozilla."""

    def __init__(self):
        self.posts = []
        self.status = 201

    def open(self, req, timeout=None):
        self.posts.append(req)
        if self.status >= 400:
            import urllib.error
            raise urllib.error.HTTPError(req.full_url, self.status, "gone", {}, io.BytesIO(b"gone"))

        class Resp:
            status = 201

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False
        return Resp()


class Browser:
    """An account's browser: HTTPS API calls with a cookie, and WebSocket sessions."""

    count = 0

    def __init__(self, port, prefix="/mystove"):
        self.port, self.prefix, self.cookie = port, prefix, None
        Browser.count += 1
        self.ip = f"198.51.100.{Browser.count}"      # every browser comes from its own address

    def api(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        headers = {"X-Requested-With": "stove", "X-Real-IP": self.ip}
        if self.cookie:
            headers["Cookie"] = self.cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        conn.request(method, f"{self.prefix}/api/{path}", body=data, headers=headers)
        r = conn.getresponse()
        raw = r.read()
        cookie = r.getheader("Set-Cookie")
        if cookie:
            self.cookie = cookie.split(";")[0]
        conn.close()
        return r.status, (json.loads(raw) if raw else None), cookie

    def connect(self, device_id, origin=None):
        return ws.connect(f"ws://127.0.0.1:{self.port}{self.prefix}/ws/client?dev={device_id}",
                          headers=[("Cookie", self.cookie or ""), ("Origin", origin or f"http://localhost:{self.port}")])

    @staticmethod
    def recv(sock, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            raw = sock.recv()
            if raw is None:
                return None
            return json.loads(raw)
        return None

    def session(self, device_id, mode, psk, link=None):
        sock = self.connect(device_id)
        msg, state = e2e.browser_open(mode, link)
        sock.send(json.dumps(msg))
        while True:
            reply = self.recv(sock)
            if reply is None or reply.get("t") != "online":
                break
        if reply is None or reply.get("t") != "accept":
            return sock, None, reply
        try:
            return sock, e2e.browser_finish(state, reply, psk, device_id), reply
        except e2e.E2EError:
            sock.close()
            raise

    def rpc(self, sock, session, method, params=None, rid=1):
        sock.send(json.dumps({"t": "data", "c": session.seal({"id": rid, "m": method, "p": params or {}})}))
        while True:
            msg = self.recv(sock)
            if msg is None:
                raise AssertionError("connection closed")
            if msg.get("t") != "data":
                continue
            obj = session.open(msg["c"])
            if obj.get("id") == rid:
                return obj
            self.last_event = obj


class Website(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = tempfile.mkdtemp(prefix="stove-cloud-")
        path = os.path.join(folder, "cloud.ini")
        with open(path, "w") as f:
            f.write(cloudcfg.render({("server", "port"): "0", ("server", "public_url"): "http://localhost:1/mystove/",
                                     ("paths", "db"): os.path.join(folder, "cloud.db")}))
        cls.cfg = cloudcfg.Config(path)
        cls.db = CloudDB(cls.cfg.db_path)
        cls.accounts = Accounts(cls.db)
        cls.mailer = Mailer(cls.cfg)
        cls.mail = []
        cls.mailer.send = lambda to, subject, body: cls.mail.append((to, subject, body)) or True
        cls.pusher = Pusher(cls.db, lambda: "mailto:admin@example.com", ["push.example.com"])
        cls.push_service = FakePushService()
        cls.pusher.opener = cls.push_service
        cls.hub = CloudHub(cls.db, cls.accounts, cls.pusher, cls.cfg)
        cls.server = CloudServer(cls.cfg, cls.db, cls.accounts, cls.mailer, cls.pusher, cls.hub)
        cls.port = cls.server.server_address[1]
        # the public origin must match what browsers send
        cls.cfg.public_url = f"http://localhost:{cls.port}/mystove/"
        cls.cfg.origin = f"http://localhost:{cls.port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

        # a hub at home with remote access switched on
        cls.pi_db = temp_db()
        pcfg = make_config(cloud_url=f"http://127.0.0.1:{cls.port}/mystove/")
        cls.stove = FakeStove()
        cls.pi = Hub(pcfg, cls.pi_db, cls.stove, Scheduler(cls.pi_db, cls.stove, pcfg))
        cls.link = CloudLink(cls.pi, pcfg, cls.pi_db)
        cls.link.start()
        deadline = time.time() + 10
        while not cls.link.connected and time.time() < deadline:
            time.sleep(0.05)
        assert cls.link.connected, cls.link.last_error

    @classmethod
    def tearDownClass(cls):
        cls.link.stop()
        cls.server.shutdown()
        cls.server.server_close()

    # -- helpers -----------------------------------------------------------------------
    def make_user(self, name, password="secret pass 1", approve=True):
        b = Browser(self.port)
        salt = base64.b64encode(os.urandom(16)).decode()
        auth, vault_key = derive(password, salt)
        status, body, _ = b.api("POST", "register", {"username": name, "email": f"{name}@example.com", "salt": salt,
                                                       "auth": auth, "iterations": ITER})
        self.assertEqual(status, 200, body)
        if approve:
            u = self.accounts.find(name)
            self.accounts.set_status(u["id"], "active")
        return b, password, vault_key

    def sign_in(self, b, name, password):
        status, pre, _ = b.api("POST", "prelogin", {"login": name})
        auth, vault_key = derive(password, pre["salt"], pre["iterations"])
        status, body, _ = b.api("POST", "login", {"login": name, "auth": auth})
        return status, body, vault_key

    def wait(self, cond, timeout=5):
        deadline = time.time() + timeout
        while not cond() and time.time() < deadline:
            time.sleep(0.03)
        return cond()

    def pair_session(self, name):
        """A new signed-in account with a pairing session open to the hub (hello read)."""
        b, pw, _ = self.make_user(name)
        self.sign_in(b, name, pw)
        device_id, secret = e2e.parse_token(self.link.token)
        sock, session, reply = b.session(device_id, "pair", secret)
        self.assertIsNotNone(session, reply)
        session.open(b.recv(sock)["c"])           # the hello
        return b, sock, session, device_id

    @staticmethod
    def next_closed(b, sock):
        while True:
            msg = b.recv(sock)
            if msg is None or msg.get("t") == "closed":
                return msg

    # -- accounts --------------------------------------------------------------------
    def test_1_admin_setup_link_registration_and_approval(self):
        link = create_setup_link(self.cfg, self.accounts, "boss", "boss@example.com")
        token = link.split("#/setup/")[1]
        b = Browser(self.port)
        salt = base64.b64encode(os.urandom(16)).decode()
        auth, _ = derive("admin pass 1", salt)
        status, body, _ = b.api("POST", "setup", {"token": token, "salt": salt, "auth": auth, "iterations": ITER})
        self.assertEqual((status, body["username"]), (200, "boss"))
        self.assertEqual(b.api("POST", "setup", {"token": token, "salt": salt, "auth": auth})[0], 404)   # used up
        status, body, _ = self.sign_in(b, "boss", "admin pass 1")
        self.assertEqual((status, body["user"]["role"]), (200, "admin"))
        # someone asks for an account; the admin is told by e-mail and approves through the link
        guest = Browser(self.port)
        salt2 = base64.b64encode(os.urandom(16)).decode()
        status, body, _ = guest.api("POST", "register", {"username": "marta", "email": "marta@example.com",
                                                         "salt": salt2, "auth": derive("marta pass 1", salt2)[0],
                                                         "iterations": ITER})
        self.assertEqual(status, 200, body)
        to, subject, text = self.mail[-1]
        self.assertEqual(to, "boss@example.com")
        approve = text.split("#/approve/")[1].split()[0]
        self.assertEqual(self.sign_in(guest, "marta", "marta pass 1")[0], 403)     # waiting for approval
        self.assertEqual(guest.api("POST", "approval", {"token": approve})[1]["user"]["username"], "marta")
        self.assertEqual(guest.api("POST", "approval/decide", {"token": approve, "decision": "approve"})[0], 200)
        self.assertEqual(self.sign_in(guest, "marta", "marta pass 1")[0], 200)
        self.assertEqual(self.mail[-1][0], "marta@example.com")
        # the admin sees everybody; ordinary users cannot
        self.assertEqual(b.api("GET", "users")[0], 200)
        self.assertEqual(guest.api("GET", "users")[0], 403)
        # "Update now" for the website: only the admin, and only when automatic updates are on
        tmp = tempfile.mkdtemp()
        unit = os.path.join(tmp, "stove-cloud-update.path")
        self.server.updates = Updates(tmp, unit)
        self.assertEqual(b.api("GET", "system")[1]["update"]["available"], False)
        self.assertEqual(b.api("POST", "system/update")[0], 409)
        open(unit, "w").close()
        self.assertEqual(guest.api("POST", "system/update")[0], 403)
        status, body, _ = b.api("POST", "system/update")
        self.assertEqual((status, body["available"]), (200, True))
        self.assertTrue(os.path.exists(os.path.join(tmp, "update-request")))

    def test_2_server_never_sees_the_password(self):
        b, pw, _ = self.make_user("noel", "noel's secret")
        row = self.accounts.find("noel")
        self.assertNotIn("noel's secret", json.dumps(row))
        self.assertTrue(row["auth_hash"].startswith("hmac-sha256$"))
        # wrong password, unknown user: same answer, and prelogin does not reveal accounts
        self.assertEqual(self.sign_in(Browser(self.port), "noel", "wrong")[0], 401)
        a = Browser(self.port).api("POST", "prelogin", {"login": "nobody"})[1]
        self.assertEqual(a, Browser(self.port).api("POST", "prelogin", {"login": "nobody"})[1])   # stable
        self.assertEqual(len(base64.b64decode(a["salt"])), 16)

    def test_3_vault_versions(self):
        b, pw, key = self.make_user("vera")
        status, body, _ = self.sign_in(b, "vera", pw)
        self.assertEqual(body["vault"], {"blob": None, "version": 0})
        blob = seal_vault(key, {"v": 1, "devices": []})
        status, body, _ = b.api("PUT", "vault", {"blob": blob, "version": 0})
        self.assertEqual((status, body), (200, {"version": 1}))
        status, body, _ = b.api("PUT", "vault", {"blob": blob, "version": 0})    # someone else was faster
        self.assertEqual((status, body["vault"]["version"]), (409, 1))
        # change the password: the vault is re-encrypted by the browser, other sign-ins end
        other = Browser(self.port)
        self.sign_in(other, "vera", pw)
        salt = base64.b64encode(os.urandom(16)).decode()
        new_auth, new_key = derive("new password 2", salt)
        old_auth = derive(pw, self.accounts.find("vera")["salt"])[0]
        status, body, _ = b.api("POST", "password/change", {"old_auth": old_auth, "salt": salt, "auth": new_auth,
                                                             "iterations": ITER, "vault": seal_vault(new_key, {"v": 1, "devices": [1]})})
        self.assertEqual(status, 200, body)
        self.assertEqual(other.api("GET", "devices")[0], 401)
        self.assertEqual(b.api("GET", "devices")[0], 200)
        status, body, key2 = self.sign_in(Browser(self.port), "vera", "new password 2")
        self.assertEqual(open_vault(key2, body["vault"]["blob"]), {"v": 1, "devices": [1]})

    # -- stoves ----------------------------------------------------------------------
    def test_4_add_stove_control_and_remove(self):
        b, pw, key = self.make_user("ana")
        self.sign_in(b, "ana", pw)
        device_id, secret = e2e.parse_token(self.link.token)
        self.assertEqual(device_id, self.link.device_id)
        # a link session before adding the stove is refused by the website
        sock, session, reply = b.session(device_id, "link", os.urandom(32), link="x")
        self.assertEqual((reply["t"], reply["code"]), ("closed", 404))
        sock.close()
        # a wrong token is caught in the browser (the hub cannot prove it knows it)
        with self.assertRaises(e2e.E2EError):
            b.session(device_id, "pair", os.urandom(15))
        # the right token: hello first, then "add"
        sock, session, _ = b.session(device_id, "pair", secret)
        hello = session.open(b.recv(sock)["c"])
        self.assertEqual((hello["ev"], hello["d"]["pairing"], hello["d"]["device"]["id"]), ("hello", True, device_id))
        self.assertEqual(b.rpc(sock, session, "status.get")["code"], 403)        # nothing else before adding
        result = b.rpc(sock, session, "pair.finish", rid=2)["r"]
        sock.close()
        self.assertTrue(self.wait(lambda: self.hub.is_linked(device_id, self.accounts.find("ana")["id"])))
        # from now on the link key opens sessions; the website routes them
        sock, session, _ = b.session(device_id, "link", base64.b64decode(result["key"]), link=result["link"])
        self.assertIsNotNone(session)
        status = b.rpc(sock, session, "stove.action", {"action": "setpoint", "value": 23.5}, rid=3)
        self.assertEqual(status["r"]["setpoint"], 23.5)
        self.assertIn(("setpoint", 23.5), self.stove.commands)
        ev = self.pi_db.events(1)[0]
        self.assertEqual((ev["actor"], ev["via"]), ("ana", "remote"))
        self.assertEqual(b.rpc(sock, session, "cloud.info", rid=4)["code"], 403)   # home only
        # what the website saw: opaque base64 only
        devices = b.api("GET", "devices")[1]["devices"]
        self.assertEqual((devices[0]["id"], devices[0]["online"]), (device_id, True))
        # removing it at home ends the session and the website stops routing
        link_id = result["link"]
        self.link.unlink(link_id, reason="test")
        closed = None
        while closed is None:
            msg = b.recv(sock)
            if msg and msg.get("t") == "closed":
                closed = msg
        self.assertEqual(closed["code"], 404)
        self.assertTrue(self.wait(lambda: not self.hub.is_linked(device_id, self.accounts.find("ana")["id"])))
        sock.close()

    def test_5_remove_on_the_website_reaches_the_hub(self):
        b, pw, key = self.make_user("rok")
        self.sign_in(b, "rok", pw)
        device_id, secret = e2e.parse_token(self.link.token)
        sock, session, _ = b.session(device_id, "pair", secret)
        session.open(b.recv(sock)["c"])           # the hello (every message must be read, in order)
        b.rpc(sock, session, "pair.finish", rid=2)
        sock.close()
        uid = str(self.accounts.find("rok")["id"])
        self.assertTrue(self.wait(lambda: any(l["profile_id"] == uid for l in self.link.links.values())))
        self.assertEqual(b.api("DELETE", f"devices/{device_id}")[0], 200)
        self.assertTrue(self.wait(lambda: not any(l["profile_id"] == uid for l in self.link.links.values())))

    def test_6_new_token_and_session_rules(self):
        device_id, old_secret = e2e.parse_token(self.link.token)
        self.link.regenerate()
        b, pw, _ = self.make_user("tina")
        self.sign_in(b, "tina", pw)
        with self.assertRaises(e2e.E2EError):
            b.session(device_id, "pair", old_secret)
        # cross-site pages may not open sessions, and signed-out browsers are told so
        with self.assertRaises(ws.HandshakeError):
            b.connect(device_id, origin="https://evil.example.com")
        anon = Browser(self.port)
        sock = anon.connect(device_id)
        self.assertIsNone(sock.recv())
        self.assertEqual(sock.close_code, 4401)

    def test_6b_pairing_works_once_and_ends_with_a_new_token(self):
        b, sock, session, device_id = self.pair_session("lea")
        self.assertIn("key", b.rpc(sock, session, "pair.finish", rid=2)["r"])
        self.assertEqual(b.rpc(sock, session, "pair.finish", rid=3)["code"], 409)     # once per session
        sock.close()
        b2, sock2, _, _ = self.pair_session("leon")
        self.link.regenerate()                    # e.g. the token leaked: sessions opened with it end
        self.assertEqual(self.next_closed(b2, sock2)["reason"], "the token was replaced")
        sock2.close()

    def test_6c_hubs_cannot_link_accounts_on_their_own(self):
        self.make_user("vic")
        victim = self.accounts.find("vic")["id"]
        self.link._send({"t": "link", "op": "add", "profile": str(victim)})          # no pairing session at all
        b, sock, session, device_id = self.pair_session("ivo")
        self.link._send({"t": "link", "op": "add", "profile": str(victim)})          # somebody else's
        b.rpc(sock, session, "pair.finish", rid=2)
        sock.close()
        self.assertTrue(self.wait(lambda: self.hub.is_linked(device_id, self.accounts.find("ivo")["id"])))
        self.assertFalse(self.hub.is_linked(device_id, victim))     # handled before ivo's, in order

    def test_6d_oversized_or_garbage_data_ends_only_that_session(self):
        uplink = self.link.ws
        b, sock, _, _ = self.pair_session("max")
        sock.send(json.dumps({"t": "data", "c": "A" * (256 * 1024 + 1)}))
        self.assertEqual(self.next_closed(b, sock)["reason"], "message too big")
        sock.close()
        # smaller ones reach the hub, which cannot decrypt them: again only this session ends
        b2, sock2, _, _ = self.pair_session("mia")
        sock2.send(json.dumps({"t": "data", "c": "A" * 250_000}))
        self.assertEqual(self.next_closed(b2, sock2)["reason"], "decryption failed")
        sock2.close()
        self.assertIs(self.link.ws, uplink)       # the hub stayed connected all along
        self.assertTrue(self.link.connected)

    def test_7_push_notification_end_to_end(self):
        b, pw, _ = self.make_user("pia")
        self.sign_in(b, "pia", pw)
        device_id, secret = e2e.parse_token(self.link.token)
        sock, session, _ = b.session(device_id, "pair", secret)
        session.open(b.recv(sock)["c"])           # the hello (every message must be read, in order)
        result = b.rpc(sock, session, "pair.finish", rid=2)["r"]
        sock.close()
        # the browser subscribes at its push service and registers the endpoint with the website ...
        self.assertEqual(b.api("POST", "push/subscriptions", {"endpoint": "https://evil.internal/x"})[0], 400)
        status, body, _ = b.api("POST", "push/subscriptions", {"endpoint": "https://fcm.googleapis.com/fcm/send/abc"})
        sub = body["sub"]
        # ... and hands the payload keys to the stove only, inside the encrypted session
        ua = ec.generate_private_key(ec.SECP256R1(), default_backend())
        ua_pub = ua.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        auth_secret = os.urandom(16)
        b64u = lambda raw: base64.urlsafe_b64encode(raw).decode().rstrip("=")  # noqa: E731
        sock, session, _ = b.session(device_id, "link", base64.b64decode(result["key"]), link=result["link"])
        r = b.rpc(sock, session, "push.subscribe", {"sub": sub, "p256dh": b64u(ua_pub), "auth": b64u(auth_secret),
                                                    "categories": ["problems"]}, rid=5)
        self.assertEqual(r["r"]["categories"], ["problems"])
        self.push_service.posts.clear()
        self.assertTrue(b.rpc(sock, session, "push.test", {"sub": sub}, rid=6)["r"]["ok"])
        self.assertTrue(self.wait(lambda: self.push_service.posts))
        req = self.push_service.posts[0]
        self.assertEqual(req.full_url, "https://fcm.googleapis.com/fcm/send/abc")
        self.assertEqual(req.get_header("Content-encoding"), "aes128gcm")
        # the VAPID signature verifies with the website's public key
        vapid = req.get_header("Authorization")
        jwt = vapid.split("t=")[1].split(",")[0]
        head, claims, sig = jwt.split(".")
        pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
        self.assertEqual(json.loads(pad(claims))["aud"], "https://fcm.googleapis.com")
        pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), pad(vapid.split("k=")[1]))
        raw = pad(sig)
        pub.verify(encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big")),
                   f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))
        # and only the browser can read the notification
        body = req.data
        salt, keyid = body[:16], body[21:86]
        z = ua.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), keyid))
        ikm = e2e.hkdf(z, auth_secret, b"WebPush: info\0" + ua_pub + keyid, 32)
        cek = e2e.hkdf(ikm, salt, b"Content-Encoding: aes128gcm\0", 16)
        nonce = e2e.hkdf(ikm, salt, b"Content-Encoding: nonce\0", 12)
        plain = AESGCM(cek).decrypt(nonce, body[86:], None).rstrip(b"\x02")
        note = json.loads(plain)
        self.assertEqual(note["title"], "Test notification")
        # an expired subscription is dropped on both sides
        self.push_service.status = 410
        self.push_service.posts.clear()
        b.rpc(sock, session, "push.test", {"sub": sub}, rid=7)
        self.assertTrue(self.wait(lambda: not self.db.one("SELECT 1 AS x FROM push_subs WHERE id = ?", (sub,))))
        self.assertTrue(self.wait(lambda: not self.pi_db.one("SELECT 1 AS x FROM push_subs WHERE id = ?", (sub,))))
        self.push_service.status = 201
        sock.close()

    def test_8_device_auth(self):
        other = ws.connect(f"ws://127.0.0.1:{self.port}/mystove/ws/device",
                           headers=[("Authorization", f"Device NEWDEV22.{'s' * 30}")])
        welcome = json.loads(other.recv())
        self.assertEqual(welcome, {"t": "welcome", "links": [], "time": welcome["time"]})
        other.close()
        with self.assertRaises(ws.HandshakeError) as ctx:     # same id, other secret: refused
            ws.connect(f"ws://127.0.0.1:{self.port}/mystove/ws/device",
                       headers=[("Authorization", f"Device NEWDEV22.{'x' * 30}")])
        self.assertEqual(ctx.exception.status, 401)
        with self.assertRaises(ws.HandshakeError):
            ws.connect(f"ws://127.0.0.1:{self.port}/mystove/ws/device")

    def test_9_app_files_and_headers(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/mystove/")
        r = conn.getresponse()
        body = r.read()
        self.assertEqual(r.status, 200)
        self.assertIn(b"crypto.js", body)
        self.assertIn(f"ws://localhost:{self.port}", r.getheader("Content-Security-Policy"))
        conn.request("GET", "/mystove/env.json")
        env = json.loads(conn.getresponse().read())
        self.assertEqual((env["mode"], env["registration"]), ("cloud", True))
        self.assertTrue(env["push_key"])
        conn.request("GET", "/mystove")
        r = conn.getresponse()
        r.read()
        self.assertEqual((r.status, r.getheader("Location")), (301, "/mystove/"))
        conn.request("GET", "/other/")
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 404)
        conn.request("POST", "/mystove/api/logout")       # no X-Requested-With: refused
        r = conn.getresponse()
        r.read()
        self.assertEqual(r.status, 403)
        conn.close()


class Unit(unittest.TestCase):
    def test_auth_hash_and_secrets(self):
        auth = base64.b64encode(os.urandom(32)).decode()
        h = accmod.hash_auth(auth)
        self.assertTrue(accmod.verify_auth(auth, h))
        self.assertFalse(accmod.verify_auth(base64.b64encode(os.urandom(32)).decode(), h))
        self.assertFalse(accmod.verify_auth("junk", h))
        self.assertFalse(accmod.verify_auth(auth, None))
        with self.assertRaises(accmod.AuthError):
            accmod.clean_secret("AA==", auth, ITER)
        with self.assertRaises(accmod.AuthError):
            accmod.clean_secret(base64.b64encode(os.urandom(16)).decode(), auth, 1000)

    def test_login_throttling(self):
        db = CloudDB(os.path.join(tempfile.mkdtemp(), "c.db"))
        acc = Accounts(db)
        salt = base64.b64encode(os.urandom(16)).decode()
        auth, _ = derive("pw pw pw pw", salt)
        uid = acc.create("ana", "ana@example.com", salt, auth, ITER, status="active")
        wrong = base64.b64encode(os.urandom(32)).decode()
        for _ in range(accmod.LOGIN_PAIR_LIMIT[0]):
            with self.assertRaises(accmod.AuthError):
                acc.authenticate("ana", wrong, "203.0.113.1")
        with self.assertRaises(accmod.AuthError) as ctx:
            acc.authenticate("ana", auth, "203.0.113.1")
        self.assertEqual(ctx.exception.status, 429)
        self.assertEqual(acc.authenticate("ana", auth, "198.51.100.2")["id"], uid)

    def test_push_endpoint_allowlist(self):
        db = CloudDB(os.path.join(tempfile.mkdtemp(), "c.db"))
        p = Pusher(db, lambda: "mailto:x@example.com", ["push.example.org"])
        for ok in ("https://fcm.googleapis.com/fcm/send/x", "https://web.push.apple.com/abc",
                   "https://updates.push.services.mozilla.com/wpush/v2/x", "https://push.example.org/1"):
            p.check_endpoint(ok)
        for bad in ("http://fcm.googleapis.com/x", "https://127.0.0.1/x", "https://fcm.googleapis.com.evil.com/x",
                    "https://evilpush.apple.com.attacker.net/", "file:///etc/passwd"):
            with self.assertRaises(Exception):
                p.check_endpoint(bad)

    def test_busy_hub_refuses_more_data_instead_of_dropping(self):
        from stovecloud import hub as hubmod

        class Peer:
            def __init__(self):
                self.sent = []

            def send(self, obj):
                self.sent.append(obj)
                return True

        hub = CloudHub(None, None, None, None)
        dev, client = Peer(), Peer()
        dev.sender = Peer()
        dev.sender.pending = hubmod.DEVICE_BACKLOG + 1
        dev.sessions = {"s1": client}
        client.sid, client.device_id = "s1", "ABCDEFGH"
        hub.devices["ABCDEFGH"] = dev
        hub._from_client(client, {"t": "data", "c": "abc"})
        self.assertEqual(client.sent, [{"t": "closed", "reason": "the stove is busy - try again in a moment"}])
        self.assertEqual(dev.sent, [{"t": "close", "sid": "s1", "reason": "the stove is busy - try again in a moment"}])
        self.assertEqual((client.sid, dev.sessions), (None, {}))
        dev.sender.pending, dev.sent, client.sid, dev.sessions = 0, [], "s2", {"s2": client}
        hub._from_client(client, {"t": "data", "c": "abc"})
        self.assertEqual(dev.sent, [{"t": "data", "sid": "s2", "c": "abc"}])

    def test_setup_mail_without_questions(self):
        path = os.path.join(tempfile.mkdtemp(), "cloud.ini")

        def cli(*args, env=None):
            return subprocess.run([sys.executable, os.path.join(ROOT, "www", "stove_cloud.py"), "--config", path] + list(args),
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True,
                                  env=dict(os.environ, **(env or {})), timeout=60)

        def mail():
            c = configparser.ConfigParser(interpolation=None)
            c.read(path)
            return dict(c["mail"])

        db_path = os.path.join(os.path.dirname(path), "cloud.db")
        self.assertEqual(cli("init-config", "--public-url", "https://example.com/mystove/", "--db", db_path).returncode, 0)
        # what mail-server.sh sets: the local Postfix, no login
        r = cli("setup-mail", "--host", "127.0.0.1", "--port", "25", "--from", "Stove <stove@example.com>")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(mail(), {"smtp_host": "127.0.0.1", "smtp_port": "25", "smtp_security": "none", "smtp_user": "",
                                  "smtp_password": "", "from_addr": "Stove <stove@example.com>"})
        # a provider: the password comes from the environment, not the command line
        r = cli("setup-mail", "--host", "smtp.example.net", "--user", "me@example.net", env={"STOVE_SMTP_PASSWORD": "s3cr3t"})
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual((mail()["smtp_port"], mail()["smtp_security"], mail()["smtp_password"], mail()["from_addr"]),
                         ("587", "starttls", "s3cr3t", "me@example.net"))
        self.assertNotEqual(cli("setup-mail", "--host", "smtp.example.net").returncode, 0)    # no sender
        self.assertEqual(cli("setup-mail", "--host", "").returncode, 0)                       # e-mail off
        self.assertEqual(mail()["smtp_host"], "")
        self.assertIn("--host", cli("setup-mail", "--help").stdout)       # mail-server.sh looks for it
        # admin-emails: who gets the test mail
        r = cli("admin", "boss", "boss@example.com", "--no-mail")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(cli("admin-emails").stdout.split(), ["boss@example.com"])

    def test_mail_headers(self):
        from stovecloud import mailer as mailmod
        sent = []

        class FakeSMTP:
            def __init__(self, host, port, timeout=None):
                sent.append((host, port))

            def ehlo(self):
                pass

            def send_message(self, msg):
                sent.append(msg)

            def quit(self):
                pass

        cfg = types.SimpleNamespace(smtp_host="127.0.0.1", smtp_port=25, smtp_security="none", smtp_user="",
                                    smtp_password="", mail_from="Stove <stove@example.com>")
        real, mailmod.smtplib.SMTP = mailmod.smtplib.SMTP, FakeSMTP
        try:
            mid = mailmod.Mailer(cfg).send_now("ana@example.org", "Hello", "Body")
        finally:
            mailmod.smtplib.SMTP = real
        msg = sent[1]
        self.assertEqual(sent[0], ("127.0.0.1", 25))
        self.assertEqual(msg["Message-ID"], mid)
        self.assertTrue(mid.endswith("@example.com>"), mid)          # not "example.com>>"
        self.assertEqual((msg["From"], msg["Auto-Submitted"]), ("Stove <stove@example.com>", "auto-generated"))

    def test_shared_websocket_module_is_identical(self):
        with open(os.path.join(ROOT, "stovemaster", "ws.py"), "rb") as a, \
                open(os.path.join(ROOT, "www", "stovecloud", "ws.py"), "rb") as b:
            self.assertEqual(a.read(), b.read(), "keep www/stovecloud/ws.py identical to stovemaster/ws.py")
        with open(os.path.join(ROOT, "stovemaster", "updates.py"), "rb") as a, \
                open(os.path.join(ROOT, "www", "stovecloud", "updates.py"), "rb") as b:
            self.assertEqual(a.read(), b.read(), "keep www/stovecloud/updates.py identical to stovemaster/updates.py")


if __name__ == "__main__":
    unittest.main()
