"""tools/auto-update.sh against a local stand-in for GitHub (a bare repository)."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from helpers import ROOT

SCRIPT = os.path.join(ROOT, "tools", "auto-update.sh")
ENV = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.com",
           GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com", GIT_TERMINAL_PROMPT="0")


@unittest.skipUnless(sys.platform.startswith("linux") and all(shutil.which(t) for t in ("bash", "git", "flock")),
                     "needs Linux with bash, git and flock")
class AutoUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stove-upd-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, "origin.git")   # "GitHub"
        self.dev = os.path.join(self.tmp, "dev")              # where commits are made
        self.server = os.path.join(self.tmp, "server")        # the checkout on the VPS / Pi
        self.log = os.path.join(self.tmp, "runs.log")         # one line per run of the "installer"
        subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", self.origin], check=True, env=ENV)
        subprocess.run(["git", "clone", "-q", self.origin, self.dev], check=True, env=ENV, stderr=subprocess.DEVNULL)
        self.commit("1")
        self.git(self.dev, "push", "-q", "origin", "main", "main:deploy")
        subprocess.run(["git", "clone", "-q", self.origin, self.server], check=True, env=ENV)

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()

    def write(self, folder, name, text):
        with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
            f.write(text)

    def commit(self, version, bad=False, push=None):
        """A commit on main with VERSION=version (and a BAD file that makes the installer fail)."""
        self.write(self.dev, "VERSION", version + "\n")
        if bad:
            self.write(self.dev, "BAD", "x")
        elif os.path.exists(os.path.join(self.dev, "BAD")):
            os.remove(os.path.join(self.dev, "BAD"))
        self.git(self.dev, "add", "-A")
        self.git(self.dev, "commit", "-q", "-m", "v" + version)
        if push:
            self.git(self.dev, "push", "-q", "--force", "origin", *push)

    def run_update(self, *extra):
        # the "installer": logs the version it sees and fails when the commit contains BAD
        installer = f"cat VERSION >> {self.log} && test ! -f BAD"
        p = subprocess.run(["bash", SCRIPT, "--repo", self.server, "--branch", "deploy", *extra, "--",
                            "sh", "-c", installer], capture_output=True, text=True, env=ENV)
        return p.returncode, p.stdout + p.stderr

    def runs(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as f:
            return f.read().split()

    def version_on_server(self):
        with open(os.path.join(self.server, "VERSION"), encoding="utf-8") as f:
            return f.read().strip()

    def test_follows_the_deploy_branch_only(self):
        code, out = self.run_update()
        self.assertEqual((code, out, self.runs()), (0, "", []))          # nothing new: silent, installer not run
        self.commit("2", push=["main:deploy"])
        code, out = self.run_update()
        self.assertEqual((code, self.runs(), self.version_on_server()), (0, ["2"], "2"))
        self.assertIn("now on", out)
        self.assertEqual(self.git(self.server, "rev-parse", "--abbrev-ref", "HEAD"), "deploy")
        self.commit("3", push=["main"])                                     # main alone moves nothing
        code, out = self.run_update()
        self.assertEqual((code, self.runs(), self.version_on_server()), (0, ["2"], "2"))

    def test_one_run_at_a_time_and_no_lock_in_tmp(self):
        import fcntl
        self.commit("2", push=["main:deploy"])
        fd = os.open(os.path.join(self.server, ".git"), os.O_RDONLY)     # a run is going on
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            code, out = self.run_update()
            self.assertEqual((code, self.runs()), (0, []))
            self.assertIn("another run is still going on", out)
        finally:
            os.close(fd)
        # what the script used to lock: any local user could make or hold that file and stop updates
        old = "/tmp/stove-auto-update-" + "".join(c if c.isalnum() and c.isascii() else "_"
                                                  for c in os.path.realpath(self.server)) + ".lock"
        with open(old, "w") as f:
            self.addCleanup(os.remove, old)
            fcntl.flock(f, fcntl.LOCK_EX)
            code, out = self.run_update()
        self.assertEqual((code, self.runs(), self.version_on_server()), (0, ["2"], "2"), out)

    def test_failed_install_goes_back_and_waits_for_a_fix(self):
        self.commit("2", bad=True, push=["main:deploy"])
        code, out = self.run_update()
        self.assertEqual(code, 1)
        self.assertIn("going back", out)
        self.assertEqual((self.runs(), self.version_on_server()), (["2", "1"], "1"))   # new one failed, old one re-installed
        code, out = self.run_update()                                      # the same bad commit is not tried again
        self.assertEqual(code, 1)
        self.assertIn("still", out)
        self.assertEqual(self.runs(), ["2", "1"])
        self.commit("3", push=["main:deploy"])                             # the fix arrives
        code, out = self.run_update()
        self.assertEqual((code, self.runs()[-1], self.version_on_server()), (0, "3", "3"))

    def test_refuses_to_touch_local_changes(self):
        self.write(self.server, "VERSION", "edited by hand\n")
        self.commit("2", push=["main:deploy"])
        code, out = self.run_update()
        self.assertEqual(code, 1)
        self.assertIn("local changes", out)
        self.assertEqual((self.runs(), self.version_on_server()), ([], "edited by hand"))

    def test_update_now_from_the_web_page(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        request, status = os.path.join(data, "update-request"), os.path.join(data, "update-status.json")
        opts = ("--request", request, "--status", status)

        def last():
            with open(status, encoding="utf-8") as f:
                return json.load(f)

        code, _ = self.run_update(*opts)                       # the timer, nothing new: no status written
        self.assertEqual((code, os.path.exists(status)), (0, False))
        open(request, "w").close()                             # "Update now", nothing new
        code, out = self.run_update(*opts)
        self.assertEqual(code, 0)
        self.assertIn("web page", out)
        self.assertFalse(os.path.exists(request))              # taken away, or systemd would start it again
        self.assertEqual((last()["result"], last()["manual"]), ("current", True))
        self.assertIn("Already up to date: v1", last()["message"])
        self.assertEqual(oct(os.stat(status).st_mode & 0o777), "0o644")   # the web service can read it
        self.commit("2", push=["main:deploy"])
        open(request, "w").close()
        code, _ = self.run_update(*opts)
        self.assertEqual((code, last()["result"], self.version_on_server()), (0, "updated", "2"))
        self.assertIn("v2", last()["message"])
        self.commit("3", bad=True, push=["main:deploy"])       # the timer finds a broken commit
        code, _ = self.run_update(*opts)
        self.assertEqual((code, last()["result"], last()["manual"]), (1, "failed", False))
        self.assertIn("not tried again", last()["message"])
        self.assertEqual(sorted(os.listdir(data)), ["update-status.json"])     # no temporary files left
        self.assertEqual(oct(os.stat(status).st_mode & 0o777), "0o644")   # every rewrite, not only the first

    def run_flaky(self, *extra):
        """An installer that asks to be run again (one whose download failed, say) while the file
        FLAKY exists, and takes the request back once it works."""
        flaky = os.path.join(self.tmp, "FLAKY")
        installer = (f"cat VERSION >> {self.log} && if [ -f {flaky} ]; then "
                     "echo 'a download failed' > .git/stove-auto-update.retry; "
                     "else rm -f .git/stove-auto-update.retry; fi")
        p = subprocess.run(["bash", SCRIPT, "--repo", self.server, "--branch", "deploy", *extra, "--",
                            "sh", "-c", installer], capture_output=True, text=True, env=ENV)
        return p.returncode, p.stdout + p.stderr

    def age(self, name, seconds):
        path = os.path.join(self.server, ".git", name)
        t = os.stat(path).st_mtime - seconds
        os.utime(path, (t, t))

    def test_an_installer_that_did_not_get_there_is_run_again_later(self):
        retry = os.path.join(self.server, ".git", "stove-auto-update.retry")
        tries = os.path.join(self.server, ".git", "stove-auto-update.tries")
        open(os.path.join(self.tmp, "FLAKY"), "w").close()
        self.commit("2", push=["main:deploy"])
        code, out = self.run_flaky()
        self.assertEqual((code, self.runs(), os.path.exists(retry)), (0, ["2"], True))
        code, out = self.run_flaky()                           # a minute later: not yet
        self.assertEqual((code, out, self.runs()), (0, "", ["2"]))
        self.age("stove-auto-update.retry", 11 * 60)           # 10 minutes after the last run
        code, out = self.run_flaky()
        self.assertEqual((code, self.runs()), (0, ["2", "2"]))
        self.assertIn("again (a download failed)", out)
        self.assertIn("still not complete", out)
        self.age("stove-auto-update.retry", 11 * 60)           # then 20 minutes
        self.assertEqual(self.run_flaky()[0:1] + (self.runs(),), (0, ["2", "2"]))
        self.age("stove-auto-update.retry", 10 * 60)
        self.run_flaky()
        self.assertEqual(self.runs(), ["2", "2", "2"])
        os.remove(os.path.join(self.tmp, "FLAKY"))             # the download works now
        self.age("stove-auto-update.retry", 41 * 60)
        code, out = self.run_flaky()
        self.assertEqual((code, self.runs()), (0, ["2", "2", "2", "2"]))
        self.assertIn("complete now", out)
        self.assertEqual((os.path.exists(retry), os.path.exists(tries)), (False, False))
        code, out = self.run_flaky()                           # done: silent again
        self.assertEqual((code, out, self.runs()), (0, "", ["2", "2", "2", "2"]))

    def test_update_now_retries_at_once(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        request, status = os.path.join(data, "update-request"), os.path.join(data, "update-status.json")
        open(os.path.join(self.tmp, "FLAKY"), "w").close()
        self.commit("2", push=["main:deploy"])
        self.run_flaky("--request", request, "--status", status)
        with open(status, encoding="utf-8") as f:
            self.assertIn("not complete yet: a download failed", json.load(f)["message"])
        os.remove(os.path.join(self.tmp, "FLAKY"))
        open(request, "w").close()                             # "Update now" a minute later
        code, _ = self.run_flaky("--request", request, "--status", status)
        self.assertEqual((code, self.runs()), (0, ["2", "2"]))
        with open(status, encoding="utf-8") as f:
            last = json.load(f)
        self.assertEqual((last["result"], last["manual"]), ("updated", True))
        self.assertIn("complete now", last["message"])

    def stop_half_way(self, version, bad=False):
        """The state a run leaves when it is stopped after moving the checkout (a shutdown)."""
        old = self.git(self.server, "rev-parse", "HEAD")
        self.commit(version, bad=bad, push=["main:deploy"])
        self.git(self.server, "fetch", "-q", "origin", "+refs/heads/deploy:refs/remotes/origin/deploy")
        new = self.git(self.server, "rev-parse", "refs/remotes/origin/deploy")
        self.write(os.path.join(self.server, ".git"), "stove-auto-update.pending", f"{old} {new}\n")
        self.git(self.server, "checkout", "-q", "-B", "deploy", "--track", "origin/deploy")

    def test_a_run_stopped_half_way_is_finished_at_the_next_check(self):
        self.stop_half_way("2")
        code, out = self.run_update()
        self.assertEqual((code, self.runs(), self.version_on_server()), (0, ["2"], "2"))
        self.assertIn("stopped before it finished", out)
        self.assertFalse(os.path.exists(os.path.join(self.server, ".git", "stove-auto-update.pending")))
        code, out = self.run_update()
        self.assertEqual((code, out, self.runs()), (0, "", ["2"]))

    def test_a_bad_commit_stopped_half_way_still_goes_back(self):
        self.stop_half_way("2", bad=True)
        code, out = self.run_update()
        self.assertEqual(code, 1)
        self.assertIn("going back", out)
        self.assertEqual((self.runs(), self.version_on_server()), (["2", "1"], "1"))
        self.assertFalse(os.path.exists(os.path.join(self.server, ".git", "stove-auto-update.pending")))

    def test_a_rollback_cut_off_is_finished_while_waiting_for_a_fix(self):
        # the "installer": like pi/install.sh it asks for another run at its start and takes the
        # request back at a clean end; it fails on a commit with BAD
        installer = (f"echo 'did not finish' > .git/stove-auto-update.retry && cat VERSION >> {self.log} && "
                     "test ! -f BAD && rm -f .git/stove-auto-update.retry")
        def run():
            p = subprocess.run(["bash", SCRIPT, "--repo", self.server, "--branch", "deploy", "--",
                                "sh", "-c", installer], capture_output=True, text=True, env=ENV)
            return p.returncode, p.stdout + p.stderr
        self.commit("2", bad=True, push=["main:deploy"])
        code, out = run()
        self.assertEqual((code, self.runs(), self.version_on_server()), (1, ["2", "1"], "1"))
        retry = os.path.join(self.server, ".git", "stove-auto-update.retry")
        self.assertFalse(os.path.exists(retry))                    # the rollback install finished
        self.write(os.path.join(self.server, ".git"), "stove-auto-update.retry", "did not finish\n")   # ...say it had not
        code, out = run()                                         # too early: still waiting for a fix
        self.assertEqual((code, self.runs()), (1, ["2", "1"]))
        self.assertIn("waiting for a new commit", out)
        self.age("stove-auto-update.retry", 11 * 60)
        code, out = run()                                         # the rollback is finished on 1
        self.assertEqual((code, self.runs(), self.version_on_server()), (0, ["2", "1", "1"], "1"))
        self.assertIn("complete now", out)
        code, out = run()                                         # and then it waits for a fix again
        self.assertEqual((code, self.runs()), (1, ["2", "1", "1"]))
        self.assertIn("waiting for a new commit", out)

    def test_a_stop_while_only_checking_leaves_the_status_alone(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        status = os.path.join(data, "update-status.json")
        with open(status, "w", encoding="utf-8") as f:
            json.dump({"result": "updated", "message": "Updated to x", "commit": "x", "at": 1, "manual": False}, f)
        # a slow "git" for the fetch, so the check can be stopped while it runs
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        real_git = shutil.which("git")
        with open(os.path.join(bindir, "git"), "w", encoding="utf-8") as f:
            f.write(f'#!/bin/sh\ncase " $* " in *" fetch "*) sleep 5;; esac\nexec {real_git} "$@"\n')
        os.chmod(os.path.join(bindir, "git"), 0o755)
        env = dict(ENV, PATH=bindir + os.pathsep + ENV.get("PATH", ""))
        p = subprocess.Popen(["bash", SCRIPT, "--repo", self.server, "--branch", "deploy", "--status", status, "--",
                              "true"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        import time
        time.sleep(1.5)
        p.terminate()
        p.communicate(timeout=20)
        with open(status, encoding="utf-8") as f:
            self.assertEqual(json.load(f)["result"], "updated")

    def test_a_checkout_stopped_half_way_is_not_taken_for_local_changes(self):
        # git wrote the new files and index, but HEAD still names the old commit
        self.commit("2", push=["main:deploy"])
        self.git(self.server, "fetch", "-q", "origin", "+refs/heads/deploy:refs/remotes/origin/deploy")
        self.git(self.server, "read-tree", "-u", "-m", "refs/remotes/origin/deploy")
        self.assertNotEqual(self.git(self.server, "status", "--porcelain", "--untracked-files=no"), "")
        code, out = self.run_update()
        self.assertEqual((code, self.runs(), self.version_on_server()), (0, ["2"], "2"))
        self.assertIn("already holds", out)
        self.assertEqual(self.git(self.server, "status", "--porcelain", "--untracked-files=no"), "")
        self.write(self.server, "VERSION", "edited by hand\n")          # real local changes still stop it
        self.commit("3", push=["main:deploy"])
        code, out = self.run_update()
        self.assertEqual((code, self.runs()), (1, ["2"]))
        self.assertIn("local changes", out)

    def test_a_stop_before_the_rollback_checkout_goes_back_next_time(self):
        # the run wrote .failed and was stopped before it moved the checkout back
        old = self.git(self.server, "rev-parse", "HEAD")
        self.stop_half_way("2", bad=True)
        new = self.git(self.server, "rev-parse", "HEAD")
        self.write(os.path.join(self.server, ".git"), "stove-auto-update.failed", new + "\n")
        code, out = self.run_update()
        self.assertEqual(code, 1)
        self.assertIn("going back", out)
        self.assertEqual((self.runs(), self.version_on_server()), (["2", "1"], "1"))
        self.assertEqual(self.git(self.server, "rev-parse", "HEAD"), old)

    def test_a_running_status_nobody_finished_is_corrected(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        status = os.path.join(data, "update-status.json")
        with open(status, "w", encoding="utf-8") as f:    # left by a run stopped without a trap
            json.dump({"result": "running", "message": "Installing x", "commit": "x", "at": 1, "manual": False}, f)
        code, _ = self.run_update("--status", status)
        self.assertEqual(code, 0)
        with open(status, encoding="utf-8") as f:
            last = json.load(f)
        self.assertEqual(last["result"], "failed")
        self.assertIn("stopped before it finished", last["message"])

    def test_a_stop_while_installing_says_so(self):
        data = os.path.join(self.tmp, "data")
        os.makedirs(data)
        status = os.path.join(data, "update-status.json")
        self.commit("2", push=["main:deploy"])
        p = subprocess.Popen(["bash", SCRIPT, "--repo", self.server, "--branch", "deploy", "--status", status, "--",
                              "sh", "-c", "sleep 5"], env=ENV, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        import time
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with open(status, encoding="utf-8") as f:
                    if json.load(f)["result"] == "running":
                        break
            except (OSError, ValueError):
                pass
            time.sleep(0.1)
        time.sleep(0.3)
        p.terminate()
        p.communicate(timeout=20)
        with open(status, encoding="utf-8") as f:
            last = json.load(f)
        self.assertEqual(last["result"], "failed")
        self.assertIn("stopped before it finished", last["message"])
        self.assertTrue(os.path.exists(os.path.join(self.server, ".git", "stove-auto-update.pending")))


if __name__ == "__main__":
    unittest.main()
