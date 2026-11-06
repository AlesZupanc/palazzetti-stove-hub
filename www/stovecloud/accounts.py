"""Accounts of the website, without ever seeing a password.

The browser turns the password into an "auth key" (PBKDF2 + HKDF, see
docs/protocol.md section 4) and sends only that; it keeps the second key it
derives (the vault key) to itself. The server stores a salted hash of the auth
key, the salt for the browser's derivation and the encrypted vault.
"""

import base64
import hashlib
import hmac
import ipaddress
import re
import secrets
import threading
import time

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[^@\s<>\"']{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,}$")

ROLES = ("admin", "user")
STATUSES = ("pending", "active", "disabled")
ITERATIONS = 600000
ITERATIONS_RANGE = (100000, 5000000)
VAULT_MAX = 64 * 1024

APPROVE_TTL = 7 * 86400
RESET_TTL = 3600
SETUP_TTL = 7 * 86400

LOGIN_PAIR_LIMIT = (5, 900)     # failures per account + client IP per 15 min
LOGIN_IP_LIMIT = (20, 900)      # failures per client IP per 15 min
LOGIN_USER_LIMIT = (30, 900)    # failures per account from all IPs per 15 min
REGISTER_IP_LIMIT = (5, 3600)
REGISTER_GLOBAL_LIMIT = (20, 86400)
MAX_PENDING = 20
FORGOT_IP_LIMIT = (5, 3600)
FORGOT_USER_LIMIT = (3, 3600)


class AuthError(Exception):
    """User-facing error; status is the HTTP status to answer."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _unb64(text, size):
    try:
        raw = base64.b64decode(str(text), validate=True)
    except (TypeError, ValueError):
        raise AuthError("malformed key") from None
    if len(raw) != size:
        raise AuthError("malformed key")
    return raw


def hash_auth(auth_b64, salt=None):
    """Store only a salted hash of the browser's auth key."""
    salt = salt or secrets.token_bytes(16)
    digest = hmac.new(salt, _unb64(auth_b64, 32), hashlib.sha256).digest()
    return f"hmac-sha256${_b64(salt)}${_b64(digest)}"


def verify_auth(auth_b64, stored):
    try:
        algo, salt, digest = (stored or "").split("$")
        if algo != "hmac-sha256":
            return False
        expected = base64.b64decode(digest)
        given = hmac.new(base64.b64decode(salt), _unb64(auth_b64, 32), hashlib.sha256).digest()
    except (ValueError, AuthError):
        return False
    return hmac.compare_digest(given, expected)


def token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def client_key(ip):
    """What the per-IP limits count: the address, but for IPv6 its /64, which one home line or
    server gets as a whole (it could otherwise use a new address for every try)."""
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return ip or "?"
    if a.version == 6 and a.ipv4_mapped:
        return str(a.ipv4_mapped)
    if a.version == 6:
        return str(ipaddress.ip_network((int(a) >> 64 << 64, 64)))
    return str(a)


def clean_username(username):
    username = username.strip() if isinstance(username, str) else ""
    if not USERNAME_RE.match(username):
        raise AuthError("Username: 3-32 characters, letters, digits and . _ - only.")
    return username


def clean_email(email):
    email = email.strip() if isinstance(email, str) else ""
    if len(email) > 254 or not EMAIL_RE.match(email):
        raise AuthError("Please enter a valid e-mail address.")
    return email


def clean_secret(salt, auth, iterations):
    """Validate what the browser derived from a new password."""
    _unb64(salt, 16)
    _unb64(auth, 32)
    try:
        iterations = int(iterations or ITERATIONS)
    except (TypeError, ValueError):
        raise AuthError("bad iteration count") from None
    if not ITERATIONS_RANGE[0] <= iterations <= ITERATIONS_RANGE[1]:
        raise AuthError("bad iteration count")
    return salt, auth, iterations


class RateLimiter:
    """Sliding-window counters in memory (reset on restart, which is fine)."""

    def __init__(self):
        self._hits = {}
        self._lock = threading.Lock()

    def _prune(self, key, window, now):
        hits = [t for t in self._hits.get(key, ()) if now - t < window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def acquire(self, rules):
        """rules: [(key, limit, window)]. Record one hit on each if all are below their
        limit (atomically) and return 0, else record nothing and return the seconds to wait."""
        now = time.monotonic()
        with self._lock:
            wait = 0
            for key, limit, window in rules:
                hits = self._prune(key, window, now)
                if len(hits) >= limit:
                    wait = max(wait, int(window - (now - hits[-limit])) + 1)
            if wait:
                return wait
            for key, _limit, _window in rules:
                self._hits.setdefault(key, []).append(now)
            if len(self._hits) > 5000:
                for k in sorted(self._hits, key=lambda k: self._hits[k][-1])[:1000]:
                    del self._hits[k]
            return 0

    def refund(self, key):
        with self._lock:
            hits = self._hits.get(key)
            if hits:
                hits.pop()
                if not hits:
                    del self._hits[key]

    def clear(self, key):
        with self._lock:
            self._hits.pop(key, None)


def wait_text(seconds):
    minutes = max(1, (seconds + 59) // 60)
    return f"{minutes} minute{'s' if minutes != 1 else ''}"


class Accounts:
    def __init__(self, db, session_days=60):
        self.db = db
        self.session_ttl = session_days * 86400
        self.limits = RateLimiter()
        secret = db.get_setting("server_secret")
        if not secret:
            secret = secrets.token_hex(32)
            db.set_setting("server_secret", secret)
        self._secret = bytes.fromhex(secret)

    # -- users -----------------------------------------------------------------
    @staticmethod
    def public(user):
        if not user:
            return None
        return {k: user.get(k) for k in ("id", "username", "email", "role", "status", "created_at", "last_login")}

    def get(self, user_id):
        return self.db.one("SELECT * FROM users WHERE id = ?", (user_id,))

    def find(self, login):
        login = login.strip() if isinstance(login, str) else ""
        if not login:
            return None
        return self.db.one("SELECT * FROM users WHERE username = ? OR email = ?", (login, login))

    def list(self):
        return self.db.all("SELECT * FROM users ORDER BY status = 'pending' DESC, username")

    def admins(self):
        return self.db.all("SELECT * FROM users WHERE role = 'admin' AND status = 'active'")

    def active_admin_count(self):
        return self.db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'admin' AND status = 'active'")["n"]

    def create(self, username, email, salt=None, auth=None, iterations=ITERATIONS, role="user", status="pending",
               reg_ip=None):
        username, email = clean_username(username), clean_email(email)
        if auth is not None:
            salt, auth, iterations = clean_secret(salt, auth, iterations)
        if role not in ROLES or status not in STATUSES:
            raise AuthError("Invalid role or status.")
        if self.db.one("SELECT id FROM users WHERE username = ? OR email = ? OR username = ? OR email = ?",
                       (username, email, email, username)):
            raise AuthError("That username or e-mail address is already registered.", 409)
        now = time.time()
        try:
            user_id, _ = self.db.run(
                "INSERT INTO users(username, email, salt, iterations, auth_hash, role, status, created_at, "
                "approved_at, reg_ip) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (username, email, salt, iterations, hash_auth(auth) if auth else None, role, status, now,
                 now if status == "active" else None, reg_ip))
        except Exception as e:  # unique constraint on a race
            raise AuthError("That username or e-mail address is already registered.", 409) from e
        return user_id

    def set_status(self, user_id, status):
        if status not in STATUSES:
            raise AuthError("Invalid status.")
        if status == "active":
            self.db.run("UPDATE users SET status = 'active', approved_at = COALESCE(approved_at, ?) WHERE id = ?",
                        (time.time(), user_id))
        else:
            self.db.run("UPDATE users SET status = ? WHERE id = ?", (status, user_id))
            self.revoke_sessions(user_id)
        if status == "disabled":   # no unused setup or reset link may bring the account back
            self.db.run("DELETE FROM tokens WHERE user_id = ?", (user_id,))
        elif status != "pending":
            self.db.run("DELETE FROM tokens WHERE user_id = ? AND kind = 'approve'", (user_id,))

    def set_role(self, user_id, role):
        if role not in ROLES:
            raise AuthError("Invalid role.")
        self.db.run("UPDATE users SET role = ? WHERE id = ?", (role, user_id))

    def delete(self, user_id):
        self.db.run("DELETE FROM users WHERE id = ?", (user_id,))

    def set_secret(self, user_id, salt, auth, iterations=ITERATIONS, vault=None, keep_session=None):
        """New password (as derived by the browser) and the vault re-encrypted with it
        (None: the old vault cannot be opened any more and is dropped)."""
        salt, auth, iterations = clean_secret(salt, auth, iterations)
        if vault is not None and (not isinstance(vault, str) or len(vault) > VAULT_MAX):
            raise AuthError("vault too large")
        self.db.run("UPDATE users SET salt = ?, iterations = ?, auth_hash = ?, vault = ?, "
                    "vault_version = vault_version + 1 WHERE id = ?",
                    (salt, iterations, hash_auth(auth), vault, user_id))
        if keep_session:
            self.db.run("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, token_hash(keep_session)))
        else:
            self.revoke_sessions(user_id)
        self.db.run("DELETE FROM tokens WHERE user_id = ? AND kind IN ('reset', 'setup')", (user_id,))

    # -- sign in -----------------------------------------------------------------
    def prelogin(self, login):
        """Salt and iterations for the browser's derivation. Unknown accounts get a stable
        made-up salt, so the answer does not reveal whether an account exists."""
        user = self.find(login)
        if user and user["salt"] and user["auth_hash"]:
            return {"salt": user["salt"], "iterations": user["iterations"]}
        fake = hmac.new(self._secret, b"prelogin:" + str(login or "").strip().lower().encode(), hashlib.sha256)
        return {"salt": _b64(fake.digest()[:16]), "iterations": ITERATIONS}

    def authenticate(self, login, auth, ip):
        login = login.strip().lower() if isinstance(login, str) else ""
        if not login or not isinstance(auth, str) or not auth:
            raise AuthError("Enter your username (or e-mail) and password.")
        ip = client_key(ip)
        rules = [("login-pair:" + login + "|" + ip, *LOGIN_PAIR_LIMIT), ("login-ip:" + ip, *LOGIN_IP_LIMIT),
                 ("login-user:" + login, *LOGIN_USER_LIMIT)]
        wait = self.limits.acquire(rules)
        if wait:
            raise AuthError(f"Too many failed attempts. Try again in {wait_text(wait)}.", 429)
        user = self.find(login)
        if not user or not verify_auth(auth, user["auth_hash"]):
            raise AuthError("Wrong username or password.", 401)      # the attempt stays counted
        for key, _limit, _window in rules:
            self.limits.refund(key)
        self.limits.clear(rules[0][0])
        if user["status"] == "pending":
            raise AuthError("Your account is waiting for the administrator's approval.", 403)
        if user["status"] != "active":
            raise AuthError("This account is disabled.", 403)
        return user

    # -- sessions ------------------------------------------------------------------
    def create_session(self, user_id, ip=None, user_agent=None):
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.run("INSERT INTO sessions(token_hash, user_id, created_at, expires_at, last_seen, ip, user_agent) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?)",
                    (token_hash(token), user_id, now, now + self.session_ttl, now, ip, (user_agent or "")[:200]))
        self.db.run("UPDATE users SET last_login = ? WHERE id = ?", (now, user_id))
        return token

    def session_user(self, token):
        if not token or len(token) > 100:
            return None
        th = token_hash(token)
        row = self.db.one("SELECT s.expires_at, s.last_seen, u.* FROM sessions s JOIN users u ON u.id = s.user_id "
                          "WHERE s.token_hash = ?", (th,))
        now = time.time()
        if not row or row["expires_at"] < now or row["status"] != "active":
            return None
        if now - row["last_seen"] > 300:
            self.db.run("UPDATE sessions SET last_seen = ?, expires_at = ? WHERE token_hash = ?",
                        (now, now + self.session_ttl, th))
        row.pop("expires_at", None)
        row.pop("last_seen", None)
        return row

    def revoke_session(self, token):
        if token:
            self.db.run("DELETE FROM sessions WHERE token_hash = ?", (token_hash(token),))

    def revoke_sessions(self, user_id):
        self.db.run("DELETE FROM sessions WHERE user_id = ?", (user_id,))

    # -- one-time links ------------------------------------------------------------
    def create_token(self, kind, user_id, ttl):
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.db.run("INSERT INTO tokens(token_hash, kind, user_id, created_at, expires_at) VALUES(?, ?, ?, ?, ?)",
                    (token_hash(token), kind, user_id, now, now + ttl))
        return token

    def peek_token(self, kind, token):
        if not isinstance(token, str) or not token or len(token) > 100:
            return None
        row = self.db.one("SELECT user_id, expires_at FROM tokens WHERE token_hash = ? AND kind = ?",
                          (token_hash(token), kind))
        if not row or row["expires_at"] < time.time():
            return None
        return self.get(row["user_id"])

    def consume_token(self, kind, token):
        """Use a link up; only one of several parallel uses succeeds."""
        user = self.peek_token(kind, token)
        if not user:
            return None
        _, deleted = self.db.run("DELETE FROM tokens WHERE token_hash = ? AND kind = ? AND expires_at >= ?",
                                 (token_hash(token), kind, time.time()))
        return user if deleted == 1 else None

    # -- registration and resets -----------------------------------------------------
    def register(self, username, email, salt, auth, iterations, ip):
        if self.db.one("SELECT COUNT(*) AS n FROM users WHERE status = 'pending'")["n"] >= MAX_PENDING:
            raise AuthError("Too many accounts are waiting for approval. Try again later.", 429)
        rules = [("register-ip:" + client_key(ip), *REGISTER_IP_LIMIT), ("register-all", *REGISTER_GLOBAL_LIMIT)]
        wait = self.limits.acquire(rules)
        if wait:
            raise AuthError(f"Too many registrations. Try again in {wait_text(wait)}.", 429)
        try:
            user_id = self.create(username, email, salt, auth, iterations, reg_ip=ip)
        except AuthError:
            for key, _limit, _window in rules:      # a typo in the form does not count
                self.limits.refund(key)
            raise
        return self.get(user_id), self.create_token("approve", user_id, APPROVE_TTL)

    def request_reset(self, login, ip):
        login = login.strip().lower() if isinstance(login, str) else ""
        wait = self.limits.acquire([("forgot-ip:" + client_key(ip), *FORGOT_IP_LIMIT),
                                    ("forgot-user:" + login, *FORGOT_USER_LIMIT)])
        if wait:
            raise AuthError(f"Too many requests. Try again in {wait_text(wait)}.", 429)
        user = self.find(login)
        if not user or user["status"] != "active":
            return None, None
        return user, self.create_token("reset", user["id"], RESET_TTL)

    # -- vault -----------------------------------------------------------------------
    def put_vault(self, user_id, blob, version):
        """Store the encrypted vault if nobody changed it meanwhile; -> new version or None."""
        if not isinstance(blob, str) or len(blob) > VAULT_MAX:
            raise AuthError("vault too large")
        _, n = self.db.run("UPDATE users SET vault = ?, vault_version = vault_version + 1 "
                           "WHERE id = ? AND vault_version = ?", (blob, user_id, int(version)))
        if n != 1:
            return None
        return self.db.one("SELECT vault_version FROM users WHERE id = ?", (user_id,))["vault_version"]
