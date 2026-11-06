"""'Update now' on the web page.

The service runs as an unprivileged user, so it cannot update itself. When automatic updates are
on (pi/install.sh --auto-update, tools/auto-update.sh), systemd watches for a request file in the
service's data folder and then runs the updater as root. The updater removes the request and
writes how it went to update-status.json, which is shown on the page.
"""

import json
import os
import time

REQUEST, STATUS = "update-request", "update-status.json"
STUCK = 15 * 60         # a run that says "running" for longer than this has died
NOT_STARTED = 90        # a request nobody picked up in this time: the path unit is not there


class UpdateError(Exception):
    pass


class Updates:
    def __init__(self, data_dir, unit):
        self.request_file = os.path.join(data_dir, REQUEST)
        self.status_file = os.path.join(data_dir, STATUS)
        self.unit = unit                 # /etc/systemd/system/<name>.path

    @property
    def available(self):
        return os.path.exists(self.unit)

    def last(self):
        try:
            with open(self.status_file, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        if data.get("result") == "running" and time.time() - (data.get("at") or 0) > STUCK:
            data = dict(data, result="failed", message="The update stopped without finishing (see the log).")
        return data

    def info(self):
        requested = None
        try:
            requested = int(os.stat(self.request_file).st_mtime)
        except OSError:
            pass
        out = {"available": self.available, "requested": requested, "last": self.last()}
        if requested and time.time() - requested > NOT_STARTED:
            out["stuck"] = "The update did not start. Re-run the installer with --auto-update once."
        return out

    def request(self):
        if not self.available:
            raise UpdateError("Automatic updates are not switched on here, so there is nothing to start.")
        try:
            with open(self.request_file, "x", encoding="utf-8") as f:
                f.write(str(int(time.time())))
        except FileExistsError:
            pass                         # already asked for; the running check will see it
        return self.info()
