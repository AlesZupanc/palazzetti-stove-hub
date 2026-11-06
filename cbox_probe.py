#!/usr/bin/env python3
"""
cbox_probe.py - map the Palazzetti Connection Box web UI.

Usage:  python cbox_probe.py [box-ip]      (default 192.168.1.50)

1. Loads cgi-bin/menu.lua and every page it links to (info.lua, network pages...).
2. Prints the form fields and every syscmd/sendmsg command those pages use.
3. Runs only READ-ONLY system queries (getparams, netdata, nwdata) and prints
   their JSON, so we can see the current network and cloud configuration.
Nothing is changed on the box.
"""
import json
import re
import sys
import urllib.request

HOST = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.50"
CGI = f"http://{HOST}/cgi-bin/"
READ_ONLY_CMDS = ["getparams", "netdata", "nwdata"]

INTERESTING = re.compile(
    r"(syscmd|sendmsg|cmd\s*[:=]|cloud|mqtt|server|gateway|\bgw\b|dns|static|dhcp|"
    r"proto|WPR|EPR|enable|disable|<input|<select|<option)",
    re.I,
)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        return f"!! {e}"


def show_page(name):
    text = get(CGI + name)
    print(f"\n===== {name} ({len(text)} bytes) =====")
    if text.startswith("!!"):
        print(text)
        return text
    for line in dict.fromkeys(l.strip() for l in text.splitlines()):
        if line and INTERESTING.search(line):
            print("  " + line[:220])
    return text


# 1) menu and pages
menu = show_page("menu.lua")
pages = re.findall(r'href\s*=\s*["\']#([\w\-]+)["\']', menu)
pages += re.findall(r'["\']([\w\-]+)\.lua["\']', menu)
print("\nmenu pages:", ", ".join(dict.fromkeys(pages)) or "(none found)")
for p in dict.fromkeys(pages):
    if p not in ("menu", "syscmd", "sendmsg"):
        show_page(p + ".lua")

# 2) read-only system queries
for cmd in READ_ONLY_CMDS:
    raw = get(CGI + "syscmd.lua?cmd=" + cmd)
    print(f"\n===== syscmd {cmd} =====")
    try:
        print(json.dumps(json.loads(raw), indent=2, ensure_ascii=False))
    except ValueError:
        print(raw[:2000])
