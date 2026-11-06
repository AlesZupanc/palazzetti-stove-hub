"""Minimal mDNS responder (RFC 6762 / 6763) that makes http://stove.local/ work.

Answers, on every IPv4 interface:

    stove.local                    A     address of the interface the query came in on
    stove.local                    NSEC  "there is no AAAA", so clients do not wait for IPv6
    _services._dns-sd._udp.local   PTR   _http._tcp.local
    _http._tcp.local               PTR   <service name>._http._tcp.local
    <service name>._http._tcp.local SRV  stove.local:<port>    TXT path=/

It binds UDP 5353 with SO_REUSEADDR/SO_REUSEPORT, so it runs happily next to
avahi-daemon (Raspberry Pi OS ships it). Standard library only; the interface
discovery uses Linux ioctls and falls back to the default route elsewhere.
"""

import errno
import logging
import select
import socket
import struct
import threading
import time

log = logging.getLogger("mdns")

MDNS_GROUP = "224.0.0.251"
MDNS_PORT = 5353

T_A, T_PTR, T_TXT, T_AAAA, T_SRV, T_NSEC, T_ANY = 1, 12, 16, 28, 33, 47, 255
C_IN = 1
CACHE_FLUSH = 0x8000
QU_BIT = 0x8000
HOST_TTL = 120
OTHER_TTL = 4500
LEGACY_TTL = 10

IP_PKTINFO = getattr(socket, "IP_PKTINFO", 8)  # Linux value
SIOCGIFFLAGS, SIOCGIFADDR = 0x8913, 0x8915
IFF_UP, IFF_LOOPBACK, IFF_MULTICAST = 0x1, 0x8, 0x1000

SERVICES = "_services._dns-sd._udp.local"


# --------------------------------------------------------------------------
# Wire format
# --------------------------------------------------------------------------
def encode_name(name):
    out = b""
    for label in name.rstrip(".").split("."):
        raw = label.encode("utf-8")
        if not 0 < len(raw) < 64:
            raise ValueError(f"bad DNS label {label!r}")
        out += bytes([len(raw)]) + raw
    return out + b"\x00"


def read_name(data, offset):
    """Decode a possibly compressed name. Returns (name, offset after the name)."""
    labels, end, jumps = [], None, 0
    while True:
        if offset >= len(data):
            raise ValueError("truncated name")
        n = data[offset]
        if n & 0xC0 == 0xC0:
            if offset + 1 >= len(data):
                raise ValueError("truncated pointer")
            if end is None:
                end = offset + 2
            offset = (n & 0x3F) << 8 | data[offset + 1]
            jumps += 1
            if jumps > 32:
                raise ValueError("compression loop")
            continue
        if n & 0xC0:
            raise ValueError("unsupported label type")
        offset += 1
        if n == 0:
            break
        if offset + n > len(data):
            raise ValueError("truncated label")
        labels.append(data[offset:offset + n].decode("utf-8", "replace"))
        offset += n
    return ".".join(labels), (end if end is not None else offset)


def parse_message(data):
    """Return (id, flags, questions, known_answers).

    questions:     [(name_lower, qtype, qclass)]
    known_answers: [(name_lower, rtype, ttl, rdata_offset, rdata_length)]
    """
    if len(data) < 12:
        raise ValueError("short packet")
    qid, flags, qdcount, ancount, _ns, _ar = struct.unpack("!6H", data[:12])
    off = 12
    questions = []
    for _ in range(qdcount):
        name, off = read_name(data, off)
        if off + 4 > len(data):
            raise ValueError("truncated question")
        qtype, qclass = struct.unpack("!HH", data[off:off + 4])
        off += 4
        questions.append((name.lower(), qtype, qclass))
    answers = []
    for _ in range(ancount):
        name, off = read_name(data, off)
        if off + 10 > len(data):
            raise ValueError("truncated answer")
        rtype, _rclass, ttl, rdlen = struct.unpack("!HHIH", data[off:off + 10])
        off += 10
        if off + rdlen > len(data):
            raise ValueError("truncated rdata")
        answers.append((name.lower(), rtype, ttl, off, rdlen))
        off += rdlen
    return qid, flags, questions, answers


def record(name, rtype, ttl, rdata, flush):
    rclass = C_IN | (CACHE_FLUSH if flush else 0)
    return encode_name(name) + struct.pack("!HHIH", rtype, rclass, ttl, len(rdata)) + rdata


def nsec_rdata(name, types):
    """NSEC rdata asserting that only `types` (all < 256) exist for name."""
    bitmap = bytearray(max(types) // 8 + 1)
    for t in types:
        bitmap[t // 8] |= 0x80 >> (t % 8)
    return encode_name(name) + bytes([0, len(bitmap)]) + bytes(bitmap)


def txt_rdata(items):
    out = b""
    for item in items:
        raw = item.encode("utf-8")[:255]
        out += bytes([len(raw)]) + raw
    return out or b"\x00"


# --------------------------------------------------------------------------
# Interfaces
# --------------------------------------------------------------------------
def ipv4_interfaces():
    """{ifname: ipv4} of interfaces that are up, not loopback and multicast capable (Linux)."""
    try:
        import fcntl
    except ImportError:
        return {}
    result = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for _index, name in socket.if_nameindex():
            req = struct.pack("16s24x", name.encode()[:15])
            try:
                flags = struct.unpack_from("H", fcntl.ioctl(s.fileno(), SIOCGIFFLAGS, req), 16)[0]
                if not flags & IFF_UP or flags & IFF_LOOPBACK or not flags & IFF_MULTICAST:
                    continue
                res = fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)
                result[name] = socket.inet_ntoa(res[20:24])
            except OSError:
                continue  # no IPv4 address
    except OSError:
        return {}
    finally:
        s.close()
    return result


def default_route_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET address: nothing is sent
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


# --------------------------------------------------------------------------
# Responder
# --------------------------------------------------------------------------
class MDNSResponder:
    def __init__(self, hostname="stove", port=80, service_name="Palazzetti stove", ip=None, path="/"):
        self.host = hostname.lower().rstrip(".") + ".local"
        self.port = port
        self.service = "_http._tcp.local"
        self.instance = f"{service_name}.{self.service}"
        self.instance_lc = self.instance.lower()
        self.fixed_ip = ip or None
        self.txt = txt_rdata([f"path={path}"])
        self.sock = None
        self.ifaces = {}
        self._stop = threading.Event()
        self._thread = None
        self._next_refresh = 0.0
        self._announce_queue = []

    # -- records ---------------------------------------------------------------
    def _a(self, ip, ttl, flush=True):
        return record(self.host, T_A, ttl, socket.inet_aton(ip), flush)

    def _host_nsec(self, ttl, flush=True):
        return record(self.host, T_NSEC, ttl, nsec_rdata(self.host, [T_A]), flush)

    def _srv(self, ttl, flush=True):
        return record(self.instance, T_SRV, ttl, struct.pack("!HHH", 0, 0, self.port) + encode_name(self.host),
                      flush)

    def _txt(self, ttl, flush=True):
        return record(self.instance, T_TXT, ttl, self.txt, flush)

    def _ptr(self, ttl):
        return record(self.service, T_PTR, ttl, encode_name(self.instance), False)

    def _services_ptr(self, ttl):
        return record(SERVICES, T_PTR, ttl, encode_name(self.service), False)

    def answer(self, data, sender, local_ip):
        """Build the reply for one received packet: (packet, destination) or None."""
        try:
            qid, flags, questions, known = parse_message(data)
        except ValueError:
            return None
        if flags & 0x8000 or flags & 0x7800:  # a response, or not a standard query
            return None
        legacy = sender[1] != MDNS_PORT  # one-shot resolver (dig -p 5353, ...)
        ip = self.fixed_ip or local_ip
        if not ip or (ip.startswith("127.") and not sender[0].startswith("127.")):
            ip = default_route_ip()
        if not ip:
            return None

        def ttl(t):
            return min(t, LEGACY_TTL) if legacy else t

        flush = not legacy
        known_set = set()
        for name, rtype, kttl, roff, rlen in known:
            try:
                if rtype == T_PTR:
                    known_set.add((name, rtype, read_name(data, roff)[0].lower(), kttl))
                elif rtype == T_A and rlen == 4:
                    known_set.add((name, rtype, socket.inet_ntoa(data[roff:roff + 4]), kttl))
            except ValueError:
                continue

        def is_known(name, rtype, value, our_ttl):
            return any(k[:3] == (name, rtype, value) and k[3] >= our_ttl // 2 for k in known_set)

        answers, additional, echoed = {}, {}, []
        unicast = legacy
        for name, qtype, qclass in questions:
            if qclass & 0x7FFF not in (C_IN, T_ANY):
                continue
            if qclass & QU_BIT:
                unicast = True
            matched = True
            if name == self.host:
                if qtype in (T_A, T_ANY) and not is_known(name, T_A, ip, HOST_TTL):
                    answers[self._a(ip, ttl(HOST_TTL), flush)] = 1
                    additional[self._host_nsec(ttl(HOST_TTL), flush)] = 1
                elif qtype == T_AAAA:
                    answers[self._host_nsec(ttl(HOST_TTL), flush)] = 1
                    additional[self._a(ip, ttl(HOST_TTL), flush)] = 1
                elif qtype not in (T_A, T_ANY):
                    matched = False
            elif name == SERVICES and qtype in (T_PTR, T_ANY):
                if not is_known(name, T_PTR, self.service, OTHER_TTL):
                    answers[self._services_ptr(ttl(OTHER_TTL))] = 1
            elif name == self.service and qtype in (T_PTR, T_ANY):
                if not is_known(name, T_PTR, self.instance_lc, OTHER_TTL):
                    answers[self._ptr(ttl(OTHER_TTL))] = 1
                    additional[self._srv(ttl(HOST_TTL), flush)] = 1
                    additional[self._txt(ttl(OTHER_TTL), flush)] = 1
                    additional[self._a(ip, ttl(HOST_TTL), flush)] = 1
            elif name == self.instance_lc and qtype in (T_SRV, T_TXT, T_ANY):
                if qtype in (T_SRV, T_ANY):
                    answers[self._srv(ttl(HOST_TTL), flush)] = 1
                if qtype in (T_TXT, T_ANY):
                    answers[self._txt(ttl(OTHER_TTL), flush)] = 1
                additional[self._a(ip, ttl(HOST_TTL), flush)] = 1
            else:
                matched = False
            if matched and legacy:
                echoed.append(encode_name(name) + struct.pack("!HH", qtype, qclass & 0x7FFF))
        if not answers:
            return None
        additional = [r for r in additional if r not in answers]
        header = struct.pack("!6H", qid if legacy else 0, 0x8400, len(echoed), len(answers), 0, len(additional))
        packet = header + b"".join(echoed) + b"".join(answers) + b"".join(additional)
        # A querier on this very host shares port 5353 with us, so a unicast
        # reply could land on our own socket: multicast it instead.
        if unicast and (legacy or sender[0] not in self.own_ips()):
            return packet, sender
        return packet, (MDNS_GROUP, MDNS_PORT)

    def own_ips(self):
        return set(self.ifaces.values()) | {"127.0.0.1"}

    def _announcement(self, ip, ttl_scale=1):
        records = [self._a(ip, HOST_TTL * ttl_scale), self._srv(HOST_TTL * ttl_scale),
                   self._txt(OTHER_TTL * ttl_scale), self._ptr(OTHER_TTL * ttl_scale),
                   self._services_ptr(OTHER_TTL * ttl_scale)]
        return struct.pack("!6H", 0, 0x8400, 0, len(records), 0, 0) + b"".join(records)

    # -- socket ------------------------------------------------------------------
    def start(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        s.bind(("", MDNS_PORT))
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
        try:
            s.setsockopt(socket.IPPROTO_IP, IP_PKTINFO, 1)
        except OSError:
            pass
        self.sock = s
        self._refresh_interfaces()
        self._thread = threading.Thread(target=self._run, name="mdns", daemon=True)
        self._thread.start()
        log.info("mDNS: answering for %s on %s", self.host,
                 ", ".join(f"{n}={ip}" for n, ip in self.ifaces.items()) or "default interface")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _join(self, ip):
        mreq = socket.inet_aton(MDNS_GROUP) + socket.inet_aton(ip)
        try:
            self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
        except OSError as e:
            if e.errno != errno.EADDRINUSE:  # EADDRINUSE: already a member there
                log.warning("mDNS: cannot join the multicast group on %s: %s", ip, e)
                return False
        return True

    def _refresh_interfaces(self):
        """Join the group on new interfaces/addresses and announce changes."""
        self._next_refresh = time.monotonic() + 30
        current = ipv4_interfaces()
        if not current:
            if not self.ifaces:
                self._join("0.0.0.0")
                ip = self.fixed_ip or default_route_ip()
                if ip:
                    self.ifaces = {"default": ip}
                    self._announce_queue.append(ip)
            return
        for name, ip in current.items():
            if self.ifaces.get(name) != ip and self._join(ip):
                log.info("mDNS: %s.local -> %s (%s)", self.host[:-6], self.fixed_ip or ip, name)
                self._announce_queue.append(ip)
        self.ifaces = current

    def _send_multicast(self, packet, via_ip):
        try:
            if via_ip and not via_ip.startswith("127."):
                self.sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(via_ip))
            self.sock.sendto(packet, (MDNS_GROUP, MDNS_PORT))
        except OSError as e:
            log.debug("mDNS send via %s failed: %s", via_ip, e)

    def _announce_pending(self):
        # RFC 6762 8.3: at least two unsolicited responses, one second apart.
        queue, self._announce_queue = self._announce_queue, []
        for _ in range(2):
            for ip in queue:
                self._send_multicast(self._announcement(self.fixed_ip or ip), ip)
            if self._stop.wait(1.0):
                break

    def _goodbye(self):
        for ip in self.ifaces.values():
            self._send_multicast(self._announcement(self.fixed_ip or ip, ttl_scale=0), ip)

    def _receive(self):
        if hasattr(self.sock, "recvmsg"):
            data, anc, _flags, sender = self.sock.recvmsg(9000, socket.CMSG_SPACE(12))
            local_ip = None
            for level, ctype, cdata in anc:
                if level == socket.IPPROTO_IP and ctype == IP_PKTINFO and len(cdata) >= 12:
                    local_ip = socket.inet_ntoa(cdata[4:8])  # in_pktinfo.ipi_spec_dst
            return data, sender, local_ip
        data, sender = self.sock.recvfrom(9000)
        return data, sender, None

    def _run(self):
        while not self._stop.is_set():
            if self._announce_queue:
                self._announce_pending()
            if time.monotonic() > self._next_refresh:
                self._refresh_interfaces()
            try:
                ready, _, _ = select.select([self.sock], [], [], 1.0)
                if not ready:
                    continue
                data, sender, local_ip = self._receive()
            except OSError as e:
                log.debug("mDNS receive failed: %s", e)
                continue
            try:
                reply = self.answer(data, sender, local_ip)
            except Exception:
                log.debug("mDNS: cannot answer packet from %s", sender, exc_info=True)
                continue
            if reply is None:
                continue
            packet, dest = reply
            try:
                if dest[0] == MDNS_GROUP:
                    self._send_multicast(packet, local_ip)
                else:
                    self.sock.sendto(packet, dest)
            except OSError as e:
                log.debug("mDNS reply to %s failed: %s", dest, e)
        self._goodbye()
        self.sock.close()
