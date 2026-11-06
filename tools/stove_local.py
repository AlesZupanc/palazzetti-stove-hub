#!/usr/bin/env python3
r"""
stove_local.py - answer "stove.local" on this computer for a hub that runs in Docker.

Docker Desktop (Windows, macOS) does not pass multicast between the containers and the
home network, so the hub's own mDNS responder cannot be heard from inside a container.
This runs the very same responder (stovemaster/mdns.py) on the computer instead: it
points <name>.local at this computer's address and announces the port the hub is
published on (compose.yaml: HUB_PORT, 8080 by default) -> http://stove.local:8080/

  python3 tools/stove_local.py                 # stove.local -> this computer, port 8080
  python3 tools/stove_local.py --port 80       # when the hub is published on port 80
  python3 tools/stove_local.py --log FILE      # for a start without a console (pythonw)

Windows: this computer resolves the name at once; phones and other devices only when
the firewall lets python receive UDP 5353. As administrator:
  netsh advfirewall firewall add rule name="stove.local (mDNS)" dir=in action=allow protocol=UDP localport=5353 program="C:\...\pythonw.exe"
To start it at every logon put a shortcut to  pythonw tools\stove_local.py --log ...
into the Startup folder (Win+R, shell:startup).

Standard library only; needs the stovemaster package of this checkout next to it.
"""

import argparse
import logging
import os
import signal
import sys
import threading
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from stovemaster.mdns import MDNSResponder, default_route_ip  # noqa: E402

log = logging.getLogger("stove-local")


def hub_reachable(port):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=3) as r:
            return r.status == 200
    except OSError:
        return False


def main():
    ap = argparse.ArgumentParser(description="answer <name>.local on this computer for the hub in Docker")
    ap.add_argument("--name", default="stove", help="answer for NAME.local (default: stove)")
    ap.add_argument("--port", type=int, default=8080,
                    help="port the hub is published on (compose.yaml HUB_PORT, default: 8080)")
    ap.add_argument("--ip", help="address to answer with (default: this computer's address towards the LAN)")
    ap.add_argument("--service-name", default="Palazzetti stove", help="name shown in service browsers")
    ap.add_argument("--log", metavar="FILE", help="write the log to FILE (for a start without a console)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()

    handlers = None
    if args.log:
        os.makedirs(os.path.dirname(os.path.abspath(args.log)), exist_ok=True)
        handlers = [logging.FileHandler(args.log, encoding="utf-8")]
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)

    responder = MDNSResponder(args.name, args.port, args.service_name, args.ip)
    try:
        responder.start()
    except OSError as e:
        sys.exit(f"cannot use UDP port 5353: {e}")
    suffix = "" if args.port == 80 else f":{args.port}"
    log.info("%s.local -> %s  (the hub: http://%s.local%s/)", args.name, args.ip or default_route_ip() or "?",
             args.name, suffix)
    if not hub_reachable(args.port):
        log.warning("nothing answers on http://127.0.0.1:%d/api/health - is the hub running (docker compose up -d)?",
                    args.port)

    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        stop.wait(1.0)        # short waits: on Windows a long wait is not interrupted by Ctrl+C
    responder.stop()          # says goodbye on the network
    log.info("stopped")


if __name__ == "__main__":
    main()
