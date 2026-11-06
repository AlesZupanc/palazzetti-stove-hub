"""The hub's API: JSON RPC over WebSocket, the same at home and through the VPS.

A Client is one connected browser: a ws:// connection on the home network
(lan.py) or an end-to-end encrypted session through the VPS (cloud.py). Both
hand decoded JSON messages to Hub.handle() and get JSON back; the Hub also
pushes events (status, schedule, activity) to every client. See
docs/protocol.md.
"""

import collections
import datetime as dt
import json
import logging
import os
import platform
import threading
import time

import palazzetti as plz

from . import __version__, urlapi
from .scheduler import DAY_NAMES, describe, entry_public, validate_entry
from .stove import fan_text, summarize, validate_command
from .updates import UpdateError, Updates

log = logging.getLogger("api")

PROBES = ("T1", "T2", "T3", "T4", "T5")
FAST_POLL = 5            # seconds between status reads while somebody watches
SLOW_POLL = 30           # ... and otherwise (alarm detection, push notifications)
STATUS_RESEND = 60       # send the status again after this long even if nothing changed
UNREACHABLE_NOTIFY = 600 # tell the phones when the box has not answered for this long
# The pellet tank. The stove only counts the pellets it burns (PQT, kg, never reset over the box),
# so what is left is the tank size minus what was burnt since the last refill.
NO_PELLETS_CODES = frozenset({253})   # the STATUS of the "no pellets" alarm (palazzetti.STATUS_NAMES)
TANK_MAX_KG = 200
TANK_LOW_DEFAULT = 20                 # % of the tank: "running low" below this
RATE_WINDOW = 12 * 3600               # burn rate from the last 12 h of samples ...
RATE_MIN_BURN = 3600                  # ... once the stove burnt at least an hour of them ...
RATE_MIN_KG = 0.5                     # ... and used at least this much (the counter may count whole kg)
RATE_GAP = 900                        # longer gaps between samples do not count as burning time
STALE_KG = 0.5                        # burnt less than this since "Tank refilled" / the alarm: not empty
REFILL_PCT = 10                       # a level set this much (% of the tank) above the estimate: a refill
NAME_MAX = 40


class RPCError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


def days_text(days):
    """[0,1,2,3,4] -> 'Mon-Fri'; [5,6] -> 'Sat, Sun'; all -> 'every day'."""
    days = sorted(days)
    if len(days) == 7:
        return "every day"
    runs, start, prev = [], None, None
    for d in days + [None]:
        if start is None:
            start = prev = d
        elif d is not None and d == prev + 1:
            prev = d
        else:
            runs.append(DAY_NAMES[start] if start == prev else
                        f"{DAY_NAMES[start]}, {DAY_NAMES[prev]}" if prev == start + 1 else
                        f"{DAY_NAMES[start]}-{DAY_NAMES[prev]}")
            start = prev = d
    return ", ".join(runs)


def local_time(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else None


class Client:
    """One connected browser. send(obj) delivers a JSON-able dict (thread-safe)."""

    def __init__(self, via, who, send, ip="", link=None, pairing=False, profile=None):
        self.via = via            # "lan" or "remote"
        self.who = who            # shown in the activity log
        self.send = send
        self.ip = ip
        self.link = link          # remote: the link row this session authenticated with
        self.pairing = pairing    # remote: a session opened with the stove's token, not yet added
        self.profile = profile    # remote: {"id", "name"} of the account (as told by the VPS)

    @property
    def lan(self):
        return self.via == "lan"


class Hub:
    def __init__(self, cfg, db, stove, scheduler, mdns=None):
        self.cfg = cfg
        self.db = db
        self.stove = stove
        self.scheduler = scheduler
        self.mdns = mdns
        self.cloud = None                  # set by cloud.CloudLink
        self.started = time.time()
        self.clients = set()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._last_status = None
        self._thread = None
        self._pellet_lock = threading.Lock()
        self._tank_lock = threading.RLock()          # the fill marks and flags: one decision per filling
        self._pellet_samples = collections.deque()   # (monotonic s, PQT kg, burning) for the burn rate
        self.methods = {
            "status.get": (self.status_get, "any"),
            "stove.action": (self.action, "any"),
            "schedule.get": (self.schedule_get, "any"),
            "schedule.save": (self.schedule_save, "any"),
            "schedule.delete": (self.schedule_delete, "any"),
            "schedule.enable": (self.schedule_enable, "any"),
            "probes.set": (self.probes_set, "any"),
            "pellets.reset": (self.pellets_reset, "any"),
            "pellets.refill": (self.pellets_refill, "any"),
            "pellets.tank": (self.pellets_tank, "any"),
            "pellets.level": (self.pellets_level, "any"),
            "events.list": (self.events_list, "any"),
            "system.get": (self.system_get, "any"),
            "device.rename": (self.device_rename, "any"),
            "cloud.info": (self.cloud_info, "lan"),
            "cloud.regenerate": (self.cloud_regenerate, "lan"),
            "cloud.unlink": (self.cloud_unlink, "lan"),
            "urlapi.info": (self.urlapi_info, "lan"),
            "urlapi.regenerate": (self.urlapi_regenerate, "lan"),
            "system.update": (self.system_update, "lan"),
        }
        self.updates = Updates(os.path.dirname(os.path.abspath(cfg.db_path)),
                               "/etc/systemd/system/stove-master-update.path")
        db.on_event = self._on_event
        scheduler.notify = lambda subject, body: self.notify("problems", subject, body, tag="schedule-problem")

    # -- lifecycle -------------------------------------------------------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._telemetry, name="telemetry", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def attach(self, client):
        with self._lock:
            self.clients.add(client)
        client.send({"ev": "hello", "d": self.hello(client)})
        if not client.pairing:
            if self._last_status is not None:
                client.send({"ev": "status", "d": self._last_status})
            self._wake.set()   # a fresh reading for the new viewer

    def detach(self, client):
        with self._lock:
            self.clients.discard(client)

    def broadcast(self, event, data, lan_only=False):
        with self._lock:
            clients = [c for c in self.clients if not c.pairing and (c.lan or not lan_only)]
        for c in clients:
            try:
                c.send({"ev": event, "d": data})
            except Exception as e:  # a closing connection; its reader thread cleans up
                log.debug("event to %s failed: %s", c.who, e)

    # -- RPC dispatch ------------------------------------------------------------
    def handle(self, client, msg):
        """Answer one request {"id", "m", "p"}; returns the reply dict (or None for junk)."""
        if not isinstance(msg, dict) or "m" not in msg:
            return None
        rid = msg.get("id")
        try:
            method = msg.get("m")
            params = msg.get("p")
            params = {} if params is None else params
            if not isinstance(params, dict):
                raise RPCError("params must be an object")
            if client.pairing:
                if method != "pair.finish" or not self.cloud:
                    raise RPCError("Add the stove to your account first.", 403)
                return {"id": rid, "r": self.cloud.pair_finish(client, params)}
            entry = self.methods.get(method)
            if entry is None and self.cloud and not client.lan:
                entry = self.cloud.methods.get(method)
            if entry is None:
                raise RPCError(f"unknown method {method!r}", 404)
            fn, who = entry
            if who == "lan" and not client.lan:
                raise RPCError("Only possible on the home network.", 403)
            return {"id": rid, "r": fn(client, params)}
        except RPCError as e:
            return {"id": rid, "e": e.message, "code": e.code}
        except ValueError as e:
            return {"id": rid, "e": str(e), "code": 400}
        except plz.PalazzettiError as e:
            return {"id": rid, "e": str(e), "code": 502}
        except Exception:
            log.exception("error in %s", msg.get("m"))
            return {"id": rid, "e": "internal error", "code": 500}

    def _log(self, client, action, detail="", ok=True):
        self.db.log_event(client.who, client.via, action, detail, ok)

    # -- device identity ---------------------------------------------------------
    @property
    def device_name(self):
        return self.db.get_setting("device_name") or "Stove"

    def hello(self, client):
        out = {
            "device": {"id": self.cloud.device_id if self.cloud else None, "name": self.device_name},
            "version": __version__,
            "via": client.via,
            "who": client.who,
            "pairing": client.pairing,
            "lan_url": self.lan_url(),
        }
        if not client.lan and client.profile:
            out["account"] = client.profile.get("name")
        return out

    def lan_url(self):
        port = self.cfg.public_port      # with a proxy in front: its port, not the hub's own
        return f"http://{self.cfg.mdns_name}.local{'' if port == 80 else f':{port}'}/"

    # -- status and telemetry --------------------------------------------------
    def probes(self):
        """Names the household gave the temperature probes, and which one is the room."""
        p = self.db.get_setting("probes", {}) or {}
        names = {k: str(v) for k, v in (p.get("names") or {}).items() if k in PROBES and str(v).strip()}
        return {"names": names, "room": p.get("room") if p.get("room") in PROBES else None}

    def pellets(self, total):
        """Pellets used since the last reset (refill). The stove's counter (PQT) only counts up and
        cannot be reset over the Connection Box, so a reset remembers where it stood; the stove is
        not touched. With a tank size set, "tank" says what is left in it."""
        try:
            total = float(total)
        except (TypeError, ValueError):
            return None
        mark = self.db.get_setting("pellets_reset") or {}
        if mark.get("kg") is None:
            out = {"total": total, "used": total, "since": None}
        else:
            used = total - mark["kg"] if total >= mark["kg"] else total     # the stove's counter started again
            out = {"total": total, "used": round(used, 1), "since": mark.get("at")}
        tank = self._tank(total, out["used"], mark)
        if tank:
            out["tank"] = tank
        return out

    def tank_settings(self):
        t = self.db.get_setting("pellet_tank") or {}
        size = t.get("size")
        low = t.get("low_pct", TANK_LOW_DEFAULT)
        return {"size": float(size) if size else None, "low_pct": int(low) if low is not None else TANK_LOW_DEFAULT}

    @staticmethod
    def _fill_id(mark):
        """Which filling of the tank a flag (empty, warned) belongs to: the last refill's mark."""
        return mark.get("fill", mark.get("at"))

    @staticmethod
    def _empty_now(empty, fill, used):
        """The no-pellets alarm marked this filling empty, and the stove has not burnt since (it does
        when the tank was filled without tapping "Tank refilled": then the level is not known)."""
        return bool(empty) and empty.get("fill") == fill and used - (empty.get("used") or 0) < STALE_KG

    def _tank(self, total, used, mark):
        t = self.tank_settings()
        size = t["size"]
        if not size:
            return None
        fill = self._fill_id(mark)
        is_empty = self._empty_now(self.db.get_setting("pellets_empty") or {}, fill, used)
        # A refill with a given amount records how much was in the tank then; otherwise the tank
        # was filled up at the last refill (or reset).
        level = self.db.get_setting("pellet_level") or {}
        if level.get("fill") == fill and level.get("kg_at") is not None:
            burnt = total - level["kg_at"] if total >= level["kg_at"] else total
            # A level set in % follows a corrected tank size. It was set by eye (or kg were added to an
            # estimate), and the counter may step whole kg: more burnt than that only once clearly so.
            raw = (level["left"] * size / level["size"] if level.get("size") else level["left"]) - burnt
            slack = max(1.0, size * REFILL_PCT / 100)
        else:
            raw = size - used
            slack = 0.05
        left = 0.0 if is_empty else max(0.0, min(size, raw))
        rate = self.pellet_rate()
        return {"size": size, "left": round(left, 1), "pct": int(round(100 * left / size)),
                "low_pct": t["low_pct"], "low": t["low_pct"] > 0 and left <= size * t["low_pct"] / 100,
                "empty": is_empty, "over": not is_empty and raw < -slack,
                "rate": round(rate, 2) if rate else None,
                "hours_left": round(left / rate, 1) if rate and left > 0 else None}

    # -- the burn rate (kg/h) from the stove's own counter while it burns --------
    def pellet_sample(self, total, burning, now=None):
        now = time.monotonic() if now is None else now
        try:
            kg = float(total)
        except (TypeError, ValueError):
            return
        with self._pellet_lock:
            q = self._pellet_samples
            last = q[-1] if q else None
            if last and kg < last[1]:
                q.clear()                                      # the stove's counter started again
                last = None
            if last is None or now - last[0] >= 60 or kg != last[1] or burning != last[2]:
                q.append((now, kg, bool(burning)))
            while q and now - q[0][0] > RATE_WINDOW:
                q.popleft()

    def pellet_rate(self):
        """kg per hour of burning over the last hours, or None while that is not known yet. Measured
        from the first to the last step of the counter: it may count whole kg, and the time before its
        first step and after its last one says nothing about how fast it goes."""
        with self._pellet_lock:
            q = list(self._pellet_samples)
        burn_s = kg = 0.0
        first = last = None                    # (burning s, kg) where the counter stepped
        for (t0, k0, b0), (t1, k1, b1) in zip(q, q[1:]):
            if b0 and b1 and 0 < t1 - t0 <= RATE_GAP and k1 >= k0:
                burn_s += t1 - t0
                kg += k1 - k0
                if k1 > k0:
                    last = (burn_s, kg)
                    first = first or last
        if not first or last[0] - first[0] < RATE_MIN_BURN or last[1] - first[1] < RATE_MIN_KG:
            return None
        return (last[1] - first[1]) / ((last[0] - first[0]) / 3600)

    def _pellets_watch(self, payload, no_pellets=None):
        """Telemetry: the burn rate, the empty tank (the stove's "no pellets" alarm) and "running low".
        no_pellets is "new" when the stove has just raised that alarm and "seen" when it was already on
        as the hub started. Returns the push (title, body) for a new no-pellets alarm."""
        P = payload.get("pellets")
        if not P:
            return None
        self.pellet_sample(P["total"], payload.get("phase") == "running")
        push = low = None
        republish = False
        with self._tank_lock:
            mark = self.db.get_setting("pellets_reset") or {}
            fill = self._fill_id(mark)
            P = self.pellets(P["total"])        # (not the payload's: a refill may have been tapped since)
            if no_pellets:
                empty = self.db.get_setting("pellets_empty") or {}
                if mark.get("kg") is not None and P["used"] < STALE_KG and \
                        (mark.get("by") != "level" or (no_pellets == "seen" and mark.get("in_alarm"))):
                    # "Tank refilled" was tapped while the fire died down (the stove raises the alarm
                    # once it is out), or the hub restarts during the alarm after a refill. (A filling
                    # a level set by eye started need not have had pellets put in: only the alarm it was
                    # set during is an old one.)
                    push = ("Stove alarm: no pellets",
                            "The stove stopped for lack of pellets just after the tank was refilled. Reset the "
                            "alarm on the stove's panel to light it again.")
                else:
                    if not self._empty_now(empty, fill, P["used"]):
                        self.db.set_setting("pellets_empty", {"fill": fill, "at": int(time.time()), "used": P["used"]})
                        since = f" since the tank was filled on {dt.date.fromtimestamp(P['since']):%d %b}" \
                            if P.get("since") else ""
                        self.db.log_event("stove", "system", "pellets empty",
                                          f"The stove stopped: no pellets. {P['used']:g} kg were burnt{since}.", ok=False)
                        payload["pellets"] = self.pellets(P["total"])
                        republish = True
                    push = ("Pellet tank empty", "The stove stopped: no pellets. Fill the tank, tap \"Tank refilled\" "
                            "and reset the alarm on the stove's panel.")
            else:
                T = P.get("tank")
                warned = self.db.get_setting("pellets_low_warned") or {}
                # (not while what is left is known to be wrong: more was burnt than the tank holds)
                if T and T["low"] and not T["empty"] and not T["over"] and not (warned and warned.get("fill") == fill):
                    self.db.set_setting("pellets_low_warned", {"fill": fill, "at": int(time.time())})
                    hours = f", roughly {T['hours_left']:g} h of burning" if T.get("hours_left") else ""
                    low = f"About {T['left']:g} kg left in the tank ({T['pct']} %){hours}."
                    self.db.log_event("hub", "system", "pellets low", low)
        if republish:
            self._publish(payload)                     # every screen shows the empty tank at once
        if low:
            self.notify("problems", "Pellets running low", low + " Time to refill.", tag="pellets")
        return push if no_pellets == "new" else None

    def _status_payload(self, data=None, error=None):
        out = summarize(data) if data is not None else {"error": error}
        if data is not None and data.get("PQT") is not None:
            out["pellets"] = self.pellets(data["PQT"])
        out["scheduler"] = self.scheduler.info()
        out["box"] = self.stove.host
        out["probes"] = self.probes()
        out["pellet_tank"] = self.tank_settings()
        return out

    def read_status(self):
        try:
            return self._status_payload(self.stove.status())
        except plz.PalazzettiError as e:
            return self._status_payload(error=str(e))

    def _publish(self, payload):
        self._last_status = payload
        self.broadcast("status", payload)

    def _telemetry(self):
        sent_key, sent_at = None, 0.0
        phase = None
        failing_since, warned = None, False
        while not self._stop.is_set():
            payload = self.read_status()
            now = time.monotonic()
            key = json.dumps(payload, sort_keys=True, default=str)
            if key != sent_key or now - sent_at > STATUS_RESEND:
                self._publish(payload)
                sent_key, sent_at = key, now
            else:
                self._last_status = payload
            # alarms and an unreachable box are worth a notification
            if payload.get("error"):
                failing_since = failing_since or now
                if not warned and now - failing_since > UNREACHABLE_NOTIFY:
                    warned = True
                    self.db.log_event("hub", "system", "box offline", payload["error"], ok=False)
                    self.notify("problems", "Stove not reachable",
                                f"The Connection Box has not answered for {UNREACHABLE_NOTIFY // 60} minutes: "
                                f"{payload['error']}", tag="box")
            else:
                if warned:
                    self.db.log_event("hub", "system", "box online", "The Connection Box answers again")
                failing_since, warned = None, False
                new_phase = payload.get("phase")
                no_pellets = new_phase == "alarm" and payload.get("status") in NO_PELLETS_CODES
                entered = new_phase == "alarm" and phase not in (None, "alarm")
                text = payload.get("status_text") or "alarm"
                if entered:
                    self.db.log_event("stove", "system", "alarm", text, ok=False)
                    if not no_pellets:                  # (that one gets its own message below)
                        self.notify("problems", "Stove alarm", f"{text}. Check the stove (pellets, door, ash).",
                                    tag="alarm")
                push = None
                try:
                    push = self._pellets_watch(payload, ("new" if entered else "seen" if phase is None else None)
                                               if no_pellets else None)
                except Exception:
                    log.exception("pellet tank bookkeeping failed")
                if entered and no_pellets:             # (without a pellet counter: the generic message)
                    title, body = push or ("Stove alarm", f"{text}. Check the stove (pellets, door, ash).")
                    self.notify("problems", title, body, tag="pellets" if push else "alarm")
                phase = new_phase
            with self._lock:
                watching = any(not c.pairing for c in self.clients)
            self._wake.wait(FAST_POLL if watching else SLOW_POLL)
            self._wake.clear()

    def notify(self, category, title, body, tag=None):
        """Push notification to the phones that asked for this category."""
        if self.cloud:
            try:
                self.cloud.notify(category, title, body, tag)
            except Exception:
                log.exception("push notification failed")

    def _on_event(self, event):
        self.broadcast("activity", event_public(event))
        if event["actor"] == "scheduler" and event["action"] in ("slot start", "slot end") and event["ok"]:
            title = "Schedule: stove on" if event["action"] == "slot start" else "Schedule: stove off"
            self.notify("schedule", title, event["detail"], tag="schedule")

    # -- stove -------------------------------------------------------------------
    def status_get(self, client, p):
        return self.read_status()

    def action(self, client, p):
        name, value = p.get("action"), p.get("value")
        value = validate_command(name, value)
        if name in ("on", "off"):
            detail = f"switched {name}"
        elif name == "setpoint":
            detail = f"setpoint {value:g} °C"
        else:
            detail = f"{name} {fan_text(value) if name == 'fan' else value}"
        t = time.monotonic()
        try:
            state = self.stove.command(name, value)
        except plz.PalazzettiError as e:
            self._log(client, name, f"{detail} failed: {e}", ok=False)
            raise RPCError(str(e), 502) from None
        self.scheduler.notify_manual(name)
        self._log(client, name, detail)
        out = self._status_payload(state)
        self._publish(out)
        threading.Timer(2.5, self._wake.set).start()   # read back what the stove made of it
        return dict(out, took=round(time.monotonic() - t, 1))

    def probes_set(self, client, p):
        names = p.get("names") or {}
        room = p.get("room")
        if not isinstance(names, dict) or any(k not in PROBES for k in names):
            raise RPCError("names must map T1..T5 to a label")
        if room is not None and room not in PROBES:
            raise RPCError("room must be one of T1..T5 or null")
        clean = {k: str(v or "").strip()[:20] for k, v in names.items() if str(v or "").strip()}
        self.db.set_setting("probes", {"names": clean, "room": room})
        labels = ", ".join(f"{k} = {v}" for k, v in sorted(clean.items()))
        self._log(client, "temperatures", f"room: {room or '-'}" + (f"; {labels}" if labels else ""))
        self._wake.set()
        return self.probes()

    def pellets_reset(self, client, p):
        try:
            data = self.stove.read_status()
        except plz.PalazzettiError as e:
            raise RPCError(str(e), 502) from None
        if data.get("PQT") is None:
            raise RPCError("the stove does not report its pellet counter", 409)
        with self._tank_lock:
            before = self.pellets(data["PQT"])
            self._set_fill(float(data["PQT"]))
        self._log(client, "pellets reset", f"{before['used']:g} kg counted until now; the stove's counter is at "
                                           f"{float(data['PQT']):g} kg")
        out = self._status_payload(data)
        self._publish(out)
        return out

    def _set_fill(self, baseline, by=None, in_alarm=False):
        """A new filling of the tank: the counter's mark, and the empty / running-low flags go.
        by="level": started by a level set by eye, not by pellets put in; in_alarm: during the stove's
        no-pellets alarm."""
        now = time.time()
        prev = (self.db.get_setting("pellets_reset") or {}).get("fill") or 0
        fill = max(int(now * 1000), int(prev) + 1)      # unique, also for two refills in one millisecond
        mark = {"kg": round(baseline, 3), "at": int(now), "fill": fill}
        if by:
            mark["by"] = by
        if in_alarm:
            mark["in_alarm"] = True
        self.db.set_setting("pellets_reset", mark)
        self.db.set_setting("pellets_empty", {})
        self.db.set_setting("pellets_low_warned", {})
        return fill

    def pellets_refill(self, client, p):
        """The tank was filled up ({}), or {"added": kg} were put in."""
        try:
            data = self.stove.read_status()
        except plz.PalazzettiError as e:
            raise RPCError(str(e), 502) from None
        if data.get("PQT") is None:
            raise RPCError("the stove does not report its pellet counter", 409)
        total = float(data["PQT"])
        added = p.get("added")
        if added not in (None, ""):
            if isinstance(added, bool):
                raise RPCError("added must be a number of kg")
            try:
                added = float(added)
            except (TypeError, ValueError):
                raise RPCError("added must be a number of kg") from None
            if not 0 < added <= TANK_MAX_KG:
                raise RPCError(f"added must be more than 0 and at most {TANK_MAX_KG} kg")
        with self._tank_lock:
            before = self.pellets(total)
            tank = before.get("tank")
            if added in (None, ""):
                self._set_fill(total)
                detail = f"tank filled up; {before['used']:g} kg burnt since the last refill" + \
                    (" (it had run empty)" if tank and tank["empty"] else "")
            else:
                if not tank:
                    raise RPCError("set the size of the pellet tank first", 409)
                if added > tank["size"] + 0.05:
                    raise RPCError(f"the tank holds only {tank['size']:g} kg")
                left = round(min(tank["size"], tank["left"] + added), 1)
                fill = self._set_fill(total)
                self.db.set_setting("pellet_level", {"fill": fill, "kg_at": total, "left": left})
                level = "the tank is full" if left >= tank["size"] else f"about {left:g} of {tank['size']:g} kg in the tank"
                detail = f"{added:g} kg added, {level}; {before['used']:g} kg burnt since the last refill"
        self._log(client, "pellets refilled", detail)
        out = self._status_payload(data)
        self._publish(out)
        return out

    def pellets_level(self, client, p):
        """{"pct": 0..100}: how full the tank is now (somebody looked into it). Clearly more than the
        estimate counts as a refill; so does any level when the estimate is known to be wrong, or a level
        above 0 on a tank that ran empty. Otherwise it corrects the estimate and "burnt since" goes on."""
        pct = p.get("pct")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
            raise RPCError("pct must be 0 to 100")
        pct = int(round(pct))
        try:
            data = self.stove.read_status()
        except plz.PalazzettiError as e:
            raise RPCError(str(e), 502) from None
        if data.get("PQT") is None:
            raise RPCError("the stove does not report its pellet counter", 409)
        total = float(data["PQT"])
        with self._tank_lock:
            mark = self.db.get_setting("pellets_reset") or {}
            before = self.pellets(total)
            tank = before.get("tank")
            if not tank:
                raise RPCError("set the size of the pellet tank first", 409)
            size = tank["size"]
            left = size * pct / 100                  # (not rounded: the tank then shows the % that was set)
            known = not tank["over"] and mark.get("kg") is not None     # the estimate means something
            put_in = known and left > tank["left"] + max(1.0, size * REFILL_PCT / 100)
            if put_in or (not known and not tank["empty"]) or (tank["empty"] and left > 0):
                fill = self._set_fill(total, by=None if put_in else "level",
                                      in_alarm=data.get("STATUS") in NO_PELLETS_CODES)
                action = "pellets refilled"
                detail = f"the tank is about {pct} % full now ({round(left, 1):g} of {size:g} kg); " \
                         f"{before['used']:g} kg burnt since the last refill"
            else:
                fill = self._fill_id(mark)
                action = "pellet level"
                detail = f"about {pct} % in the tank ({round(left, 1):g} of {size:g} kg); " \
                         f"the estimate was {tank['pct']} %"
            self.db.set_setting("pellet_level", {"fill": fill, "kg_at": total, "left": left, "size": size})
            low = self.tank_settings()["low_pct"]
            # (set low by hand: they know, so no "running low" message for this filling; set higher: it may come)
            self.db.set_setting("pellets_low_warned", {"fill": fill, "at": int(time.time())}
                                if low and left <= size * low / 100 else {})
        self._log(client, action, detail)
        out = self._status_payload(data)
        self._publish(out)
        return out

    def pellets_tank(self, client, p):
        """{"size": kg or null (no tank), "low_pct": 0..90 (0 = no warning)}"""
        size = p.get("size")
        if size in (None, "", 0):
            size = None
        else:
            if isinstance(size, bool):
                raise RPCError("size must be a number of kg")
            try:
                size = float(size)
            except (TypeError, ValueError):
                raise RPCError("size must be a number of kg") from None
            if not 1 <= size <= TANK_MAX_KG:
                raise RPCError(f"the tank size must be 1 to {TANK_MAX_KG} kg")
            size = round(size, 1)
        low = p.get("low_pct", self.tank_settings()["low_pct"])
        if isinstance(low, bool) or not isinstance(low, (int, float)) or not 0 <= low <= 90:
            raise RPCError("low_pct must be 0 to 90")
        low = int(low)
        with self._tank_lock:
            prev = self.tank_settings()
            self.db.set_setting("pellet_tank", {"size": size, "low_pct": low})
            if (prev["size"], prev["low_pct"]) != (size, low):
                self.db.set_setting("pellets_low_warned", {})    # the new size or level may warn again
        self._log(client, "pellet tank", f"{size:g} kg, a warning below {low} %" if size and low else
                  f"{size:g} kg, no warning" if size else "no tank size (only the kg used are shown)")
        try:
            out = self._status_payload(self.stove.read_status())
        except plz.PalazzettiError as e:
            out = self._status_payload(error=str(e))
        self._publish(out)
        return out

    # -- schedule ----------------------------------------------------------------
    def schedule(self):
        now = dt.datetime.now()
        return {
            "enabled": self.scheduler.enabled,
            "entries": [entry_public(e) for e in self.db.schedule_entries()],
            "info": self.scheduler.info(now),
            "hub_time": now.strftime("%a %H:%M"),
            "timezone": time.strftime("%Z"),
        }

    def _slot_text(self, e):
        return (f"'{e['name'] or 'slot'}' {days_text([d for d in range(7) if e['days'] >> d & 1])} "
                f"{e['start_time']}-{e['end_time']}, {describe(e)}"
                + ("" if e["enabled"] else " (disabled)"))

    def _schedule_changed(self):
        self.scheduler.poke()
        out = self.schedule()
        self.broadcast("schedule", out)
        self._wake.set()       # the status card shows the schedule state too
        return out

    def schedule_get(self, client, p):
        return self.schedule()

    def schedule_save(self, client, p):
        entry = validate_entry(p.get("entry") or {})
        entry_id = p.get("id")
        if entry_id is None:
            if len(self.db.schedule_entries()) >= 100:
                raise RPCError("Too many slots (max 100).")
            entry_id = self.db.schedule_save(entry, by=client.who)
            self._log(client, "slot added", self._slot_text(entry))
        else:
            if not isinstance(entry_id, int) or not self.db.schedule_entry(entry_id):
                raise RPCError("slot not found", 404)
            self.db.schedule_save(entry, entry_id=entry_id, by=client.who)
            self._log(client, "slot changed", self._slot_text(entry))
        out = self._schedule_changed()
        saved = self.db.schedule_entry(entry_id)
        out["saved"] = entry_id
        out["active_now"] = bool(saved and self.scheduler.covers_now(saved)) and out["enabled"]
        return out

    def schedule_delete(self, client, p):
        entry = self.db.schedule_entry(p.get("id")) if isinstance(p.get("id"), int) else None
        if not entry:
            raise RPCError("slot not found", 404)
        self.db.schedule_delete(entry["id"])
        self._log(client, "slot deleted", self._slot_text(entry))
        return self._schedule_changed()

    def schedule_enable(self, client, p):
        enabled = p.get("enabled")
        if not isinstance(enabled, bool):
            raise RPCError("enabled must be true or false")
        self.scheduler.set_enabled(enabled)
        self._log(client, "schedule " + ("resumed" if enabled else "paused"))
        return self._schedule_changed()

    # -- activity, system ----------------------------------------------------------
    def events_list(self, client, p):
        try:
            limit = min(max(int(p.get("limit", 100)), 1), 500)
        except (TypeError, ValueError):
            limit = 100
        return {"events": [event_public(e) for e in self.db.events(limit)]}

    def system_get(self, client, p):
        return {
            "version": __version__,
            "python": platform.python_version(),
            "uptime_s": int(time.time() - self.started),
            "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S ") + time.strftime("%Z"),
            "clock_ok": self.scheduler.clock_ok,
            "box": {"host": self.stove.host, "auto": not self.stove.fixed_host,
                    "last_ok": local_time(self.stove.last_ok), "last_error": self.stove.last_error},
            "mdns": {"enabled": self.mdns is not None, "name": f"{self.cfg.mdns_name}.local",
                     "interfaces": dict(self.mdns.ifaces) if self.mdns else {}},
            "cloud": self.cloud.status() if self.cloud else {"configured": False},
            "lan_url": self.lan_url(),
            "device": {"id": self.cloud.device_id if self.cloud else None, "name": self.device_name},
            "update": self.updates.info(),
        }

    def system_update(self, client, p):
        try:
            info = self.updates.request()
        except UpdateError as e:
            raise RPCError(str(e), 409) from None
        self._log(client, "update", "asked for an update from GitHub")
        return info

    def device_rename(self, client, p):
        name = str(p.get("name") or "").strip()
        if not name or len(name) > NAME_MAX:
            raise RPCError(f"Give the stove a name of 1-{NAME_MAX} characters.")
        old = self.device_name
        self.db.set_setting("device_name", name)
        self._log(client, "renamed", f"{old} -> {name}")
        if self.cloud:
            self.cloud.changed()
        return {"name": name}

    # -- cloud (home network only) --------------------------------------------------
    def _need_cloud(self):
        if not self.cloud:
            raise RPCError("Remote access is not set up ([cloud] url in the config).", 400)
        return self.cloud

    # -- the local API (urlapi.py): its secret token is shown only at home ----------------
    def urlapi_info(self, client, p):
        return {"token": urlapi.api_token(self.db), "base": self.lan_url().rstrip("/")}

    def urlapi_regenerate(self, client, p):
        urlapi.new_api_token(self.db)
        self._log(client, "new API token", "Links with the old token stop working")
        return self.urlapi_info(client, p)

    def cloud_info(self, client, p):
        if not self.cloud:
            return {"configured": False}
        return self.cloud.info(lan=True)

    def cloud_regenerate(self, client, p):
        info = self._need_cloud().regenerate()
        self._log(client, "new token", "The old token can no longer add the stove to an account")
        return info

    def cloud_unlink(self, client, p):
        cloud = self._need_cloud()
        link = cloud.links.get(str(p.get("link") or ""))
        if not link:
            raise RPCError("unknown link", 404)
        cloud.unlink(link["id"], reason="removed at home")
        self._log(client, "account removed", f"{link['profile']} can no longer reach the stove")
        return cloud.info(lan=True)


def event_public(e):
    return dict(e, time=local_time(e["ts"]), ok=bool(e["ok"]))
