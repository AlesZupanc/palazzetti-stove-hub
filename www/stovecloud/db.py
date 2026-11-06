"""SQLite storage of the website: accounts, sessions, one-time links, hubs, links, push.

What the VPS knows: who has an account, which account may reach which hub
(device id), and where to deliver push notifications. It never sees the
stove's data: sessions are end-to-end encrypted and each account's list of
stoves (with their keys) is stored encrypted with a key only the browser has.
"""

import contextlib
import json
import os
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email         TEXT NOT NULL UNIQUE COLLATE NOCASE,
    salt          TEXT,                       -- for the browser's password derivation
    iterations    INTEGER NOT NULL DEFAULT 600000,
    auth_hash     TEXT,                       -- hash of the auth key; NULL until a password is set
    role          TEXT NOT NULL DEFAULT 'user',
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | active | disabled
    created_at    REAL NOT NULL,
    approved_at   REAL,
    last_login    REAL,
    reg_ip        TEXT,
    vault         TEXT,                       -- encrypted by the browser, opaque here
    vault_version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash  TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL,
    last_seen   REAL NOT NULL,
    ip          TEXT,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS tokens (
    token_hash  TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,                -- approve | reset | setup
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    expires_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id          TEXT PRIMARY KEY,             -- the hub's device id (first part of its token)
    secret_hash TEXT NOT NULL,                -- registered on first connection
    created_at  REAL NOT NULL,
    last_seen   REAL,
    last_ip     TEXT
);
CREATE TABLE IF NOT EXISTS links (
    device_id   TEXT NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  REAL NOT NULL,
    PRIMARY KEY (device_id, user_id)
);
CREATE TABLE IF NOT EXISTS push_subs (
    id          TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    endpoint    TEXT NOT NULL UNIQUE,
    created_at  REAL NOT NULL,
    last_ok     REAL,
    failures    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
"""


class DB:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)

    def close(self):
        with self.lock:
            self.conn.close()

    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def run(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            return cur.lastrowid, cur.rowcount

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            self.conn.execute("COMMIT")

    def get_setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def set_setting(self, key, value):
        self.run("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))

    def cleanup(self):
        now = time.time()
        self.run("DELETE FROM sessions WHERE expires_at < ?", (now,))
        self.run("DELETE FROM tokens WHERE expires_at < ?", (now,))
