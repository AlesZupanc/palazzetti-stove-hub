#!/usr/bin/env python3
"""
stove_master.py - home hub for a Palazzetti stove (made for a Raspberry Pi Zero W).

Needs palazzetti.py, the stovemaster/ package and the web app (www/app) next to
it; for remote access also the Python "cryptography" package.

Commands:
  serve                         local dashboard (http://stove.local, no login,
                                home network only), weekly scheduler, mDNS
                                responder and the connection to the VPS
  setup                         interactive wizard that writes the config file
  token [--new]                 print the token that adds this stove to an
                                online account (--new: make a new one)
  links [remove ID]             accounts that can reach the stove online
  schedule list | export [FILE|-] | import FILE|- [--replace]
  status                        print the stove status

Config file: --config PATH, $STOVE_MASTER_CONFIG, /etc/stove-master/stove.ini or ./stove.ini
On the Pi use the wrapper "sudo stove-master <command>" (installed by pi/install.sh);
it runs as the service user so the database keeps the right owner.
"""

import argparse
import json
import logging
import os
import re
import signal
import sys
import threading
import time

import palazzetti as plz
from stovemaster import __version__
from stovemaster import config as cfgmod
from stovemaster.db import DB
from stovemaster.scheduler import describe, entry_public, validate_entry

log = logging.getLogger("stove-master")


# --------------------------------------------------------------------------
# serve
# --------------------------------------------------------------------------
def serve(cfg, args):
    from stovemaster.api import Hub
    from stovemaster.lan import LanServer
    from stovemaster.scheduler import Scheduler
    from stovemaster.stove import Stove

    port = args.port or cfg.port
    cfg.port = port
    db = DB(cfg.db_path)
    stove = Stove(args.box or cfg.box_host or None, timeout=cfg.box_timeout)
    scheduler = Scheduler(db, stove, cfg)

    mdns = None
    if cfg.mdns_enabled and not args.no_mdns:
        mdns = mdns_responder(cfg)
        try:
            mdns.start()
        except OSError as e:
            log.error("mDNS responder disabled, cannot use UDP port 5353: %s", e)
            mdns = None

    hub = Hub(cfg, db, stove, scheduler, mdns=mdns)
    cloud = None
    if cfg.cloud_url and not args.no_cloud:
        from stovemaster.cloud import CloudLink
        try:
            cloud = CloudLink(hub, cfg, db)
        except RuntimeError as e:          # cryptography package missing
            log.error("remote access disabled: %s", e)

    try:
        server = LanServer(hub, cfg)
    except PermissionError:
        sys.exit(f"Cannot listen on port {port}: ports below 1024 need root or CAP_NET_BIND_SERVICE "
                 f"(the systemd unit grants it). For a test run use --port 8080.")
    except OSError as e:
        sys.exit(f"Cannot listen on {cfg.bind}:{port}: {e}")

    scheduler.start()
    hub.start()
    if cloud:
        cloud.start()

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    db.log_event("system", "system", "start", f"stove master {__version__} started")
    host = f"{cfg.mdns_name}.local" if mdns else (plz.local_ip() if _has_route() else "localhost")
    public = cfg.public_port
    log.info("stove master %s - dashboard on http://%s%s/%s  (box: %s)%s", __version__, host,
             "" if public == 80 else f":{public}",
             f" through a proxy, the hub listens on {cfg.bind}:{port}" if public != port else "",
             stove.host or "auto-discovery", f"  online: {cfg.cloud_url}" if cloud else "")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("shutting down")
        scheduler.stop()
        hub.stop()
        if cloud:
            cloud.stop()
        if mdns:
            mdns.stop()
        server.server_close()


def mdns_responder(cfg):
    """stove.local and its DNS-SD record, which names the port browsers use (the proxy's, when one is in front)."""
    from stovemaster.mdns import MDNSResponder
    return MDNSResponder(cfg.mdns_name, cfg.public_port, cfg.mdns_service, cfg.mdns_ip or None)


def _has_route():
    try:
        plz.local_ip()
        return True
    except OSError:
        return False


# --------------------------------------------------------------------------
# setup wizard
# --------------------------------------------------------------------------
def ask(question, default="", validate=None):
    while True:
        shown = f" [{default}]" if default else ""
        try:
            value = input(f"{question}{shown}: ")
        except EOFError:
            sys.exit("\ninput ended - nothing was saved")
        value = value.strip() or default
        if validate:
            error = validate(value)
            if error:
                print(f"  {error}")
                continue
        return value


def _url_ok(value):
    if value and not re.match(r"^https?://[^/\s]+(/\S*)?$", value):
        return "enter a URL like https://example.com/mystove/ (or leave empty)"
    return None


def setup(path, args):
    exists = os.path.exists(path)
    cur = cfgmod.Config(path if exists else None)
    raw = cur.raw
    print(f"\nStove master setup - writing {path}\n" + "-" * 60)
    values = {}

    print("\n1) Palazzetti Connection Box")
    print("   Looking for it on the network (UDP broadcast)...")
    try:
        found = plz.discover_udp()
    except OSError:
        found = {}
    for ip, info in found.items():
        label = info.get("LABEL") or info.get("label") or ""
        print(f"   found {ip} {label}")
    if not found:
        print("   none answered. Enter its IP (see your router's DHCP list), or leave it empty and the")
        print("   hub keeps searching (it also scans the whole /24 network) every time it starts.")
    default_box = raw.get("stove", "box_host") or (next(iter(found)) if found else "")
    values[("stove", "box_host")] = ask("   Box IP (empty = discover it at every start)", default_box)

    print("\n2) Home network")
    values[("mdns", "name")] = ask("   Local name -> http://NAME.local", cur.mdns_name,
                                   lambda v: None if re.match(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$", v)
                                   else "use lowercase letters, digits and -")
    if cur.trust_proxy:      # a reverse proxy in front of the hub ([web] trust_proxy, set by hand)
        print(f"   Web port: {cur.public_port}, through the proxy (the hub itself listens on {cur.bind}:{cur.port};")
        print(f"   change [web] in {path} for another one)")
    else:
        values[("web", "port")] = ask("   Web port (80 = no :port in the address)", str(cur.port),
                                      lambda v: None if v.isdigit() and 0 < int(v) < 65536 else "enter a port number")

    print("\n3) Remote access through your website on the VPS (www/deploy.sh installs it)")
    print("   The hub keeps a WebSocket open to it. Afterwards add the stove to your online")
    print("   account with the token shown on the local dashboard (Online).")
    values[("cloud", "url")] = ask("   Website address, e.g. https://example.com/mystove/ (empty = home only)",
                                   raw.get("cloud", "url"), _url_ok)

    # keep every other setting as it is
    for section in cfgmod.DEFAULTS:
        for key in cfgmod.DEFAULTS[section]:
            values.setdefault((section, key), raw.get(section, key))

    text = cfgmod.render(values)
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    old_umask = os.umask(0o027)
    try:
        with open(path + ".new", "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(path + ".new", path)
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o640)
    if os.geteuid() == 0:
        try:
            import grp
            os.chown(path, 0, grp.getgrnam("stove").gr_gid)
        except (KeyError, ImportError):
            pass
    print(f"\nSaved {path}.")
    if os.environ.get("STOVE_INSTALLER"):          # pi/install.sh runs it, and restarts the service itself
        return
    print("\nRestart the service to use it:  sudo systemctl restart stove-master\n")


# --------------------------------------------------------------------------
# token and linked accounts
# --------------------------------------------------------------------------
def _cloud(cfg):
    from stovemaster.cloud import CloudLink

    class Offline:   # just enough of a hub for reading and editing the links
        device_name = None

        def broadcast(self, *a, **k):
            pass

    if not cfg.cloud_url:
        sys.exit("Remote access is not set up: add the website address under [cloud] url "
                 "(sudo stove-master setup).")
    db = DB(cfg.db_path)
    hub = Offline()
    hub.device_name = db.get_setting("device_name") or "Stove"
    try:
        return db, CloudLink(hub, cfg, db)
    except RuntimeError as e:
        sys.exit(str(e))


def token_cmd(cfg, args):
    db, cloud = _cloud(cfg)
    if args.new:
        cloud.regenerate()
        db.log_event("cli", "cli", "new token", "The old token can no longer add the stove to an account")
        print("A new token was made. The old one no longer works; accounts added before keep working.\n")
    print(f"Token:  {cloud.token}")
    print(f"Or open this link and sign in:  {cloud.info(lan=True)['add_url']}")
    print("(The running service picks up a new token at once and ends pairing sessions opened with the old one"
          " within half a minute.)" if args.new else "")


def api_token_cmd(cfg, args):
    from stovemaster import urlapi
    db = DB(cfg.db_path)
    if args.new:
        urlapi.new_api_token(db)
        db.log_event("cli", "cli", "new API token", "Links with the old token stop working")
        print("A new secret token was made; links with the old one stop working.\n")
    token = urlapi.api_token(db)
    base = f"http://{cfg.mdns_name}.local" + ("" if cfg.public_port == 80 else f":{cfg.public_port}")
    print(f"Secret token: {token}\n")
    print(f"  {base}/api/control?token={token}&state=on")
    print(f"  {base}/api/control?token={token}&state=off")
    print(f"  {base}/api/control?token={token}&temp=22.5")
    print(f"  {base}/api/control?token={token}&level=3&fan=auto")
    print(f"  {base}/api/command?token={token}&text=turn+on+and+set+the+temperature+to+23")
    print(f"  {base}/api/state                 (no token needed)")
    print("\nAdd &format=text for a sentence instead of JSON (e.g. for Siri to speak).")


def links_cmd(cfg, args):
    db, cloud = _cloud(cfg)
    if args.action == "remove":
        if args.id not in cloud.links:
            sys.exit(f"no link {args.id!r} (see: stove-master links)")
        link = cloud.links[args.id]
        cloud.unlink(args.id, reason="removed on the command line")
        db.log_event("cli", "cli", "account removed", f"{link['profile']} can no longer reach the stove")
        print(f"{link['profile']} can no longer reach the stove (the running service ends its open sessions"
              f" and tells the website within half a minute)")
        return
    if not cloud.links:
        print("No account has added this stove yet. Token: " + cloud.token)
    for link in cloud.links.values():
        seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(link["last_seen"])) if link["last_seen"] else "never"
        added = time.strftime("%Y-%m-%d", time.localtime(link["created_at"]))
        print(f"{link['id']}  {link['profile']:<24} added {added}  last used {seen}")


# --------------------------------------------------------------------------
# schedule
# --------------------------------------------------------------------------
def schedule_cmd(cfg, args):
    db = DB(cfg.db_path)
    if args.schedule_cmd == "list":
        entries = db.schedule_entries()
        print(f"automatic schedule: {'ON' if db.get_setting('scheduler_enabled', True) else 'PAUSED'}")
        if not entries:
            print("no slots")
        for e in entries:
            pub = entry_public(e)
            days = ",".join(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[d] for d in pub["days"])
            print(f"#{e['id']:<3} {e['start_time']}-{e['end_time']} {days:<28} {describe(e):<34} "
                  f"{'' if e['enabled'] else '(disabled) '}{e['name']}")
    elif args.schedule_cmd == "export":
        data = [{k: v for k, v in entry_public(e).items() if k not in ("id", "fan_text", "overnight", "updated_by")}
                for e in db.schedule_entries()]
        text = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        if args.file and args.file != "-":
            try:
                with open(args.file, "w", encoding="utf-8") as f:
                    f.write(text)
            except OSError as e:
                sys.exit(f"cannot write {args.file}: {e.strerror} (the command runs as the service user; "
                         f"use: sudo stove-master schedule export > {args.file})")
        else:
            sys.stdout.write(text)
    elif args.schedule_cmd == "import":
        try:
            if args.file == "-":
                data = json.load(sys.stdin)
            else:
                with open(args.file, encoding="utf-8") as f:
                    data = json.load(f)
        except OSError as e:
            sys.exit(f"cannot read {args.file}: {e.strerror} (the command runs as the service user; "
                     f"use: sudo stove-master schedule import - < {args.file})")
        except ValueError as e:
            sys.exit(f"{args.file} is not valid JSON: {e}")
        if not isinstance(data, list):
            sys.exit("expected a JSON list of slots")
        try:
            entries = [validate_entry(d) for d in data]
        except ValueError as e:
            sys.exit(f"invalid slot: {e}")
        with db.transaction():  # all or nothing; the scheduler adopts unchanged running slots
            if args.replace:
                for e in db.schedule_entries():
                    db.schedule_delete(e["id"])
            for e in entries:
                db.schedule_save(e, by="cli")
        db.log_event("cli", "cli", "schedule imported", f"{len(entries)} slots")
        print(f"imported {len(entries)} slots (the running service picks them up within 20 s)")


# --------------------------------------------------------------------------
# misc
# --------------------------------------------------------------------------
def status(cfg, args):
    host = args.box or cfg.box_host
    if not host:
        found = plz.discover()
        if not found:
            sys.exit("No Connection Box found. Set [stove] box_host.")
        host = next(iter(found))
    box = plz.PalazzettiBox(host, timeout=cfg.box_timeout)
    try:
        print(f"Box: {host}")
        plz.print_status(box.all_status())
    except plz.PalazzettiError as e:
        sys.exit(f"Error: {e}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Palazzetti stove master (Raspberry Pi hub)")
    ap.add_argument("--config", help="config file (default: /etc/stove-master/stove.ini or ./stove.ini)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    ap.add_argument("--version", action="version", version=f"stove-master {__version__}")
    sub = ap.add_subparsers(dest="cmd")

    sp = sub.add_parser("serve", help="run the hub (default)")
    sp.add_argument("--port", type=int, help="override [web] port")
    sp.add_argument("--box", help="override [stove] box_host")
    sp.add_argument("--no-mdns", action="store_true", help="do not answer for NAME.local")
    sp.add_argument("--no-cloud", action="store_true", help="do not connect to the VPS")

    sub.add_parser("setup", help="interactive configuration wizard")
    tk = sub.add_parser("token", help="token that adds the stove to an online account")
    tk.add_argument("--new", action="store_true", help="make a new token (the old one stops working)")
    lk = sub.add_parser("links", help="accounts that can reach the stove online")
    lk.add_argument("action", nargs="?", choices=["list", "remove"], default="list")
    lk.add_argument("id", nargs="?", help="link id (for remove)")

    scp = sub.add_parser("schedule", help="list / export / import the weekly schedule")
    ss = scp.add_subparsers(dest="schedule_cmd", required=True)
    ss.add_parser("list")
    se = ss.add_parser("export")
    se.add_argument("file", nargs="?", help="default/-: standard output")
    si = ss.add_parser("import")
    si.add_argument("file", help="JSON file, or - for standard input")
    si.add_argument("--replace", action="store_true", help="delete the current slots first")

    ak = sub.add_parser("api-token", aliases=["api-key"],
                        help="secret token of the local API: /api/control?token=...")
    ak.add_argument("--new", action="store_true", help="make a new token (links with the old one stop working)")

    st = sub.add_parser("status", help="print the stove status")
    st.add_argument("--box", help="Connection Box IP")

    # iOS "Smart Punctuation" turns -- into an em/en dash; accept that too.
    argv = [re.sub(r"^[—–]-?", "--", a) for a in (sys.argv[1:] if argv is None else argv)]
    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args(argv + ["serve"])

    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s" if sys.stderr.isatty() else "%(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format=fmt)

    path = cfgmod.find_config(args.config)
    if args.cmd == "setup":
        return setup(path, args)
    try:
        cfg = cfgmod.Config(path if os.path.exists(path) or args.config else None)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))
    if not os.path.exists(path):
        log.info("no config file %s - using defaults", path)
    if args.cmd == "links" and args.action == "remove" and not args.id:
        ap.error("links remove needs the link id")
    handlers = {"serve": serve, "token": token_cmd, "links": links_cmd, "schedule": schedule_cmd, "status": status,
                "api-token": api_token_cmd, "api-key": api_token_cmd}
    return handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
