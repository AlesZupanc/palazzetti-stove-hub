"""www/mail_dns.py: SPF evaluation and merging, DKIM/DMARC/PTR checks, the records it suggests."""

import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest

from helpers import ROOT

sys.path.insert(0, os.path.join(ROOT, "www"))

import mail_dns  # noqa: E402

IP = "203.0.113.7"
KEY = "MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAtest" + "A" * 300
DKIM = f"v=DKIM1; h=sha256; k=rsa; p={KEY}"


class FakeDNS:
    def __init__(self, records):
        self.records = records

    def lookup(self, name, rtype):
        value = self.records.get((name.rstrip("."), rtype), [])
        if isinstance(value, Exception):
            raise value
        return list(value)


def zone(extra=None):
    """A domain where everything is in place; extra overrides (name, type) entries."""
    records = {
        ("example.com", "TXT"): ["google-site-verification=abc", f"v=spf1 ip4:{IP} ~all"],
        ("example.com", "A"): [IP],
        ("stove._domainkey.example.com", "TXT"): [DKIM],
        ("_dmarc.example.com", "TXT"): ["v=DMARC1; p=none"],
        ("7.113.0.203.in-addr.arpa", "PTR"): ["example.com"],
    }
    records.update(extra or {})
    return FakeDNS(records)


def run(dns, hostname="example.com"):
    return mail_dns.check(dns, "example.com", IP, hostname, "stove", DKIM, mail_dns.Report())


class TxtParsing(unittest.TestCase):
    def test_quoted_strings_are_joined(self):
        self.assertEqual(mail_dns.txt_value('"v=DKIM1; k=rsa; " "p=AB" "CD"'), "v=DKIM1; k=rsa; p=ABCD")
        self.assertEqual(mail_dns.txt_value('"say \\"hi\\"\\059"'), 'say "hi";')
        self.assertEqual(mail_dns.txt_value("plain"), "plain")

    def test_opendkim_genkey_file(self):
        text = ('stove._domainkey\tIN\tTXT\t( "v=DKIM1; h=sha256; k=rsa; "\n\t  "p=MIIBIjANBg"\n'
                '\t  "kqhkiG9w0B" )  ; ----- DKIM key stove for example.com\n')
        self.assertEqual(mail_dns.txt_value(text), "v=DKIM1; h=sha256; k=rsa; p=MIIBIjANBgkqhkiG9w0B")
        self.assertEqual(mail_dns.tags("v=DKIM1; p=AB CD\tEF")["p"], "ABCDEF")


class Spf(unittest.TestCase):
    def allows(self, record, records=None):
        return mail_dns.spf_allows(FakeDNS(records or {}), record, "example.com", IP)

    def test_mechanisms(self):
        self.assertTrue(self.allows(f"v=spf1 ip4:{IP} -all"))
        self.assertTrue(self.allows("v=spf1 ip4:203.0.113.0/24 -all"))
        self.assertFalse(self.allows("v=spf1 ip4:203.0.114.0/24 ~all"))
        self.assertFalse(self.allows(f"v=spf1 -ip4:{IP} +all"))                     # first match wins
        self.assertTrue(self.allows("v=spf1 a -all", {("example.com", "A"): [IP]}))
        self.assertTrue(self.allows("v=spf1 a:web.example.com/28 -all", {("web.example.com", "A"): ["203.0.113.1"]}))
        self.assertTrue(self.allows("v=spf1 mx -all", {("example.com", "MX"): ["mx.example.com"],
                                                        ("mx.example.com", "A"): [IP]}))
        self.assertTrue(self.allows("v=spf1 include:_spf.host.net ~all",
                                    {("_spf.host.net", "TXT"): [f"v=spf1 ip4:{IP} -all"]}))
        self.assertFalse(self.allows("v=spf1 include:_spf.google.com ~all",
                                     {("_spf.google.com", "TXT"): ["v=spf1 ip4:64.233.160.0/19 ~all"]}))
        self.assertTrue(self.allows("v=spf1 redirect=_spf.example.net",
                                    {("_spf.example.net", "TXT"): [f"v=spf1 ip4:{IP} -all"]}))
        self.assertFalse(self.allows("v=spf1 ~all"))
        self.assertFalse(self.allows("v=spf1 include:loop.example.com", {("loop.example.com", "TXT"):
                                                                          ["v=spf1 include:loop.example.com"]}))

    def test_merging_keeps_the_other_senders(self):
        self.assertEqual(mail_dns.spf_with_ip("v=spf1 include:_spf.google.com ~all", IP),
                         f"v=spf1 ip4:{IP} include:_spf.google.com ~all")


class Check(unittest.TestCase):
    def test_all_in_place(self):
        report = run(zone())
        self.assertEqual((report.problems, report.records), (0, []), report.lines)

    def test_new_domain_gets_every_record(self):
        report = run(zone({("example.com", "TXT"): [], ("stove._domainkey.example.com", "TXT"): [],
                             ("_dmarc.example.com", "TXT"): [], ("7.113.0.203.in-addr.arpa", "PTR"): []}))
        self.assertEqual(report.problems, 4)
        self.assertEqual([(r[0], r[1], r[2]) for r in report.records], [
            ("PTR", IP, "example.com"),
            ("TXT", "example.com", f"v=spf1 ip4:{IP} ~all"),
            ("TXT", "stove._domainkey.example.com", DKIM),
            ("TXT", "_dmarc.example.com", "v=DMARC1; p=none")])

    def test_existing_spf_is_extended_not_replaced(self):
        report = run(zone({("example.com", "TXT"): ["v=spf1 include:_spf.mx.example.net -all"],
                             ("_spf.mx.example.net", "TXT"): ["v=spf1 ip4:198.51.100.0/24 -all"]}))
        self.assertEqual(report.records, [("TXT", "example.com", f"v=spf1 ip4:{IP} include:_spf.mx.example.net -all",
                                           "SPF: change the existing record to this")])

    def test_two_spf_records_are_an_error(self):
        report = run(zone({("example.com", "TXT"): [f"v=spf1 ip4:{IP} ~all", "v=spf1 mx ~all"]}))
        self.assertEqual(report.problems, 1)
        self.assertIn("replace ALL", report.records[0][3])

    def test_old_dkim_key_and_ptr_that_does_not_point_back(self):
        report = run(zone({("stove._domainkey.example.com", "TXT"): ["v=DKIM1; k=rsa; p=OLDKEY"],
                             ("7.113.0.203.in-addr.arpa", "PTR"): ["vps-7.host.example.net"]}))
        self.assertEqual(report.problems, 2)
        self.assertTrue(any("does not point back" in l for l in report.lines), report.lines)
        self.assertTrue(any("a different key" in l for l in report.lines), report.lines)

    def test_generic_ptr_that_points_back_is_fine(self):
        report = run(zone({("7.113.0.203.in-addr.arpa", "PTR"): ["vps-7.host.example.net"],
                             ("vps-7.host.example.net", "A"): [IP]}), hostname="vps-7.host.example.net")
        self.assertEqual(report.problems, 0, report.lines)

    def test_mail_name_must_point_to_the_server(self):
        report = run(zone({("example.com", "A"): ["104.16.1.1"]}))     # e.g. behind a CDN
        self.assertIn(("A", "example.com", IP), [r[:3] for r in report.records])
        report = run(zone(), hostname="mail.example.com")
        self.assertIn(("A", "mail.example.com", IP), [r[:3] for r in report.records])

    def test_before_the_first_setup_dkim_is_left_out(self):
        dns = zone({("stove._domainkey.example.com", "TXT"): []})
        report = mail_dns.check(dns, "example.com", IP, "example.com", "stove", None, mail_dns.Report())
        self.assertEqual((report.problems, report.records), (0, []), report.lines)
        self.assertTrue(any("once the mail server is set up" in l for l in report.lines))

    def test_lookup_failures_are_reported(self):
        report = run(zone({("_dmarc.example.com", "TXT"): mail_dns.DNSError("timed out")}))
        self.assertEqual(report.problems, 1)
        self.assertTrue(any("lookup failed" in l for l in report.lines))


class Reachable(unittest.TestCase):
    def serve(self, greeting):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.listen(5)

        def run():
            while True:
                try:
                    c, _ = s.accept()
                except OSError:
                    return
                if greeting:
                    c.sendall(greeting)
                self.addCleanup(c.close)
        threading.Thread(target=run, daemon=True).start()
        self.addCleanup(s.close)
        return s.getsockname()[1]

    def test_mail_server_greeting(self):
        port = self.serve(b"220 mx.example.com ESMTP\r\n")
        self.assertEqual(mail_dns.smtp_reachable(port, ["127.0.0.1"]), (True, "127.0.0.1: 220 mx.example.com ESMTP"))

    def test_silent_wrong_or_closed(self):
        ok, text = mail_dns.smtp_reachable(self.serve(b""), ["127.0.0.1"], timeout=0.5)   # e.g. a filter
        self.assertFalse(ok)
        ok, text = mail_dns.smtp_reachable(self.serve(b"554 go away\r\n"), ["127.0.0.1"])
        self.assertEqual((ok, text), (False, "127.0.0.1 answered: 554 go away"))
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        ok, text = mail_dns.smtp_reachable(port, ["127.0.0.1", "127.0.0.1"], timeout=1)
        self.assertFalse(ok)
        self.assertIn("refused", text.lower())


class LocalFirewall(unittest.TestCase):
    """mail-server.sh: is outgoing port 25 blocked by this server's own firewall, or by the provider?"""

    UFW_ALLOW = "Status: active\nLogging: on (low)\nDefault: deny (incoming), allow (outgoing), disabled (routed)\n"
    UFW_DENY = "Status: active\nDefault: deny (incoming), deny (outgoing), disabled (routed)\n"
    UFW_RULE = "Status: active\n\nTo                         Action      From\n--                         ------      ----\n" \
               "22/tcp                     ALLOW IN    Anywhere\n25/tcp                     DENY OUT    Anywhere\n"

    def run_check(self, ufw=None, iptables=""):
        with open(os.path.join(ROOT, "www", "mail-server.sh")) as f:
            func = re.search(r"^local_firewall\(\) \{.*?^\}", f.read(), re.S | re.M).group(0)
        folder = tempfile.mkdtemp()
        tools = {"iptables": f"cat <<'EOF'\n{iptables}EOF\n"}
        if ufw is not None:
            tools["ufw"] = f"cat <<'EOF'\n{ufw}EOF\n"
        for name, body in tools.items():
            path = os.path.join(folder, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n" + body)
            os.chmod(path, 0o755)
        env = dict(os.environ, PATH=folder + ":/usr/bin:/bin")
        r = subprocess.run(["bash", "-c", func + "\nlocal_firewall"], stdout=subprocess.PIPE, universal_newlines=True,
                           env=env, timeout=30)
        return r.stdout.strip()

    def test_cases(self):
        self.assertEqual(self.run_check(ufw=self.UFW_ALLOW), "ok-ufw")          # Ubuntu's default
        self.assertIn("sudo ufw allow out 25/tcp", self.run_check(ufw=self.UFW_DENY))
        self.assertIn("a ufw rule blocks", self.run_check(ufw=self.UFW_RULE))
        self.assertEqual(self.run_check(ufw="Status: inactive\n"), "ok")       # then iptables decides
        self.assertEqual(self.run_check(iptables="-P OUTPUT ACCEPT\n"), "ok")
        self.assertIn("drops outgoing", self.run_check(iptables="-P OUTPUT DROP\n"))
        self.assertIn("rule blocks outgoing port 25",
                      self.run_check(iptables="-P OUTPUT ACCEPT\n-A OUTPUT -p tcp -m tcp --dport 25 -j REJECT\n"))
        self.assertEqual(self.run_check(iptables="-P OUTPUT ACCEPT\n-A OUTPUT -p tcp --dport 2525 -j DROP\n"), "ok")


FAKE_DIG = """#!%s
import json, sys
args = sys.argv[1:]
server = next((a[1:] for a in args if a.startswith("@")), "local")
name, rtype = [a for a in args if a[0] not in "+@"][:2]
with open(%r) as f:
    table = json.load(f)
with open(%r, "a") as f:
    f.write(server + " " + name + " " + rtype + "\\n")
for key in (server + " " + name.rstrip(".") + " " + rtype, server + " *"):
    if key in table:
        if table[key] == "TIMEOUT":
            print(";; connection timed out; no servers could be reached")
            sys.exit(9)
        print("\\n".join(table[key]))
        break
"""


def fake_dig(table):
    """A dig that answers from table: {"SERVER NAME TYPE": [lines] or "TIMEOUT", "SERVER *": ...};
    SERVER is an address or "local" (this server's own resolver); anything else has no records."""
    folder = tempfile.mkdtemp()
    data, log, path = (os.path.join(folder, n) for n in ("table.json", "calls", "dig"))
    with open(data, "w") as f:
        json.dump(table, f)
    with open(path, "w") as f:
        f.write(FAKE_DIG % (sys.executable, data, log))
    os.chmod(path, 0o755)
    return path, log


class PublicDNS(unittest.TestCase):
    """Ubuntu's systemd-resolved names the server's own IP after its host name ("ubuntu");
    the internet does not. The checks must see what other mail servers see."""

    REV = "7.113.0.203.in-addr.arpa"

    def test_the_local_resolvers_made_up_reverse_dns_is_not_used(self):
        dig, log = fake_dig({f"local {self.REV} PTR": ["ubuntu."], "local ubuntu A": [IP]})
        r = mail_dns.Resolver(zone="example.com", dig=dig)
        self.assertEqual(mail_dns.ptr_name(r, IP), "")
        report = mail_dns.check(r, "example.com", IP, "example.com", "stove", None, mail_dns.Report())
        self.assertTrue(any("reverse DNS (PTR) of 203.0.113.7: none" in l for l in report.lines), report.lines)
        with open(log) as f:
            self.assertNotIn(f"local {self.REV} PTR", f.read())      # public resolvers answered

    def test_without_public_resolvers_a_bare_host_name_is_still_ignored(self):
        dig, _ = fake_dig({"1.1.1.1 *": "TIMEOUT", "8.8.8.8 *": "TIMEOUT",
                           f"local {self.REV} PTR": ["ubuntu."], "local ubuntu A": [IP]})
        r = mail_dns.Resolver(dig=dig)
        self.assertEqual(mail_dns.ptr_name(r, IP), "")
        self.assertTrue(r.used_local)

    def test_a_new_reverse_dns_is_seen_at_the_providers_name_servers(self):
        dig, log = fake_dig({"1.1.1.1 0.203.in-addr.arpa NS": ["ns1.provider.example."],
                             "1.1.1.1 ns1.provider.example A": ["192.0.2.99"],
                             f"192.0.2.99 {self.REV} PTR": ["mail.example.com."],
                             f"1.1.1.1 {self.REV} PTR": [],              # still cached as "none"
                             "1.1.1.1 mail.example.com A": [IP]})
        r = mail_dns.Resolver(dig=dig)
        self.assertEqual(mail_dns.ptr_name(r, IP), "mail.example.com")
        with open(log) as f:
            calls = f.read()
        self.assertIn("1.1.1.1 113.0.203.in-addr.arpa NS", calls)          # /24 first, then /16
        self.assertNotIn(f"1.1.1.1 {self.REV} PTR", calls)


class Resolver(unittest.TestCase):
    def test_dig_output_and_authoritative_first(self):
        folder = tempfile.mkdtemp()
        log = os.path.join(folder, "calls")
        fake = os.path.join(folder, "dig")
        with open(fake, "w") as f:
            f.write(f"""#!/bin/sh
echo "$*" >> {log}
case "$*" in
  *"example.com NS") echo "ns1.dns.example.net." ;;
  *"ns1.dns.example.net A") echo "192.0.2.53" ;;
  *"@192.0.2.53 +norecurse example.com TXT") echo '"v=spf1 " "ip4:{IP} ~all"'; echo '";; not an error line"' ;;
  *"@192.0.2.53 +norecurse _dmarc.example.com TXT") ;;
  *"_dmarc.example.com TXT") echo '"v=DMARC1; p=none"' ;;
  *"down.example.org A") echo ";; connection timed out; no servers could be reached"; exit 9 ;;
esac
""")
        os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
        r = mail_dns.Resolver(zone="example.com", dig=fake)
        self.assertEqual(r.lookup("example.com", "TXT"), [f"v=spf1 ip4:{IP} ~all", ";; not an error line"])
        self.assertEqual(r.lookup("_dmarc.example.com", "TXT"), ["v=DMARC1; p=none"])   # falls back
        with self.assertRaises(mail_dns.DNSError) as ctx:
            r.lookup("down.example.org", "A")
        self.assertIn("timed out", str(ctx.exception))
        with open(log) as f:
            calls = f.read()
        self.assertEqual(calls.count("example.com NS"), 1)            # name servers looked up once


if __name__ == "__main__":
    unittest.main()
