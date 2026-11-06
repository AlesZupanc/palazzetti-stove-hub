"""pi/make-image.py: the files that set a fresh Raspberry Pi OS card up as the stove bridge."""

import importlib.util
import io
import os
import shutil
import subprocess
import tarfile
import tempfile
import types
import unittest

from helpers import ROOT

spec = importlib.util.spec_from_file_location("make_image", os.path.join(ROOT, "pi", "make-image.py"))
mi = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mi)

try:
    import yaml
except ImportError:
    yaml = None


def opts(**kw):
    base = dict(timezone="Europe/Vienna", user="anna", ssh_password=False,
                wifi_ssid='Home "5G"', wifi_country="DE", local_name="stove", box_host="",
                website="https://example.com/mystove/", auto_update=True, ssh_key=None)
    base.update(kw)
    return types.SimpleNamespace(**base)


class Password(unittest.TestCase):
    def test_sha512_crypt_matches_the_reference(self):
        # test vectors of the SHA-crypt specification (Ulrich Drepper)
        self.assertEqual(mi.sha512_crypt("Hello world!", "saltstring"),
                         "$6$saltstring$svn8UoSVapNtMuq1ukKS4tPQd8iKwSMHWjl/O817G3uBnIFNjnQJuesI68u4OTLiBFdcbYEdF"
                         "CoEOfaS35inz1")
        self.assertEqual(mi.sha512_crypt("Hello world!", "saltstringsaltstring", rounds=10000),
                         "$6$rounds=10000$saltstringsaltst$OW1/O6BYHV6BcXZu8QVeXbDWra3Oeqh0sbHbbMCVNSnCM/UrjmM0Dp8"
                         "vOuZeHBy/YTBmSK6H9qs/y3RnOaw5v.")

    def test_random_salt(self):
        a, b = mi.sha512_crypt("pw"), mi.sha512_crypt("pw")
        self.assertNotEqual(a, b)
        self.assertRegex(a, r"^\$6\$[./0-9A-Za-z]{16}\$[./0-9A-Za-z]{86}$")


@unittest.skipUnless(yaml, "PyYAML is not installed")
class CloudInit(unittest.TestCase):
    def test_user_data(self):
        text = mi.user_data(opts(), "$6$x$y", ["ssh-ed25519 AAAA key@pc"])
        self.assertTrue(text.startswith("#cloud-config\n"))
        d = yaml.safe_load(text)
        self.assertEqual((d["hostname"], d["enable_ssh"], d["ssh_pwauth"]), ("stove", True, False))
        self.assertEqual(d["users"][0]["passwd"], "$6$x$y")
        self.assertEqual(d["users"][0]["ssh_authorized_keys"], ["ssh-ed25519 AAAA key@pc"])
        self.assertEqual(d["runcmd"], [["bash", "/boot/firmware/stove-setup/firstboot.sh", "--start"]])
        self.assertTrue(yaml.safe_load(mi.user_data(opts(), "$6$x$y", []))["ssh_pwauth"])   # no key: password

    def test_network_config_survives_odd_wifi_names_and_passwords(self):
        for ssid, pw in (('Home "5G"', 'a\\b"c:d #e'), ("Žiga's net: 2", "yes: no"), ("- x", "{[*&!|>%@`]}")):
            d = yaml.safe_load(mi.network_config(opts(wifi_ssid=ssid), pw))
            w = d["network"]["wifis"]["wlan0"]
            self.assertEqual(w["access-points"], {ssid: {"password": pw}})
            self.assertEqual((w["regulatory-domain"], w["optional"]), ("DE", True))

    def test_cmdline_gets_the_wifi_country_once(self):
        line = "console=tty1 root=PARTUUID=4c032286-02 rootwait resize\n"
        once = mi.patch_cmdline(line, "DE")
        self.assertEqual(once, "console=tty1 root=PARTUUID=4c032286-02 rootwait resize cfg80211.ieee80211_regdom=DE\n")
        self.assertEqual(mi.patch_cmdline(once, "AT").count("regdom"), 1)
        self.assertNotIn("\n", mi.patch_cmdline(once, "AT").rstrip("\n"))


class Card(unittest.TestCase):
    def boot(self, *names):
        d = tempfile.mkdtemp()
        for n in names:
            with open(os.path.join(d, n), "w") as f:
                f.write("console=tty1 rootwait\n" if n == "cmdline.txt" else "")
        return d

    def test_files_on_the_card(self):
        d = self.boot("cmdline.txt", "config.txt", "meta-data", "user-data", "network-config")
        files = mi.build_files(opts(), "wifi pass 1", "pi pass 1")
        mi.write_card(d, files, "DE")
        with open(os.path.join(d, "cmdline.txt")) as f:
            self.assertIn("cfg80211.ieee80211_regdom=DE", f.read())
        with open(os.path.join(d, "stove-setup", "stove.ini")) as f:
            ini = f.read()
        self.assertIn("url = https://example.com/mystove/", ini)
        with tarfile.open(os.path.join(d, "stove-setup", "repo.tar.gz")) as tar:
            names = set(tar.getnames())
        self.assertTrue({"pi/install.sh", "stove_master.py", "stovemaster/cloud.py", "www/app/index.html",
                         "tools/auto-update.sh", "LICENSE"} <= names)
        self.assertFalse([n for n in names if n.startswith(("tests/", "www/stovecloud/", ".git/"))])
        script = os.path.join(d, "stove-setup", "firstboot.sh")
        self.assertEqual(subprocess.run(["bash", "-n", script]).returncode, 0)
        with open(script) as f:
            body = f.read()
        self.assertIn("USER_NAME=anna", body)
        self.assertNotIn("@", body.split("\n", 6)[5])              # every placeholder was filled

    def test_the_copy_on_the_card_has_unix_line_endings(self):
        folder = tempfile.mkdtemp()
        os.makedirs(os.path.join(folder, "pi"))
        with open(os.path.join(folder, "pi", "stove-master.service"), "wb") as f:
            f.write(b"[Service]\r\nExecStart=/usr/bin/python3 x\r\n")
        with open(os.path.join(folder, "pi", "logo.png"), "wb") as f:
            f.write(b"\x89PNG\0\r\n")
        with open(os.path.join(folder, "pi", "install.sh"), "wb") as f:
            f.write(b"#!/usr/bin/env bash\r\necho hi\r\n")
        names = ["pi/stove-master.service", "pi/logo.png", "pi/install.sh"]
        with mock.patch.object(mi, "ROOT", folder), mock.patch.object(mi, "repo_files", return_value=names):
            data = mi.repo_tar()
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            self.assertEqual(tar.extractfile("pi/stove-master.service").read(),
                             b"[Service]\nExecStart=/usr/bin/python3 x\n")
            self.assertEqual(tar.extractfile("pi/logo.png").read(), b"\x89PNG\0\r\n")     # binary: untouched
            self.assertEqual(tar.extractfile("pi/install.sh").read(), b"#!/usr/bin/env bash\necho hi\n")
            # scripts stay executable on the Pi, even from a checkout that lost the x bit (Windows)
            self.assertEqual((tar.getmember("pi/install.sh").mode, tar.getmember("pi/logo.png").mode), (0o755, 0o644))

    def test_refuses_what_is_not_a_new_raspberry_pi_os_card(self):
        files = {"user-data": b""}
        with self.assertRaises(mi.Fail):
            mi.write_card(self.boot("readme.txt"), files, "DE")
        with self.assertRaises(mi.Fail) as ctx:
            mi.write_card(self.boot("cmdline.txt", "config.txt"), files, "DE")    # Bookworm: no cloud-init
        self.assertIn("too old", str(ctx.exception))

    def test_bad_settings_are_refused(self):
        os.environ["STOVE_WIFI_PASSWORD"], os.environ["STOVE_USER_PASSWORD"] = "wifi pass 1", "pi pass 1"
        try:
            d = self.boot("cmdline.txt", "config.txt", "meta-data")
            base = ["--card", d, "--wifi-ssid", "Home", "--wifi-country", "de", "--user", "anna"]
            err = io.StringIO()
            for extra in (["--user", "Root!"], ["--wifi-country", "DEU"], ["--local-name", "stove_pi"],
                          ["--website", "example.com"], ["--ssh-key", "/nonexistent.pub"]):
                with unittest.mock.patch("sys.stderr", err):
                    self.assertEqual(mi.main(base + extra), 1, extra)
            self.assertEqual(mi.main(base), 0)
        finally:
            del os.environ["STOVE_WIFI_PASSWORD"], os.environ["STOVE_USER_PASSWORD"]


class TimeZone(unittest.TestCase):
    def test_this_computers_time_zone(self):
        d = tempfile.mkdtemp()
        link, copy, tz_file, none = (os.path.join(d, n) for n in ("localtime", "copy", "timezone", "missing"))
        os.symlink("/usr/share/zoneinfo/posix/Asia/Tokyo", link)      # (the target need not exist here)
        with open(copy, "wb") as f:
            f.write(b"TZif2")                                          # a copy, not a link: no name in it
        with open(tz_file, "w") as f:
            f.write("Europe/Vienna\n")
        self.assertEqual(mi.local_timezone({"TZ": ":America/New_York"}, link, tz_file), "America/New_York")
        self.assertEqual(mi.local_timezone({"TZ": "CET-1CEST"}, link, tz_file), "Asia/Tokyo")   # not a name
        self.assertEqual(mi.local_timezone({}, copy, tz_file), "Europe/Vienna")
        self.assertEqual(mi.local_timezone({}, none, none), "UTC")                             # e.g. Windows

    def test_empty_means_this_computers(self):
        o = opts(timezone="")
        with mock.patch.object(mi, "local_timezone", return_value="Asia/Tokyo"):
            mi.check_settings(o)
        self.assertEqual(o.timezone, "Asia/Tokyo")
        o = opts(timezone=" America/Argentina/Buenos_Aires ")
        mi.check_settings(o)
        self.assertEqual(o.timezone, "America/Argentina/Buenos_Aires")
        self.assertIn('timezone: "America/Argentina/Buenos_Aires"', mi.user_data(o, "$6$x$y", []))


@unittest.skipUnless(shutil.which("mkfs.vfat") and shutil.which("fsck.fat") and shutil.which("mtype"),
                     "needs dosfstools and mtools to check the result")
class Fat(unittest.TestCase):
    """The FAT32 writer against the real tools: fsck.fat finds no fault, mtools reads every file."""

    def make(self):
        path = os.path.join(tempfile.mkdtemp(), "boot.img")
        with open(path, "wb") as f:
            f.truncate(40 << 20)
        # like Raspberry Pi OS's boot partition: FAT32, 512-byte clusters, two FATs
        subprocess.run(["mkfs.vfat", "-F", "32", "-S", "512", "-s", "1", "-n", "BOOTFS", path], check=True,
                       stdout=subprocess.DEVNULL)
        return path

    def check(self, path, files):
        r = subprocess.run(["fsck.fat", "-n", path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("Free cluster summary", r.stdout)             # FSInfo is right too
        for name, data in files.items():
            got = subprocess.run(["mtype", "-i", path, "::/" + name], stdout=subprocess.PIPE,
                                 env=dict(os.environ, MTOOLS_SKIP_CHECK="1")).stdout
            self.assertEqual(got, data, name)

    def test_write_overwrite_folders_and_long_names(self):
        path = self.make()
        files = {"cmdline.txt": b"console=tty1 rootwait\n", "user-data": b"#cloud-config\n" * 50,
                 "stove-setup/repo.bundle": os.urandom(300_000), "stove-setup/firstboot.sh": b"#!/bin/bash\n",
                 "\u017diga \u2603 notes.txt": "\u017e\u0161\u010d".encode()}
        with open(path, "r+b") as f:
            fs = mi.Fat32(f, 0)
            for name, data in files.items():
                fs.write_file(name, data)
            fs.flush()
        self.check(path, files)
        # again: replace with bigger and smaller files, an empty one, and grow a folder past one cluster
        files["user-data"] = b"#cloud-config\nhostname: stove\n" * 400
        files["stove-setup/repo.bundle"] = b"small"
        files["network-config"] = b""
        files.update({f"stove-setup/file-number-{i:03d}.txt": str(i).encode() for i in range(300)})
        with open(path, "r+b") as f:
            fs = mi.Fat32(f, 0)
            for name, data in files.items():
                fs.write_file(name, data)
            fs.flush()
            self.assertEqual(fs.read_file("stove-setup/file-number-299.txt"), b"299")
        self.check(path, {k: v for k, v in files.items() if v})
        listing = subprocess.run(["mdir", "-b", "-i", path, "::/stove-setup"], stdout=subprocess.PIPE, text=True,
                                 env=dict(os.environ, MTOOLS_SKIP_CHECK="1")).stdout.split()
        self.assertEqual(len(listing), 302)

    def test_refuses_other_file_systems(self):
        path = os.path.join(tempfile.mkdtemp(), "x.img")
        with open(path, "wb") as f:
            f.write(b"\0" * 4096)
        with open(path, "r+b") as f, self.assertRaises(mi.Fail):
            mi.Fat32(f, 0)


import unittest.mock  # noqa: E402
from unittest import mock  # noqa: E402

if __name__ == "__main__":
    unittest.main()
