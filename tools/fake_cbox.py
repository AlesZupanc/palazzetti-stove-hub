#!/usr/bin/env python3
"""
fake_cbox.py - Palazzetti Connection Box simulator for development and tests.

Speaks the same HTTP API as the real box (cgi-bin/sendmsg.lua and
cgi-bin/syscmd.lua) and can answer UDP discovery ("plzbridge?" on port 54549).
The simulated stove goes through ignition and cool-down phases.

  python3 tools/fake_cbox.py --port 8081
  python3 stove_master.py --config dev.ini serve --box 127.0.0.1:8081 --port 8080 --no-mdns
  python3 palazzetti.py --host 127.0.0.1:8081 status

Test helpers:  GET /_fake/state      internal state as JSON
               POST /_fake/state     {"STATUS": 253} etc. to force values
"""

import argparse
import json
import random
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ON_SEQUENCE = [(2, 3.0), (3, 5.0), (4, 5.0), (5, 3.0), (6, None)]   # (STATUS, seconds)
OFF_SEQUENCE = [(10, 4.0), (12, 8.0), (0, None)]


class FakeStove:
    def __init__(self, speed=1.0, latency=0.0, fail_rate=0.0):
        self.speed = speed
        self.latency = latency
        self.fail_rate = fail_rate
        self.lock = threading.Lock()
        self.state = {"STATUS": 0, "LSTATUS": 0, "SETP": 21.0, "PWR": 3, "F2L": 7, "F2LF": 0,
                      "T1": 19.5, "T2": 20.1, "T3": 35.0, "T4": 0.0, "T5": 19.8, "PQT": 123}
        self.plan = []
        self.commands = []
        self.busy = threading.Lock()  # the real box serves one command at a time

    def _schedule(self, sequence):
        t = time.monotonic()
        self.plan = []
        for status, seconds in sequence:
            self.plan.append((t, status))
            if seconds is not None:
                t += seconds / self.speed

    def _advance(self):
        now = time.monotonic()
        while self.plan and now >= self.plan[0][0]:
            self.state["STATUS"] = self.state["LSTATUS"] = self.plan.pop(0)[1]
        burning = self.state["STATUS"] in (6, 7, 51)
        target = self.state["SETP"] + 0.5 if burning else 17.0
        self.state["T1"] = round(self.state["T1"] + (target - self.state["T1"]) * 0.02, 1)
        self.state["T3"] = 180.0 if burning else 35.0

    def command(self, cmd):
        """Return (success, rsp, data) like the box."""
        with self.busy:
            if self.latency:
                time.sleep(self.latency)
            with self.lock:
                self.commands.append(cmd)
                self._advance()
                if self.fail_rate and random.random() < self.fail_rate:
                    return False, "TIMEOUT", {}
                parts = cmd.split()
                try:
                    return self._dispatch(cmd, parts)
                except (ValueError, IndexError):
                    return False, "ERROR", {}

    def _dispatch(self, cmd, parts):
        if cmd == "GET ALLS":
            pass
        elif cmd == "GET STDT":
            return True, "OK", {"LABEL": "fake-stove", "MAC": "AA:BB:CC:00:11:22", "SYSTEM": "fake 1.0",
                                "STOVETYPE": 1, "MAINTPROBE": 0}
        elif cmd == "CMD ON":
            if self.state["STATUS"] in (0, 1):
                self._schedule(ON_SEQUENCE)
                self._advance()
        elif cmd == "CMD OFF":
            if self.state["STATUS"] not in (0, 1, 10, 12):
                self._schedule(OFF_SEQUENCE)
                self._advance()
        elif parts[:2] == ["SET", "SETP"] and len(parts) == 3:
            self.state["SETP"] = float(parts[2])
        elif parts[:2] == ["SET", "POWR"] and len(parts) == 3 and 1 <= int(parts[2]) <= 5:
            self.state["PWR"] = int(parts[2])
        elif parts[:2] == ["SET", "RFAN"] and len(parts) == 3 and 0 <= int(parts[2]) <= 7:
            self.state["F2L"] = int(parts[2])
        else:
            return False, "ERROR", {}
        return True, "OK", dict(self.state)

    def force(self, values):
        with self.lock:
            self.plan = []
            self.state.update(values)


def make_server(stove, host="127.0.0.1", port=8081):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            q = dict(urllib.parse.parse_qsl(url.query))
            if url.path == "/":
                body = b'<html><head><meta http-equiv="refresh" content="0;url=./cgi-bin/menu.lua"></head></html>'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif url.path == "/cgi-bin/sendmsg.lua":
                cmd = q.get("cmd", "")
                ok, rsp, data = stove.command(cmd)
                self._json({"INFO": {"CMD": cmd, "RSP": rsp, "TS": int(time.time())}, "SUCCESS": ok, "DATA": data})
            elif url.path == "/cgi-bin/syscmd.lua" and q.get("cmd") == "netdata":
                self._json({"INFO": {"CMD": "netdata", "RSP": "OK"}, "SUCCESS": True,
                            "DATA": {"LABEL": "fake-stove", "MAC": "AA:BB:CC:00:11:22", "SYSTEM": "fake 1.0",
                                     "ICONN": 0, "WPR": "wlan", "GATEWAY": "192.168.1.1"}})
            elif url.path == "/_fake/state":
                with stove.lock:
                    stove._advance()
                    self._json({"state": stove.state, "commands": stove.commands[-50:]})
            else:
                self._json({"error": "not found"}, 404)

        def do_POST(self):
            if self.path != "/_fake/state":
                return self._json({"error": "not found"}, 404)
            n = int(self.headers.get("Content-Length") or 0)
            stove.force(json.loads(self.rfile.read(n) or b"{}"))
            self._json({"ok": True})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def udp_discovery(port_http, bind="0.0.0.0"):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind((bind, 54549))
    while True:
        data, addr = s.recvfrom(1024)
        if data.strip() == b"plzbridge?":
            reply = {"INFO": {"CMD": "plzbridge?", "RSP": "OK"}, "SUCCESS": True,
                     "DATA": {"LABEL": "fake-stove", "MAC": "AA:BB:CC:00:11:22", "HTTPPORT": port_http}}
            s.sendto(json.dumps(reply).encode(), addr)


def main():
    ap = argparse.ArgumentParser(description="Palazzetti Connection Box simulator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--speed", type=float, default=1.0, help="speed up ignition/cool-down (e.g. 10)")
    ap.add_argument("--latency", type=float, default=0.3, help="seconds per command (serial link)")
    ap.add_argument("--fail-rate", type=float, default=0.0, help="fraction of commands answered RSP=TIMEOUT")
    ap.add_argument("--udp", action="store_true", help="answer UDP discovery on port 54549")
    args = ap.parse_args()
    stove = FakeStove(args.speed, args.latency, args.fail_rate)
    server = make_server(stove, args.host, args.port)
    if args.udp:
        threading.Thread(target=udp_discovery, args=(args.port,), daemon=True).start()
    print(f"fake Connection Box on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
