#!/usr/bin/env python3
"""mail_dns.py - DNS checks for the VPS's own mail server (used by mail-server.sh).

  mail_dns.py public-ip                 this server's public IPv4 address
  mail_dns.py port25                    can this server reach other mail servers (outgoing port 25)?
  mail_dns.py port587                   can it reach e-mail providers' submission port (Gmail, ...)?
  mail_dns.py ptr IP                    the reverse DNS name of IP, if it points back to IP
  mail_dns.py a NAME                    the IPv4 addresses of NAME in DNS (not /etc/hosts)
  mail_dns.py dkim-value FILE           the TXT value from the .txt file of opendkim-genkey
  mail_dns.py check --domain D --ip IP --hostname H --selector S [--dkim FILE]
                                        check SPF, DKIM, DMARC and reverse DNS; print what to add
                                        (without --dkim the DKIM record is not checked yet)

Names are looked up with dig (package dnsutils), the way other mail servers see
them: at public resolvers, not this server's own (on Ubuntu, systemd-resolved
answers from /etc/hosts and names this server's own IP after its host name, e.g.
"ubuntu"). Records of the domain and the reverse DNS of the IP are asked from
their own name servers first, so a record added a minute ago is seen at once
(resolvers may remember "no such record" for a while).
Exit code of "check": 0 when everything needed is in place, 1 otherwise.
"""

import argparse
import ipaddress
import re
import socket
import subprocess
import sys
import urllib.request

MX_PROBE_HOSTS = ("gmail-smtp-in.l.google.com", "alt1.gmail-smtp-in.l.google.com")
PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")
HOSTNAME_RE = re.compile(r"^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$")
SUBMISSION_PROBE_HOSTS = ("smtp.gmail.com", "smtp-mail.outlook.com")


class DNSError(Exception):
    pass


def txt_value(text):
    """'"part one" "part two"' (dig output or zone file) -> 'part onepart two'."""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', text)
    if not parts:
        return text.strip()
    out = "".join(parts)
    out = re.sub(r"\\(\d{3})", lambda m: chr(int(m.group(1))), out)
    return re.sub(r"\\(.)", r"\1", out)


def tags(record):
    """'v=DKIM1; k=rsa; p=AB CD' -> {'v': 'DKIM1', 'k': 'rsa', 'p': 'ABCD'}"""
    out = {}
    for item in record.split(";"):
        if "=" in item:
            k, v = item.split("=", 1)
            out[k.strip().lower()] = re.sub(r"\s+", "", v) if k.strip().lower() == "p" else v.strip()
    return out


class Resolver:
    """dig wrapper: lookup(name, type) -> list of strings (TXT values joined).

    Asks the name's own name servers, then public resolvers; this server's own
    resolver only when no public one answers (then used_local is set)."""

    def __init__(self, zone=None, dig="dig", resolvers=PUBLIC_RESOLVERS):
        self.dig = dig
        self.zone = zone
        self.resolvers = list(resolvers) + [None]      # None: this server's own resolver
        self.used_local = False
        self._auth = {}
        self._dead = set()

    def _run(self, args):
        try:
            p = subprocess.run([self.dig, "+short", "+time=3", "+tries=2"] + args,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=30)
        except FileNotFoundError:
            raise DNSError("dig is not installed (apt install dnsutils)") from None
        except subprocess.TimeoutExpired:
            raise DNSError("DNS lookup timed out") from None
        lines = [l.strip() for l in p.stdout.splitlines() if l.strip()]
        if p.returncode != 0:      # 9: no answer at all (blocked or down); NXDOMAIN is 0 with no lines
            raise DNSError(" ".join(l.lstrip("; ") for l in lines if l.startswith(";")) or p.stderr.strip()
                           or f"dig failed ({p.returncode})")
        return [l for l in lines if not l.startswith(";")]

    def _recursive(self, args):
        error = None
        for server in self.resolvers:
            if server in self._dead:
                continue
            try:
                lines = self._run((["@" + server] if server else []) + args)
            except DNSError as e:
                if server:
                    self._dead.add(server)          # unreachable: do not wait for it again
                error = e
                continue
            if server is None:
                self.used_local = True
            return lines
        raise error

    def _authoritative(self, zone):
        """An address of one of the zone's name servers (or None)."""
        if zone not in self._auth:
            self._auth[zone] = None
            try:
                for ns in self._recursive([zone, "NS"]):
                    addrs = [a for a in self._recursive([ns.rstrip("."), "A"]) if _is_ipv4(a)]
                    if addrs:
                        self._auth[zone] = addrs[0]
                        break
            except DNSError:
                pass
        return self._auth[zone]

    def _server_for(self, name):
        if self.zone and (name == self.zone or name.endswith("." + self.zone)):
            return self._authoritative(self.zone)
        if name.endswith(".in-addr.arpa"):         # reverse DNS: the IP owner's name servers
            labels = name.split(".")
            for i in range(1, len(labels) - 3):
                server = self._authoritative(".".join(labels[i:]))
                if server:
                    return server
        return None

    def lookup(self, name, rtype):
        name = name.rstrip(".")
        args = [name, rtype]
        server = self._server_for(name)
        lines = []
        if server:
            try:
                lines = self._run(["@" + server, "+norecurse"] + args)
            except DNSError:
                lines = []
        if not lines:              # also for names the zone delegates elsewhere
            lines = self._recursive(args)
        if rtype == "TXT":
            return [txt_value(l) for l in lines]
        if rtype == "A":
            return [l for l in lines if _is_ipv4(l)]
        if rtype == "MX":
            return [l.split()[-1].rstrip(".") for l in lines if len(l.split()) == 2]
        return [l.rstrip(".") for l in lines]


def _is_ipv4(text):
    try:
        return isinstance(ipaddress.ip_address(text), ipaddress.IPv4Address)
    except ValueError:
        return False


# --------------------------------------------------------------------------- SPF
def spf_records(resolver, domain):
    return [t for t in resolver.lookup(domain, "TXT") if t.lower().split()[:1] == ["v=spf1"]]


def spf_allows(resolver, record, domain, ip, depth=0):
    """Does this SPF record let ip send for domain? (enough of RFC 7208 for the usual records)"""
    if depth > 8:
        return False
    addr = ipaddress.ip_address(ip)
    redirect = None
    for term in record.split()[1:]:
        if "=" in term and ":" not in term.split("=", 1)[0]:
            if term.lower().startswith("redirect="):
                redirect = term.split("=", 1)[1]
            continue
        qualifier = term[0] if term[0] in "+-~?" else "+"
        mech = term[1:] if term[0] in "+-~?" else term
        name, _, arg = mech.partition(":")
        name, _, cidr = name.partition("/")
        arg, _, cidr2 = arg.partition("/")
        cidr = cidr or cidr2
        name = name.lower()
        try:
            if name == "all":
                return qualifier == "+"
            if name == "ip4":
                hit = addr in ipaddress.ip_network(f"{arg}/{cidr or 32}", strict=False)
            elif name in ("a", "mx"):
                target = arg or domain
                hosts = [target] if name == "a" else resolver.lookup(target, "MX")
                prefix = int(cidr or 32)
                hit = any(addr in ipaddress.ip_network(f"{a}/{prefix}", strict=False)
                          for h in hosts for a in resolver.lookup(h, "A"))
            elif name == "include":
                hit = any(spf_allows(resolver, r, arg, ip, depth + 1) for r in spf_records(resolver, arg))
            else:                       # ip6, ptr, exists: not used for this server
                continue
        except (ValueError, DNSError):
            continue
        if hit:
            return qualifier == "+"
    if redirect:
        return any(spf_allows(resolver, r, redirect, ip, depth + 1) for r in spf_records(resolver, redirect))
    return False


def spf_with_ip(record, ip):
    """Add ip4:IP to an existing SPF record, right after v=spf1 (so it is matched before any -all)."""
    words = record.split()
    return " ".join([words[0], f"ip4:{ip}"] + words[1:])


# ------------------------------------------------------------------------ report
class Report:
    def __init__(self):
        self.lines = []
        self.records = []        # (type, name, value, why)
        self.problems = 0

    def ok(self, text):
        self.lines.append("  ok   " + text)

    def bad(self, text):
        self.problems += 1
        self.lines.append("  TODO " + text)

    def failed(self, text):
        self.problems += 1
        self.lines.append("  ??   " + text)

    def note(self, text):
        self.lines.append("       " + text)

    def add(self, rtype, name, value, why):
        self.records.append((rtype, name, value, why))


def check(resolver, domain, ip, hostname, selector, dkim_value, report):
    # --- reverse DNS: the first thing big mail providers check
    try:
        ptr = ptr_names(resolver, ip)
    except DNSError as e:
        ptr = None
        report.failed(f"reverse DNS of {ip}: lookup failed ({e})")
    if ptr is not None:
        good = [n for n in ptr if ip in _safe(resolver.lookup, n, "A")]
        if not ptr:
            report.bad(f"reverse DNS (PTR) of {ip}: none")
        elif not good:
            report.bad(f"reverse DNS of {ip} is {ptr[0]}, but {ptr[0]} does not point back to {ip}")
        elif hostname.lower() not in (n.lower() for n in good):
            report.ok(f"reverse DNS of {ip}: {good[0]}")
            report.note(f"(the mail server calls itself {hostname}; re-run mail-server.sh to use {good[0]})")
        else:
            report.ok(f"reverse DNS of {ip}: {hostname}")
        if not good:
            report.note(f"Set it in your VPS provider's control panel (\"reverse DNS\" / \"PTR\" of {ip}) to {hostname}")
            report.add("PTR", ip, hostname, "at your VPS provider, not at your DNS provider")
    # --- the server's own name must point to it
    addrs = _safe(resolver.lookup, hostname, "A")
    if ip in addrs:
        report.ok(f"{hostname} points to {ip}")
    else:
        report.bad(f"{hostname} does not point to {ip}" + (f" (it points to {', '.join(addrs)})" if addrs else ""))
        report.add("A", hostname, ip, "the mail server's name")
    # --- SPF
    try:
        spf = spf_records(resolver, domain)
    except DNSError as e:
        spf = None
        report.failed(f"SPF of {domain}: lookup failed ({e})")
    if spf is not None:
        if not spf:
            report.bad(f"SPF: {domain} has no SPF record")
            report.add("TXT", domain, f"v=spf1 ip4:{ip} ~all", "SPF: this server may send mail for the domain")
        elif len(spf) > 1:
            report.bad(f"SPF: {domain} has {len(spf)} SPF records - receivers ignore them all; keep one")
            report.add("TXT", domain, spf_with_ip(spf[0], ip), "SPF: replace ALL SPF records with this one")
        elif spf_allows(resolver, spf[0], domain, ip):
            report.ok(f"SPF allows {ip}: {spf[0]}")
        else:
            report.bad(f"SPF of {domain} does not include {ip}: {spf[0]}")
            report.add("TXT", domain, spf_with_ip(spf[0], ip), "SPF: change the existing record to this")
    # --- DKIM
    name = f"{selector}._domainkey.{domain}"
    want = tags(dkim_value).get("p") if dkim_value else None
    found = None
    if not dkim_value:
        report.note(f"(DKIM {name}: its value is shown once the mail server is set up)")
    else:
        try:
            found = [t for t in resolver.lookup(name, "TXT") if "p=" in t]
        except DNSError as e:
            report.failed(f"DKIM {name}: lookup failed ({e})")
    if found is not None:
        if any(tags(t).get("p") == want for t in found):
            report.ok(f"DKIM key {name}")
        else:
            report.bad(f"DKIM key {name}: " + ("a different key is published" if found else "not published"))
            report.add("TXT", name, dkim_value, "DKIM: the public key of the signature")
    # --- DMARC
    try:
        dmarc = [t for t in resolver.lookup(f"_dmarc.{domain}", "TXT") if t.upper().startswith("V=DMARC1")]
    except DNSError as e:
        dmarc = None
        report.failed(f"DMARC: lookup failed ({e})")
    if dmarc is not None:
        if dmarc:
            report.ok(f"DMARC: {dmarc[0]}")
        else:
            report.bad(f"DMARC: _dmarc.{domain} has no record")
            report.add("TXT", f"_dmarc.{domain}", "v=DMARC1; p=none", "DMARC (p=none: only asks receivers to check)")
    return report


def _safe(fn, *args):
    try:
        return fn(*args)
    except DNSError:
        return []


def print_report(report, domain):
    print("\n".join(report.lines))
    dns = [r for r in report.records if r[0] != "PTR"]
    if dns:
        print(f"\nAdd these records at the DNS provider of {domain}:")
        for rtype, name, value, why in dns:
            short = name[: -len(domain) - 1] if name.endswith("." + domain) else ("@" if name == domain else name)
            print(f"\n  {why}")
            print(f"    Type:  {rtype}")
            print(f"    Name:  {short}    ({name})")
            print(f"    Value: {value}")
        if any(len(v) > 255 for _, _, v, _ in dns):
            print("\n  (A value longer than 255 characters is fine for most DNS providers; if yours"
                  " refuses it, split it into several quoted strings of up to 255 characters.)")
    for rtype, name, value, why in report.records:
        if rtype == "PTR":
            print(f"\nReverse DNS (PTR) of {name} -> {value}: {why}.")


# ------------------------------------------------------------------- the network
def public_ip():
    """(address, how): the local address towards the internet, or ask a web service when behind NAT."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 53))         # no packet is sent; this only picks the route
        local = s.getsockname()[0]
    finally:
        s.close()
    if ipaddress.ip_address(local).is_global:
        return local, "local address"
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=8) as r:
            found = r.read(64).decode().strip()
        if _is_ipv4(found):
            return found, f"this server is behind NAT ({local}); asked api.ipify.org"
    except OSError:
        pass
    raise SystemExit(f"could not find this server's public IPv4 address (local address {local}) - pass --ip")


def smtp_reachable(port, hosts, timeout=10):
    """(ok, text): connect to a big mail server on this port (IPv4) and read its greeting."""
    last = ""
    for host in hosts:
        try:
            addr = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
            with socket.create_connection(addr, timeout=timeout) as s:
                s.settimeout(timeout)
                banner = s.recv(512).decode("utf-8", "replace").strip()
                try:
                    s.sendall(b"QUIT\r\n")
                except OSError:
                    pass
            if banner.startswith("220"):
                return True, f"{host}: {banner.splitlines()[0][:80]}"
            last = f"{host} answered: {banner[:80] or '(nothing)'}"
        except OSError as e:
            last = f"{host}: {e or e.__class__.__name__}"
    return False, last


def ptr_names(resolver, ip):
    """The reverse DNS names of ip that are real host names (not e.g. "ubuntu")."""
    names = [n.rstrip(".").lower() for n in resolver.lookup(ipaddress.ip_address(ip).reverse_pointer, "PTR")]
    return [n for n in names if HOSTNAME_RE.match(n)]


def ptr_name(resolver, ip):
    """The reverse DNS name of ip if it resolves back to ip (forward-confirmed), else ''."""
    for name in _safe(ptr_names, resolver, ip):
        if ip in _safe(resolver.lookup, name, "A"):
            return name
    return ""


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("public-ip")
    sub.add_parser("port25")
    sub.add_parser("port587")
    p = sub.add_parser("ptr")
    p.add_argument("ip")
    a = sub.add_parser("a")
    a.add_argument("name")
    d = sub.add_parser("dkim-value")
    d.add_argument("file")
    c = sub.add_parser("check")
    c.add_argument("--domain", required=True)
    c.add_argument("--ip", required=True)
    c.add_argument("--hostname", required=True)
    c.add_argument("--selector", default="stove")
    c.add_argument("--dkim", help="the .txt file of opendkim-genkey")
    args = ap.parse_args(argv)
    if args.cmd == "public-ip":
        ip, how = public_ip()
        print(ip)
        print(how, file=sys.stderr)
    elif args.cmd in ("port25", "port587"):
        ok, text = (smtp_reachable(25, MX_PROBE_HOSTS) if args.cmd == "port25"
                    else smtp_reachable(587, SUBMISSION_PROBE_HOSTS))
        print(text)
        return 0 if ok else 1
    elif args.cmd == "ptr":
        name = ptr_name(Resolver(), args.ip)
        print(name)
        return 0 if name else 1
    elif args.cmd == "a":
        addrs = _safe(Resolver().lookup, args.name, "A")
        print("\n".join(addrs))
        return 0 if addrs else 1
    elif args.cmd == "dkim-value":
        with open(args.file) as f:
            print(txt_value(f.read()))
    elif args.cmd == "check":
        value = None
        if args.dkim:
            with open(args.dkim) as f:
                value = txt_value(f.read())
        domain = args.domain.lower().rstrip(".")
        resolver = Resolver(zone=domain)
        report = check(resolver, domain, args.ip, args.hostname.lower(), args.selector, value, Report())
        if resolver.used_local:
            report.note("(no public DNS resolver answered, so these answers come from this server's own")
            report.note(" resolver, which may know names the internet does not)")
        print_report(report, domain)
        return 1 if report.problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
