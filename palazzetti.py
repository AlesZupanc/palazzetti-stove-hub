#!/usr/bin/env python3
"""
palazzetti.py - discover and control a Palazzetti Connection Box on your LAN.

Pure standard library, Python 3.8+.

Usage:
  python palazzetti.py                  # same as "discover"
  python palazzetti.py discover
  python palazzetti.py status
  python palazzetti.py on
  python palazzetti.py off
  python palazzetti.py setpoint 21.5
  python palazzetti.py power 3          # 1..5
  python palazzetti.py fan 7            # room fan: 0=off, 1..5, 6=high, 7=auto (model dependent)
  python palazzetti.py raw "GET ALLS"   # send any command, print full JSON
  python palazzetti.py --host 192.168.1.50 status

How it works:
  * Discovery: UDP broadcast "plzbridge?" to port 54549; the box answers.
    If nothing answers (e.g. broadcast blocked), it falls back to probing
    every host on your /24 subnet over HTTP.
  * Control: HTTP GET http://<box>/cgi-bin/sendmsg.lua?cmd=<COMMAND>
    The box replies with JSON: {"INFO": {...}, "SUCCESS": true, "DATA": {...}}
"""

import argparse
import ipaddress
import json
import re
import socket
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

DISCOVERY_PORT = 54549
DISCOVERY_MSG = b"plzbridge?"
HTTP_TIMEOUT = 5.0

# Common STATUS codes (exact set varies by stove model / firmware)
STATUS_NAMES = {
    0: "OFF",
    1: "OFF (timer)",
    2: "Test fire",
    3: "Heat-up",
    4: "Fuel ignition",
    5: "Ignition test",
    6: "Burning",
    9: "Cooling fluid",
    10: "Fire stop",
    11: "Clean fire",
    12: "Cooling",
    51: "Burning (ECO)",
    253: "ALARM: no pellets",
}


class PalazzettiError(Exception):
    pass


# --------------------------------------------------------------------------
# Device API
# --------------------------------------------------------------------------
class PalazzettiBox:
    def __init__(self, host: str, timeout: float = HTTP_TIMEOUT):
        self.host = host
        self.timeout = timeout

    def _get_json(self, url: str, what: str) -> dict:
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
        except (OSError, ValueError) as e:
            raise PalazzettiError(f"{self.host}: '{what}' failed: {e}") from e
        if not isinstance(payload, dict):
            raise PalazzettiError(f"{self.host}: '{what}' returned {payload!r}")
        return payload

    def send(self, cmd: str, retries: int = 2) -> dict:
        """Send a stove command (e.g. 'GET ALLS', 'CMD ON') and return the full JSON reply.

        The box answers RSP=TIMEOUT when the stove doesn't reply on its serial
        link in time; that is usually transient, so it is retried.
        """
        url = f"http://{self.host}/cgi-bin/sendmsg.lua?cmd={urllib.parse.quote(cmd)}"
        for attempt in range(retries + 1):
            payload = self._get_json(url, cmd)
            if payload.get("SUCCESS", False):
                return payload
            info = payload.get("INFO", {})
            if info.get("RSP") == "TIMEOUT" and attempt < retries:
                time.sleep(1.0)
                continue
            if info.get("RSP") == "TIMEOUT":
                raise PalazzettiError(
                    f"{self.host}: box is reachable but the stove did not answer '{cmd}' "
                    f"(RSP=TIMEOUT). Is the stove powered and the COM cable connected?")
            raise PalazzettiError(f"{self.host}: '{cmd}' rejected: {info}")

    def box_info(self) -> dict:
        """Connection Box network/cloud data. Answered by the box itself, no stove needed."""
        url = f"http://{self.host}/cgi-bin/syscmd.lua?cmd=netdata"
        return self._get_json(url, "netdata").get("DATA", {})

    def data(self, cmd: str) -> dict:
        return self.send(cmd).get("DATA", {})

    # Read
    def static_data(self) -> dict:
        return self.data("GET STDT")   # label, MAC, firmware, stove type...

    def all_status(self) -> dict:
        return self.data("GET ALLS")   # status, temperatures, setpoint, power, fans...

    # Write
    def turn_on(self) -> dict:
        return self.data("CMD ON")

    def turn_off(self) -> dict:
        return self.data("CMD OFF")

    def set_setpoint(self, celsius: float) -> dict:
        # Most boxes accept integers; newer firmware also accepts decimals.
        value = int(celsius) if float(celsius).is_integer() else round(celsius, 1)
        return self.data(f"SET SETP {value}")

    def set_power(self, level: int) -> dict:
        if not 1 <= level <= 5:
            raise ValueError("power must be 1..5")
        return self.data(f"SET POWR {level}")

    def set_room_fan(self, level: int) -> dict:
        if not 0 <= level <= 7:
            raise ValueError("fan must be 0..7")
        return self.data(f"SET RFAN {level}")


# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------
def local_ip() -> str:
    """IP of the interface that routes to the internet (no packet is actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def discover_udp(timeout: float = 3.0) -> dict:
    """Broadcast 'plzbridge?' and collect replies. Returns {ip: reply_dict}."""
    found = {}
    targets = ["255.255.255.255"]
    try:
        net = ipaddress.ip_network(f"{local_ip()}/24", strict=False)
        targets.append(str(net.broadcast_address))
    except OSError:
        pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.5)
    try:
        for t in targets:
            try:
                sock.sendto(DISCOVERY_MSG, (t, DISCOVERY_PORT))
            except OSError:
                pass
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw, (ip, _port) = sock.recvfrom(4096)
            except socket.timeout:
                continue
            try:
                reply = json.loads(raw.decode("utf-8", "replace"))
                reply = reply.get("DATA", reply) if isinstance(reply, dict) else {}
            except ValueError:
                reply = {"raw": raw[:100].decode("utf-8", "replace")}
            found[ip] = reply
    finally:
        sock.close()
    return found


def discover_http(timeout: float = 1.5) -> dict:
    """Fallback: probe every host on the local /24 for the Connection Box web UI.

    Step 1 (fast): GET / and look for the box's redirect to ./cgi-bin/.
    Step 2 (slow, only on matches): GET STDT, which waits on the stove's serial link.
    """
    net = ipaddress.ip_network(f"{local_ip()}/24", strict=False)

    def probe(ip):
        try:
            with urllib.request.urlopen(f"http://{ip}/", timeout=timeout) as r:
                page = r.read(512).decode("utf-8", "replace")
        except OSError:
            return None
        if "cgi-bin" not in page:
            return None
        try:
            return ip, PalazzettiBox(ip).box_info()
        except PalazzettiError:
            return ip, {}  # web UI found, netdata failed

    found = {}
    with ThreadPoolExecutor(max_workers=64) as pool:
        for result in pool.map(probe, [str(h) for h in net.hosts()]):
            if result:
                found[result[0]] = result[1]
    return found


def discover(verbose: bool = True) -> dict:
    if verbose:
        print("Broadcasting discovery (UDP 54549)...", file=sys.stderr)
    found = discover_udp()
    if not found:
        if verbose:
            print("No UDP reply, scanning local /24 over HTTP...", file=sys.stderr)
        found = discover_http()
    return found


# --------------------------------------------------------------------------
# Pretty printing
# --------------------------------------------------------------------------
def print_status(d: dict) -> None:
    code = d.get("STATUS")
    name = STATUS_NAMES.get(code, "unknown") if isinstance(code, int) else "?"
    print(f"Status      : {code} ({name})")
    print(f"Setpoint    : {d.get('SETP')} °C")
    print(f"Power level : {d.get('PWR')}")
    print(f"Room fan    : {d.get('F2L')}")
    temps = {k: d[k] for k in ("T1", "T2", "T3", "T4", "T5") if k in d}
    if temps:
        print("Temperatures: " + ", ".join(f"{k}={v}" for k, v in temps.items()))
    if "PQT" in d:
        print(f"Pellets used: {d['PQT']} kg")


def resolve_host(args) -> str:
    if args.host:
        return args.host
    found = discover()
    if not found:
        sys.exit("No Connection Box found. Try --host <ip>.")
    ip = next(iter(found))
    if len(found) > 1:
        print(f"Several boxes found {list(found)}, using {ip}. Use --host to pick.", file=sys.stderr)
    return ip


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main() -> None:
    p = argparse.ArgumentParser(description="Palazzetti Connection Box tool")
    p.add_argument("--host", help="IP of the box (skips discovery)")
    sub = p.add_subparsers(dest="cmd")  # no command -> discover
    sub.add_parser("discover", help="find boxes on the network")
    sub.add_parser("status", help="show stove status")
    sub.add_parser("on", help="turn stove on")
    sub.add_parser("off", help="turn stove off")
    sp = sub.add_parser("setpoint", help="set target temperature (°C)")
    sp.add_argument("value", type=float)
    pw = sub.add_parser("power", help="set power level 1..5")
    pw.add_argument("value", type=int)
    fn = sub.add_parser("fan", help="set room fan 0..7")
    fn.add_argument("value", type=int)
    rw = sub.add_parser("raw", help='send raw command, e.g. "GET ALLS"')
    rw.add_argument("command")
    # iOS "Smart Punctuation" turns -- into an em/en dash; accept that too.
    sys.argv[1:] = [re.sub(r"^[—–]-?", "--", a) for a in sys.argv[1:]]
    args = p.parse_args()
    if args.cmd is None:
        args.cmd = "discover"

    try:
        if args.cmd == "discover":
            found = {args.host: {}} if args.host else discover()
            if not found:
                sys.exit("No Connection Box found.")
            for ip in found:
                try:
                    info = PalazzettiBox(ip).box_info()
                except PalazzettiError as e:
                    print(f"{ip}: replied to discovery but netdata failed ({e})")
                    continue
                cloud = "ONLINE (cloud reachable)" if info.get("ICONN") else "offline (no internet)"
                print(f"{ip}  label={info.get('LABEL')}  mac={info.get('MAC')}  "
                      f"fw={info.get('SYSTEM')}")
                print(f"{'':{len(ip)}}  wifi={info.get('WPR')} gw={info.get('GATEWAY')}  "
                      f"internet: {cloud}")
            return

        box = PalazzettiBox(resolve_host(args))
        if args.cmd == "status":
            print(f"Box: {box.host}")
            print_status(box.all_status())
        elif args.cmd == "on":
            box.turn_on()
            print("Stove ON command sent")
        elif args.cmd == "off":
            box.turn_off()
            print("Stove OFF command sent")
        elif args.cmd == "setpoint":
            box.set_setpoint(args.value)
            print(f"Setpoint set to {args.value} °C")
        elif args.cmd == "power":
            box.set_power(args.value)
            print(f"Power set to {args.value}")
        elif args.cmd == "fan":
            box.set_room_fan(args.value)
            print(f"Room fan set to {args.value}")
        elif args.cmd == "raw":
            print(json.dumps(box.send(args.command), indent=2, ensure_ascii=False))
    except (PalazzettiError, ValueError) as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
