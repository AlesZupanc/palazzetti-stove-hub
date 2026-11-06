#!/usr/bin/env python3
"""
stove_cloud.py - the stove website on the VPS (accounts, web app, relay to the hubs).

Needs the stovecloud/ package and the web app (app/) next to it; for push
notifications also the Python "cryptography" package. deploy.sh installs it
as the systemd service "stove-cloud" behind nginx.

Commands:
  serve                           run the website (default)
  init-config --public-url URL    write the config file (keeps other settings)
  setup-mail                      ask for the SMTP server and test it
  setup-mail --host H [--port N --security starttls|ssl|none --user U --from ADDR]
                                  set it without asking (password: $STOVE_SMTP_PASSWORD)
  admin USERNAME EMAIL            create an administrator; prints a link to set the password
  users                           list accounts
  user approve|disable|enable|delete USERNAME
  user role USERNAME admin|user
  reset-link USERNAME             print a password reset link (when e-mail is not set up)
  devices [--forget ID]           list the hubs that have connected (or forget one)
  test-mail ADDRESS               send a test e-mail
  has-admin                       exit code 0 if an administrator can sign in (for deploy.sh)
  admin-emails                    print the administrators' e-mail addresses (for mail-server.sh)

Config file: --config PATH, $STOVE_CLOUD_CONFIG, /etc/stove-cloud/cloud.ini or ./cloud.ini
"""

import argparse
import datetime as dt
import getpass
import logging
import os
import re
import signal
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from stovecloud import __version__  # noqa: E402
from stovecloud import config as cfgmod  # noqa: E402

log = logging.getLogger("stove-cloud")


def _open(cfg):
    from stovecloud.accounts import Accounts
    from stovecloud.db import DB
    db = DB(cfg.db_path)
    return db, Accounts(db, cfg.session_days)


def _parts(cfg, db, accounts):
    from stovecloud.hub import Hub
    from stovecloud.mailer import Mailer
    from stovecloud.push import Pusher
    mailer = Mailer(cfg)
    holder = {}
    pusher = Pusher(db, lambda: holder["server"].push_subject(), cfg.push_hosts)
    hub = Hub(db, accounts, pusher, cfg)
    return mailer, pusher, hub, holder


def serve(cfg, args):
    from stovecloud.web import CloudServer
    if args.port:
        cfg.port = args.port
    db, accounts = _open(cfg)
    db.cleanup()
    mailer, pusher, hub, holder = _parts(cfg, db, accounts)
    try:
        server = CloudServer(cfg, db, accounts, mailer, pusher, hub)
    except OSError as e:
        sys.exit(f"Cannot listen on {cfg.bind}:{cfg.port}: {e}")
    holder["server"] = server
    if mailer.configured:
        mailer.start()
    if not accounts.admins():
        log.warning("no administrator yet - create one with: stove_cloud.py admin USERNAME EMAIL")

    def stop(signum, frame):
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, stop)
    log.info("stove website %s on http://%s:%d%s/ (public: %s, push %s)", __version__, cfg.bind, cfg.port,
             cfg.prefix, cfg.public_url, "on" if pusher.available else "off")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# --------------------------------------------------------------------------
# configuration
# --------------------------------------------------------------------------
def _write_config(path, values):
    text = cfgmod.render(values)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    old = os.umask(0o027)
    try:
        with open(path + ".new", "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(path + ".new", path)
    finally:
        os.umask(old)
    os.chmod(path, 0o640)


def _current(path):
    cp = cfgmod.Config(path if os.path.exists(path) else None).raw
    return {(s, k): cp.get(s, k) for s in cfgmod.DEFAULTS for k in cfgmod.DEFAULTS[s]}


def init_config(path, args):
    values = _current(path)
    url = args.public_url.strip()
    if not re.match(r"^https?://[^/\s]+(/\S*)?$", url):
        sys.exit("--public-url must look like https://example.com/mystove/")
    values[("server", "public_url")] = url.rstrip("/") + "/"
    if args.port:
        values[("server", "port")] = str(args.port)
    if args.db:
        values[("paths", "db")] = args.db
    if args.app:
        values[("paths", "app")] = args.app
    _write_config(path, values)
    print(f"wrote {path}")


def ask(question, default="", secret=False):
    shown = (" [keep current]" if secret else f" [{default}]") if default else ""
    try:
        value = getpass.getpass(f"{question}{shown}: ") if secret else input(f"{question}{shown}: ")
    except EOFError:
        sys.exit("\ninput ended - nothing was saved")
    return value.strip() or default


def setup_mail(path, args):
    values = _current(path)
    if args.host is not None:
        _set_mail(values, args)
    else:
        _ask_mail(values)
    _write_config(path, values)
    print(f"saved {path} - a running website uses it after: sudo systemctl restart stove-cloud")
    cfg = cfgmod.Config(path)
    if cfg.smtp_host and args.test_to:
        try:
            mid = _send_test(cfg, args.test_to)
            print(f"test mail sent to {args.test_to} (Message-ID {mid}) - check the inbox (and the spam folder)")
        except Exception as e:  # show any SMTP/TLS/login problem plainly
            print(f"test mail FAILED: {e}")


def _set_mail(values, args):
    """setup-mail with options: exactly these settings, nothing asked."""
    sec = args.security or ("none" if args.port == 25 else "ssl" if args.port == 465 else "starttls")
    sender = args.from_addr or args.user or ""
    if args.host and not sender:
        sys.exit("setup-mail: --from is needed (or --user)")
    values[("mail", "smtp_host")] = args.host
    values[("mail", "smtp_port")] = str(args.port or {"ssl": 465, "none": 25}.get(sec, 587))
    values[("mail", "smtp_security")] = sec
    values[("mail", "smtp_user")] = args.user or ""
    values[("mail", "smtp_password")] = os.environ.get("STOVE_SMTP_PASSWORD", "") if args.user else ""
    values[("mail", "from_addr")] = sender


def _ask_mail(values):
    print("E-mail for account requests, approvals and password resets.")
    print("Gmail: smtp.gmail.com, port 587, starttls, your address and an app password")
    print("(Google account > Security > 2-Step Verification > App passwords).")
    host = ask("SMTP server (empty = no e-mail, links go to the log)", values[("mail", "smtp_host")])
    values[("mail", "smtp_host")] = host
    if host:
        gmail = "gmail" in host
        values[("mail", "smtp_port")] = ask("SMTP port", values[("mail", "smtp_port")] or "587")
        while True:
            sec = ask("Security: starttls, ssl or none", values[("mail", "smtp_security")] or "starttls")
            if sec in ("starttls", "ssl", "none"):
                break
        values[("mail", "smtp_security")] = sec
        values[("mail", "smtp_user")] = ask("SMTP user" + (" (your Gmail address)" if gmail else ""),
                                            values[("mail", "smtp_user")])
        values[("mail", "smtp_password")] = ask("SMTP password" + (" (app password)" if gmail else ""),
                                                values[("mail", "smtp_password")], secret=True)
        values[("mail", "from_addr")] = ask("Sender address", values[("mail", "from_addr")] or values[("mail", "smtp_user")])


def _send_test(cfg, to):
    from stovecloud.mailer import Mailer
    return Mailer(cfg).send_now(to, "Stove website test e-mail",
                                "If you can read this, the stove website can send e-mail.\n")


def test_mail(cfg, args):
    try:
        mid = _send_test(cfg, args.address)
    except Exception as e:  # report any SMTP problem plainly
        sys.exit(f"sending failed: {e}")
    print(f"test mail sent to {args.address} (Message-ID {mid})")


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------
def admin_cmd(cfg, args):
    from stovecloud.accounts import AuthError
    from stovecloud.mailer import Mailer
    from stovecloud.web import create_setup_link
    db, accounts = _open(cfg)
    try:
        link = create_setup_link(cfg, accounts, args.username, args.email)
    except AuthError as e:
        sys.exit(e.message)
    u = accounts.find(args.username)       # as stored, which is what the link sets up
    print(f"Administrator {u['username']} <{u['email']}>.")
    print("Open this link to choose the password (valid for 7 days, works once):\n")
    print(f"  {link}\n")
    mailer = Mailer(cfg)
    if mailer.configured and not args.no_mail:
        try:
            mailer.send_now(u["email"], "Stove website: set your password",
                                f"Hello {u['username']},\n\nyou are the administrator of the stove website.\n"
                                f"Choose your password here (valid for 7 days):\n\n{link}\n")
            print(f"(also sent to {u['email']})")
        except Exception as e:  # the link above still works
            print(f"(could not e-mail it: {e})")


def _user(accounts, name):
    u = accounts.find(name)
    if not u:
        sys.exit(f"no user {name!r}")
    return u


def users_cmd(cfg, args):
    db, accounts = _open(cfg)
    users = accounts.list()
    if not users:
        print("no accounts yet - create the administrator with: stove_cloud.py admin USERNAME EMAIL")
    for u in users:
        last = dt.datetime.fromtimestamp(u["last_login"]).strftime("%Y-%m-%d %H:%M") if u["last_login"] else "never"
        stoves = db.one("SELECT COUNT(*) AS n FROM links WHERE user_id = ?", (u["id"],))["n"]
        pw = "" if u["auth_hash"] else " (no password yet)"
        print(f"{u['username']:<20} {u['email']:<32} {u['role']:<6} {u['status']:<9} stoves {stoves}  "
              f"last sign-in {last}{pw}")


def user_cmd(cfg, args):
    from stovecloud.accounts import AuthError
    db, accounts = _open(cfg)
    u = _user(accounts, args.username)
    try:
        if args.action in ("approve", "enable"):
            accounts.set_status(u["id"], "active")
        elif args.action == "disable":
            accounts.set_status(u["id"], "disabled")
        elif args.action == "delete":
            accounts.delete(u["id"])
        elif args.action == "role":
            if args.role not in ("admin", "user"):
                sys.exit("role must be admin or user")
            accounts.set_role(u["id"], args.role)
    except AuthError as e:
        sys.exit(e.message)
    print(f"{u['username']}: {args.action} {args.role or ''}".strip())
    print("(a running website notices at the next request; open connections end when they reconnect)")


def reset_link(cfg, args):
    from stovecloud.accounts import RESET_TTL
    db, accounts = _open(cfg)
    u = _user(accounts, args.username)
    token = accounts.create_token("reset", u["id"], RESET_TTL)
    print(f"Password reset link for {u['username']} (valid for one hour):\n\n  {cfg.public_url}#/reset/{token}\n")
    print("Note: the stoves in that account are encrypted with the old password; they are added again afterwards.")


def has_admin(cfg, args):
    db, accounts = _open(cfg)
    sys.exit(0 if any(a["auth_hash"] for a in accounts.admins()) else 1)


def admin_emails(cfg, args):
    db, accounts = _open(cfg)
    # also the administrator who has not chosen a password yet: the test mail goes to them
    for a in db.all("SELECT email FROM users WHERE role = 'admin' AND status IN ('active', 'pending') ORDER BY id"):
        print(a["email"])


def devices_cmd(cfg, args):
    db, _ = _open(cfg)
    if args.forget:
        _, n = db.run("DELETE FROM devices WHERE id = ?", (args.forget.upper(),))
        sys.exit(f"device {args.forget} forgotten (it registers again when it connects)" if n else f"no device {args.forget}")
    rows = db.all("SELECT * FROM devices ORDER BY last_seen DESC")
    if not rows:
        print("no hub has connected yet")
    for d in rows:
        seen = dt.datetime.fromtimestamp(d["last_seen"]).strftime("%Y-%m-%d %H:%M") if d["last_seen"] else "never"
        names = [r["username"] for r in db.all("SELECT u.username FROM links l JOIN users u ON u.id = l.user_id "
                                               "WHERE l.device_id = ?", (d["id"],))]
        print(f"{d['id']}  last seen {seen} from {d['last_ip'] or '?':<15} accounts: {', '.join(names) or '-'}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="The stove website on the VPS")
    ap.add_argument("--config", help="config file (default: /etc/stove-cloud/cloud.ini or ./cloud.ini)")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--version", action="version", version=f"stove-cloud {__version__}")
    sub = ap.add_subparsers(dest="cmd")
    sp = sub.add_parser("serve", help="run the website (default)")
    sp.add_argument("--port", type=int)
    ic = sub.add_parser("init-config", help="write the config file")
    ic.add_argument("--public-url", required=True)
    ic.add_argument("--port", type=int)
    ic.add_argument("--db")
    ic.add_argument("--app")
    sm = sub.add_parser("setup-mail", help="configure e-mail (asks, unless --host is given)")
    sm.add_argument("--test-to", help="send a test e-mail to this address afterwards")
    sm.add_argument("--host", help="SMTP server (empty: no e-mail); with this option nothing is asked")
    sm.add_argument("--port", type=int)
    sm.add_argument("--security", choices=["starttls", "ssl", "none"])
    sm.add_argument("--user", help="SMTP user (password from $STOVE_SMTP_PASSWORD)")
    sm.add_argument("--from", dest="from_addr", help='sender, e.g. "Stove <stove@example.com>"')
    ad = sub.add_parser("admin", help="create an administrator")
    ad.add_argument("username")
    ad.add_argument("email")
    ad.add_argument("--no-mail", action="store_true", help="only print the link")
    sub.add_parser("users", help="list accounts")
    us = sub.add_parser("user", help="change an account")
    us.add_argument("action", choices=["approve", "disable", "enable", "delete", "role"])
    us.add_argument("username")
    us.add_argument("role", nargs="?")
    rl = sub.add_parser("reset-link", help="print a password reset link")
    rl.add_argument("username")
    dv = sub.add_parser("devices", help="list hubs")
    dv.add_argument("--forget", metavar="ID", help="remove a hub (and its links) so it can register again")
    tm = sub.add_parser("test-mail", help="send a test e-mail")
    tm.add_argument("address")
    sub.add_parser("has-admin", help="exit 0 if an administrator can sign in")
    sub.add_parser("admin-emails", help="print the administrators' e-mail addresses")

    argv = [re.sub(r"^[—–]-?", "--", a) for a in (sys.argv[1:] if argv is None else argv)]
    args = ap.parse_args(argv)
    if args.cmd is None:
        args = ap.parse_args(argv + ["serve"])
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    path = cfgmod.find_config(args.config)
    if args.cmd == "init-config":
        return init_config(path, args)
    if args.cmd == "setup-mail":
        return setup_mail(path, args)
    try:
        cfg = cfgmod.Config(path if os.path.exists(path) or args.config else None)
    except (FileNotFoundError, ValueError) as e:
        sys.exit(str(e))
    handlers = {"serve": serve, "admin": admin_cmd, "users": users_cmd, "user": user_cmd, "reset-link": reset_link,
                "devices": devices_cmd, "test-mail": test_mail, "has-admin": has_admin, "admin-emails": admin_emails}
    return handlers[args.cmd](cfg, args)


if __name__ == "__main__":
    main()
