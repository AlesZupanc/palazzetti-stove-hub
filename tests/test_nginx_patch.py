import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

from helpers import ROOT

spec = importlib.util.spec_from_file_location("nginx_patch", os.path.join(ROOT, "www", "nginx_patch.py"))
np = importlib.util.module_from_spec(spec)
spec.loader.exec_module(np)

INCLUDE = "/etc/nginx/snippets/stove-cloud.conf"

SITE = r"""
# certbot-managed site with its own routing
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
upstream app { server 127.0.0.1:9001; }

server {
    server_name example.com www.example.com;
    root /var/www/html;
    set $x "${host}{weird}";   # braces inside quotes and ${var}
    location / { try_files $uri $uri/ =404; }
    location ~* \.(js|css)$ { expires 30d; }
    location ~ "^/re{2}x$" { return 204; }
    if ($request_method = POST) { set $p 1; }
    listen 443 ssl; # managed by Certbot
    ssl_certificate /etc/letsencrypt/live/example.com/fullchain.pem;
}

server {
    if ($host = example.com) { return 301 https://$host$request_uri; }
    listen 80;
    server_name example.com www.example.com;
    return 404;
}

server { listen 443 ssl; server_name other.org; location /mystove/ { return 200; } }
server { listen 8080; server_name plain.net; }
server { listen 443 quic reuseport; listen 443 ssl; server_name *.wild.dev; }
"""


def blocks_from(text, path="/etc/nginx/sites-enabled/site"):
    tree = np.parse(text)
    out = []
    for node, parents in np.walk(tree):
        if node.name == "server" and node.children is not None:
            kids = node.children
            listens = [c.args for c in kids if c.name == "listen"]
            out.append({"file": path, "real": path, "line": node.line,
                        "names": [a for c in kids if c.name == "server_name" for a in c.args],
                        "listen": [" ".join(a) for a in listens], "ssl": np.is_ssl(listens, kids),
                        "includes": [c.args[0] for c in kids if c.name == "include" and c.args],
                        "locations": [c.args for c in kids if c.name == "location" and c.children is not None],
                        "node": node})
    return out


class Parsing(unittest.TestCase):
    def test_tokenizer_edge_cases(self):
        tokens = [v for k, v, s, e in np.tokenize('set $a "${host}x"; return 200 \'a;b\'; # c { }\n')]
        self.assertEqual(tokens, ["set", "$a", "${host}x", ";", "return", "200", "a;b", ";"])
        tokens = [v for k, v, s, e in np.tokenize("rewrite ^/(.*)$ /${1}x break;")]
        self.assertEqual(tokens, ["rewrite", "^/(.*)$", "/${1}x", "break", ";"])

    def test_unbalanced(self):
        with self.assertRaises(np.ParseError):
            np.parse("server { listen 80;")
        with self.assertRaises(np.ParseError):
            np.parse("}")

    def test_blocks(self):
        blocks = blocks_from(SITE)
        self.assertEqual([b["names"][0] for b in blocks], ["example.com", "example.com", "other.org", "plain.net",
                                                         "*.wild.dev"])
        self.assertEqual([b["ssl"] for b in blocks], [True, False, True, False, True])


class Planning(unittest.TestCase):
    def setUp(self):
        self.blocks = blocks_from(SITE)

    def test_picks_https_block(self):
        p = np.plan(self.blocks, "example.com", "/mystove", INCLUDE)
        self.assertIsNone(p["error"])
        self.assertEqual([t["line"] for t in p["targets"]], [6])

    def test_conflict(self):
        p = np.plan(self.blocks, "other.org", "/mystove", INCLUDE)
        self.assertIn("already has location", p["error"])
        p = np.plan(self.blocks, "other.org", "/stove2", INCLUDE)
        self.assertIsNone(p["error"])

    def test_http_only(self):
        p = np.plan(self.blocks, "plain.net", "/mystove", INCLUDE)
        self.assertTrue(p["http_only"])
        p = np.plan(self.blocks, "plain.net", "/mystove", INCLUDE, allow_http=True)
        self.assertIsNone(p["error"])

    def test_wildcard_and_unknown(self):
        self.assertIsNone(np.plan(self.blocks, "stove.wild.dev", "/mystove", INCLUDE)["error"])
        self.assertIn("no nginx server block", np.plan(self.blocks, "nope.com", "/mystove", INCLUDE)["error"])

    def test_already_included(self):
        patched = np.insert_include(SITE, [self.blocks[0]["node"]], INCLUDE)
        p = np.plan(blocks_from(patched), "example.com", "/mystove", INCLUDE)
        self.assertEqual((p["targets"], len(p["already"])), ([], 1))


class Editing(unittest.TestCase):
    def test_insert_and_remove(self):
        blocks = blocks_from(SITE)
        patched = np.insert_include(SITE, [blocks[0]["node"]], INCLUDE)
        self.assertIn(f"    {np.MARKER}\n    include {INCLUDE};\n}}", patched)
        self.assertEqual(patched.count("include"), 1)
        self.assertEqual(np.remove_include(patched, INCLUDE), SITE)

    def test_insert_when_brace_shares_a_line(self):
        text = "server { listen 443 ssl; server_name a.b; location / { return 200; } }\n"
        node = blocks_from(text)[0]["node"]
        patched = np.insert_include(text, [node], INCLUDE)
        self.assertIn(f"include {INCLUDE};\n}}", patched)
        np.parse(patched)  # still balanced

    def test_backup_restore(self):
        folder = tempfile.mkdtemp()
        try:
            path = os.path.join(folder, "site.conf")
            with open(path, "w") as f:
                f.write("original\n")
            os.chmod(path, 0o640)
            manifest, _ = np.backup([path], os.path.join(folder, "backups"))
            np.write_file(path, "changed\n")
            self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o640")   # mode kept
            np.restore(manifest)
            with open(path) as f:
                self.assertEqual(f.read(), "original\n")
        finally:
            shutil.rmtree(folder)


@unittest.skipUnless(shutil.which("nginx"), "nginx not installed")
class RealNginx(unittest.TestCase):
    """Patch a throw-away nginx config and let nginx itself validate it."""

    def test_snippet_passes_nginx_t(self):
        folder = tempfile.mkdtemp()
        try:
            for d in ("sites", "snippets", "logs", "tmp"):
                os.makedirs(os.path.join(folder, d))
            snippet = os.path.join(folder, "snippets", "stove-cloud.conf")
            with open(os.path.join(ROOT, "www", "nginx-stove-cloud.conf.in")) as f:
                text = f.read()
            for k, v in {"@DOMAIN@": "example.com", "@PREFIX@": "/mystove", "@PORT@": "8787",
                         "@INSTALL_DIR@": folder, "@SNIPPET@": snippet}.items():
                text = text.replace(k, v)
            with open(snippet, "w") as f:
                f.write(text)
            site = os.path.join(folder, "sites", "example.conf")
            with open(site, "w") as f:
                f.write("server {\n    listen 127.0.0.1:18443;\n    server_name example.com;\n"
                        "    location ~* \\.(js|css)$ { expires 30d; }\n}\n")
            conf = os.path.join(folder, "nginx.conf")
            # Own temp paths: "nginx -t" run as root would otherwise chown the system nginx's
            # temp directories to the user of this test config.
            temps = "".join(f"    {kind}_temp_path {folder}/tmp/{kind};\n"
                            for kind in ("client_body", "proxy", "fastcgi", "uwsgi", "scgi"))
            with open(conf, "w") as f:
                f.write(f"pid {folder}/nginx.pid;\nerror_log {folder}/logs/error.log;\n"
                        f"events {{}}\nhttp {{\n    access_log off;\n{temps}"
                        f"    include {folder}/sites/*.conf;\n}}\n")
            args = ["--nginx-arg=-p", f"--nginx-arg={folder}/", "--nginx-arg=-c", f"--nginx-arg={conf}"]
            rc = np.main(["apply", "--domain", "example.com", "--prefix", "/mystove", "--include", snippet,
                          "--backup-dir", os.path.join(folder, "backups"), "--allow-http", *args])
            self.assertEqual(rc, 0)
            with open(site) as f:
                self.assertIn(f"include {snippet};", f.read())
            out = subprocess.run(["nginx", "-t", "-p", folder + "/", "-c", conf],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, universal_newlines=True)
            self.assertEqual(out.returncode, 0, out.stdout)
            rc = np.main(["remove", "--include", snippet, "--backup-dir", os.path.join(folder, "backups"), *args])
            self.assertEqual(rc, 0)
            with open(site) as f:
                self.assertNotIn("include", f.read())
        finally:
            shutil.rmtree(folder)


if __name__ == "__main__":
    unittest.main()
