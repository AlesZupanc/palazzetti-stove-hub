"""The local API: /api/state and /api/control?token=... (Siri Shortcuts, scripts)."""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from unittest import mock

from helpers import FakeStove, make_config, temp_db

from stovemaster import lan, urlapi
from stovemaster.api import Client, Hub
from stovemaster.scheduler import Scheduler


class Parsing(unittest.TestCase):
    def test_commands_in_a_safe_order(self):
        self.assertEqual(urlapi.parse_commands({"state": "on", "temp": "23,5", "fan": "auto", "level": "4"}),
                         [("setpoint", "23.5"), ("power", "4"), ("fan", 7), ("on", None)])
        self.assertEqual(urlapi.parse_commands({"power": "OFF"}), [("off", None)])
        self.assertEqual(urlapi.parse_commands({"power": "2"}), [("power", "2")])
        self.assertEqual(urlapi.parse_commands({"setpoint": "21", "key": "x", "format": "text"}), [("setpoint", "21")])

    def test_mistakes_are_explained(self):
        for params in ({}, {"key": "x"}, {"state": "maybe"}, {"tmp": "22"}):
            with self.assertRaises(urlapi.APIError):
                urlapi.parse_commands(params)

    def test_sentences(self):
        probes = {"room": "T1"}
        self.assertEqual(urlapi.describe_state({"phase": "off", "temps": {"T1": 19.5}, "setpoint": 21.0}, probes),
                         "The stove is off. Room 19.5 degrees, target 21.")
        self.assertIn("level 3, fan auto", urlapi.describe_state(
            {"phase": "running", "status_text": "BURNING", "power": 3, "fan": 7, "temps": {}}, probes, "Kamin"))
        self.assertEqual(urlapi.describe_done([("setpoint", "22.5"), ("on", None)]),
                         "OK, target 22.5 degrees, switching on.")


class Server(unittest.TestCase):
    LOCAL = "Lt0kenOfTheLocalApi_0123456789ab"

    @classmethod
    def setUpClass(cls):
        cls.db = temp_db()
        cls.token_file = os.path.join(tempfile.mkdtemp(prefix="stove-token-"), "local-api-token")
        with open(cls.token_file, "w") as f:
            f.write(cls.LOCAL + "\n")
        cfg = make_config(web_bind="127.0.0.1", web_port="0", mdns_name="stove", web_local_token_file=cls.token_file)
        cls.stove = FakeStove()
        cls.hub = Hub(cfg, cls.db, cls.stove, Scheduler(cls.db, cls.stove, cfg))
        cls.server = lan.LanServer(cls.hub, cfg)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.key = urlapi.api_token(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def call(self, path, data=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as r:
                return r.status, r.read().decode(), r.headers
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers

    def test_state_needs_no_key(self):
        status, body, h = self.call("/api/state")
        self.assertEqual(status, 200)
        d = json.loads(body)
        self.assertEqual((d["ok"], d["state"]["setpoint"]), (True, self.stove.state["SETP"]))
        self.assertIn("degrees", d["say"])
        status, body, h = self.call("/api/state?format=text")
        self.assertTrue(h["Content-Type"].startswith("text/plain"))
        self.assertTrue(body.startswith("Stove is "), body)

    def test_control_with_the_key(self):
        self.stove.commands.clear()
        status, body, _ = self.call(f"/api/control?token={self.key}&state=on&temp=23.5&fan=3&format=text")
        self.assertEqual((status, body), (200, "OK, target 23.5 degrees, fan 3, switching on."))
        self.assertEqual(self.stove.commands, [("setpoint", 23.5), ("fan", 3), ("on", None)])
        ev = self.db.events(1)[0]
        self.assertEqual((ev["via"], ev["action"]), ("api", "on"))
        self.assertIn("API", ev["actor"])
        # POST with the key in a header works too (e.g. Home Assistant)
        status, body, _ = self.call("/api/control", data=b"state=off", headers={"X-Stove-Token": self.key})
        self.assertEqual(status, 200, body)
        self.assertEqual(json.loads(body)["done"], ["off"])
        status, body, _ = self.call("/api/control", data=json.dumps({"temp": 21}).encode(),
                                    headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        self.assertEqual(json.loads(body)["state"]["setpoint"], 21.0)

    def test_a_sentence(self):
        self.stove.commands.clear()
        text = urllib.parse.quote("Turn on the stove and set the temperature to 23 degrees.")
        status, body, _ = self.call(f"/api/command?token={self.key}&format=text&text={text}")
        self.assertEqual((status, body), (200, "OK, target 23 degrees, switching on."))
        self.assertEqual(self.stove.commands, [("setpoint", 23.0), ("on", None)])
        # as the Siri shortcut sends it: POST, JSON body; "warmer" starts from the stove's target
        status, body, _ = self.call(f"/api/command?token={self.key}&format=text",
                                    data=json.dumps({"text": "two degrees warmer, fan auto"}).encode(),
                                    headers={"Content-Type": "application/json"})
        self.assertEqual((status, body), (200, "OK, target 25 degrees, fan auto."))
        # a form, also multipart
        status, body, _ = self.call(f"/api/command?token={self.key}", data=b"text=turn+off")
        self.assertEqual(json.loads(body)["done"], ["off"])
        multipart = (b"--XyZ\r\nContent-Disposition: form-data; name=\"text\"\r\n\r\nhow is the stove\r\n--XyZ--\r\n")
        status, body, _ = self.call(f"/api/command?token={self.key}&format=text", data=multipart,
                                    headers={"Content-Type": "multipart/form-data; boundary=XyZ"})
        self.assertEqual(status, 200)
        self.assertTrue(body.startswith("Stove is "), body)

    def test_a_sentence_not_understood_changes_nothing(self):
        self.stove.commands.clear()
        status, body, _ = self.call(f"/api/command?token={self.key}&format=text&text=turn+on+and+banana")
        self.assertEqual(status, 400)
        self.assertIn("banana", body)
        self.assertEqual(self.stove.commands, [])
        self.assertEqual(self.call("/api/command?text=turn+on")[0], 401)
        self.assertEqual(self.stove.commands, [])

    def test_a_failure_says_what_was_done(self):
        # the commands go one by one: those before the failing one stay done, and "done" says so
        # (a program that calls the API can tell what is left to retry)
        self.stove.commands.clear()
        self.stove.fail_once = {"fan"}
        try:
            status, body, _ = self.call("/api/control", data=b"temp=22&fan=3&state=on",
                                        headers={"X-Stove-Token": self.LOCAL})
        finally:
            self.stove.fail_once = set()
        self.assertEqual(status, 502, body)
        d = json.loads(body)
        self.assertEqual((d["ok"], d["done"]), (False, ["setpoint"]))
        self.assertIn("timed out", d["error"])
        self.assertEqual(self.stove.commands, [("setpoint", 22.0)])      # "on" was not sent
        # a bad value after a good one
        status, body, _ = self.call(f"/api/control?token={self.key}&temp=21&level=9")
        self.assertEqual((status, json.loads(body)["done"]), (400, ["setpoint"]))
        # nothing sent: an empty list, in every kind of error reply
        for path, data in ((f"/api/control?token={self.key}&level=9", None), ("/api/control?state=on", None),
                           (f"/api/control?token={self.key}&tmp=3", None),
                           (f"/api/command?token={self.key}&text=banana", None),
                           ("/api/control", b"x" * 5000)):
            status, body, _ = self.call(path, data=data, headers={"X-Stove-Token": self.key} if data else None)
            self.assertIn(status, (400, 401, 413), path)
            self.assertEqual(json.loads(body)["done"], [], path)
        status, body, _ = self.call("/api/state", data=b"x" * 5000)
        self.assertEqual(status, 413)
        self.assertNotIn("done", json.loads(body))
        # all of them went through: the same list as before
        status, body, _ = self.call(f"/api/control?token={self.key}&temp=21&state=off")
        self.assertEqual((status, json.loads(body)["done"]), (200, ["setpoint", "off"]))

    def test_first_name_key_still_works(self):
        self.assertEqual(self.call(f"/api/control?key={self.key}&temp=21")[0], 200)
        self.assertEqual(self.call("/api/control?temp=21", headers={"X-Stove-Key": self.key})[0], 200)

    def test_no_key_no_control(self):
        self.stove.commands.clear()
        for q in ("state=on", "token=wrong&state=on", "token=&state=on", "key=wrong&state=on"):
            status, body, _ = self.call("/api/control?" + q)
            self.assertEqual(status, 401, q)
            self.assertIn("secret token", json.loads(body)["error"])
        self.assertEqual(self.stove.commands, [])

    def test_bad_values(self):
        status, body, _ = self.call(f"/api/control?key={self.key}&temp=80&format=text")
        self.assertEqual(status, 400)
        self.assertIn("between", body)
        status, body, _ = self.call(f"/api/control?key={self.key}&level=9")
        self.assertEqual(status, 400)
        self.assertEqual(self.call(f"/api/control?key={self.key}")[0], 400)

    def test_local_token_from_the_pi_itself(self):
        self.stove.commands.clear()
        status, body, _ = self.call("/api/control", data=b"temp=21.5", headers={"X-Stove-Token": self.LOCAL})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.stove.commands, [("setpoint", 21.5)])
        self.assertEqual((self.db.events(1)[0]["actor"], self.db.events(1)[0]["via"]), ("Local program", "api"))
        status, body, _ = self.call("/api/control", data=b"fan=7",
                                    headers={"X-Stove-Token": self.LOCAL, "X-Stove-Actor": "Home Assistant"})
        self.assertEqual(status, 200, body)
        self.assertEqual(self.db.events(1)[0]["actor"], "Local program (Home Assistant)")
        self.call("/api/control", data=b"fan=6", headers={"X-Stove-Token": self.LOCAL,
                                                          "X-Stove-Actor": " \x07 a  b " + "x" * 60})
        self.assertEqual(self.db.events(1)[0]["actor"], "Local program (a b " + "x" * 36 + ")")
        status, body, _ = self.call("/api/command", data=b"text=fan+auto", headers={"X-Stove-Token": self.LOCAL})
        self.assertEqual(status, 200, body)

    def test_local_token_never_through_a_proxy(self):
        self.stove.commands.clear()
        for real in ("192.168.1.40", "127.0.0.1", ""):
            status, body, _ = self.call("/api/control", data=b"state=on",
                                        headers={"X-Stove-Token": self.LOCAL, "X-Real-IP": real})
            self.assertEqual(status, 401, real)
        self.assertEqual(self.stove.commands, [])
        # the dashboard's token still works through a proxy; the actor is "API (...)", never "Local program"
        status, body, _ = self.call("/api/control", data=b"level=2", headers={"X-Stove-Token": self.key,
                                                                              "X-Stove-Actor": "Home Assistant"})
        self.assertEqual(status, 200, body)
        self.assertTrue(self.db.events(1)[0]["actor"].startswith("API ("))

    def test_local_token_follows_its_file(self):
        try:
            new = "N3wLocalToken_after_reinstall_xyz"
            with open(self.token_file, "w") as f:
                f.write(new)
            os.utime(self.token_file, ns=(1, 1_000_000_000))       # a different mtime for sure
            self.assertEqual(self.call("/api/control?temp=21", headers={"X-Stove-Token": self.LOCAL})[0], 401)
            self.assertEqual(self.call("/api/control?temp=21", headers={"X-Stove-Token": new})[0], 200)
            with open(self.token_file, "w") as f:
                f.write("short")                                   # too short to count
            os.utime(self.token_file, ns=(2, 2_000_000_000))
            self.assertEqual(self.call("/api/control?temp=21", headers={"X-Stove-Token": "short"})[0], 401)
            os.remove(self.token_file)                             # no file: no local token
            self.assertEqual(self.call("/api/control?temp=21", headers={"X-Stove-Token": new})[0], 401)
            self.assertEqual(self.call("/api/control?temp=21", headers={"X-Stove-Token": self.key})[0], 200)
        finally:
            with open(self.token_file, "w") as f:
                f.write(self.LOCAL + "\n")

    def test_local_token_after_its_permissions_are_fixed(self):
        # unreadable at first (e.g. made by hand as root:root 0600), then chmod/chown: that changes
        # only the file's ctime, and the hub must read it again without a restart
        path = os.path.join(tempfile.mkdtemp(prefix="stove-token-"), "local-api-token")
        with open(path, "w") as f:
            f.write(self.LOCAL)
        os.chmod(path, 0o600)
        token = urlapi.LocalToken(path)
        with mock.patch("builtins.open", side_effect=PermissionError(13, "Permission denied")):
            self.assertEqual(token.get(), "")
        self.assertEqual(token.get(), "")            # cached: no new read (and log line) per request
        time.sleep(0.05)                             # past the file system's timestamp granularity
        before = os.stat(path)
        os.chmod(path, 0o640)
        after = os.stat(path)
        self.assertEqual((before.st_mtime_ns, before.st_size, before.st_ino),
                         (after.st_mtime_ns, after.st_size, after.st_ino))
        self.assertEqual(token.get(), self.LOCAL)

    def test_local_token_is_never_shown(self):
        home = Client("lan", "192.168.1.5", lambda o: None)
        info = self.hub.handle(home, {"id": 1, "m": "urlapi.info"})["r"]
        self.assertNotIn(self.LOCAL, json.dumps(info))
        self.assertEqual(urlapi.LocalToken("").get(), "")
        self.assertEqual(urlapi.LocalToken("/nonexistent/token").get(), "")

    def test_key_only_for_the_home_network(self):
        home = Client("lan", "192.168.1.5", lambda o: None)
        remote = Client("remote", "ana", lambda o: None, link={"id": "x"}, profile={"id": "1", "name": "ana"})
        r = self.hub.handle(home, {"id": 1, "m": "urlapi.info"})
        self.assertEqual(r["r"]["token"], self.key)
        self.assertEqual(self.hub.handle(remote, {"id": 1, "m": "urlapi.info"})["code"], 403)
        new = self.hub.handle(home, {"id": 2, "m": "urlapi.regenerate"})["r"]["token"]
        self.assertNotEqual(new, self.key)
        self.assertEqual(self.call(f"/api/control?key={self.key}&state=on")[0], 401)   # the old key is gone
        type(self).key = new


if __name__ == "__main__":
    unittest.main()
