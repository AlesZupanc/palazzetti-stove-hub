#!/usr/bin/env python3
"""stove-sd.py - make the SD card for the stove's Raspberry Pi, in a small window.

  python3 pi/stove-sd.py

Fill in the WiFi, a login and the stove website, put the SD card in and press "Write to SD
card": the newest Raspberry Pi OS Lite (32-bit, for the Pi Zero W and every other Pi) is
downloaded (once, SHA-256 checked), the settings are put in (pi/make-image.py) and the card
is written. Put it in the Pi and switch it on: it updates itself, clones the repository from
GitHub, installs the bridge and http://stove.local/ is ready (20-40 minutes on a Pi Zero).

Needs: Python 3 with Tk (Windows and macOS installers from python.org include it; Debian/Ubuntu:
sudo apt install python3-tk). Writing the card asks for your password (pkexec on Linux, the system
dialog on macOS). On Windows press "Save image file..." and write that file to the card later with
Raspberry Pi Imager ("Use custom", no OS customisation) or balenaEtcher; or put the settings onto
a card you have just flashed with the official image.
Only removable disks of 1-256 GB are offered, and you confirm the one you picked.
"""

import importlib.util
import json
import os
import platform
import plistlib
import queue
import re
import shutil
import subprocess
import sys
import threading
import types

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("make_image", os.path.join(HERE, "make-image.py"))
mi = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mi)

SETTINGS = os.path.join(os.path.expanduser("~"), ".config", "stove-sd.json")
MIN_SIZE, MAX_SIZE = 1 << 30, 256 << 30
SYSTEM_MOUNTS = {"/", "/boot", "/boot/efi", "/home", "/usr", "/var", "[SWAP]"}


class Disk(types.SimpleNamespace):
    """path (what to write), size (bytes), label (what to show), parts (mounted partitions)."""


# ------------------------------------------------------------------ disks
def _human(size):
    return f"{size / 1e9:.1f} GB"


def linux_disks(lsblk_json=None):
    """Removable disks a card can be (from lsblk), never one the running system uses."""
    if lsblk_json is None:
        out = subprocess.run(["lsblk", "-J", "-b", "-o", "NAME,PATH,SIZE,TYPE,RM,HOTPLUG,TRAN,RO,MODEL,VENDOR,"
                              "MOUNTPOINTS"], stdout=subprocess.PIPE, check=True).stdout
        lsblk_json = json.loads(out)
    disks = []
    for d in lsblk_json.get("blockdevices", []):
        if d.get("type") != "disk" or d.get("ro") in (True, "1", 1):
            continue
        removable = d.get("rm") in (True, "1", 1) or d.get("hotplug") in (True, "1", 1) or \
            (d.get("tran") or "") in ("usb", "mmc") or d.get("name", "").startswith("mmcblk")
        size = int(d.get("size") or 0)
        mounts = [m for c in [d] + (d.get("children") or []) for m in (c.get("mountpoints") or []) if m]
        if not removable or not MIN_SIZE <= size <= MAX_SIZE or SYSTEM_MOUNTS & set(mounts):
            continue
        name = " ".join(x.strip() for x in (d.get("vendor"), d.get("model")) if x and x.strip()) or "card"
        parts = [c["path"] for c in (d.get("children") or []) if any(c.get("mountpoints") or [])]
        disks.append(Disk(path=d["path"], size=size, label=f"{d['path']}  {_human(size)}  {name}", parts=parts))
    return disks


def mac_disks():
    out = subprocess.run(["diskutil", "list", "-plist", "external", "physical"], stdout=subprocess.PIPE,
                         check=True).stdout
    disks = []
    for d in plistlib.loads(out).get("AllDisksAndPartitions", []):
        ident, size = d.get("DeviceIdentifier", ""), int(d.get("Size") or 0)
        if not re.match(r"^disk\d+$", ident) or not MIN_SIZE <= size <= MAX_SIZE:
            continue
        info = plistlib.loads(subprocess.run(["diskutil", "info", "-plist", ident], stdout=subprocess.PIPE).stdout
                              or plistlib.dumps({}))
        if info.get("Internal") or info.get("VirtualOrPhysical") == "Virtual":
            continue
        name = info.get("MediaName") or "card"
        disks.append(Disk(path=f"/dev/{ident}", size=size, label=f"/dev/{ident}  {_human(size)}  {name}", parts=[]))
    return disks


def list_disks():
    system = platform.system()
    if system == "Linux":
        return linux_disks()
    if system == "Darwin":
        return mac_disks()
    return []


def write_command(image, disk):
    """The command that writes image to disk (as root), and whether it reports dd progress."""
    system = platform.system()
    if system == "Darwin":
        raw = disk.path.replace("/dev/disk", "/dev/rdisk")
        script = (f"diskutil unmountDisk {disk.path} && dd if={_shq(image)} of={raw} bs=4m && sync && "
                  f"diskutil eject {disk.path}")
        return ["osascript", "-e", f"do shell script {json.dumps(script)} with administrator privileges"], False
    dd = ["dd", f"if={image}", f"of={disk.path}", "bs=4M", "conv=fsync", "oflag=direct", "status=progress"]
    if os.geteuid() == 0:
        return dd, True
    if shutil.which("pkexec"):
        return ["pkexec"] + dd, True
    raise mi.Fail("writing the card needs root: start the app with sudo, or install pkexec (policykit-1)")


def _shq(path):
    return "'" + path.replace("'", "'\\''") + "'"


def unmount(disk):
    for part in disk.parts:          # desktop auto-mounts: udisks lets the user unmount them
        if shutil.which("udisksctl"):
            subprocess.run(["udisksctl", "unmount", "-b", part], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(["umount", part], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def flash(image, disk, progress):
    """Write image to disk; progress(text, fraction)."""
    total = os.path.getsize(image)
    if total > disk.size:
        raise mi.Fail(f"the image ({_human(total)}) is larger than the card ({_human(disk.size)})")
    unmount(disk)
    cmd, reports = write_command(image, disk)
    progress(f"writing {disk.path} (asks for your password)", 0.0 if reports else None)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    tail, buf = [], b""
    while True:
        ch = proc.stderr.read(1)
        if not ch:
            break
        if ch in (b"\r", b"\n"):
            line = buf.decode(errors="replace").strip()
            buf = b""
            m = re.match(r"^(\d+) bytes", line)
            if m and reports:
                progress(f"writing {disk.path}: {int(m.group(1)) >> 20} / {total >> 20} MB",
                         min(int(m.group(1)) / total, 1.0))
            elif line:
                tail = (tail + [line])[-5:]
        else:
            buf += ch
    proc.stderr.close()
    if proc.wait() != 0:
        raise mi.Fail("writing the card failed: " + (" ".join(tail) or f"exit code {proc.returncode}"))
    if platform.system() == "Linux" and shutil.which("udisksctl"):
        subprocess.run(["udisksctl", "power-off", "-b", disk.path], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
    progress(f"{disk.path} is written - you can take the card out", 1.0)


# ------------------------------------------------------------------ settings
FIELDS = [  # key, label, default, kind
    ("wifi_ssid", "WiFi name", "", "text"),
    ("wifi_password", "WiFi password", "", "secret"),
    ("wifi_country", "WiFi country (2 letters)", "", "text"),
    ("local_name", "Name of the Pi (.local)", "stove", "text"),
    ("user", "Login user", "", "text"),
    ("user_password", "Login password", "", "secret"),
    ("ssh_key", "SSH public key (optional)", "", "file"),
    ("timezone", "Time zone", mi.local_timezone(), "text"),     # this computer's
    ("website", "Stove website", "", "text"),
    ("box_host", "Connection Box IP (optional)", "", "text"),
]


def load_settings():
    try:
        with open(SETTINGS, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(values):
    keep = {k: v for k, v in values.items() if k not in ("wifi_password", "user_password")}   # never secrets
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(keep, f, indent=1)
    except OSError:
        pass


def options(values):
    """The make-image options from the form values (checked; raises mi.Fail)."""
    o = types.SimpleNamespace(
        wifi_ssid=values["wifi_ssid"], wifi_country=values["wifi_country"], local_name=values["local_name"],
        user=values["user"].strip(), ssh_key=[values["ssh_key"]] if values["ssh_key"].strip() else None,
        ssh_password=True, timezone=values["timezone"],     # empty: this computer's (check_settings)
        website=values["website"], box_host=values["box_host"].strip(), auto_update=bool(values["auto_update"]))
    if not o.wifi_ssid:
        raise mi.Fail("enter the WiFi name")
    if not o.wifi_country.strip():
        raise mi.Fail("enter the WiFi country: its two-letter code, e.g. DE (the WiFi rules depend on it)")
    if not o.user:
        raise mi.Fail("enter a login user name")
    mi.check_settings(o)
    mi.check_passwords(values["wifi_password"], values["user_password"])
    return o


# ------------------------------------------------------------------ window
def run_app():
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("Stove SD card")
    root.minsize(560, 0)
    frm = ttk.Frame(root, padding=14)
    frm.grid(sticky="nsew")
    root.columnconfigure(0, weight=1)
    frm.columnconfigure(1, weight=1)
    saved = load_settings()
    vars_ = {}

    def heading(text, row):
        ttk.Label(frm, text=text, font=("TkDefaultFont", 11, "bold")).grid(row=row, column=0, columnspan=3,
                                                                          sticky="w", pady=(10, 2))

    row = 0
    sections = {0: "WiFi at home", 3: "The Raspberry Pi", 8: "The stove"}
    for i, (key, label, default, kind) in enumerate(FIELDS):
        if i in sections:
            heading(sections[i], row)
            row += 1
        ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=2)
        var = tk.StringVar(value=saved.get(key, default) if kind != "secret" else "")
        vars_[key] = var
        entry = ttk.Entry(frm, textvariable=var, show="•" if kind == "secret" else "")
        entry.grid(row=row, column=1, sticky="ew", pady=2)
        if kind == "secret":
            shown = tk.BooleanVar(value=False)
            ttk.Checkbutton(frm, text="show", variable=shown,
                            command=lambda e=entry, s=shown: e.configure(show="" if s.get() else "•")
                            ).grid(row=row, column=2, sticky="w", padx=(6, 0))
        if kind == "file":
            ttk.Button(frm, text="Choose…", command=lambda v=var: v.set(
                filedialog.askopenfilename(title="SSH public key (.pub)", initialdir=os.path.expanduser("~/.ssh"))
                or v.get())).grid(row=row, column=2, sticky="w", padx=(6, 0))
        row += 1
    auto = tk.BooleanVar(value=saved.get("auto_update", False))   # opt-in: the Pi then follows REPO_URL
    ttk.Checkbutton(frm, text="Keep the bridge up to date automatically (from GitHub)", variable=auto
                    ).grid(row=row, column=1, sticky="w", pady=2)
    row += 1

    heading("SD card", row)
    row += 1
    disk_var = tk.StringVar()
    disk_box = ttk.Combobox(frm, textvariable=disk_var, state="readonly")
    disk_box.grid(row=row, column=0, columnspan=2, sticky="ew", pady=2)
    disks = []

    def refresh():
        nonlocal disks
        try:
            disks = list_disks()
        except (OSError, subprocess.CalledProcessError, ValueError) as e:
            disks = []
            log(f"cannot list the disks: {e}")
        disk_box["values"] = [d.label for d in disks]
        disk_var.set(disks[0].label if len(disks) == 1 else "")
        if platform.system() not in ("Linux", "Darwin"):
            disk_var.set("(on Windows: save the image file, or settings onto a flashed card)")
        elif not disks:
            disk_var.set("no SD card found - put one in and press Refresh")

    ttk.Button(frm, text="Refresh", command=refresh).grid(row=row, column=2, sticky="w", padx=(6, 0))
    row += 1

    buttons = ttk.Frame(frm)
    buttons.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(12, 4))
    row += 1
    bar = ttk.Progressbar(frm, mode="determinate", maximum=1.0)
    bar.grid(row=row, column=0, columnspan=3, sticky="ew", pady=(4, 2))
    row += 1
    status = tk.StringVar(value="Fill in the settings, put the SD card in and press Write.")
    ttk.Label(frm, textvariable=status, wraplength=520).grid(row=row, column=0, columnspan=3, sticky="w")
    row += 1
    logbox = tk.Text(frm, height=7, width=70, state="disabled", wrap="word")
    logbox.grid(row=row, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
    frm.rowconfigure(row, weight=1)

    events = queue.Queue()
    busy = []

    def log(text):
        logbox.configure(state="normal")
        logbox.insert("end", text + "\n")
        logbox.see("end")
        logbox.configure(state="disabled")

    def progress(text, frac=None):
        events.put(("progress", text, frac))

    def poll():
        try:
            while True:
                kind, text, frac = events.get_nowait()
                status.set(text)
                if kind == "progress":
                    if frac is None:
                        bar.configure(mode="indeterminate")
                        bar.start(15)
                    else:
                        bar.stop()
                        bar.configure(mode="determinate", value=frac)
                    if frac is None or frac in (0.0, 1.0):
                        log(text)
                elif kind in ("done", "error"):
                    bar.stop()
                    bar.configure(mode="determinate", value=1.0 if kind == "done" else 0)
                    log(("Done. " if kind == "done" else "Error: ") + text)
                    busy.clear()
                    for b in buttons.winfo_children():
                        b.state(["!disabled"])
                    (messagebox.showinfo if kind == "done" else messagebox.showerror)("Stove SD card", text)
        except queue.Empty:
            pass
        root.after(100, poll)

    def values():
        v = {k: var.get() for k, var in vars_.items()}
        v["auto_update"] = auto.get()
        return v

    def start(job, *args):
        if busy:
            return
        try:
            v = values()
            o = options(v)
        except mi.Fail as e:
            messagebox.showerror("Stove SD card", str(e))
            return
        save_settings(v)
        busy.append(1)
        for b in buttons.winfo_children():
            b.state(["disabled"])

        def work():
            try:
                files = mi.build_files(o, v["wifi_password"], v["user_password"])
                text = job(o, files, *args)
                events.put(("done", text, None))
            except mi.Fail as e:
                events.put(("error", str(e), None))
            except Exception as e:  # show anything unexpected instead of dying silently
                events.put(("error", f"{e.__class__.__name__}: {e}", None))
        threading.Thread(target=work, daemon=True).start()

    def build_image(o, files, out):
        src = mi.fetch_latest(progress=progress)
        progress("putting the settings in", None)
        mi.make_image(src, out, files, o.wifi_country, progress=progress)
        return out

    def job_card(o, files, disk):
        out = os.path.join(mi.CACHE, "stove-pi.img")
        try:
            build_image(o, files, out)
            flash(out, disk, progress)
        finally:
            if os.path.exists(out):
                os.remove(out)
        return f"The card is ready. {mi.FIRST_START.format(name=o.local_name, user=o.user)}"

    def job_file(o, files, out):
        build_image(o, files, out)
        return (f"Saved {out}. Write it with Raspberry Pi Imager (\"Use custom\", no OS customisation). "
                + mi.FIRST_START.format(name=o.local_name, user=o.user))

    def job_flashed(o, files, boot):
        mi.write_card(boot, files, o.wifi_country)
        return f"The settings are on the card. {mi.FIRST_START.format(name=o.local_name, user=o.user)}"

    def write_card():
        disk = next((d for d in disks if d.label == disk_var.get()), None)
        if disk is None:
            messagebox.showerror("Stove SD card", "Choose the SD card first (Refresh after putting it in).")
            return
        if messagebox.askyesno("Stove SD card", f"Erase everything on\n\n{disk.label}\n\nand write the stove's "
                               "Raspberry Pi OS onto it?", icon="warning"):
            start(job_card, disk)

    def save_file():
        out = filedialog.asksaveasfilename(title="Save the image", defaultextension=".img",
                                           initialfile="stove-pi.img")
        if out:
            start(job_file, out)

    def flashed_card():
        boot = filedialog.askdirectory(title="The boot drive (bootfs) of a card flashed with Raspberry Pi OS")
        if boot:
            start(job_flashed, boot)

    if platform.system() in ("Linux", "Darwin"):
        ttk.Button(buttons, text="Write to SD card", command=write_card).pack(side="left")
    ttk.Button(buttons, text="Save image file…", command=save_file).pack(side="left", padx=6)
    ttk.Button(buttons, text="Settings onto a flashed card…", command=flashed_card).pack(side="left")

    refresh()
    poll()
    root.mainloop()


if __name__ == "__main__":
    try:
        run_app()
    except ImportError:
        sys.exit("Tk is missing: sudo apt install python3-tk (macOS: brew install python-tk)")
