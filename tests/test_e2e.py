"""End-to-end encryption: token, handshake, sessions, push payloads, and interop with crypto.js."""

import base64
import hashlib
import json
import os
import shutil
import subprocess
import unittest

from helpers import ROOT

from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from stovemaster import e2e, webpush


def unb64url(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


class TokenFormat(unittest.TestCase):
    def test_roundtrip_and_errors(self):
        for _ in range(20):
            did, secret = e2e.new_device_id(), e2e.new_pair_secret()
            tok = e2e.format_token(did, secret)
            self.assertRegex(tok, r"^([A-Z2-7]{4}-){7}[A-Z2-7]{4}$")
            self.assertEqual(e2e.parse_token(tok.lower().replace("-", " ")), (did, secret))
        for bad in ("", "ABCD", "1" * 32, "A" * 33):
            with self.assertRaises(ValueError):
                e2e.parse_token(bad)


class Handshake(unittest.TestCase):
    def setUp(self):
        self.did, self.psk = e2e.new_device_id(), os.urandom(32)

    def pair(self, mode="link", link="L1", hub_psk=None, hub_device=None):
        msg, state = e2e.browser_open(mode, link)
        accept, hub = e2e.hub_accept(msg, hub_psk or self.psk, hub_device or self.did)
        return e2e.browser_finish(state, accept, self.psk, self.did), hub

    def test_messages_both_ways(self):
        browser, hub = self.pair()
        for i in range(3):
            self.assertEqual(hub.open(browser.seal({"id": i, "m": "status.get"})), {"id": i, "m": "status.get"})
            self.assertEqual(browser.open(hub.seal({"ev": "status", "n": i})), {"ev": "status", "n": i})

    def test_wrong_key_or_device_is_detected(self):
        with self.assertRaises(e2e.E2EError):
            self.pair(hub_psk=os.urandom(32))          # the hub does not know our key
        with self.assertRaises(e2e.E2EError):
            self.pair(hub_device="ZZZZZZZZ")           # the VPS routed us to another device

    def test_replay_reorder_tamper(self):
        browser, hub = self.pair()
        a, b = browser.seal({"n": 1}), browser.seal({"n": 2})
        with self.assertRaises(e2e.E2EError):
            hub.open(b)                                # out of order
        self.assertEqual(hub.open(a), {"n": 1})
        with self.assertRaises(e2e.E2EError):
            hub.open(a)                                # replayed
        raw = bytearray(base64.b64decode(b))
        raw[3] ^= 1
        with self.assertRaises(e2e.E2EError):
            hub.open(base64.b64encode(bytes(raw)).decode())
        self.assertEqual(hub.open(b), {"n": 2})

    def test_bad_open_messages(self):
        for msg in ({"mode": "x", "eb": "", "nb": ""}, {"mode": "pair", "eb": "AA==", "nb": "AA=="},
                    {"mode": "pair", "eb": base64.b64encode(b"\x04" + b"\x01" * 64).decode(),
                     "nb": base64.b64encode(b"n" * 16).decode()}):
            with self.assertRaises(e2e.E2EError):
                e2e.hub_accept(msg, self.psk, self.did)


class WebPush(unittest.TestCase):
    def test_rfc8291_example(self):
        as_priv = ec.derive_private_key(int.from_bytes(unb64url("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"), "big"),
                                        ec.SECP256R1(), default_backend())
        as_pub = as_priv.public_key().public_bytes(serialization.Encoding.X962,
                                                   serialization.PublicFormat.UncompressedPoint)
        body = webpush.encrypt(b"When I grow up, I want to be a watermelon",
                               "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
                               "BTBZMqHH6r4Tts7J_aSIgg", salt=unb64url("DGv6ra1nlYgDCS1FRnbzlw"),
                               keypair=(as_priv, as_pub))
        self.assertEqual(base64.urlsafe_b64encode(body).decode().rstrip("="),
                         "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6"
                         "TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXLWyouBWLVWGNWQexSgSxsj_Qulcy4a-fN")

    def test_checks_keys_and_size(self):
        with self.assertRaises(ValueError):
            webpush.encrypt(b"x", "AAAA", "BBBB")
        ua = ec.generate_private_key(ec.SECP256R1(), default_backend()).public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        p256dh = base64.urlsafe_b64encode(ua).decode().rstrip("=")
        auth = base64.urlsafe_b64encode(os.urandom(16)).decode().rstrip("=")
        self.assertGreater(len(webpush.encrypt(b"short", p256dh, auth)), 86)
        with self.assertRaises(ValueError):
            webpush.encrypt(b"x" * 5000, p256dh, auth)


NODE_SIDE = r"""
const { Bytes, E2E, Token, Account, Vault } = require(process.argv[1]);
const readline = require("readline");
const rl = readline.createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const say = obj => console.log(JSON.stringify(obj));
const next = async () => JSON.parse((await lines.next()).value);
(async () => {
  const cfg = await next();
  const t = Token.parse(cfg.token);
  const state = await E2E.open("pair", null);
  say(state.msg);
  const session = await E2E.finish(state, await next(), t.secret, t.deviceId);
  say({ c: await session.seal({ id: 1, m: "pair.finish", p: { from: "js" } }) });
  const reply = await session.open((await next()).c);
  const acct = await Account.derive("correct horse", cfg.salt, 1000);
  const blob = await Vault.encrypt(acct.vaultKey, { v: 1, devices: [{ id: t.deviceId }] });
  let wrong = "";
  try { await Vault.decrypt((await Account.derive("wrong", cfg.salt, 1000)).vaultKey, blob); } catch (e) { wrong = e.message; }
  say({ reply, deviceId: t.deviceId, secret: Bytes.b64(t.secret), auth: acct.auth, blob, wrong,
        back: await Vault.decrypt(acct.vaultKey, cfg.vault) });
})().catch(e => { say({ error: String(e && e.stack || e) }); process.exit(1); });
"""


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class BrowserInterop(unittest.TestCase):
    """www/app/crypto.js (run in Node's WebCrypto) against the Python implementation."""

    def test_handshake_account_and_vault(self):
        did, secret = e2e.new_device_id(), e2e.new_pair_secret()
        salt = os.urandom(16)
        # what the browser derives from the password (reference implementation of crypto.js Account)
        master = hashlib.pbkdf2_hmac("sha256", b"correct horse", salt, 1000)
        auth = e2e.hkdf(master, b"stove-account-v1", b"stove auth", 32)
        vault_key = e2e.hkdf(master, b"stove-account-v1", b"stove vault", 32)
        iv = os.urandom(12)
        py_vault = base64.b64encode(iv + AESGCM(vault_key).encrypt(iv, json.dumps({"v": 1, "devices": [{"id": "PY"}]}).encode(), None)).decode()

        proc = subprocess.Popen(["node", "-e", NODE_SIDE, os.path.join(ROOT, "www", "app", "crypto.js")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def send(obj):
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

        def recv():
            line = proc.stdout.readline()
            if not line:
                self.fail("crypto.js stopped: " + proc.stderr.read())
            out = json.loads(line)
            self.assertNotIn("error", out, out.get("error"))
            return out

        try:
            send({"token": e2e.format_token(did, secret).lower(), "salt": base64.b64encode(salt).decode(),
                  "vault": py_vault})
            accept, hub = e2e.hub_accept(recv(), secret, did)
            send(accept)
            self.assertEqual(hub.open(recv()["c"]), {"id": 1, "m": "pair.finish", "p": {"from": "js"}})
            send({"c": hub.seal({"id": 1, "r": {"link": "abc"}})})
            out = recv()
        finally:
            proc.stdin.close()
            proc.wait(10)
        self.assertEqual(out["reply"], {"id": 1, "r": {"link": "abc"}})
        self.assertEqual((out["deviceId"], base64.b64decode(out["secret"])), (did, secret))
        self.assertEqual(base64.b64decode(out["auth"]), auth)
        raw = base64.b64decode(out["blob"])
        self.assertEqual(json.loads(AESGCM(vault_key).decrypt(raw[:12], raw[12:], None)), {"v": 1, "devices": [{"id": did}]})
        self.assertEqual(out["wrong"], "Wrong password.")
        self.assertEqual(out["back"], {"v": 1, "devices": [{"id": "PY"}]})


if __name__ == "__main__":
    unittest.main()
