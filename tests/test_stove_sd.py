"""pi/stove-sd.py: which disks it offers, the settings it accepts, writing the card."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from helpers import ROOT

spec = importlib.util.spec_from_file_location("stove_sd", os.path.join(ROOT, "pi", "stove-sd.py"))
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)

GB = 1 << 30
LSBLK = {"blockdevices": [
    # the laptop's own disk: never
    {"name": "nvme0n1", "path": "/dev/nvme0n1", "size": 512 * GB, "type": "disk", "rm": False, "hotplug": False,
     "tran": "nvme", "ro": False, "model": "Samsung SSD", "vendor": None, "mountpoints": [None],
     "children": [{"name": "nvme0n1p2", "path": "/dev/nvme0n1p2", "mountpoints": ["/"]}]},
    # an SD card in the built-in reader, auto-mounted by the desktop: yes
    {"name": "mmcblk0", "path": "/dev/mmcblk0", "size": 32 * GB, "type": "disk", "rm": False, "hotplug": False,
     "tran": None, "ro": False, "model": None, "vendor": None, "mountpoints": [None],
     "children": [{"name": "mmcblk0p1", "path": "/dev/mmcblk0p1", "mountpoints": ["/media/anna/bootfs"]},
                  {"name": "mmcblk0p2", "path": "/dev/mmcblk0p2", "mountpoints": [None]}]},
    # a USB card reader: yes
    {"name": "sdb", "path": "/dev/sdb", "size": 16 * GB, "type": "disk", "rm": True, "hotplug": True,
     "tran": "usb", "ro": False, "model": "SD Reader ", "vendor": "Generic ", "mountpoints": [None]},
    # a 2 TB USB backup disk: too big to be an SD card
    {"name": "sdc", "path": "/dev/sdc", "size": 2000 * GB, "type": "disk", "rm": False, "hotplug": True,
     "tran": "usb", "ro": False, "model": "Backup", "vendor": "WD", "mountpoints": [None]},
    # a write-protected card, a USB stick that holds the running system, a CD
    {"name": "sdd", "path": "/dev/sdd", "size": 8 * GB, "type": "disk", "rm": True, "ro": True, "tran": "usb"},
    {"name": "sde", "path": "/dev/sde", "size": 64 * GB, "type": "disk", "rm": True, "tran": "usb", "ro": False,
     "children": [{"name": "sde1", "path": "/dev/sde1", "mountpoints": ["/"]}]},
    {"name": "sr0", "path": "/dev/sr0", "size": 1 * GB, "type": "rom", "rm": True, "ro": False},
]}


def form(**kw):
    v = dict(wifi_ssid="Home", wifi_password="wifi pass 1", wifi_country="de", local_name="Stove", user="anna",
             user_password="pi pass 1", ssh_key="", timezone="Europe/Vienna",
             website="https://example.com/mystove", box_host="", auto_update=True)
    v.update(kw)
    return v


class Disks(unittest.TestCase):
    def test_only_cards_are_offered(self):
        disks = sd.linux_disks(LSBLK)
        self.assertEqual([d.path for d in disks], ["/dev/mmcblk0", "/dev/sdb"])
        self.assertEqual(disks[0].parts, ["/dev/mmcblk0p1"])            # unmounted before writing
        self.assertEqual(disks[1].label, "/dev/sdb  17.2 GB  Generic SD Reader")

    def test_write_command(self):
        disk = sd.Disk(path="/dev/sdb", size=16 * GB, label="", parts=[])
        with mock.patch.object(sd.platform, "system", return_value="Linux"), \
                mock.patch.object(sd.os, "geteuid", return_value=1000), \
                mock.patch.object(sd.shutil, "which", return_value="/usr/bin/pkexec"):
            cmd, reports = sd.write_command("/tmp/x.img", disk)
        self.assertEqual(cmd[:2], ["pkexec", "dd"])
        self.assertIn("of=/dev/sdb", cmd)
        self.assertTrue(reports)
        with mock.patch.object(sd.platform, "system", return_value="Darwin"):
            cmd, reports = sd.write_command("/Users/a/it's.img", sd.Disk(path="/dev/disk4", size=0, label="", parts=[]))
        self.assertEqual(cmd[0], "osascript")
        self.assertIn("of=/dev/rdisk4", cmd[2])
        self.assertIn("'/Users/a/it'\\\\''s.img'", cmd[2])                # quoted for the shell


class Form(unittest.TestCase):
    def test_settings(self):
        o = sd.options(form())
        self.assertEqual((o.wifi_country, o.local_name, o.website, o.ssh_key, o.timezone),
                         ("DE", "stove", "https://example.com/mystove/", None, "Europe/Vienna"))
        for bad in (dict(wifi_ssid=""), dict(user=""), dict(wifi_password="short"), dict(user_password="short"),
                    dict(box_host="stove.lan"), dict(local_name="my stove"), dict(wifi_country="DEU"),
                    dict(wifi_country=" ")):
            with self.assertRaises(sd.mi.Fail, msg=bad):
                sd.options(form(**bad))
        with mock.patch.object(sd.mi, "local_timezone", return_value="Asia/Tokyo"):
            self.assertEqual(sd.options(form(timezone=" ")).timezone, "Asia/Tokyo")     # empty: this computer's

    def test_the_form_starts_with_this_computers_time_zone(self):
        defaults = {key: default for key, _, default, _ in sd.FIELDS}
        self.assertEqual((defaults["wifi_country"], defaults["website"]), ("", ""))     # nobody's in particular
        self.assertEqual(defaults["timezone"], sd.mi.local_timezone())

    def test_passwords_are_never_saved(self):
        path = os.path.join(tempfile.mkdtemp(), "s.json")
        with mock.patch.object(sd, "SETTINGS", path):
            sd.save_settings(form())
            saved = sd.load_settings()
        self.assertEqual(saved["wifi_ssid"], "Home")
        self.assertNotIn("wifi_password", saved)
        self.assertNotIn("user_password", saved)


@unittest.skipUnless(os.geteuid() == 0 and os.path.exists("/dev/loop-control"), "needs root and loop devices")
class Flash(unittest.TestCase):
    def test_writes_the_image_to_the_card(self):
        folder = tempfile.mkdtemp()
        image, card = os.path.join(folder, "img"), os.path.join(folder, "card")
        with open(image, "wb") as f:
            f.write(os.urandom(6 << 20))
        with open(card, "wb") as f:
            f.truncate(8 << 20)
        loop = subprocess.run(["losetup", "-f", "--show", card], stdout=subprocess.PIPE, text=True,
                              check=True).stdout.strip()
        try:
            seen = []
            sd.flash(image, sd.Disk(path=loop, size=8 << 20, label=loop, parts=[]), lambda t, f=None: seen.append(f))
            with open(loop, "rb") as dev, open(image, "rb") as src:
                self.assertEqual(dev.read(6 << 20), src.read())
            self.assertEqual(seen[-1], 1.0)
            with self.assertRaises(sd.mi.Fail):                       # never larger than the card
                sd.flash(image, sd.Disk(path=loop, size=4 << 20, label=loop, parts=[]), lambda t, f=None: None)
        finally:
            subprocess.run(["losetup", "-d", loop])


if __name__ == "__main__":
    unittest.main()
