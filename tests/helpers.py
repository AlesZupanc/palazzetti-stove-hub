"""Shared test helpers: in-memory stove, captured mail, temporary database, SMTP sink."""

import atexit
import logging
import os
import shutil
import socketserver
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import palazzetti as plz  # noqa: E402
from stovemaster import config as cfgmod  # noqa: E402
from stovemaster.db import DB  # noqa: E402
from stovemaster.stove import validate_command  # noqa: E402


class FakeStove:
    """Stands in for stovemaster.stove.Stove without any network."""

    FIELD = {"setpoint": "SETP", "power": "PWR", "fan": "F2L"}

    def __init__(self, status=0):
        self.state = {"STATUS": status, "SETP": 20.0, "PWR": 3, "F2L": 7, "T1": 19.0, "PQT": 10}
        self.commands = []
        self.fail = None           # set to an exception instance to make every call fail
        self.host = "fake-box"
        self.fixed_host = "fake-box"
        self.last_error = ""
        self.last_ok = 0.0
        self.on_turns = 6          # STATUS after CMD ON (6 = burning; 0 = the stove ignores ON)
        self.fail_once = set()     # command names that fail once (box timeout)

    def _check(self):
        if self.fail is not None:
            raise self.fail

    def status(self):
        self._check()
        return dict(self.state)

    def read_status(self):
        return self.status()

    def command(self, name, value=None, refresh=True):
        value = validate_command(name, value)
        self._check()
        if name in self.fail_once:
            self.fail_once.discard(name)
            raise box_unreachable()
        self.commands.append((name, value))
        if name == "on" and self.state["STATUS"] in (0, 1):
            self.state["STATUS"] = self.on_turns
        elif name == "off" and self.state["STATUS"] not in (0, 1, 10, 12):
            self.state["STATUS"] = 10
        elif name in self.FIELD:
            self.state[self.FIELD[name]] = value
        return dict(self.state)

    def refresh_later(self, delay=1.0):
        pass


def box_unreachable():
    err = plz.PalazzettiError("fake-box: 'GET ALLS' failed: timed out")
    err.__cause__ = OSError("timed out")
    return err


class FakeMailer:
    def __init__(self, configured=True):
        self.configured = configured
        self.sent = []

    def send(self, to, subject, body):
        self.sent.append((to, subject, body))
        return True

    def start(self):
        pass


logging.disable(logging.CRITICAL)  # keep test output readable

_TEMP = []


@atexit.register
def _cleanup():
    for db in _TEMP:
        try:
            db.close()
        except Exception:
            pass
        shutil.rmtree(os.path.dirname(db.path), ignore_errors=True)


def temp_db():
    folder = tempfile.mkdtemp(prefix="stove-test-")
    db = DB(os.path.join(folder, "stove.db"))
    _TEMP.append(db)
    return db


def make_config(**overrides):
    """Config object with test-friendly values; overrides use section_key=value."""
    values = {("scheduler", "wait_for_time_sync"): "no"}
    for k, v in overrides.items():
        section, key = k.split("_", 1)
        values[(section, key)] = v
    folder = tempfile.mkdtemp(prefix="stove-cfg-")
    path = os.path.join(folder, "stove.ini")
    with open(path, "w", encoding="utf-8") as f:
        f.write(cfgmod.render(values))
    return cfgmod.Config(path)


class SMTPSink:
    """Tiny SMTP server (no TLS, no auth) that keeps the messages it receives."""

    def __init__(self):
        self.messages = []
        sink = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.wfile.write(b"220 sink ESMTP\r\n")
                mail_from, rcpt, data_mode, lines = None, [], False, []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if data_mode:
                        if line == ".":
                            sink.messages.append({"from": mail_from, "to": rcpt, "data": "\n".join(lines)})
                            data_mode, lines, rcpt = False, [], []
                            self.wfile.write(b"250 queued\r\n")
                        else:
                            lines.append(line[1:] if line.startswith("..") else line)
                        continue
                    cmd = line[:4].upper()
                    if cmd in ("EHLO", "HELO"):
                        self.wfile.write(b"250-sink\r\n250 8BITMIME\r\n")
                    elif cmd == "MAIL":
                        mail_from = line.split(":", 1)[1].strip()
                        self.wfile.write(b"250 ok\r\n")
                    elif cmd == "RCPT":
                        rcpt.append(line.split(":", 1)[1].strip())
                        self.wfile.write(b"250 ok\r\n")
                    elif cmd == "DATA":
                        data_mode = True
                        self.wfile.write(b"354 go ahead\r\n")
                    elif cmd == "QUIT":
                        self.wfile.write(b"221 bye\r\n")
                        return
                    else:
                        self.wfile.write(b"250 ok\r\n")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
