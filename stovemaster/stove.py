"""Thread-safe access to the stove through its Connection Box.

The box handles one stove command at a time on its serial link, so every
request (local dashboard, remote sessions, scheduler) goes through one lock. Status reads are
cached for a few seconds; writes answer with an optimistic status and the real
one is re-read in the background (same approach as stove_web.py).
"""

import logging
import threading
import time

import palazzetti as plz

log = logging.getLogger("stove")

STATUS_TTL = 3.0

OFF_CODES = frozenset({0, 1})
SHUTDOWN_CODES = frozenset({10, 12})                     # fire stop, cooling
RUNNING_CODES = frozenset({2, 3, 4, 5, 6, 7, 9, 11, 51})  # ignition ... burning
FAN_LABELS = {0: "Off", 6: "High", 7: "Auto"}            # 1..5 are shown as numbers
SETPOINT_RANGE = (5.0, 40.0)


def is_alarm(code):
    return isinstance(code, int) and (240 <= code <= 255 or code >= 1000)


def phase_of(code):
    """off | shutdown | running | alarm | unknown"""
    if code in OFF_CODES:
        return "off"
    if code in SHUTDOWN_CODES:
        return "shutdown"
    if code in RUNNING_CODES:
        return "running"
    if is_alarm(code):
        return "alarm"
    return "unknown"


def status_text(code):
    if code in plz.STATUS_NAMES:
        return plz.STATUS_NAMES[code]
    if is_alarm(code):
        return f"ALARM (code {code})"
    return f"code {code}"


def fan_text(value):
    try:
        return FAN_LABELS.get(int(value), str(int(value)))
    except (TypeError, ValueError):
        return "?"


def summarize(d):
    code = d.get("STATUS")
    return {
        "status": code,
        "status_text": status_text(code) if code is not None else "unknown",
        "phase": phase_of(code),
        "on": code not in (0, 1, None),
        "alarm": is_alarm(code),
        "setpoint": d.get("SETP"),
        "power": d.get("PWR"),
        "fan": d.get("F2L"),
        "temps": {k: d[k] for k in ("T1", "T2", "T3", "T4", "T5") if k in d},
        "pellets_kg": d.get("PQT"),
    }


def _as_int(value, name, lo, hi):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number")
    try:
        f = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number") from None
    if not f.is_integer() or not lo <= f <= hi:
        raise ValueError(f"{name} must be a whole number {lo}..{hi}")
    return int(f)


def validate_command(name, value=None):
    """Return the cleaned value for a stove command or raise ValueError."""
    if name in ("on", "off"):
        return None
    if name == "setpoint":
        if isinstance(value, bool):
            raise ValueError("setpoint must be a number")
        try:
            v = round(float(value), 1)
        except (TypeError, ValueError):
            raise ValueError("setpoint must be a number") from None
        lo, hi = SETPOINT_RANGE
        if not lo <= v <= hi:
            raise ValueError(f"setpoint must be between {lo:g} and {hi:g} °C")
        return v
    if name == "power":
        return _as_int(value, "power", 1, 5)
    if name == "fan":
        return _as_int(value, "fan", 0, 7)
    raise ValueError(f"unknown action {name!r}")


class StoveUnavailable(plz.PalazzettiError):
    pass


class Stove:
    FIELD = {"setpoint": "SETP", "power": "PWR", "fan": "F2L"}

    def __init__(self, host=None, timeout=15.0, discover=True):
        self.timeout = timeout
        self.fixed_host = host or None
        self.box = plz.PalazzettiBox(host, timeout=timeout) if host else None
        self.lock = threading.Lock()
        self.last_error = ""
        self.last_ok = 0.0
        self._cache = None
        self._cache_t = 0.0
        self._net_errors = 0
        self._discovery = None
        if self.box is None and discover:
            self.start_discovery()

    @property
    def host(self):
        return self.box.host if self.box else None

    # -- discovery -------------------------------------------------------------
    def start_discovery(self):
        if self._discovery and self._discovery.is_alive():
            return
        self._discovery = threading.Thread(target=self._discover_loop, name="discovery", daemon=True)
        self._discovery.start()

    def _discover_loop(self):
        delay = 10
        while True:
            try:
                found = plz.discover(verbose=False)
            except OSError as e:
                log.warning("discovery failed: %s", e)
                found = {}
            if found:
                ip = next(iter(found))
                if self.box is None or self.box.host != ip:
                    log.info("Connection Box found at %s", ip)
                    self.box = plz.PalazzettiBox(ip, timeout=self.timeout)
                self._net_errors = 0
                return
            log.info("no Connection Box found yet, retrying in %d s", delay)
            time.sleep(delay)
            delay = min(delay * 2, 300)

    # -- low level -------------------------------------------------------------
    def _call(self, label, fn, *args):
        if self.box is None:
            raise StoveUnavailable("Connection Box not found yet (discovery is running)")
        t = time.monotonic()
        try:
            result = fn(*args)
        except plz.PalazzettiError as e:
            self.last_error = str(e)
            if isinstance(e.__cause__, OSError):  # box unreachable (not a stove timeout)
                self._net_errors += 1
                if self._net_errors >= 3 and not self.fixed_host:
                    self.start_discovery()
            raise
        finally:
            log.debug("stove %-14s %5.1f s", label, time.monotonic() - t)
        self._net_errors = 0
        self.last_error = ""
        self.last_ok = time.time()
        return result

    def _refresh(self):
        self._cache = self._call("GET ALLS", lambda: self.box.all_status())
        self._cache_t = time.monotonic()
        return self._cache

    # -- reads -----------------------------------------------------------------
    def status(self):
        """Recent status (cached for a few seconds); never queues behind a command."""
        if self._cache is not None and time.monotonic() - self._cache_t < STATUS_TTL:
            return self._cache
        if not self.lock.acquire(blocking=self._cache is None):
            return self._cache
        try:
            return self._refresh()
        finally:
            self.lock.release()

    def read_status(self):
        """Always a fresh read (used by the scheduler before it decides)."""
        with self.lock:
            return self._refresh()

    # -- writes ----------------------------------------------------------------
    def command(self, name, value=None, refresh=True):
        """Send on/off/setpoint/power/fan. Returns an optimistic status dict."""
        value = validate_command(name, value)
        with self.lock:
            if name == "on":
                self._call("CMD ON", lambda: self.box.turn_on())
            elif name == "off":
                self._call("CMD OFF", lambda: self.box.turn_off())
            elif name == "setpoint":
                self._call(f"SET SETP {value:g}", lambda: self.box.set_setpoint(value))
            elif name == "power":
                self._call(f"SET POWR {value}", lambda: self.box.set_power(value))
            elif name == "fan":
                self._call(f"SET RFAN {value}", lambda: self.box.set_room_fan(value))
            optimistic = dict(self._cache or {})
            if name in self.FIELD:
                optimistic[self.FIELD[name]] = value
            self._cache, self._cache_t = optimistic, time.monotonic()
        if refresh:
            self.refresh_later()
        return optimistic

    def refresh_later(self, delay=1.0):
        threading.Thread(target=self._background_refresh, args=(delay,), daemon=True).start()

    def _background_refresh(self, delay):
        time.sleep(delay)  # give the stove a moment to apply the change
        with self.lock:
            try:
                self._refresh()
            except plz.PalazzettiError as e:
                log.info("background refresh failed: %s", e)
