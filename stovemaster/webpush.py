"""Web Push payload encryption (RFC 8291, "aes128gcm" content encoding).

The hub encrypts each notification for one browser subscription with the keys
the browser handed over through the end-to-end session (p256dh, auth); the VPS
only adds the VAPID signature and posts the ciphertext to the push service.
"""

import os
import struct

from .e2e import _ecdh, _keypair, b64url_decode, hkdf, require_crypto

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
except ImportError:  # pragma: no cover
    AESGCM = None


def encrypt(payload, p256dh, auth, salt=None, keypair=None, record_size=4096):
    """Return the request body for the push service.

    p256dh and auth are the subscription's keys (base64url, as in
    PushSubscription.toJSON()). salt/keypair are only given by the RFC test.
    """
    require_crypto()
    ua_public = b64url_decode(p256dh)
    auth_secret = b64url_decode(auth)
    if len(ua_public) != 65 or len(auth_secret) != 16:
        raise ValueError("invalid push subscription keys")
    as_private, as_public = keypair or _keypair()
    salt = salt or os.urandom(16)
    ecdh_secret = _ecdh(as_private, ua_public)
    ikm = hkdf(ecdh_secret, auth_secret, b"WebPush: info\0" + ua_public + as_public, 32)
    cek = hkdf(ikm, salt, b"Content-Encoding: aes128gcm\0", 16)
    nonce = hkdf(ikm, salt, b"Content-Encoding: nonce\0", 12)
    if len(payload) + 17 > record_size:
        raise ValueError("notification too long")
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)   # one record, delimiter 2 = last
    return salt + struct.pack("!IB", record_size, len(as_public)) + as_public + ciphertext
