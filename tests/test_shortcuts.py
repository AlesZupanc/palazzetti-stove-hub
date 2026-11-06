"""tools/make-shortcuts.py: the Siri shortcuts, played against the hub as the iPhone would."""

import contextlib
import importlib.util
import io
import os
import plistlib
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request

from helpers import ROOT, FakeStove, make_config, temp_db

from stovemaster import lan, urlapi
from stovemaster.api import Hub
from stovemaster.scheduler import Scheduler

spec = importlib.util.spec_from_file_location("make_shortcuts", os.path.join(ROOT, "tools", "make-shortcuts.py"))
ms = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ms)


def run(wf, answer=None):
    """A small Shortcuts interpreter for the actions the script uses."""
    outputs = {}

    def resolve(token):
        v = token["Value"]
        if token["WFSerializationType"] == "WFTextTokenAttachment":
            return outputs[(v["OutputUUID"], v["OutputName"])]
        s = v["string"]
        for rng, att in sorted(v.get("attachmentsByRange", {}).items(),
                               key=lambda kv: -int(kv[0].strip("{}").split(",")[0])):
            at = int(rng.strip("{}").split(",")[0])
            s = s[:at] + outputs[(att["OutputUUID"], att["OutputName"])] + s[at + 1:]
        return s

    spoken = None
    for a in wf["WFWorkflowActions"]:
        kind, p = a["WFWorkflowActionIdentifier"].rsplit(".", 1)[1], a["WFWorkflowActionParameters"]
        if kind == "ask":
            outputs[(p["UUID"], "Provided Input")] = answer
        elif kind == "urlencode":
            outputs[(p["UUID"], "URL Encoded Text")] = urllib.parse.quote(resolve(p["WFInput"]), safe="")
        elif kind == "downloadurl":
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(resolve(p["WFURL"]), timeout=10) as r:
                    body = r.read().decode()
            except urllib.error.HTTPError as e:
                body = e.read().decode()
            outputs[(p["UUID"], "Contents of URL")] = body
        elif kind == "speaktext":
            spoken = resolve(p["WFText"])
        else:
            raise AssertionError(kind)
    return spoken


class Shortcuts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.db = temp_db()
        cfg = make_config(web_bind="127.0.0.1", web_port="0", mdns_name="stove")
        cls.stove = FakeStove()
        cls.server = lan.LanServer(Hub(cfg, cls.db, cls.stove, Scheduler(cls.db, cls.stove, cfg)), cfg)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        cls.token = urlapi.api_token(cls.db)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_the_files(self):
        out = tempfile.mkdtemp()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(ms.main(["--token", self.token, "--out", out, "--no-sign"]), 0)
        names = sorted(os.listdir(out))
        self.assertEqual(names, sorted(n + ".shortcut" for n in ms.shortcuts("http://x", "t")))
        with open(os.path.join(out, "Control the stove.shortcut"), "rb") as f:
            self.assertTrue(f.read(8).startswith(b"bplist00"))
        with open(os.path.join(out, "Stove on.shortcut"), "rb") as f:
            wf = plistlib.load(f)
        url = wf["WFWorkflowActions"][0]["WFWorkflowActionParameters"]["WFURL"]["Value"]["string"]
        self.assertEqual(url, f"http://stove.local/api/control?token={self.token}&format=text&state=on")
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            ms.main(["--token", "no spaces!", "--out", out])

    def test_they_work(self):
        sc = ms.shortcuts(self.base, self.token)
        self.stove.commands.clear()
        said = run(sc["Control the stove"], "Turn on the stove and set the temperature to 23 degrees.")
        self.assertEqual(said, "OK, target 23 degrees, switching on.")
        self.assertEqual(self.stove.commands, [("setpoint", 23.0), ("on", None)])
        self.assertEqual(run(sc["Stove warmer"]), "OK, target 24 degrees.")
        self.assertEqual(run(sc["Stove colder"]), "OK, target 23 degrees.")
        self.assertEqual(run(sc["Stove temperature"], "21,5"), "OK, target 21.5 degrees.")
        self.assertEqual(run(sc["Stove off"]), "OK, switching off.")
        self.assertEqual(run(sc["Stove on"]), "OK, switching on.")
        self.assertTrue(run(sc["How is the stove"]).startswith("Stove is "))
        self.assertIn("did not understand", run(sc["Control the stove"], "make me a coffee"))


if __name__ == "__main__":
    unittest.main()
