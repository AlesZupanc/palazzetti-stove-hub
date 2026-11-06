#!/usr/bin/env python3
"""Makes Siri shortcuts (.shortcut files for the iPhone's Shortcuts app) that control the stove.

    python3 tools/make-shortcuts.py --token TOKEN [--host stove.local] [--out shortcuts]

The token is on the local dashboard (Online -> API) or: sudo stove-master api-token
The shortcuts made:

    Control the stove   Siri asks "What should I do?", the answer goes to /api/command
    Stove on            switch on                        Stove off       switch off
    Stove temperature   asks for a temperature           Stove warmer    one degree warmer
    Stove colder        one degree colder                How is the stove   speaks the state

The iPhone opens only signed shortcut files, and only a Mac can sign them (with the Apple ID
signed in to it). On a Mac this script signs them itself; then send them to the iPhone with
AirDrop (or iCloud Drive) and tap each one. Elsewhere it writes unsigned files - sign them on any
Mac with:  shortcuts sign --mode anyone --input "Stove on.shortcut" --output "Stove on.shortcut"
Without a Mac, build them by hand (two minutes each): README, "Siri shortcuts, step by step".
"""

import argparse
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import uuid

OBJ = "￼"          # where a variable sits in a Shortcuts text


def _uuid():
    return str(uuid.uuid4()).upper()


def text(*parts):
    """A Shortcuts text: strings and variables (action, output name) in a row."""
    string, attachments = "", {}
    for p in parts:
        if isinstance(p, str):
            string += p
        else:
            action, name = p
            attachments[f"{{{len(string)}, 1}}"] = {"OutputUUID": action["WFWorkflowActionParameters"]["UUID"],
                                                    "Type": "ActionOutput", "OutputName": name}
            string += OBJ
    value = {"string": string}
    if attachments:
        value["attachmentsByRange"] = attachments
    return {"Value": value, "WFSerializationType": "WFTextTokenString"}


def variable(action, name):
    return {"Value": {"OutputUUID": action["WFWorkflowActionParameters"]["UUID"], "Type": "ActionOutput",
                      "OutputName": name}, "WFSerializationType": "WFTextTokenAttachment"}


def action(identifier, **params):
    params["UUID"] = _uuid()
    return {"WFWorkflowActionIdentifier": "is.workflow.actions." + identifier, "WFWorkflowActionParameters": params}


def ask(prompt, kind="Text"):
    return action("ask", WFAskActionPrompt=prompt, WFInputType=kind)


def url_encode(of):
    return action("urlencode", WFInput=variable(of, "Provided Input"), WFEncodeMode="Encode")


def get(*url_parts):
    return action("downloadurl", WFURL=text(*url_parts), WFHTTPMethod="GET")


def speak(of):
    return action("speaktext", WFText=text((of, "Contents of URL")))


def workflow(actions, color, glyph):
    return {
        "WFWorkflowActions": actions,
        "WFWorkflowClientVersion": "2302.0.4",
        "WFWorkflowMinimumClientVersion": 900,
        "WFWorkflowMinimumClientVersionString": "900",
        "WFWorkflowIcon": {"WFWorkflowIconStartColor": color, "WFWorkflowIconGlyphNumber": glyph},
        "WFWorkflowImportQuestions": [],
        "WFWorkflowInputContentItemClasses": [],
        "WFWorkflowOutputContentItemClasses": [],
        "WFWorkflowTypes": [],
        "WFWorkflowHasShortcutInputVariables": False,
        "WFWorkflowHasOutputFallback": False,
        "WFQuickActionSurfaces": [],
    }


ORANGE, RED, BLUE, GREY = 4282601983, 4271458815, 463140863, 255      # Shortcuts' own icon colours
FLAME, POWER, THERMOMETER, QUESTION = 59446, 59755, 59776, 59532


def shortcuts(base, token):
    control = f"{base}/api/control?token={urllib.parse.quote(token)}&format=text&"
    command = f"{base}/api/command?token={urllib.parse.quote(token)}&format=text&text="

    def fixed(url, color, glyph):
        g = get(url)
        return workflow([g, speak(g)], color, glyph)

    def asking(prompt, kind, url, color, glyph):
        a = ask(prompt, kind)
        e = url_encode(a)
        g = get(url, (e, "URL Encoded Text"))
        return workflow([a, e, g, speak(g)], color, glyph)

    return {
        "Control the stove": asking("What should I do?", "Text", command, ORANGE, FLAME),
        "Stove on": fixed(control + "state=on", ORANGE, POWER),
        "Stove off": fixed(control + "state=off", GREY, POWER),
        "Stove temperature": asking("Which temperature?", "Number", control + "temp=", RED, THERMOMETER),
        "Stove warmer": fixed(command + "one+degree+warmer", RED, THERMOMETER),
        "Stove colder": fixed(command + "one+degree+colder", BLUE, THERMOMETER),
        "How is the stove": fixed(f"{base}/api/state?format=text", BLUE, QUESTION),
    }


def sign(path):
    """macOS only: sign with the Apple ID of this Mac, so that any iPhone can open it."""
    signed = path + ".signed"
    r = subprocess.run(["shortcuts", "sign", "--mode", "anyone", "--input", path, "--output", signed],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    if r.returncode != 0 or not os.path.exists(signed):
        raise RuntimeError(r.stdout.strip() or "shortcuts sign failed")
    os.replace(signed, path)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Make Siri shortcuts (.shortcut files) for the stove.")
    ap.add_argument("--token", default=os.environ.get("STOVE_API_TOKEN"),
                    help="the secret token (dashboard: Online -> API); or $STOVE_API_TOKEN")
    ap.add_argument("--host", default="stove.local", help="the hub's address (default stove.local)")
    ap.add_argument("--out", default="shortcuts", help="folder for the files (default ./shortcuts)")
    ap.add_argument("--no-sign", action="store_true", help="do not sign, even on a Mac")
    args = ap.parse_args(argv)
    if not args.token or not re.fullmatch(r"[A-Za-z0-9_-]{8,}", args.token):
        ap.error("--token: the secret token from the dashboard (Online -> API)")
    host = args.host.strip().rstrip("/")
    base = host if host.startswith(("http://", "https://")) else "http://" + host
    can_sign = not args.no_sign and sys.platform == "darwin" and shutil.which("shortcuts")
    os.makedirs(args.out, exist_ok=True)
    for name, wf in shortcuts(base, args.token).items():
        path = os.path.join(args.out, name + ".shortcut")
        fd, tmp = tempfile.mkstemp(dir=args.out, suffix=".shortcut")
        with os.fdopen(fd, "wb") as f:
            plistlib.dump(wf, f, fmt=plistlib.FMT_BINARY)
        try:
            if can_sign:
                sign(tmp)
            os.replace(tmp, path)
        except RuntimeError as e:
            os.unlink(tmp)
            print(f"error signing {name}: {e}", file=sys.stderr)
            return 1
        print(("signed   " if can_sign else "unsigned ") + path)
    if can_sign:
        print("\nSend them to the iPhone (AirDrop or iCloud Drive) and tap each one to add it.")
    else:
        print("\nThe iPhone opens only signed shortcuts. Sign them on a Mac with:\n"
              '  shortcuts sign --mode anyone --input "NAME.shortcut" --output "NAME.shortcut"\n'
              "(or run this script there). Without a Mac: README, \"Siri shortcuts, step by step\".")
    print("The files contain the secret token: keep them to yourself.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
