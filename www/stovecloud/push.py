"""Delivery of Web Push notifications with VAPID (RFC 8292).

The hubs encrypt each notification for the browser (RFC 8291) themselves; this
module only signs the request with the website's VAPID key and posts the
ciphertext to the push service. Only the big push services are allowed as
endpoints, so a subscription cannot make the server post to arbitrary URLs.
"""

import base64
import json
import logging
import queue
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
except ImportError:  # pragma: no cover
    ec = None

log = logging.getLogger("push")

KNOWN_HOSTS = ("fcm.googleapis.com", "android.googleapis.com", "updates.push.services.mozilla.com",
               "push.services.mozilla.com", ".push.apple.com", ".notify.windows.com")


def b64url(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class PushError(Exception):
    pass


class Pusher:
    def __init__(self, db, subject, extra_hosts=(), workers=2):
        self.db = db
        self.subject = subject
        self.hosts = tuple(KNOWN_HOSTS) + tuple(extra_hosts)
        self.available = ec is not None
        self._jwt = {}
        self._queue = queue.Queue(500)
        self.opener = urllib.request.build_opener()
        if not self.available:
            log.warning("push notifications disabled: install the Python 'cryptography' package")
            return
        value = db.get_setting("vapid_private")
        if value:
            self.key = ec.derive_private_key(int(value, 16), ec.SECP256R1(), default_backend())
        else:
            self.key = ec.generate_private_key(ec.SECP256R1(), default_backend())
            db.set_setting("vapid_private", format(self.key.private_numbers().private_value, "064x"))
        self.public_key = b64url(self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
        for i in range(workers):
            threading.Thread(target=self._run, name=f"push-{i}", daemon=True).start()

    def check_endpoint(self, url):
        u = urllib.parse.urlsplit(str(url or ""))
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not host or len(url) > 1000:
            raise PushError("The push endpoint must be an https address.")
        if not any(host == h or (h.startswith(".") and host.endswith(h)) for h in self.hosts):
            raise PushError(f"Push service {host} is not allowed ([limits] push_hosts).")
        return url

    def _vapid(self, endpoint):
        u = urllib.parse.urlsplit(endpoint)
        aud = f"{u.scheme}://{u.netloc}"
        cached = self._jwt.get(aud)
        if cached and cached[1] > time.time() + 3600:
            return cached[0]
        exp = int(time.time()) + 12 * 3600
        head = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
        claims = b64url(json.dumps({"aud": aud, "exp": exp, "sub": self.subject()}, separators=(",", ":")).encode())
        signing_input = f"{head}.{claims}".encode("ascii")
        r, s = decode_dss_signature(self.key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
        token = f"{head}.{claims}.{b64url(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"
        self._jwt[aud] = (token, exp)
        return token

    def post(self, endpoint, body, ttl=3600, urgency="normal", topic=None):
        """Deliver now -> (ok, gone, detail)."""
        headers = {
            "Authorization": f"vapid t={self._vapid(endpoint)}, k={self.public_key}",
            "TTL": str(int(ttl)),
            "Urgency": urgency if urgency in ("very-low", "low", "normal", "high") else "normal",
            "Content-Encoding": "aes128gcm",
            "Content-Type": "application/octet-stream",
        }
        if topic:
            headers["Topic"] = "".join(c for c in str(topic) if c.isalnum() or c in "-_")[:32]
        req = urllib.request.Request(endpoint, data=body, method="POST", headers=headers)
        try:
            with self.opener.open(req, timeout=20) as resp:
                return True, False, str(resp.status)
        except urllib.error.HTTPError as e:
            try:
                body = e.read(300)
            except Exception:        # body unreadable: the status code still counts
                body = b""
            if not isinstance(body, bytes):
                body = str(body).encode()
            detail = f"{e.code} {body.decode('utf-8', 'replace')}".strip()
            return False, e.code in (404, 410), detail
        except (OSError, ValueError) as e:
            return False, False, str(e)

    def submit(self, endpoint, body, ttl, urgency, topic, done):
        """Queue a delivery; done(ok, gone, detail) is called from a worker thread."""
        try:
            self._queue.put_nowait((endpoint, body, ttl, urgency, topic, done))
        except queue.Full:
            done(False, False, "push queue full")

    def _run(self):
        while True:
            endpoint, body, ttl, urgency, topic, done = self._queue.get()
            try:
                result = self.post(endpoint, body, ttl, urgency, topic)
            except Exception as e:  # never kill the worker
                log.exception("push failed")
                result = (False, False, str(e))
            try:
                done(*result)
            except Exception:
                log.exception("push callback failed")
