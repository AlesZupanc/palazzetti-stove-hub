"""Weekly scheduler.

A schedule is a list of slots. Each slot has days of the week, a start and an
end time (an end at or before the start means "next day") and its own power
level, room-fan level and target temperature.

The scheduler acts only at slot boundaries:

* slot starts -> switch the stove on (if it is off) and apply the slot's
  setpoint, power and fan;
* slot ends and no other slot takes over -> switch the stove off;
* back-to-back or overlapping slots -> the slot that started last wins, its
  settings are applied without switching off in between.

Between boundaries it never reverts what you do by hand. A manual on/off
cancels a start or stop the scheduler is still retrying.

Robustness:

* the current slot is persisted, so a restart in the middle of a slot does not
  switch the stove on again (and a slot that began while the hub was down is
  still started, and a missed end is still applied within off_grace_minutes);
* a start waits while the stove is cooling down / in an unknown state / in
  alarm, and retries when the box does not answer (up to start_wait_minutes);
* it waits for NTP before acting (the Pi has no battery-backed clock).
"""

import ctypes
import ctypes.util
import datetime as dt
import logging
import os
import re
import threading
import time

import palazzetti as plz

from .stove import fan_text, phase_of, status_text, validate_command

log = logging.getLogger("scheduler")

DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
STATE_KEY = "scheduler_state"
ENABLED_KEY = "scheduler_enabled"

TICK_SECONDS = 20
RETRY_SECONDS = 60
ALARM_RETRY_SECONDS = 300
OFF_RETRY_WINDOW = 1800
CLOCK_WAIT_SECONDS = 600
MAX_ON_TRIES = 3          # CMD ON is re-sent if the stove stays off, at most this often
ON_VERIFY_LIMIT = 600     # off again this long after our ON = someone switched it off


# --------------------------------------------------------------------------
# Slot definitions
# --------------------------------------------------------------------------
def parse_hhmm(text):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise ValueError(f"time must be HH:MM, got {text!r}")
    return dt.time(int(m.group(1)), int(m.group(2)))


def days_to_mask(days):
    """[0, 1, 4] / ["Mon", "Fri"] / 31 -> bit mask (bit 0 = Monday)."""
    if isinstance(days, bool):
        raise ValueError("days must be a list of weekdays")
    if isinstance(days, int):
        mask = days
    elif isinstance(days, (list, tuple)):
        mask = 0
        for d in days:
            if isinstance(d, str):
                name = d.strip()[:3].title()
                if name not in DAY_NAMES:
                    raise ValueError(f"unknown day {d!r}")
                d = DAY_NAMES.index(name)
            if isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6:
                raise ValueError(f"unknown day {d!r}")
            mask |= 1 << d
    else:
        raise ValueError("days must be a list of weekdays")
    if not 0 < mask < 128:
        raise ValueError("pick at least one day")
    return mask


def mask_to_days(mask):
    return [i for i in range(7) if mask >> i & 1]


def validate_entry(data):
    """Clean a slot coming from the UI/API; raises ValueError with a readable message."""
    if not isinstance(data, dict):
        raise ValueError("invalid slot")
    name = str(data.get("name") or "").strip()[:40]
    start = parse_hhmm(data.get("start"))
    end = parse_hhmm(data.get("end"))
    if start == end:
        raise ValueError("start and end time must differ")
    try:
        setpoint = validate_command("setpoint", data.get("setpoint"))
        power = validate_command("power", data.get("power"))
        fan = validate_command("fan", data.get("fan"))
    except ValueError as e:
        raise ValueError(str(e).replace("setpoint", "temperature")) from None
    return {
        "name": name,
        "days": days_to_mask(data.get("days")),
        "start_time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
        "power": power,
        "fan": fan,
        "setpoint": setpoint,
        "enabled": bool(data.get("enabled", True)),
    }


def describe(entry):
    return f"power {entry['power']}, fan {fan_text(entry['fan'])}, {entry['setpoint']:g} °C"


def entry_public(e):
    return {
        "id": e["id"],
        "name": e["name"],
        "days": mask_to_days(e["days"]),
        "start": e["start_time"],
        "end": e["end_time"],
        "overnight": parse_hhmm(e["end_time"]) <= parse_hhmm(e["start_time"]),
        "power": e["power"],
        "fan": e["fan"],
        "fan_text": fan_text(e["fan"]),
        "setpoint": e["setpoint"],
        "enabled": e["enabled"],
        "updated_by": e.get("updated_by", ""),
    }


class Occurrence:
    __slots__ = ("entry", "start", "end")

    def __init__(self, entry, start, end):
        self.entry, self.start, self.end = entry, start, end

    @property
    def key(self):
        return f"{self.entry['id']}@{self.start:%Y-%m-%dT%H:%M}"

    @property
    def name(self):
        return self.entry["name"] or f"{self.entry['start_time']}-{self.entry['end_time']}"

    def public(self):
        e = self.entry
        return {
            "id": e["id"], "name": self.name,
            "day": DAY_NAMES[self.start.weekday()],
            "start": self.start.strftime("%H:%M"), "end": self.end.strftime("%H:%M"),
            "start_iso": self.start.isoformat(timespec="minutes"),
            "end_iso": self.end.isoformat(timespec="minutes"),
            "power": e["power"], "fan": e["fan"], "fan_text": fan_text(e["fan"]),
            "setpoint": e["setpoint"],
        }


def occurrences_on(entries, day):
    out = []
    for e in entries:
        if not e["enabled"] or not e["days"] >> day.weekday() & 1:
            continue
        start = dt.datetime.combine(day, parse_hhmm(e["start_time"]))
        end = dt.datetime.combine(day, parse_hhmm(e["end_time"]))
        if end <= start:
            end += dt.timedelta(days=1)
        out.append(Occurrence(e, start, end))
    return out


def active_at(entries, now):
    """The slot in force at `now` (latest start wins when slots overlap)."""
    today = now.date()
    candidates = [o for day in (today - dt.timedelta(days=1), today)
                  for o in occurrences_on(entries, day) if o.start <= now < o.end]
    if not candidates:
        return None
    return max(candidates, key=lambda o: (o.start, o.entry["id"]))


def next_start(entries, now, horizon_days=8):
    today = now.date()
    candidates = [o for i in range(horizon_days)
                  for o in occurrences_on(entries, today + dt.timedelta(days=i)) if o.start > now]
    if not candidates:
        return None
    return min(candidates, key=lambda o: (o.start, -o.entry["id"]))


def _same(a, b):
    try:
        return abs(float(a) - float(b)) < 0.05
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------
# Clock
# --------------------------------------------------------------------------
def system_clock_synced():
    """True/False when the kernel knows whether NTP synchronised the clock, None if unknown."""
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or None, use_errno=True)
        buf = ctypes.create_string_buffer(1024)  # struct timex with modes = 0: read only
        state = libc.adjtimex(buf)
        if state >= 0:
            return state != 5  # TIME_ERROR: clock not synchronised
    except (OSError, AttributeError, TypeError):
        pass
    if os.path.exists("/run/systemd/timesync/synchronized"):
        return True
    return None


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------
class Scheduler:
    def __init__(self, db, stove, cfg=None, notify=None, clock=None, clock_synced=None):
        self.db = db
        self.stove = stove
        self.notify = notify  # notify(subject, body): e-mail the admins
        self.clock = clock or dt.datetime.now
        self.clock_synced = clock_synced or system_clock_synced
        self.off_grace = (cfg.off_grace_minutes if cfg else 120) * 60
        self.start_wait = (cfg.start_wait_minutes if cfg else 30) * 60
        self.wait_for_sync = cfg.wait_for_time_sync if cfg else False
        self.lock = threading.RLock()
        self.clock_ok = not self.wait_for_sync
        self.state = self._load_state()
        self._manual = []
        self._mlock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread = None

    # -- persistence -----------------------------------------------------------
    def _load_state(self):
        st = self.db.get_setting(STATE_KEY, None)
        return st if isinstance(st, dict) else {"key": None, "off": None}

    def _save(self, st):
        self.state = st
        self.db.set_setting(STATE_KEY, st)

    @property
    def enabled(self):
        return bool(self.db.get_setting(ENABLED_KEY, True))

    def set_enabled(self, value):
        self.db.set_setting(ENABLED_KEY, bool(value))
        self.poke()

    # -- thread ----------------------------------------------------------------
    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="scheduler", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()

    def poke(self):
        """Re-evaluate soon (after a schedule edit or a manual command)."""
        self._wake.set()

    def _wait_for_clock(self):
        if not self.wait_for_sync:
            self.clock_ok = True
            return
        deadline = time.monotonic() + CLOCK_WAIT_SECONDS
        warned = False
        while not self._stop.is_set():
            synced = self.clock_synced()
            if synced is None or synced:
                break
            if time.monotonic() > deadline:
                log.warning("clock still not synchronised after %d min - running the schedule anyway",
                            CLOCK_WAIT_SECONDS // 60)
                self._event("clock", "System clock not synchronised (no NTP?) - schedule runs on local time",
                            ok=False)
                break
            if not warned:
                log.info("waiting for the system clock to be synchronised before running the schedule")
                warned = True
            self._stop.wait(5)
        self.clock_ok = True

    def _run(self):
        self._wait_for_clock()
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:  # never let the scheduler thread die
                log.exception("scheduler tick failed")
            self._wake.wait(TICK_SECONDS)
            self._wake.clear()

    # -- events ----------------------------------------------------------------
    def _event(self, action, detail, ok=True):
        (log.info if ok else log.warning)("%s: %s", action, detail)
        try:
            self.db.log_event("scheduler", "scheduler", action, detail, ok)
        except Exception:
            log.exception("could not write event")

    def _notify(self, subject, body):
        if self.notify:
            try:
                self.notify(subject, body)
            except Exception:
                log.exception("notification failed")

    # -- manual commands ---------------------------------------------------------
    def notify_manual(self, action):
        """Called after a user switched the stove on/off by hand."""
        if action not in ("on", "off"):
            return
        with self._mlock:
            self._manual.append((action, self.clock().timestamp()))
        self.poke()

    def _apply_manual(self, st):
        with self._mlock:
            actions, self._manual = self._manual, []
        changed = False
        for action, ts in actions:
            off = st.get("off")
            if off and ts >= off.get("due_ts", 0):
                self._event("manual", f"Stove switched {action} by hand - "
                                      f"not switching off for slot '{off['name']}'")
                st["off"] = None
                changed = True
            if (action == "off" and st.get("key") and st.get("phase") == "pending"
                    and ts >= st.get("start_ts", 0)):
                self._event("manual", f"Stove switched off by hand - slot '{st['name']}' will not start it")
                st["phase"] = "overridden"
                changed = True
        return changed

    # -- the state machine -----------------------------------------------------
    def tick(self, now=None):
        with self.lock:
            now = now or self.clock()
            ts = now.timestamp()
            st = dict(self.state)
            enabled = self.enabled
            entries = self.db.schedule_entries()
            occ = active_at(entries, now) if enabled else None
            changed = False

            if occ is not None and occ.key != st.get("key"):
                window = [occ.start.isoformat(timespec="minutes"), occ.end.isoformat(timespec="minutes")]
                if st.get("key") and [st.get("start"), st.get("end")] == window:
                    # Same time window under a new id (slot re-created, schedule re-imported):
                    # adopt it, do not switch anything.
                    st.update(key=occ.key, entry_id=occ.entry["id"], name=occ.name)
                    changed = True
                elif window not in st.get("done", []):
                    # (a finished occurrence is never entered again, e.g. when the clock
                    # goes back an hour at the end of daylight saving time)
                    st = self._enter_slot(occ, ts, st)
                    changed = True
            elif occ is None and st.get("key"):
                st = self._leave_slot(st, entries, now, ts, enabled)
                changed = True

            if not enabled and st.get("off"):
                self._event("paused", f"Schedule paused - not switching off for slot '{st['off']['name']}'")
                st["off"] = None
                changed = True
            if self._apply_manual(st):
                changed = True
            if occ is not None and st.get("phase") == "pending" and ts >= st.get("next_try", 0):
                self._try_start(occ, st, ts)
                changed = True
            if st.get("off") and ts >= st["off"].get("next_try", 0):
                self._try_off(st, ts)
                changed = True
            if changed:
                self._save(st)

    def _enter_slot(self, occ, ts, old):
        if old.get("off"):
            log.info("slot %r starts, cancelling the pending switch-off of %r", occ.name, old["off"]["name"])
        return {
            "key": occ.key,
            "entry_id": occ.entry["id"],
            "name": occ.name,
            "start": occ.start.isoformat(timespec="minutes"),
            "end": occ.end.isoformat(timespec="minutes"),
            "start_ts": occ.start.timestamp(),
            "phase": "pending",
            "on_sent": False,
            "attempts": 0,
            "next_try": 0,
            "since": ts,
            "error": "",
            "logged": "",
            "off": None,
            "done": old.get("done", []),
        }

    def _leave_slot(self, st, entries, now, ts, enabled):
        name = st.get("name") or "slot"
        idle = {"key": None, "off": None, "done": st.get("done", [])}
        if not enabled:
            self._event("paused", f"Schedule paused during slot '{name}' - stove left as it is")
            return idle
        end = self._effective_end(st, entries)
        if end is None or end > now:
            self._event("slot end", f"Slot '{name}' was changed or removed while running - stove left as it is")
            return idle
        idle["done"] = (idle["done"] + [[st.get("start"), st.get("end")]])[-5:]  # recently finished windows
        late = (now - end).total_seconds()
        if late > self.off_grace:
            self._event("slot end", f"Missed the end of slot '{name}' ({end:%a %H:%M}, hub was not running) "
                                    "- stove left as it is", ok=False)
            return idle
        idle["off"] = {"name": name, "due": end.isoformat(timespec="minutes"), "due_ts": end.timestamp(),
                       "since": ts, "attempts": 0, "next_try": 0, "error": "", "logged": ""}
        return idle

    @staticmethod
    def _effective_end(st, entries):
        """End of the remembered slot according to the *current* schedule, or None if it changed."""
        e = next((x for x in entries if x["id"] == st.get("entry_id")), None)
        if e is None or not e["enabled"]:
            return None
        try:
            start = dt.datetime.fromisoformat(st["start"])
        except (KeyError, TypeError, ValueError):
            return None
        if not e["days"] >> start.weekday() & 1 or parse_hhmm(e["start_time"]) != start.time():
            return None
        end = dt.datetime.combine(start.date(), parse_hhmm(e["end_time"]))
        if end <= start:
            end += dt.timedelta(days=1)
        return end

    def _retry(self, st, ts, delay, reason, ok=False):
        """A start attempt did not finish: try again later, or give up after start_wait."""
        st["error"] = reason
        if ts - st.get("since", ts) > self.start_wait:
            st["phase"] = "gave_up"
            msg = f"Slot '{st['name']}': gave up starting the stove after {self.start_wait // 60} min ({reason})"
            self._event("slot start", msg, ok=False)
            self._notify("Stove schedule: slot could not start", msg)
            return
        st["next_try"] = ts + delay
        if st.get("logged") != reason:  # log each distinct problem once
            st["logged"] = reason
            self._event("slot start", f"Slot '{st['name']}': {reason} - will retry", ok=ok)

    def _apply_settings(self, e, d):
        for name, field in (("setpoint", "SETP"), ("power", "PWR"), ("fan", "F2L")):
            if not _same(d.get(field), e[name]):
                self.stove.command(name, e[name], refresh=False)

    def _try_start(self, occ, st, ts):
        e = occ.entry
        st["attempts"] = st.get("attempts", 0) + 1
        try:
            d = self.stove.read_status()
        except plz.PalazzettiError as ex:
            return self._retry(st, ts, RETRY_SECONDS, f"cannot read the stove status ({ex})")
        code = d.get("STATUS")
        phase = phase_of(code)
        if phase == "alarm":
            return self._retry(st, ts, ALARM_RETRY_SECONDS, f"stove reports {status_text(code)}")
        if phase == "shutdown" and st.get("on_sent"):
            # It lit after our ON and was switched off again (on the stove or elsewhere).
            st["phase"] = "overridden"
            self._event("slot start", f"Slot '{st['name']}': the stove was switched off again after the slot "
                                      "lit it - leaving it off")
            return
        if phase in ("shutdown", "unknown"):
            return self._retry(st, ts, RETRY_SECONDS, f"waiting for the stove ({status_text(code)})", ok=True)

        # A manual "off" may have arrived while we were reading the status.
        self._apply_manual(st)
        if st.get("phase") != "pending":
            return
        try:
            if phase == "off":
                if st.get("on_sent") and ts - st.get("on_at", ts) > ON_VERIFY_LIMIT:
                    st["phase"] = "overridden"
                    self._event("slot start", f"Slot '{st['name']}': the stove is off again after the slot "
                                              "switched it on - leaving it off")
                    return
                if st.get("on_tries", 0) >= MAX_ON_TRIES:
                    st["phase"] = "gave_up"
                    msg = (f"Slot '{st['name']}': the stove did not start after {MAX_ON_TRIES} ON commands "
                           f"(it still reports {status_text(code)})")
                    self._event("slot start", msg, ok=False)
                    self._notify("Stove schedule: the stove did not start", msg)
                    return
                self.stove.command("on", refresh=False)
                st["on_sent"] = True
                st["on_at"] = ts
                st["on_tries"] = st.get("on_tries", 0) + 1
                self._apply_settings(e, d)
                # Check in a minute that it is really lighting before calling it done.
                st["next_try"] = ts + RETRY_SECONDS
                st["error"] = ""
                return
            self._apply_settings(e, d)
        except plz.PalazzettiError as ex:
            return self._retry(st, ts, RETRY_SECONDS, f"stove command failed ({ex})")
        except ValueError as ex:  # a stored value the stove wrapper refuses
            st["phase"] = "gave_up"
            self._event("slot start", f"Slot '{st['name']}': invalid setting ({ex})", ok=False)
            return
        st["phase"] = "done"
        st["error"] = ""
        self.stove.refresh_later()
        how = "switched on" if st.get("on_sent") else "stove already running"
        self._event("slot start", f"Slot '{st['name']}' ({occ.start:%H:%M}-{occ.end:%H:%M}): "
                                  f"{how}, {describe(e)}")

    def _retry_off(self, st, ts, reason):
        """A switch-off attempt failed: try again, or give up once it is too late."""
        off = st["off"]
        off["error"] = reason
        if ts - off.get("due_ts", ts) > max(self.off_grace, OFF_RETRY_WINDOW):
            msg = f"Slot '{off['name']}' ended but the stove could not be switched off ({reason})"
            self._event("slot end", msg, ok=False)
            self._notify("Stove schedule: could not switch the stove off", msg)
            st["off"] = None
            return
        off["next_try"] = ts + RETRY_SECONDS
        if off.get("logged") != reason:
            off["logged"] = reason
            self._event("slot end", f"Slot '{off['name']}': {reason} - will retry", ok=False)

    def _try_off(self, st, ts):
        off = st["off"]
        name = off["name"]
        if ts - off.get("due_ts", ts) > self.off_grace + OFF_RETRY_WINDOW:
            # still pending from before a long outage of the hub: too late now
            self._event("slot end", f"Missed the end of slot '{name}' (hub was not running) - "
                                    "stove left as it is", ok=False)
            st["off"] = None
            return
        off["attempts"] = off.get("attempts", 0) + 1   # always at least one attempt
        try:
            d = self.stove.read_status()
        except plz.PalazzettiError as ex:
            return self._retry_off(st, ts, f"cannot read the stove status ({ex})")
        code = d.get("STATUS")
        phase = phase_of(code)
        if phase in ("off", "shutdown"):
            self._event("slot end", f"Slot '{name}' ended: stove already off")
        elif phase == "alarm":
            self._event("slot end", f"Slot '{name}' ended: stove reports {status_text(code)}, "
                                    "not switching off", ok=False)
        else:
            # A manual "on" after the slot ended keeps the stove running.
            self._apply_manual(st)
            if st.get("off") is None:
                return
            try:
                self.stove.command("off")
            except plz.PalazzettiError as ex:
                return self._retry_off(st, ts, f"switching off failed ({ex})")
            self._event("slot end", f"Slot '{name}' ended: switched off")
        st["off"] = None

    # -- information for the UI ------------------------------------------------
    def info(self, now=None):
        now = now or self.clock()
        entries = self.db.schedule_entries()
        enabled = self.enabled
        st = self.state
        occ = active_at(entries, now) if enabled else None
        nxt = next_start(entries, now) if enabled else None
        active = None
        if occ is not None:
            active = occ.public()
            if st.get("key") == occ.key:
                active["phase"] = st.get("phase")
                active["error"] = st.get("error", "")
            else:
                active["phase"] = "pending"
        return {
            "enabled": enabled,
            "clock_ok": self.clock_ok,
            "active": active,
            "next": nxt.public() if nxt else None,
            "off_pending": bool(st.get("off")),
        }

    def covers_now(self, entry, now=None):
        """True if this (saved) slot is the one in force right now."""
        now = now or self.clock()
        occ = active_at(self.db.schedule_entries(), now)
        return occ is not None and occ.entry["id"] == entry["id"]
