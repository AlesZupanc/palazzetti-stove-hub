"""SQLite storage: schedule, settings, event log, linked accounts, push subscriptions.

Everything lives on the Pi. Accounts are on the VPS; the Pi only knows which
accounts were linked to it (and their link keys, see cloud.py).
"""

import contextlib
import json
import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS schedule (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,  -- the scheduler remembers slots by id
    name        TEXT NOT NULL DEFAULT '',
    days        INTEGER NOT NULL,                 -- bit 0 = Monday ... bit 6 = Sunday
    start_time  TEXT NOT NULL,                    -- HH:MM
    end_time    TEXT NOT NULL,                    -- HH:MM, <= start means next day
    power       INTEGER NOT NULL,
    fan         INTEGER NOT NULL,
    setpoint    REAL NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    updated_at  REAL NOT NULL,
    updated_by  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    ts          REAL NOT NULL,
    actor       TEXT NOT NULL,                    -- address at home, account name, scheduler, system
    via         TEXT NOT NULL,                    -- lan, remote, scheduler, cli, system
    action      TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    ok          INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS links (
    id          TEXT PRIMARY KEY,                 -- link id the browser presents
    profile_id  TEXT NOT NULL UNIQUE,             -- account id on the VPS (one link per account)
    profile     TEXT NOT NULL,                    -- account name when it was linked
    key         TEXT NOT NULL,                    -- base64 link key (pre-shared key of the sessions)
    created_at  REAL NOT NULL,
    last_seen   REAL
);
CREATE TABLE IF NOT EXISTS push_subs (
    id          TEXT PRIMARY KEY,                 -- subscription id given by the VPS
    link_id     TEXT NOT NULL REFERENCES links(id) ON DELETE CASCADE,
    p256dh      TEXT NOT NULL,                    -- the browser's keys for the payload encryption
    auth        TEXT NOT NULL,
    categories  TEXT NOT NULL,                    -- JSON list: problems, schedule
    created_at  REAL NOT NULL
);
"""

EVENTS_KEEP = 2000


class DB:
    """One shared connection guarded by a lock; plenty for a household."""

    def __init__(self, path):
        self.path = path
        self.on_event = None   # called with each new activity event (the hub forwards it to the apps)
        folder = os.path.dirname(os.path.abspath(path))
        os.makedirs(folder, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(path, timeout=15, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._inserts = 0
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.executescript(SCHEMA)
            self.conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")

    def close(self):
        with self.lock:
            self.conn.close()

    # -- query helpers -------------------------------------------------------
    def all(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            row = self.conn.execute(sql, args).fetchone()
        return dict(row) if row else None

    def run(self, sql, args=()):
        """Execute a statement; returns (lastrowid, rowcount)."""
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

    # -- settings (JSON values) ------------------------------------------------
    def get_setting(self, key, default=None):
        row = self.one("SELECT value FROM settings WHERE key = ?", (key,))
        if row is None:
            return default
        try:
            return json.loads(row["value"])
        except ValueError:
            return default

    def set_setting(self, key, value):
        self.run("INSERT INTO settings(key, value) VALUES(?, ?) "
                 "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, json.dumps(value)))

    # -- event log -------------------------------------------------------------
    def log_event(self, actor, via, action, detail="", ok=True):
        event = {"ts": time.time(), "actor": str(actor), "via": str(via), "action": str(action),
                 "detail": str(detail)[:500], "ok": 1 if ok else 0}
        event["id"], _ = self.run("INSERT INTO events(ts, actor, via, action, detail, ok) VALUES(?, ?, ?, ?, ?, ?)",
                                  tuple(event[k] for k in ("ts", "actor", "via", "action", "detail", "ok")))
        self._inserts += 1
        if self._inserts % 100 == 0:
            self.run("DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC "
                     "LIMIT 1 OFFSET ?)", (EVENTS_KEEP,))
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:   # never let a listener break logging
                pass

    def events(self, limit=100):
        return self.all("SELECT * FROM events ORDER BY id DESC LIMIT ?", (int(limit),))

    # -- schedule --------------------------------------------------------------
    def schedule_entries(self):
        rows = self.all("SELECT * FROM schedule ORDER BY start_time, id")
        for r in rows:
            r["enabled"] = bool(r["enabled"])
        return rows

    def schedule_entry(self, entry_id):
        r = self.one("SELECT * FROM schedule WHERE id = ?", (entry_id,))
        if r:
            r["enabled"] = bool(r["enabled"])
        return r

    def schedule_save(self, entry, entry_id=None, by=""):
        cols = ("name", "days", "start_time", "end_time", "power", "fan", "setpoint", "enabled")
        values = [entry[c] for c in cols]
        values[-1] = 1 if values[-1] else 0
        if entry_id is None:
            rowid, _ = self.run(
                f"INSERT INTO schedule({', '.join(cols)}, updated_at, updated_by) "
                f"VALUES({', '.join('?' * len(cols))}, ?, ?)", (*values, time.time(), by))
            return rowid
        _, n = self.run(
            f"UPDATE schedule SET {', '.join(c + ' = ?' for c in cols)}, updated_at = ?, updated_by = ? "
            "WHERE id = ?", (*values, time.time(), by, entry_id))
        return entry_id if n else None

    def schedule_delete(self, entry_id):
        _, n = self.run("DELETE FROM schedule WHERE id = ?", (entry_id,))
        return n > 0
