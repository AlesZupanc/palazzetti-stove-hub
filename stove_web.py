#!/usr/bin/env python3
"""
stove_web.py - small web app to control a Palazzetti stove via its Connection Box.

Needs palazzetti.py in the same folder. Standard library only.

Run:
  python stove_web.py                      # discover the box, serve on port 8080
  python stove_web.py --host 192.168.1.50  # skip discovery
  python stove_web.py --port 8000

Then open http://<this-computer-ip>:8080 on any phone/PC on your network.
(On the same device: http://localhost:8080)
"""

import argparse
import json
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import palazzetti as plz

STATUS_TTL = 3.0  # seconds a status read is reused, keeps load off the stove's serial link


class Stove:
    """Thread-safe wrapper: the box handles one stove command at a time.

    Writes are sent immediately and answered right away with an optimistic
    status; the real status is re-read in the background afterwards.
    """

    # action -> field in GET ALLS that it changes
    FIELD = {"setpoint": "SETP", "power": "PWR", "fan": "F2L"}

    def __init__(self, host: str):
        self.box = plz.PalazzettiBox(host, timeout=15)
        self.lock = threading.Lock()
        self._cache = None
        self._cache_t = 0.0

    def _timed(self, label, fn, *a):
        t = time.monotonic()
        try:
            return fn(*a)
        finally:
            print(f"  stove {label:<14} {time.monotonic() - t:5.1f} s", file=sys.stderr)

    def _refresh(self) -> dict:
        self._cache = self._timed("GET ALLS", self.box.all_status)
        self._cache_t = time.monotonic()
        return self._cache

    def status(self) -> dict:
        fresh = self._cache is not None and time.monotonic() - self._cache_t < STATUS_TTL
        if fresh:
            return self._cache
        # A command is in progress: don't queue a poll behind it, reuse what we have.
        if not self.lock.acquire(blocking=self._cache is None):
            return self._cache
        try:
            return self._refresh()
        finally:
            self.lock.release()

    def action(self, name: str, value=None) -> dict:
        with self.lock:
            if name == "on":
                self._timed("CMD ON", self.box.turn_on)
            elif name == "off":
                self._timed("CMD OFF", self.box.turn_off)
            elif name == "setpoint":
                self._timed(f"SET SETP {value}", self.box.set_setpoint, float(value))
            elif name == "power":
                self._timed(f"SET POWR {value}", self.box.set_power, int(value))
            elif name == "fan":
                self._timed(f"SET RFAN {value}", self.box.set_room_fan, int(value))
            else:
                raise ValueError(f"unknown action {name!r}")
            # Optimistic update so the page reacts instantly ...
            optimistic = dict(self._cache or {})
            if name in self.FIELD:
                optimistic[self.FIELD[name]] = float(value) if name == "setpoint" else int(value)
            self._cache, self._cache_t = optimistic, time.monotonic()
        # ... and confirm the real values in the background.
        threading.Thread(target=self._background_refresh, daemon=True).start()
        return optimistic

    def _background_refresh(self):
        time.sleep(1.0)  # give the stove a moment to apply the change
        with self.lock:
            try:
                self._refresh()
            except plz.PalazzettiError as e:
                print(f"  background refresh failed: {e}", file=sys.stderr)


def summarize(d: dict) -> dict:
    code = d.get("STATUS")
    return {
        "status": code,
        "status_text": plz.STATUS_NAMES.get(code, f"code {code}"),
        "on": code not in (0, 1, None),
        "setpoint": d.get("SETP"),
        "power": d.get("PWR"),
        "fan": d.get("F2L"),
        "temps": {k: d[k] for k in ("T1", "T2", "T3", "T4", "T5") if k in d},
        "pellets_kg": d.get("PQT"),
    }


PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Stove</title>
<style>
:root{--bg:#f6f4f1;--card:#fff;--fg:#1d1b19;--muted:#7a746d;--accent:#d2452c;--ok:#2f8f4e;--line:#e7e2dc}
@media (prefers-color-scheme:dark){:root{--bg:#161412;--card:#221f1c;--fg:#f2eee9;--muted:#9a938b;--line:#35302b}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.4 -apple-system,system-ui,sans-serif}
main{max-width:460px;margin:0 auto;padding:16px}
h1{font-size:20px;margin:4px 0 16px;display:flex;justify-content:space-between;align-items:center}
#dot{width:10px;height:10px;border-radius:50%;background:var(--muted);display:inline-block}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:12px}
.label{color:var(--muted);font-size:13px;text-transform:uppercase;letter-spacing:.04em}
#state{font-size:26px;font-weight:600;margin:4px 0 12px}
.row{display:flex;gap:8px}
button{flex:1;font:inherit;font-weight:600;padding:14px 0;border-radius:10px;border:1px solid var(--line);
  background:var(--bg);color:var(--fg);cursor:pointer}
button:active{transform:scale(.97)}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
button.sel{outline:2px solid var(--accent);outline-offset:-2px}
button:disabled{opacity:.5}
.big{font-size:40px;font-weight:600;text-align:center;flex:1.4;align-self:center}
.temps{display:grid;grid-template-columns:repeat(auto-fill,minmax(80px,1fr));gap:8px;margin-top:8px}
.temps div{background:var(--bg);border-radius:8px;padding:8px;text-align:center}
.temps b{display:block;font-size:18px}
#msg{min-height:20px;color:var(--accent);font-size:14px;text-align:center}
</style></head><body><main>
<h1>Stove <span id="dot" title="connection"></span></h1>

<div class="card">
  <div class="label">Status</div>
  <div id="state">…</div>
  <div class="row">
    <button class="primary" onclick="act('on')">Turn on</button>
    <button onclick="act('off')">Turn off</button>
  </div>
</div>

<div class="card">
  <div class="label">Target temperature</div>
  <div class="row" style="margin-top:8px">
    <button onclick="setp(-0.5)">−</button>
    <div class="big"><span id="setp">–</span>°</div>
    <button onclick="setp(0.5)">+</button>
  </div>
</div>

<div class="card">
  <div class="label">Power</div>
  <div class="row" id="power" style="margin-top:8px"></div>
</div>

<div class="card">
  <div class="label">Room fan</div>
  <div class="row" id="fan" style="margin-top:8px"></div>
</div>

<div class="card">
  <div class="label">Temperatures</div>
  <div class="temps" id="temps"></div>
  <div class="label" style="margin-top:12px">Pellets used: <span id="pqt">–</span> kg</div>
</div>

<div id="msg"></div>
</main>
<script>
let S = null, pending = null;
const $ = id => document.getElementById(id);

function buttons(el, items, current, action) {
  el.innerHTML = "";
  for (const [v, label] of items) {
    const b = document.createElement("button");
    b.textContent = label;
    if (v === current) b.className = "sel";
    b.onclick = () => act(action, v);
    el.appendChild(b);
  }
}

function render() {
  if (!S) return;
  $("state").textContent = S.status_text;
  $("state").style.color = S.on ? "var(--accent)" : "var(--fg)";
  if (pending === null) $("setp").textContent = S.setpoint ?? "–";
  buttons($("power"), [1,2,3,4,5].map(v => [v, String(v)]), S.power, "power");
  buttons($("fan"), [[0,"Off"],[1,"1"],[3,"3"],[5,"5"],[6,"Hi"],[7,"Auto"]], S.fan, "fan");
  $("temps").innerHTML = Object.entries(S.temps)
    .map(([k, v]) => `<div>${k}<b>${v}°</b></div>`).join("") || "–";
  $("pqt").textContent = S.pellets_kg ?? "–";
}

async function load() {
  try {
    const r = await fetch("/api/status");
    const j = await r.json();
    if (!r.ok) throw new Error(j.error);
    S = j; render();
    $("dot").style.background = "var(--ok)"; $("msg").textContent = "";
  } catch (e) {
    $("dot").style.background = "var(--accent)"; $("msg").textContent = e.message;
  }
}

async function act(action, value) {
  if ((action === "on" || action === "off") && !confirm(`Turn the stove ${action}?`)) return;
  document.querySelectorAll("button").forEach(b => b.disabled = true);
  $("msg").textContent = "Sending…";
  try {
    const r = await fetch("/api/action", {method: "POST",
      headers: {"Content-Type": "application/json"}, body: JSON.stringify({action, value})});
    const j = await r.json();
    if (!r.ok) throw new Error(j.error);
    S = j; render(); $("msg").style.color = "var(--ok)";
    $("msg").textContent = `✓ Stove accepted it (${j.took} s)`;
    setTimeout(() => { $("msg").textContent = ""; $("msg").style.color = ""; load(); }, 2500);
  } catch (e) { $("msg").textContent = e.message; }
  document.querySelectorAll("button").forEach(b => b.disabled = false);
}

// setpoint: change locally, send once the user stops tapping
let timer = null;
function setp(delta) {
  pending = Math.round(((pending ?? S?.setpoint ?? 20) + delta) * 2) / 2;
  $("setp").textContent = pending;
  clearTimeout(timer);
  timer = setTimeout(async () => { const v = pending; pending = null; await act("setpoint", v); }, 600);
}

load();
setInterval(() => { if (pending === null) load(); }, 10000);
</script></body></html>
"""


def make_handler(stove: Stove):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, code: int, obj) -> None:
            body = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                body = PAGE.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/api/status":
                try:
                    self._json(200, summarize(stove.status()))
                except plz.PalazzettiError as e:
                    self._json(502, {"error": str(e)})
            else:
                self._json(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/api/action":
                return self._json(404, {"error": "not found"})
            try:
                n = int(self.headers.get("Content-Length", 0))
                req = json.loads(self.rfile.read(n) or b"{}")
                t = time.monotonic()
                state = stove.action(req.get("action"), req.get("value"))
                out = summarize(state)
                out["took"] = round(time.monotonic() - t, 1)
                self._json(200, out)
            except (ValueError, TypeError) as e:
                self._json(400, {"error": str(e)})
            except plz.PalazzettiError as e:
                self._json(502, {"error": str(e)})

        def log_message(self, fmt, *args):  # quieter console
            if not self.path.startswith("/api/status"):
                sys.stderr.write("%s  %s\n" % (self.address_string(), fmt % args))

    return Handler


def main():
    ap = argparse.ArgumentParser(description="Palazzetti stove web app")
    ap.add_argument("--host", help="Connection Box IP (skips discovery)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--bind", default="0.0.0.0", help="listen address (default: all)")
    sys.argv[1:] = [a.replace("—", "--").replace("–", "--") if a[:1] in "—–" else a
                    for a in sys.argv[1:]]
    args = ap.parse_args()

    host = args.host
    if not host:
        found = plz.discover()
        if not found:
            sys.exit("No Connection Box found. Use --host <ip>.")
        host = next(iter(found))
    print(f"Connection Box: {host}")

    server = ThreadingHTTPServer((args.bind, args.port), make_handler(Stove(host)))
    try:
        me = plz.local_ip()
    except OSError:
        me = socket.gethostname()
    print(f"Open http://{me}:{args.port}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
