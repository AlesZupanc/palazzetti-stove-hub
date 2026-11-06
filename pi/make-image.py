#!/usr/bin/env python3
"""make-image.py - a Raspberry Pi OS card that sets itself up as the stove bridge.

  python3 pi/make-image.py --image latest -o stove-pi.img --wifi-ssid "Home" --wifi-country DE \\
          --user anna --website https://example.com/mystove/
  python3 pi/make-image.py --card /media/$USER/bootfs ...     # a card just flashed with Raspberry Pi OS

pi/stove-sd.py is the same with a window, and writes the card too.

On the first start the Pi joins the WiFi, creates the user and switches SSH on; then it
updates the system, clones this repository from GitHub (the tested "deploy" branch; the
copy on the card if GitHub cannot be reached) and installs the bridge (pi/install.sh) with
the settings given here - no questions. The Pi is called stove: http://stove.local/ and
ssh USER@stove.local, answered by the bridge (avahi is switched off). With --website, the
dashboard's Online page shows the token to add the stove to your account. The Pi gets this
computer's time zone (else UTC) unless --timezone names another one.

Raspberry Pi OS Lite (32-bit) from 2025-11 or newer ("Trixie") is needed: it reads its
first-start settings with cloud-init from the files user-data and network-config on the
boot partition; this script writes those (and a folder stove-setup/). For a Pi Zero W
take the 32-bit image (--image latest downloads it); it also suits every other Pi.

  --image FILE|latest   an image (.img or .img.xz); writes the result to -o (just Python,
                        also on Windows). "latest" downloads the
                        current Raspberry Pi OS Lite 32-bit image and checks its SHA-256
  --card DIR            instead: the boot partition (bootfs) of a freshly flashed card;
                        works on Windows and macOS too (no extra tools)

Passwords are asked for (or taken from $STOVE_WIFI_PASSWORD and $STOVE_USER_PASSWORD), never
taken from the command line. The user's password is stored only as a SHA-512 crypt hash;
the WiFi password is in network-config on the boot partition, as Raspberry Pi Imager does.
Do not use Raspberry Pi Imager's own "OS customisation" on top: it replaces these files.
"""

import argparse
import getpass
import hashlib
import io
import json
import lzma
import os
import re
import secrets
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

LATEST = "https://downloads.raspberrypi.com/raspios_lite_armhf_latest"
REPO_URL = "https://github.com/AlesZupanc/palazzetti-stove-hub.git"
SETUP_DIR = "stove-setup"
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
TZ_RE = re.compile(r"^(UTC|[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)+)$")   # "Europe/Vienna", not "CET-1CEST"
# what the bridge needs from the repository (pi/install.sh checks for these)
REPO_PATHS = ["palazzetti.py", "stove_master.py", "stove_web.py", "cbox_probe.py", "stovemaster", "www/app",
              "pi", "tools/auto-update.sh", "README.md", "LICENSE"]


class Fail(Exception):
    pass


# ------------------------------------------------------------------ passwords
_ITOA64 = "./0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"


def sha512_crypt(password, salt=None, rounds=5000):
    """glibc's $6$ password hash (the format of /etc/shadow), in plain Python so that
    it also works where the crypt module and openssl are missing (Windows, Python 3.13)."""
    pw = password.encode("utf-8")
    salt = (salt if salt is not None else "".join(secrets.choice(_ITOA64) for _ in range(16)))[:16]
    s = salt.encode("ascii")

    def h(*parts):
        return hashlib.sha512(b"".join(parts)).digest()

    b = h(pw, s, pw)
    a = hashlib.sha512(pw + s)
    a.update((b * (len(pw) // 64 + 1))[:len(pw)])
    n = len(pw)
    while n:
        a.update(b if n & 1 else pw)
        n >>= 1
    a = a.digest()
    dp = h(pw * len(pw))
    p = (dp * (len(pw) // 64 + 1))[:len(pw)]
    ds = h(s * (16 + a[0]))
    sbytes = (ds * (len(s) // 64 + 1))[:len(s)]
    c = a
    for i in range(rounds):
        x = hashlib.sha512()
        x.update(p if i & 1 else c)
        if i % 3:
            x.update(sbytes)
        if i % 7:
            x.update(p)
        x.update(c if i & 1 else p)
        c = x.digest()
    order = [(0, 21, 42), (22, 43, 1), (44, 2, 23), (3, 24, 45), (25, 46, 4), (47, 5, 26), (6, 27, 48),
             (28, 49, 7), (50, 8, 29), (9, 30, 51), (31, 52, 10), (53, 11, 32), (12, 33, 54), (34, 55, 13),
             (56, 14, 35), (15, 36, 57), (37, 58, 16), (59, 17, 38), (18, 39, 60), (40, 61, 19), (62, 20, 41)]
    out = ""
    for i, j, k in order:
        v = (c[i] << 16) | (c[j] << 8) | c[k]
        for _ in range(4):
            out += _ITOA64[v & 63]
            v >>= 6
    v = c[63]
    for _ in range(2):
        out += _ITOA64[v & 63]
        v >>= 6
    prefix = "$6$" if rounds == 5000 else f"$6$rounds={rounds}$"
    return f"{prefix}{salt}${out}"


# ------------------------------------------------------------------ the files
def q(value):
    """A YAML double-quoted string (JSON strings are valid YAML)."""
    return json.dumps(value, ensure_ascii=False)


def user_data(opts, pw_hash, keys):
    lines = ["#cloud-config", "# written by palazzetti-stove-hub pi/make-image.py",
             f"hostname: {opts.local_name}", "manage_etc_hosts: true", f"timezone: {q(opts.timezone)}",
             "users:", f"  - name: {opts.user}",
             "    groups: users,adm,dialout,audio,netdev,video,plugdev,cdrom,games,input,gpio,spi,i2c,render,sudo",
             "    shell: /bin/bash", "    lock_passwd: false", f"    passwd: {q(pw_hash)}",
             "    sudo: ALL=(ALL) ALL"]
    if keys:
        lines.append("    ssh_authorized_keys:")
        lines += [f"      - {q(k)}" for k in keys]
    lines += ["enable_ssh: true", f"ssh_pwauth: {'false' if keys and not opts.ssh_password else 'true'}",
              "runcmd:", f"  - [bash, /boot/firmware/{SETUP_DIR}/firstboot.sh, --start]", ""]
    return "\n".join(lines)


def network_config(opts, wifi_password):
    return "\n".join([
        "# written by palazzetti-stove-hub pi/make-image.py", "network:", "  version: 2", "  wifis:",
        "    renderer: NetworkManager", "    wlan0:", "      dhcp4: true",
        f"      regulatory-domain: {q(opts.wifi_country)}", "      access-points:",
        f"        {q(opts.wifi_ssid)}:", f"          password: {q(wifi_password)}",
        "      optional: true", ""])


def stove_ini(opts):
    from stovemaster import config as cfgmod
    values = {(s, k): v for s, keys in cfgmod.DEFAULTS.items() for k, v in keys.items()}
    values[("stove", "box_host")] = opts.box_host or ""
    values[("mdns", "name")] = opts.local_name
    values[("cloud", "url")] = opts.website or ""
    return cfgmod.render(values)


FIRSTBOOT = r"""#!/bin/bash
# firstboot.sh - installs the stove bridge on the first start (written by pi/make-image.py).
# cloud-init runs "firstboot.sh --start" once; that makes the systemd unit stove-firstboot,
# which runs it again at every start until the bridge is installed (no network yet, ...).
# Log: /var/log/stove-firstboot.log
set -uo pipefail
USER_NAME=@USER@
AUTO_UPDATE=@AUTO_UPDATE@
REPO_URL=@REPO_URL@
BRANCH=@BRANCH@
SETUP=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
DONE=/var/lib/stove-firstboot.done
UNIT=/etc/systemd/system/stove-firstboot.service
LOG=/var/log/stove-firstboot.log

if [ "${1:-}" = --start ]; then          # from cloud-init: hand over to systemd (it retries)
    cat >"$UNIT" <<EOF
[Unit]
Description=Install the stove bridge (first start)
Wants=network-online.target
After=network-online.target
ConditionPathExists=!$DONE

[Service]
Type=oneshot
ExecStart=/bin/bash $SETUP/firstboot.sh
Restart=on-failure
RestartSec=60
TimeoutStartSec=infinity

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable stove-firstboot.service
    systemctl start --no-block stove-firstboot.service
    exit 0
fi

exec >>"$LOG" 2>&1
exec 9>/run/stove-firstboot.lock
flock 9                                  # one run at a time
echo "=== $(date '+%F %T') stove bridge setup"
[ -f "$DONE" ] && { echo "already done"; exit 0; }
export DEBIAN_FRONTEND=noninteractive
step() { echo "--- $(date '+%T') $*"; }
TRIES=/var/lib/stove-firstboot.tries
tries=$(( $(cat "$TRIES" 2>/dev/null || echo 0) + 1 )); echo "$tries" >"$TRIES"

step "waiting for the network"
online=0
for _ in $(seq 1 60); do getent hosts github.com >/dev/null && { online=1; break; }; sleep 5; done
if [ "$online" = 0 ] && [ "$tries" -lt 30 ]; then
    echo "no internet yet (WiFi name/password right?) - trying again in a minute"
    exit 1
fi

if [ "$online" = 1 ] && [ ! -f /var/lib/stove-firstboot.upgraded ]; then
    step "updating the system (apt full-upgrade; on a Pi Zero this takes a while)"
    dpkg --configure -a --force-confdef --force-confold   # finishes an update that a power cut interrupted
    if apt-get update -q && apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
            full-upgrade && apt-get install -y -q git; then
        touch /var/lib/stove-firstboot.upgraded
    else
        echo "the system update failed - going on; it is tried again at the next start"
    fi
fi

repo=/home/$USER_NAME/palazzetti-stove-hub
as_user() { runuser -u "$USER_NAME" -- env GIT_TERMINAL_PROMPT=0 "$@"; }
if [ ! -f "$repo/pi/install.sh" ]; then
    rm -rf "$repo"
    if command -v git >/dev/null && [ "$online" = 1 ]; then
        for b in deploy main; do          # deploy = the newest commit whose tests passed
            step "cloning $REPO_URL (branch $b)"
            as_user git clone -q -b "$b" "$REPO_URL" "$repo" && break
            rm -rf "$repo"
        done
    fi
    if [ ! -d "$repo/.git" ] && command -v git >/dev/null && [ -f "$SETUP/repo.bundle" ]; then
        step "GitHub cannot be reached - using the copy on the card"
        as_user git clone -q -b "$BRANCH" "$SETUP/repo.bundle" "$repo" &&
            as_user git -C "$repo" remote set-url origin "$REPO_URL" || rm -rf "$repo"
    fi
    if [ ! -f "$repo/pi/install.sh" ]; then
        step "using the plain copy on the card"
        install -d -o "$USER_NAME" -g "$USER_NAME" "$repo"
        tar -xzf "$SETUP/repo.tar.gz" -C "$repo" && chown -R "$USER_NAME:$USER_NAME" "$repo" || exit 1
    fi
fi
if [ ! -f /etc/stove-master/stove.ini ]; then
    install -d -m 755 /etc/stove-master
    install -m 640 "$SETUP/stove.ini" /etc/stove-master/stove.ini
fi
step "installing the bridge (pi/install.sh)"
if ! bash "$repo/pi/install.sh" </dev/null; then
    echo "=== pi/install.sh failed - trying again in a minute"
    exit 1
fi
touch "$DONE"
systemctl disable stove-firstboot.service >/dev/null 2>&1

# The bridge answers NAME.local itself (also for ssh); avahi would claim the same name
step "the bridge answers @LOCAL_NAME@.local now - switching avahi off"
systemctl disable --now avahi-daemon.socket avahi-daemon.service >/dev/null 2>&1
systemctl mask avahi-daemon.socket avahi-daemon.service >/dev/null 2>&1

# automatic updates are a bonus: the bridge works without them, so never retry for them
if [ "$AUTO_UPDATE" = 1 ]; then
    if [ -d "$repo/.git" ] && bash "$repo/pi/install.sh" --auto-update </dev/null; then
        echo "automatic updates are on"
    else
        echo "automatic updates are NOT on (see above); later: cd $repo && sudo pi/install.sh --auto-update"
    fi
fi
echo "=== done: http://@LOCAL_NAME@.local/"
if [ ! -d "/lib/modules/$(uname -r)" ] || [ -f /run/reboot-required ]; then
    echo "a new kernel was installed - restarting"
    systemctl reboot
fi
"""


def firstboot(opts, branch):
    return (FIRSTBOOT.replace("@USER@", opts.user).replace("@AUTO_UPDATE@", "1" if opts.auto_update else "0")
            .replace("@LOCAL_NAME@", opts.local_name)
            .replace("@REPO_URL@", REPO_URL).replace("@BRANCH@", branch))


def repo_files():
    """Paths (relative to the repository) that go onto the card: the tracked files when this is a
    git checkout, else everything under REPO_PATHS."""
    try:
        listed = subprocess.run(["git", "-C", ROOT, "ls-files", "-z", "--"] + REPO_PATHS, check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode().split("\0")
        files = [f for f in listed if f and os.path.isfile(os.path.join(ROOT, f))]
        if files:
            return files
    except (OSError, subprocess.CalledProcessError):
        pass
    files = []
    for p in REPO_PATHS:
        full = os.path.join(ROOT, p)
        if os.path.isfile(full):
            files.append(p)
        for d, dirs, names in os.walk(full):
            dirs[:] = [x for x in dirs if x != "__pycache__"]
            files += [os.path.relpath(os.path.join(d, n), ROOT) for n in names if not n.endswith(".pyc")]
    return files


def repo_tar():
    """The repository files as .tar.gz, text files with LF line endings: a checkout on Windows may
    have CRLF, which bash and systemd on the Pi do not accept."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for f in sorted(repo_files()):
            path = os.path.join(ROOT, f)
            with open(path, "rb") as fh:
                data = fh.read()
            if b"\0" not in data:
                data = data.replace(b"\r\n", b"\n")
            info = tarfile.TarInfo(f)
            info.size, info.mtime = len(data), int(os.path.getmtime(path))
            info.mode = 0o755 if os.access(path, os.X_OK) or f.endswith(".sh") else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def repo_bundle():
    """(bundle bytes, branch) of the checked-out branch, or (None, "main") without git."""
    try:
        branch = subprocess.run(["git", "-C", ROOT, "rev-parse", "--abbrev-ref", "HEAD"], check=True,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout.decode().strip()
        if branch in ("", "HEAD"):
            return None, "main"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "repo.bundle")
            subprocess.run(["git", "-C", ROOT, "bundle", "create", path, branch], check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with open(path, "rb") as f:
                return f.read(), branch
    except (OSError, subprocess.CalledProcessError):
        return None, "main"


def build_files(opts, wifi_password, user_password):
    keys = []
    for path in opts.ssh_key or []:
        try:
            with open(os.path.expanduser(path), encoding="utf-8") as f:
                keys += [l.strip() for l in f if l.strip() and not l.startswith("#")]
        except OSError as e:
            raise Fail(f"--ssh-key {path}: {e.strerror}") from None
    for k in keys:
        if not re.match(r"^(ssh-|ecdsa-|sk-)\S+ \S+", k):
            raise Fail(f"{k[:30]}... does not look like an SSH public key (the .pub file)")
    bundle, branch = repo_bundle()
    files = {
        "user-data": user_data(opts, sha512_crypt(user_password), keys).encode(),
        "network-config": network_config(opts, wifi_password).encode(),
        f"{SETUP_DIR}/firstboot.sh": firstboot(opts, branch).encode(),
        f"{SETUP_DIR}/stove.ini": stove_ini(opts).encode(),
        f"{SETUP_DIR}/repo.tar.gz": repo_tar(),
    }
    if bundle:
        files[f"{SETUP_DIR}/repo.bundle"] = bundle
    return files


def patch_cmdline(text, country):
    """cmdline.txt with the WiFi country as a kernel argument, as raspi-config sets it
    (NetworkManager ignores network-config's regulatory-domain; without it the Pi does
    not see routers on channels 12 and 13)."""
    words = [w for w in text.split() if not w.startswith("cfg80211.ieee80211_regdom=")]
    return " ".join(words + [f"cfg80211.ieee80211_regdom={country}"]) + "\n"


# ------------------------------------------------------------------ targets
def check_boot_listing(names):
    names = {n.lower() for n in names}
    if "cmdline.txt" not in names or "config.txt" not in names:
        raise Fail("that is not the boot partition of Raspberry Pi OS (no cmdline.txt / config.txt)")
    if "meta-data" not in names:
        raise Fail("this Raspberry Pi OS is too old: its first start is not set up with cloud-init. "
                   "Use Raspberry Pi OS Lite (32-bit) from 2025-11 or newer (--image latest)")


def write_card(boot, files, country):
    check_boot_listing(os.listdir(boot))
    with open(os.path.join(boot, "cmdline.txt"), encoding="utf-8") as f:
        files = dict(files, **{"cmdline.txt": patch_cmdline(f.read(), country).encode()})
    for name, data in files.items():
        path = os.path.join(boot, *name.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    print(f"wrote {', '.join(sorted(files))} to {boot}")


def partitions(path):
    """[(start byte, size bytes, type)] from the MBR."""
    with open(path, "rb") as f:
        mbr = f.read(512)
    if mbr[510:512] != b"\x55\xaa":
        raise Fail(f"{path} has no partition table - is it a Raspberry Pi OS image?")
    out = []
    for i in range(4):
        e = mbr[446 + 16 * i:462 + 16 * i]
        start, size = int.from_bytes(e[8:12], "little"), int.from_bytes(e[12:16], "little")
        if size:
            out.append((start * 512, size * 512, e[4]))
    return out


class Fat32:
    """Just enough FAT32 to replace and add files on the boot partition inside an image file:
    no mtools, no mounting, no root - so images can be made on Windows too. Long file names,
    folders, both FAT copies and the free-cluster count in FSInfo."""

    END = 0x0FFFFFF8

    def __init__(self, f, offset):
        self.f, self.off = f, offset
        f.seek(offset)
        bs = f.read(512)
        if bs[510:512] != b"\x55\xaa" or bs[82:87] != b"FAT32":
            raise Fail("the boot partition is not FAT32 - is it a Raspberry Pi OS image?")
        self.bps, self.spc, reserved, self.nfats = struct.unpack_from("<HBHB", bs, 11)
        self.fatsz, self.root = struct.unpack_from("<I", bs, 36)[0], struct.unpack_from("<I", bs, 44)[0]
        self.fsinfo = struct.unpack_from("<H", bs, 48)[0]
        total = struct.unpack_from("<I", bs, 32)[0] or struct.unpack_from("<H", bs, 19)[0]
        self.fat_off = offset + reserved * self.bps
        self.data_sec = reserved + self.nfats * self.fatsz
        self.csize = self.bps * self.spc
        self.nclusters = min((total - self.data_sec) // self.spc + 2, self.fatsz * self.bps // 4)
        f.seek(self.fat_off)
        self.fat = list(struct.unpack(f"<{self.fatsz * self.bps // 4}I", f.read(self.fatsz * self.bps)))

    # -- clusters
    def _pos(self, c):
        return self.off + (self.data_sec + (c - 2) * self.spc) * self.bps

    def chain(self, c):
        out = []
        while 2 <= c < self.END and len(out) <= self.nclusters:
            out.append(c)
            c = self.fat[c] & 0x0FFFFFFF
        return out

    def _set(self, c, value):
        self.fat[c] = (self.fat[c] & 0xF0000000) | value

    def alloc(self, n):
        free = []
        for c in range(2, self.nclusters):
            if not self.fat[c] & 0x0FFFFFFF:
                free.append(c)
                if len(free) == n:
                    break
        if len(free) < n:
            raise Fail("the boot partition is full")
        for c, nxt in zip(free, free[1:] + [0x0FFFFFFF]):
            self._set(c, nxt)
        return free

    def release(self, c):
        for x in self.chain(c):
            self._set(x, 0)

    def read(self, chain, size=None):
        data = b""
        for c in chain:
            self.f.seek(self._pos(c))
            data += self.f.read(self.csize)
        return data if size is None else data[:size]

    def _write(self, chain, data):
        for i, c in enumerate(chain):
            self.f.seek(self._pos(c))
            self.f.write(data[i * self.csize:(i + 1) * self.csize].ljust(self.csize, b"\0"))

    # -- directories
    def entries(self, dc):
        chain = self.chain(dc)
        raw = self.read(chain)
        out, lfn = [], []
        for i in range(len(raw) // 32):
            e = raw[i * 32:i * 32 + 32]
            if e[0] == 0:
                break
            if e[0] == 0xE5:
                lfn = []
                continue
            if e[11] == 0x0F:
                lfn.append(e)
                continue
            if e[11] & 0x08:                         # volume label
                lfn = []
                continue
            short = e[0:8].rstrip().decode("latin-1")
            ext = e[8:11].rstrip().decode("latin-1")
            name = self._lfn_name(lfn) if lfn else (short.lower() if e[12] & 0x08 else short) + \
                ("." + (ext.lower() if e[12] & 0x10 else ext) if ext else "")
            out.append({"index": i, "name": name, "short": e[0:11], "attr": e[11],
                        "cluster": (struct.unpack_from("<H", e, 20)[0] << 16) | struct.unpack_from("<H", e, 26)[0],
                        "size": struct.unpack_from("<I", e, 28)[0]})
            lfn = []
        return chain, raw, out

    @staticmethod
    def _lfn_name(parts):
        text = b""
        for e in sorted(parts, key=lambda e: e[0] & 0x1F):
            text += e[1:11] + e[14:26] + e[28:32]
        name = text.decode("utf-16-le", "replace")
        return name.split("\0", 1)[0].replace("￿", "")

    def find(self, dc, name):
        return next((e for e in self.entries(dc)[2] if e["name"].lower() == name.lower()), None)

    def _put_entry(self, chain, index, data):
        c, pos = chain[index * 32 // self.csize], index * 32 % self.csize
        self.f.seek(self._pos(c) + pos)
        self.f.write(data)

    @staticmethod
    def _stamp():
        t = time.localtime()
        return ((t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2),
                ((max(t.tm_year, 1980) - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday)

    def _short_name(self, existing, name):
        base, _, ext = name.upper().rpartition(".") if "." in name.strip(".") else (name.upper(), "", "")
        clean = lambda s: re.sub(r"[^A-Z0-9!#$%&'()@^_`{}~-]", "_", s.replace(" ", "").replace(".", ""))
        base, ext = clean(base)[:6] or "FILE", clean(ext)[:3]
        for n in range(1, 1000):
            tail = f"~{n}"
            short = (base[:8 - len(tail)] + tail).ljust(8).encode() + ext.ljust(3).encode()
            if short not in existing:
                return short
        raise Fail(f"no short name left for {name}")

    def create(self, dc, name, attr, cluster, size):
        chain, raw, entries = self.entries(dc)
        short = self._short_name({e["short"] for e in entries}, name)
        csum = 0
        for b in short:
            csum = (((csum & 1) << 7) + (csum >> 1) + b) & 0xFF
        units = name.encode("utf-16-le")
        units += b"\0\0" if len(units) % 26 else b""
        units = units.ljust(-(-len(units) // 26) * 26, b"\xff")
        slots = []
        count = len(units) // 26
        for n in range(count, 0, -1):
            u = units[(n - 1) * 26:n * 26]
            slots.append(bytes([n | (0x40 if n == count else 0)]) + u[0:10] + b"\x0f\x00" + bytes([csum]) +
                         u[10:22] + b"\0\0" + u[22:26])
        tm, dt = self._stamp()
        slots.append(short + bytes([attr, 0, 0]) + struct.pack("<HHHHHHHI", tm, dt, dt, cluster >> 16, tm, dt,
                                                               cluster & 0xFFFF, size))
        need, run, start = len(slots), 0, None
        for i in range(len(raw) // 32):                # a run of free entries (0xE5 or the end)
            if raw[i * 32] in (0, 0xE5):
                run += 1
                if run == need:
                    start = i - need + 1
                    break
            else:
                run = 0
        if start is None:                              # grow the folder
            extra = self.alloc(-(-need * 32 // self.csize) + 1)
            self._set(chain[-1], extra[0])
            self._write(extra, b"")
            start = len(raw) // 32 - run
            chain = chain + extra
        for k, slot in enumerate(slots):
            self._put_entry(chain, start + k, slot)

    def mkdir(self, parent, name):
        e = self.find(parent, name)
        if e:
            if not e["attr"] & 0x10:
                raise Fail(f"{name} on the boot partition is a file, not a folder")
            return e["cluster"]
        c = self.alloc(1)[0]
        tm, dt = self._stamp()
        dot = lambda n, cl: n.ljust(11).encode() + bytes([0x10, 0, 0]) + struct.pack(
            "<HHHHHHHI", tm, dt, dt, cl >> 16, tm, dt, cl & 0xFFFF, 0)
        self._write([c], dot(".", c) + dot("..", 0 if parent == self.root else parent))
        self.create(parent, name, 0x10, c, 0)
        return c

    def write_file(self, path, data):
        *folders, name = path.split("/")
        dc = self.root
        for folder in folders:
            dc = self.mkdir(dc, folder)
        chain = self.alloc(max(1, -(-len(data) // self.csize))) if data else []
        self._write(chain, data)
        first = chain[0] if chain else 0
        e = self.find(dc, name)
        if e is None:
            self.create(dc, name, 0x20, first, len(data))
            return
        if e["attr"] & 0x10:
            raise Fail(f"{path} on the boot partition is a folder")
        if e["cluster"]:
            self.release(e["cluster"])
        tm, dt = self._stamp()
        dchain = self.entries(dc)[0]
        c, pos = dchain[e["index"] * 32 // self.csize], e["index"] * 32 % self.csize
        self.f.seek(self._pos(c) + pos + 20)
        self.f.write(struct.pack("<HHHHI", first >> 16, tm, dt, first & 0xFFFF, len(data)))

    def read_file(self, path):
        *folders, name = path.split("/")
        dc = self.root
        for folder in folders:
            e = self.find(dc, folder)
            if not e:
                return None
            dc = e["cluster"]
        e = self.find(dc, name)
        return None if e is None else self.read(self.chain(e["cluster"]), e["size"])

    def flush(self):
        packed = struct.pack(f"<{len(self.fat)}I", *self.fat)
        for i in range(self.nfats):
            self.f.seek(self.fat_off + i * self.fatsz * self.bps)
            self.f.write(packed)
        if self.fsinfo not in (0, 0xFFFF):              # FSInfo: free clusters and where to look next
            free = [c for c in range(2, self.nclusters) if not self.fat[c] & 0x0FFFFFFF]
            self.f.seek(self.off + self.fsinfo * self.bps + 488)
            self.f.write(struct.pack("<II", len(free), free[0] if free else 0xFFFFFFFF))
        self.f.flush()


def write_image(image, files, country):
    parts = partitions(image)
    if not parts or parts[0][2] not in (0x0B, 0x0C, 0x0E):
        raise Fail(f"{image}: the first partition is not FAT - is it a Raspberry Pi OS image?")
    with open(image, "r+b") as f:
        fs = Fat32(f, parts[0][0])
        check_boot_listing(e["name"] for e in fs.entries(fs.root)[2])
        cmdline = fs.read_file("cmdline.txt").decode()
        files = dict(files, **{"cmdline.txt": patch_cmdline(cmdline, country).encode()})
        for name, data in files.items():
            fs.write_file(name, data)
        fs.flush()
    print(f"wrote {', '.join(sorted(files))} into {image}")


CACHE = os.path.join(os.path.expanduser("~"), ".cache", "stove-sd")


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_latest(folder=CACHE, progress=None):
    """The current Raspberry Pi OS Lite (32-bit) image, downloaded once into folder (SHA-256 checked).
    progress(text, fraction or None) is called along the way."""
    say = progress or (lambda text, frac=None: print(text) if frac is None else None)
    os.makedirs(folder, exist_ok=True)
    say("asking downloads.raspberrypi.com for the newest Raspberry Pi OS Lite (32-bit)")
    with urllib.request.urlopen(LATEST, timeout=60) as r:
        url = r.geturl()
        path = os.path.join(folder, url.rsplit("/", 1)[-1])
        with urllib.request.urlopen(url + ".sha256", timeout=60) as s:
            want = s.read().decode().split()[0].lower()
        if os.path.exists(path) and _sha256(path) == want:
            say(f"{os.path.basename(path)} is already downloaded")
            return path
        total = int(r.headers.get("Content-Length") or 0)
        digest, done = hashlib.sha256(), 0
        say(f"downloading {os.path.basename(path)} ({total >> 20} MB)")
        with open(path + ".part", "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                digest.update(chunk)
                f.write(chunk)
                done += len(chunk)
                if total:
                    say(f"downloading {done >> 20} / {total >> 20} MB", done / total)
    if digest.hexdigest() != want:
        os.remove(path + ".part")
        raise Fail(f"SHA-256 of {url} does not match - download it again")
    os.replace(path + ".part", path)
    say(f"{os.path.basename(path)} downloaded, SHA-256 checked")
    return path


def unpack(src, dst, progress=None):
    say = progress or (lambda text, frac=None: print(text) if frac is None else None)
    total = os.path.getsize(src)
    say(f"unpacking {os.path.basename(src)}")
    with open(src, "rb") as raw:
        fin = lzma.open(raw) if src.endswith(".xz") else raw
        with fin, open(dst, "wb") as fout:
            while True:
                chunk = fin.read(1 << 22)
                if not chunk:
                    break
                fout.write(chunk)
                say("unpacking", raw.tell() / total)


# ------------------------------------------------------------------ main
FIRST_START = ("The first start takes 20-40 minutes on a Pi Zero (system update, then the bridge). Then open "
               "http://{name}.local/ - ssh {user}@{name}.local works too; progress: /var/log/stove-firstboot.log")


def local_timezone(environ=None, localtime="/etc/localtime", timezone_file="/etc/timezone"):
    """This computer's time zone for the Pi ("Europe/Vienna"): from $TZ, the link /etc/localtime or
    /etc/timezone (Linux, macOS); UTC when none of them names one (Windows)."""
    names = [(os.environ if environ is None else environ).get("TZ", "").lstrip(":")]
    link = os.path.realpath(localtime)
    if os.path.lexists(localtime) and link != os.path.abspath(localtime):
        names.append(link)
    try:
        with open(timezone_file, encoding="utf-8") as f:
            names.append(f.read().strip())
    except (OSError, ValueError):
        pass
    for name in names:
        name = re.sub(r"^(posix|right)/", "", name.split("/zoneinfo/", 1)[-1])
        if TZ_RE.match(name):
            return name
    return "UTC"


def check_settings(opts):
    """Normalise and check the settings (raises Fail); used by the command line and pi/stove-sd.py."""
    opts.wifi_country = opts.wifi_country.strip().upper()
    opts.timezone = (opts.timezone or "").strip() or local_timezone()
    opts.local_name = opts.local_name.strip().lower()
    for value, rx, what in ((opts.user, USER_RE, "user name"), (opts.local_name, HOST_RE, "local name"),
                            (opts.wifi_country, COUNTRY_RE, "WiFi country")):
        if not rx.match(value):
            raise Fail(f"{what} {value!r} is not valid")
    if opts.user in ("root", "stove"):
        raise Fail(f"the user name {opts.user} is reserved")
    opts.website = (opts.website or "").strip()
    if opts.website and not re.match(r"^https?://[^/\s]+(/\S*)?$", opts.website):
        raise Fail("the website must look like https://example.com/mystove/")
    if opts.website and not opts.website.endswith("/"):
        opts.website += "/"
    if opts.box_host and not re.match(r"^[0-9]{1,3}(\.[0-9]{1,3}){3}$", opts.box_host.strip()):
        raise Fail("the Connection Box address must be an IP address like 192.168.1.50 (or empty)")
    if not 1 <= len(opts.wifi_ssid.encode()) <= 32:
        raise Fail("the WiFi name has 1 to 32 characters")


def check_passwords(wifi_password, user_password):
    if not 8 <= len(wifi_password) <= 63:
        raise Fail("a WiFi (WPA) password has 8 to 63 characters")
    if len(user_password) < 8:
        raise Fail("the login password needs at least 8 characters")


def make_image(src, out, files, country, progress=None):
    """Unpack the official image src to out and put the settings in."""
    unpack(src, out, progress)
    write_image(out, files, country)


def secret(env, prompt, minimum=1):
    value = os.environ.get(env)
    if value is not None:
        return value
    while True:
        a = getpass.getpass(prompt + ": ")
        if len(a) < minimum:
            print(f"  at least {minimum} characters")
            continue
        if getpass.getpass("  again: ") == a:
            return a
        print("  the two do not match")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    where = ap.add_mutually_exclusive_group(required=True)
    where.add_argument("--image", help='Raspberry Pi OS image (.img / .img.xz), or "latest"')
    where.add_argument("--card", help="boot partition (bootfs) of a freshly flashed card")
    ap.add_argument("-o", "--output", default="stove-pi.img", help="image to write (with --image)")
    ap.add_argument("--wifi-ssid", required=True)
    ap.add_argument("--wifi-country", required=True, help="two-letter country code, e.g. DE (WiFi rules)")
    ap.add_argument("--user", required=True, help="user name for logging in (SSH)")
    ap.add_argument("--ssh-key", action="append", help="public key file allowed to log in (repeatable)")
    ap.add_argument("--ssh-password", action="store_true", help="with --ssh-key: also allow the password over SSH")
    ap.add_argument("--local-name", default="stove",
                    help="the Pi's name: http://NAME.local/ and ssh USER@NAME.local (default stove)")
    ap.add_argument("--timezone", help="for the schedule, e.g. Europe/Vienna (default: this computer's, else UTC)")
    ap.add_argument("--website", help="the stove website, e.g. https://example.com/mystove/")
    ap.add_argument("--box-host", help="the Connection Box's IP (default: found automatically)")
    ap.add_argument("--auto-update", action="store_true", help="follow the tested 'deploy' branch from GitHub")
    opts = ap.parse_args(argv)
    try:
        check_settings(opts)
        wifi_password = secret("STOVE_WIFI_PASSWORD", f"WiFi password of {opts.wifi_ssid}", 8)
        user_password = secret("STOVE_USER_PASSWORD", f"password for {opts.user} on the Pi", 8)
        check_passwords(wifi_password, user_password)
        files = build_files(opts, wifi_password, user_password)
        if opts.card:
            write_card(opts.card, files, opts.wifi_country)
        else:
            src = fetch_latest() if opts.image == "latest" else opts.image
            if os.path.abspath(src) == os.path.abspath(opts.output):
                raise Fail("-o must be a new file, not the original image")
            make_image(src, opts.output, files, opts.wifi_country)
            print(f"\nDone: {opts.output}. Write it to the card with pi/stove-sd.py, Raspberry Pi Imager"
                  " (\"Use custom\", and skip its OS customisation) or balenaEtcher.")
        print(f"\n{FIRST_START.format(name=opts.local_name, user=opts.user)}")
        return 0
    except Fail as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
