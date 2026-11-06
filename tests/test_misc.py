"""Config file and the real Stove wrapper against the Connection Box simulator."""

import contextlib
import io
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from helpers import ROOT, make_config

import palazzetti as plz
import stove_master
from stovemaster import config as cfgmod
from stovemaster.stove import Stove, phase_of, status_text, summarize
from tools import fake_cbox


class ConfigFile(unittest.TestCase):
    def test_example_matches_template(self):
        with open(os.path.join(ROOT, "pi", "stove.ini.example"), encoding="utf-8") as f:
            self.assertEqual(f.read(), cfgmod.render(),
                             "pi/stove.ini.example is out of date - regenerate it from config.render()")

    def test_defaults(self):
        cfg = cfgmod.Config(os.path.join(ROOT, "pi", "stove.ini.example"))
        self.assertEqual((cfg.port, cfg.mdns_name, cfg.cloud_url, cfg.lan_only), (80, "stove", "", True))
        self.assertTrue(cfg.mdns_enabled)
        # no proxy in front: the hub serves port 80 itself
        self.assertEqual((cfg.bind, cfg.public_port, cfg.public_port_setting, cfg.trust_proxy),
                         ("0.0.0.0", 80, 0, False))
        self.assertEqual(cfg.local_token_file, "/etc/stove-master/local-api-token")

    def test_derived_values(self):
        cfg = make_config(mdns_name="Kamin.local", web_allowed_hosts="Pi.lan, kamin.home.")
        self.assertEqual((cfg.mdns_name, cfg.allowed_hosts), ("kamin", ["pi.lan", "kamin.home"]))
        cfg = make_config(cloud_url="https://example.com/mystove")
        self.assertEqual(cfg.cloud_url, "https://example.com/mystove/")
        cfg = make_config(web_bind="127.0.0.1", web_port="8080", web_public_port="80", web_trust_proxy="yes")
        self.assertEqual((cfg.port, cfg.public_port, cfg.trust_proxy), (8080, 80, True))
        cfg.port = 9000                                   # serve --port: the public port follows only if unset
        self.assertEqual(cfg.public_port, 80)
        self.assertEqual(make_config(web_port="8080").public_port, 8080)

    def test_rejects_bad_values(self):
        with self.assertRaises(ValueError):
            cfgmod.render({("cloud", "url"): "a\nb"})
        with self.assertRaises(ValueError):
            make_config(cloud_url="example.com/mystove")
        with self.assertRaises(ValueError):
            make_config(web_lan_only="maybe")
        for bad in ("eighty", "70000", "-1"):
            with self.assertRaises(ValueError):
                make_config(web_public_port=bad)
        with self.assertRaises(ValueError):
            make_config(web_trust_proxy="perhaps")
        with self.assertRaises(FileNotFoundError):
            cfgmod.Config("/nonexistent/stove.ini")


class Installer(unittest.TestCase):
    """pi/install.sh runs as root from the auto-updater; at least it must parse."""

    def test_install_sh_parses(self):
        r = subprocess.run(["bash", "-n", os.path.join(ROOT, "pi", "install.sh")], capture_output=True, text=True,
                           check=False)
        self.assertEqual(r.returncode, 0, r.stderr)

    def script(self):
        with open(os.path.join(ROOT, "pi", "install.sh"), encoding="utf-8") as f:
            return f.read()

    def function(self, name):
        script = self.script()
        body = script[script.index(name + "() {"):]
        return body[:body.index("\n}\n") + 3]

    def run_functions(self, code, *names):
        """The installer's functions NAMES, then CODE, with the hub's code from this checkout."""
        prelude = (f"set -Eeuo pipefail\nPYTHON={shlex.quote(sys.executable)}\nAPP={shlex.quote(ROOT)}\n"
                   "SVC_USER=stove\nchown() { :; }\n")
        r = subprocess.run(["bash", "-c", prelude + "".join(self.function(n) for n in names) + code],
                           capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout

    def test_help_and_bad_options(self):
        # (both before the check for root)
        script = os.path.join(ROOT, "pi", "install.sh")
        r = subprocess.run(["bash", script, "--help"], capture_output=True, text=True, check=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(r.stdout.startswith("install.sh - install or update"), r.stdout)
        self.assertTrue(r.stdout.endswith("add the stove to your online account\n"), r.stdout)   # the whole header
        for bad in (["--port", "0"], ["--port", "80a"], ["--port", "70000"], ["--bogus"]):
            r = subprocess.run(["bash", script] + bad, capture_output=True, text=True, check=False)
            self.assertEqual(r.returncode, 1, bad)

    def test_the_local_token_is_the_one_the_hub_reads(self):
        # made once for programs on the Pi itself; only root and the stove group can read it
        token_file = cfgmod.DEFAULTS["web"]["local_token_file"]
        script = self.script()
        self.assertRegex(script, rf"(?m)^LOCAL_TOKEN={token_file}\s")
        self.assertIn('chown root:"$SVC_USER" "$LOCAL_TOKEN"\nchmod 640 "$LOCAL_TOKEN"\n', script)
        self.assertIn("(umask 077 && ", script)

    def test_set_port_keeps_every_other_setting(self):
        # e.g. a reverse proxy set up by hand in front of the hub stays as it is
        folder = tempfile.mkdtemp(prefix="stove-install-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "stove.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write(cfgmod.render({("web", "bind"): "127.0.0.1", ("web", "port"): "8080", ("web", "public_port"): "80",
                                   ("web", "trust_proxy"): "yes", ("cloud", "url"): "https://example.com/mystove/"}))
        set_port = f"CONFIG={shlex.quote(path)}\nset_port 8090\n"
        self.run_functions(set_port, "set_port")
        cfg = cfgmod.Config(path)
        self.assertEqual((cfg.bind, cfg.port, cfg.public_port, cfg.trust_proxy, cfg.cloud_url),
                         ("127.0.0.1", 8090, 80, True, "https://example.com/mystove/"))
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        # the same port again: the file stays exactly as it is (a comment added by hand too)
        with open(path, "a", encoding="utf-8") as f:
            f.write("# mine\n")
        self.run_functions(set_port, "set_port")
        with open(path, encoding="utf-8") as f:
            self.assertTrue(f.read().endswith("# mine\n"))

    def test_load_config(self):
        folder = tempfile.mkdtemp(prefix="stove-install-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "stove.ini")
        show = f'CONFIG={shlex.quote(path)}\nload_config\necho "$CFG_PORT|$CFG_PUBLIC|$CFG_NAME|$CFG_CLOUD"\n'
        out = self.run_functions(show, "load_config")
        self.assertEqual(out, "80||stove|\n")                        # no file yet: the defaults
        with open(path, "w", encoding="utf-8") as f:
            f.write(cfgmod.render({("web", "port"): "8080", ("web", "public_port"): "80", ("mdns", "name"): "Kamin.local",
                                   ("cloud", "url"): "https://example.com/my stove's/"}))
        out = self.run_functions(show, "load_config")
        self.assertEqual(out, "8080|80|kamin|https://example.com/my stove's/\n")

    def test_the_wizard_leaves_the_restart_to_the_installer(self):
        self.assertIn('STOVE_INSTALLER=1 "$WRAPPER" setup', self.script())


class SetupWizard(unittest.TestCase):
    """stove-master setup: the questions, and what it says afterwards."""

    def run_setup(self, trust_proxy, answers, env=None):
        folder = tempfile.mkdtemp(prefix="stove-setup-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "stove.ini")
        with open(path, "w", encoding="utf-8") as f:
            f.write(cfgmod.render({("mdns", "name"): "stove", ("web", "trust_proxy"): "yes" if trust_proxy else "no",
                                   ("web", "bind"): "127.0.0.1" if trust_proxy else "0.0.0.0",
                                   ("web", "port"): "8080" if trust_proxy else "80",
                                   ("web", "public_port"): "80" if trust_proxy else ""}))
        out = io.StringIO()
        with mock.patch.dict(os.environ, env or {}), mock.patch("builtins.input", side_effect=answers), \
                mock.patch.object(plz, "discover_udp", return_value={}), contextlib.redirect_stdout(out):
            stove_master.setup(path, None)
        return cfgmod.Config(path), out.getvalue()

    def test_name_port_and_website(self):
        with mock.patch.dict(os.environ):
            os.environ.pop("STOVE_INSTALLER", None)
            cfg, out = self.run_setup(False, ["", "kamin", "8080", "https://example.com/mystove"])
        self.assertEqual((cfg.mdns_name, cfg.port, cfg.cloud_url), ("kamin", 8080, "https://example.com/mystove/"))
        self.assertIn("sudo systemctl restart stove-master", out)

    def test_behind_a_proxy_the_ports_stay(self):
        # [web] set by hand for a reverse proxy: the wizard shows it and does not ask
        cfg, out = self.run_setup(True, ["", "kamin", ""])
        self.assertEqual((cfg.mdns_name, cfg.bind, cfg.port, cfg.public_port, cfg.trust_proxy),
                         ("kamin", "127.0.0.1", 8080, 80, True))
        self.assertIn("Web port: 80, through the proxy (the hub itself listens on 127.0.0.1:8080;", out)

    def test_run_by_the_installer(self):
        # pi/install.sh (first run, --reconfigure) restarts the service itself right after
        cfg, out = self.run_setup(False, ["", "kamin", "80", ""], env={"STOVE_INSTALLER": "1"})
        self.assertEqual(cfg.mdns_name, "kamin")
        self.assertNotIn("systemctl restart", out)


class StoveWrapper(unittest.TestCase):
    """The real Stove class + palazzetti.py over HTTP against tools/fake_cbox.py."""

    @classmethod
    def setUpClass(cls):
        cls.fake = fake_cbox.FakeStove(speed=50, latency=0.05)
        cls.server = fake_cbox.make_server(cls.fake, "127.0.0.1", 0)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.host = f"127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_commands_and_status(self):
        stove = Stove(self.host, timeout=5)
        self.assertEqual(stove.status()["STATUS"], 0)
        state = stove.command("power", 5)
        self.assertEqual(state["PWR"], 5)                 # optimistic answer
        stove.command("setpoint", 21.5, refresh=False)
        self.assertEqual(self.fake.state["SETP"], 21.5)
        stove.command("on")
        time.sleep(0.6)                                     # ignition takes ~0.3 s at speed 50
        self.assertEqual(stove.read_status()["STATUS"], 6)
        stove.command("fan", 3, refresh=False)
        self.assertIn("SET RFAN 3", self.fake.commands)
        with self.assertRaises(ValueError):
            stove.command("power", 0)
        stove.command("off")
        self.assertEqual(phase_of(stove.read_status()["STATUS"]), "shutdown")

    def test_errors(self):
        stove = Stove("127.0.0.1:9", timeout=1)            # nothing listens there
        with self.assertRaises(plz.PalazzettiError):
            stove.read_status()
        self.assertIn("127.0.0.1:9", stove.last_error)
        self.fake.fail_rate = 1.0
        try:
            with self.assertRaises(plz.PalazzettiError) as ctx:
                Stove(self.host, timeout=2).read_status()
            self.assertIn("RSP=TIMEOUT", str(ctx.exception))
        finally:
            self.fake.fail_rate = 0.0

    def test_summary_texts(self):
        self.assertEqual(summarize({"STATUS": 253})["status_text"], "ALARM: no pellets")
        self.assertTrue(summarize({"STATUS": 247})["alarm"])
        self.assertEqual(status_text(247), "ALARM (code 247)")
        self.assertEqual(status_text(77), "code 77")
        self.assertEqual(phase_of(51), "running")


class StoveWebPage(unittest.TestCase):
    """stove_web.py: no login, but no other web site may use it through a visitor's browser."""

    class FakeStove:
        def __init__(self):
            self.done = []

        def status(self):
            return {"STATUS": 0, "SETP": 20.0}

        def action(self, name, value=None):
            self.done.append((name, value))
            return self.status()

    def setUp(self):
        import stove_web
        from http.server import ThreadingHTTPServer
        self.stove = self.FakeStove()
        handler = stove_web.make_handler(self.stove)
        handler.log_message = lambda *a: None
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"

    def call(self, method, path, body=None, **headers):
        import http.client
        conn = http.client.HTTPConnection(self.host, timeout=5)
        headers = {k.replace("_", "-"): v for k, v in headers.items()}
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        r.read()
        conn.close()
        return r.status

    def test_only_its_own_page(self):
        own = {"Content_Type": "application/json", "Origin": f"http://{self.host}"}
        self.assertEqual(self.call("GET", "/"), 200)
        self.assertEqual(self.call("GET", "/api/status"), 200)
        self.assertEqual(self.call("POST", "/api/action", '{"action": "on"}', **own), 200)
        self.assertEqual(self.call("POST", "/api/action", '{"action": "off"}', Content_Type="application/json"), 200)
        # a page elsewhere: no preflight-free request, no foreign Origin, no rebound host name
        attacks = (dict(Content_Type="text/plain;charset=UTF-8"),
                   dict(Content_Type="application/x-www-form-urlencoded"),
                   dict(own, Origin="https://evil.example"), dict(own, Origin="null"),
                   dict(own, Host="rebind.evil.example", Origin="http://rebind.evil.example"))
        for headers in attacks:
            self.assertEqual(self.call("POST", "/api/action", '{"action": "on"}', **headers), 403, headers)
        self.assertEqual(self.call("GET", "/api/status", Host=f"rebind.evil.example:{self.host.split(':')[1]}"), 403)
        # and only values the stove takes
        for bad in ('{"action": "setpoint", "value": 99}', '{"action": "power", "value": 0}',
                    '{"action": "fan", "value": true}', '{"action": "explode"}', '[1]'):
            self.assertEqual(self.call("POST", "/api/action", bad, **own), 400, bad)
        self.assertEqual(self.call("POST", "/api/action", '{"action": "setpoint", "value": 21.5}', **own), 200)
        self.assertEqual(self.stove.done, [("on", None), ("off", None), ("setpoint", 21.5)])


if __name__ == "__main__":
    unittest.main()
