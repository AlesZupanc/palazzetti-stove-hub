"""The GitHub Actions workflow: pinned actions, a read-only test job, hashed pip installs, and the
forward-only move of the branch "deploy" that automatic updates install as root."""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

from helpers import ROOT

WORKFLOWS = os.path.join(ROOT, ".github", "workflows")
CI = os.path.join(WORKFLOWS, "ci.yml")
REQUIREMENTS = os.path.join(ROOT, "tests", "requirements.txt")
ENV = dict(os.environ, GIT_AUTHOR_NAME="test", GIT_AUTHOR_EMAIL="test@example.com",
           GIT_COMMITTER_NAME="test", GIT_COMMITTER_EMAIL="test@example.com", GIT_TERMINAL_PROMPT="0")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def jobs(text):
    """{job id: its text} - just enough YAML for this file (the tests run without PyYAML)."""
    found = re.split(r"^  ([\w-]+):\n", text.split("\njobs:\n", 1)[1], flags=re.M)
    return dict(zip(found[1::2], found[2::2]))


def promote_script():
    """The shell script of promote's step "Move the deploy branch ..."."""
    lines = read(CI).split("- name: Move the deploy branch to this tested commit\n", 1)[1].splitlines()
    indent = len(lines[0]) - len(lines[0].lstrip())
    assert lines[0].strip() == "run: |", lines[0]
    body = []
    for line in lines[1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line[indent + 2:])
    return "\n".join(body) + "\n"


class Workflow(unittest.TestCase):
    def test_actions_are_pinned_to_a_commit(self):
        # a tag can be moved to other code, a commit cannot; the comment names the release, so
        # Dependabot can propose the next one
        uses = [(name, line.strip()) for name in sorted(os.listdir(WORKFLOWS))
                for line in read(os.path.join(WORKFLOWS, name)).splitlines() if re.match(r"\s*(- )?uses:", line)]
        self.assertTrue(uses)
        for name, line in uses:
            self.assertRegex(line, r"^(- )?uses: [\w.-]+/[\w./-]+@[0-9a-f]{40} # v\d+(\.\d+)*$", name)

    def test_only_promote_can_write(self):
        text = read(CI)
        self.assertIn("\npermissions:\n  contents: read ", text.split("\njobs:\n")[0])
        found = jobs(text)
        self.assertEqual(set(found), {"test", "promote"})
        for job, body in found.items():
            self.assertRegex(body, r"\n    timeout-minutes: \d+\n", job)
            perms = re.search(r"\n    permissions:\n((?:      .*\n)+)", body).group(1)
            grants = re.findall(r"^      ([\w-]+): (\w+)", perms, flags=re.M)
            self.assertEqual(grants, [("contents", "write" if job == "promote" else "read")], job)
        # pull requests (also from forks) and any trigger added later never reach the write job
        self.assertIn("\n    if: (github.event_name == 'push' || github.event_name == 'workflow_dispatch')"
                      " && github.ref == 'refs/heads/main'\n", found["promote"])

    def test_the_tests_do_not_keep_the_token(self):
        found = jobs(read(CI))
        self.assertRegex(found["test"], r"- uses: actions/checkout@\w+ # [\w.]+\n\s+with:\n\s+persist-credentials: false")
        # promote needs its credentials for the push, and all of main's history for the ancestry check
        self.assertNotIn("persist-credentials", found["promote"])
        self.assertIn("fetch-depth: 0", found["promote"])

    def test_pip_installs_only_hashed_wheels(self):
        installs = [line.strip() for line in read(CI).splitlines() if "pip install" in line and "#" not in line]
        self.assertEqual(installs, ["python -m pip install --disable-pip-version-check --require-hashes "
                                    "--only-binary :all: -r tests/requirements.txt"])
        entries = [e.strip() for e in re.sub(r"\\\n", " ", read(REQUIREMENTS)).splitlines()
                   if e.strip() and not e.strip().startswith("#")]
        names = []
        for entry in entries:
            self.assertRegex(entry, r"^[\w.-]+==[\w.]+( +--hash=sha256:[0-9a-f]{64})+$", entry[:60])
            names.append(entry.split("==")[0])
        self.assertIn("cryptography", names)


@unittest.skipUnless(shutil.which("bash") and shutil.which("git"), "needs bash and git")
class PromoteDeploy(unittest.TestCase):
    """promote's script against a stand-in for GitHub that refuses force pushes, as a ruleset with
    "Block force pushes" on deploy does: deploy only ever moves forward."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="stove-ci-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.origin = os.path.join(self.tmp, "origin.git")
        self.dev = os.path.join(self.tmp, "dev")
        subprocess.run(["git", "init", "-q", "--bare", "--initial-branch=main", self.origin], check=True, env=ENV)
        self.git(self.origin, "config", "receive.denyNonFastForwards", "true")
        subprocess.run(["git", "clone", "-q", self.origin, self.dev], check=True, env=ENV, stderr=subprocess.DEVNULL)

    def git(self, cwd, *args):
        return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True, text=True, env=ENV).stdout.strip()

    def commit(self, n):
        self.git(self.dev, "commit", "-q", "--allow-empty", "-m", str(n))
        self.git(self.dev, "push", "-q", "origin", "main")
        return self.git(self.dev, "rev-parse", "HEAD")

    def promote(self, commit):
        """One run of the job for COMMIT: a full checkout of it (like actions/checkout with
        fetch-depth: 0), then the script."""
        work = tempfile.mkdtemp(dir=self.tmp)
        subprocess.run(["git", "clone", "-q", self.origin, work], check=True, env=ENV)
        self.git(work, "checkout", "-q", "--detach", commit)
        p = subprocess.run(["bash", "-e", "-c", promote_script()], cwd=work, capture_output=True, text=True, env=ENV)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return p.stdout + p.stderr

    def deploy(self):
        return self.git(self.origin, "rev-parse", "deploy")

    def test_moves_only_forward(self):
        c1 = self.commit(1)
        self.promote(c1)                                   # the first run creates deploy
        self.assertEqual(self.deploy(), c1)
        c2 = self.commit(2)
        c3 = self.commit(3)
        self.promote(c3)                                   # a fast-forward: no force push needed
        self.assertEqual(self.deploy(), c3)
        out = self.promote(c2)                             # an older run that finished later
        self.assertIn("not moving it back", out)
        self.assertEqual(self.deploy(), c3)


if __name__ == "__main__":
    unittest.main()
