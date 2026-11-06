"""End-to-end encryption between a browser and the hub (docs/protocol.md, section 3).

The hub side of the handshake, the encrypted session and the stove's token.
The browser side is www/app/crypto.js; browser_open()/browser_finish() below
are the same steps in Python, used by the tests and as a reference.

Needs the "cryptography" package (Raspberry Pi OS: sudo apt install python3-cryptography)
for P-256 and AES-GCM; HKDF and HMAC come from the standard library.
"""

import base64
import hashlib
import hmac
import json
import os
import re
import struct
import threading

try:
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover - reported when remote access is switched on
    ec = None

PROTO = b"stove-e2e-v1"
B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"


class E2EError(Exception):
    pass


def require_crypto():
    if ec is None:
        raise RuntimeError("remote access needs the Python 'cryptography' package: "
                           "sudo apt install python3-cryptography")


def b64e(raw):
    return base64.b64encode(raw).decode("ascii")


def b64d(text, size=None):
    try:
        raw = base64.b64decode(text, validate=True)
    except (TypeError, ValueError):
        raise E2EError("bad base64") from None
    if size is not None and len(raw) != size:
        raise E2EError(f"expected {size} bytes, got {len(raw)}")
    return raw


def b64url_decode(text):
    text = str(text).strip()
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --------------------------------------------------------------------------
# the stove's token: 8 characters device id + 24 characters secret (120 bits)
# --------------------------------------------------------------------------
def b32(raw):
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def new_device_id():
    return b32(os.urandom(5))


def new_pair_secret():
    return os.urandom(15)


def format_token(device_id, secret):
    text = device_id + b32(secret)
    return "-".join(text[i:i + 4] for i in range(0, len(text), 4))


def parse_token(text):
    """'abcd-efgh-...' -> (device id, 15-byte secret). Raises ValueError."""
    s = re.sub(r"[^A-Za-z0-9]", "", str(text or "")).upper()
    if len(s) != 32 or any(c not in B32 for c in s):
        raise ValueError("A stove token has 32 letters and digits (A-Z, 2-7).")
    return s[:8], base64.b32decode(s[8:])


# --------------------------------------------------------------------------
# key schedule
# --------------------------------------------------------------------------
def hkdf(ikm, salt, info, length):
    """HKDF-SHA256 (RFC 5869)."""
    prk = hmac.new(salt or b"\0" * 32, ikm, hashlib.sha256).digest()
    out, block, i = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([i]), hashlib.sha256).digest()
        out += block
        i += 1
    return out[:length]


def transcript(mode, link_id, device_id, eb, nb, ep, np_):
    return hashlib.sha256(PROTO + b"\0" + mode.encode() + b"\0" + (link_id or "").encode() + b"\0"
                          + device_id.encode() + b"\0" + eb + nb + ep + np_).digest()


def derive(z, psk, th):
    okm = hkdf(z, psk, PROTO + b" keys" + th, 96)
    return okm[:32], okm[32:64], okm[64:]   # confirmation key, browser->hub, hub->browser


def _keypair():
    priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
    pub = priv.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return priv, pub


def _ecdh(priv, peer_pub):
    try:
        peer = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), peer_pub)
    except ValueError:
        raise E2EError("invalid public key") from None
    return priv.exchange(ec.ECDH(), peer)


class Session:
    """AES-256-GCM in both directions; the 96-bit nonce counts messages, so a
    message that is replayed, dropped or reordered does not decrypt."""

    def __init__(self, send_key, recv_key):
        self._tx = AESGCM(send_key)
        self._rx = AESGCM(recv_key)
        self._tx_n = 0
        self._rx_n = 0
        self.lock = threading.Lock()   # hold it while sealing *and* queueing, to keep the order

    @staticmethod
    def _nonce(n):
        return b"\0\0\0\0" + struct.pack("!Q", n)

    def seal(self, obj, limit=None):
        """obj -> base64 ciphertext; one longer than limit raises E2EError (and uses no nonce)."""
        data = json.dumps(obj, separators=(",", ":"), default=str).encode("utf-8")
        if limit is not None and 4 * ((len(data) + 18) // 3) > limit:
            raise E2EError(f"message too big ({len(data)} bytes)")
        c = self._tx.encrypt(self._nonce(self._tx_n), data, None)
        self._tx_n += 1
        return b64e(c)

    def open(self, text):
        try:
            data = self._rx.decrypt(self._nonce(self._rx_n), b64d(text), None)
        except InvalidTag:
            raise E2EError("message does not decrypt (wrong key, tampered or out of order)") from None
        self._rx_n += 1
        try:
            return json.loads(data)
        except ValueError:
            raise E2EError("not JSON") from None


def hub_accept(msg, psk, device_id):
    """Hub side: answer {"t":"open"} -> ({"t":"accept",...}, Session)."""
    require_crypto()
    mode, link_id = msg.get("mode"), msg.get("link") or ""
    if mode not in ("pair", "link"):
        raise E2EError("unknown mode")
    eb, nb = b64d(msg.get("eb"), 65), b64d(msg.get("nb"), 16)
    priv, ep = _keypair()
    np_ = os.urandom(16)
    th = transcript(mode, link_id, device_id, eb, nb, ep, np_)
    kc, k_b2h, k_h2b = derive(_ecdh(priv, eb), psk, th)
    mac = hmac.new(kc, b"H" + th, hashlib.sha256).digest()
    return {"t": "accept", "ep": b64e(ep), "np": b64e(np_), "mac": b64e(mac)}, Session(k_h2b, k_b2h)


# -- the browser's side (for tests; the app does this in crypto.js) -----------
def browser_open(mode, link_id=None):
    require_crypto()
    priv, eb = _keypair()
    nb = os.urandom(16)
    msg = {"t": "open", "mode": mode, "link": link_id, "eb": b64e(eb), "nb": b64e(nb)}
    return msg, (priv, eb, nb, mode, link_id or "")


def browser_finish(state, accept, psk, device_id):
    priv, eb, nb, mode, link_id = state
    ep, np_ = b64d(accept.get("ep"), 65), b64d(accept.get("np"), 16)
    th = transcript(mode, link_id, device_id, eb, nb, ep, np_)
    kc, k_b2h, k_h2b = derive(_ecdh(priv, ep), psk, th)
    if not hmac.compare_digest(hmac.new(kc, b"H" + th, hashlib.sha256).digest(), b64d(accept.get("mac"), 32)):
        raise E2EError("the stove could not prove that it knows the key (wrong token?)")
    return Session(k_b2h, k_h2b)
