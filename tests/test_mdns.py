import socket
import struct
import unittest

import helpers  # noqa: F401  (sets sys.path)

from stovemaster import mdns
from stovemaster.mdns import (MDNS_GROUP, MDNS_PORT, T_A, T_AAAA, T_ANY, T_NSEC, T_PTR, T_SRV, T_TXT,
                              MDNSResponder, encode_name, nsec_rdata, parse_message, read_name)


def query(name, qtype, qclass=1, qid=0, known=b"", ancount=0):
    return (struct.pack("!6H", qid, 0, 1, ancount, 0, 0) + encode_name(name) + struct.pack("!HH", qtype, qclass)
            + known)


def records(packet):
    """[(name, type, class, ttl, rdata_offset, rdata_len)] of all answer + additional records."""
    _qid, _flags, qd, an, ns, ar = struct.unpack("!6H", packet[:12])
    off = 12
    for _ in range(qd):
        _, off = read_name(packet, off)
        off += 4
    out = []
    for _ in range(an + ns + ar):
        name, off = read_name(packet, off)
        rtype, rclass, ttl, rdlen = struct.unpack("!HHIH", packet[off:off + 10])
        off += 10
        out.append((name, rtype, rclass, ttl, off, rdlen))
        off += rdlen
    return out


class Wire(unittest.TestCase):
    def test_name_compression(self):
        pkt = b"\x00" * 12 + encode_name("stove.local") + b"\x05_http\x04_tcp\xc0\x12"
        self.assertEqual(read_name(pkt, 12), ("stove.local", 25))
        name, end = read_name(pkt, 25)
        self.assertEqual((name, end), ("_http._tcp.local", 38))

    def test_pointer_loop_is_rejected(self):
        with self.assertRaises(ValueError):
            read_name(b"\x00" * 12 + b"\xc0\x0c", 12)

    def test_nsec_bitmap(self):
        self.assertEqual(nsec_rdata("a.local", [T_A])[-3:], b"\x00\x01\x40")
        self.assertEqual(nsec_rdata("a.local", [T_TXT, T_SRV])[-7:], b"\x00\x05\x00\x00\x80\x00\x40")

    def test_parse_rejects_garbage(self):
        with self.assertRaises(ValueError):
            parse_message(b"\x00\x01")


class Answers(unittest.TestCase):
    def setUp(self):
        self.r = MDNSResponder("stove", 80, "Palazzetti stove")
        self.r.ifaces = {"wlan0": "192.168.1.50"}
        self.peer = ("192.168.1.20", MDNS_PORT)

    def test_a_query_multicast(self):
        pkt, dest = self.r.answer(query("stove.local", T_A), self.peer, "192.168.1.50")
        self.assertEqual(dest, (MDNS_GROUP, MDNS_PORT))
        recs = records(pkt)
        a = [r for r in recs if r[1] == T_A][0]
        self.assertEqual(socket.inet_ntoa(pkt[a[4]:a[4] + 4]), "192.168.1.50")
        self.assertEqual(a[2], 0x8001)  # cache-flush + IN
        self.assertTrue(any(r[1] == T_NSEC for r in recs))

    def test_uses_the_interface_the_query_came_in_on(self):
        pkt, _ = self.r.answer(query("STOVE.local", T_A), ("10.0.0.7", MDNS_PORT), "10.0.0.2")
        a = [r for r in records(pkt) if r[1] == T_A][0]
        self.assertEqual(socket.inet_ntoa(pkt[a[4]:a[4] + 4]), "10.0.0.2")

    def test_legacy_unicast(self):
        pkt, dest = self.r.answer(query("stove.local", T_A, qid=4242), ("192.168.1.20", 40000), "192.168.1.50")
        self.assertEqual(dest, ("192.168.1.20", 40000))
        qid, flags, qd, _, _, _ = struct.unpack("!6H", pkt[:12])
        self.assertEqual((qid, flags, qd), (4242, 0x8400, 1))
        a = [r for r in records(pkt) if r[1] == T_A][0]
        self.assertLessEqual(a[3], 10)          # short TTL
        self.assertEqual(a[2], 1)                # no cache-flush bit

    def test_qu_bit_unicast(self):
        _, dest = self.r.answer(query("stove.local", T_A, qclass=0x8001), self.peer, "192.168.1.50")
        self.assertEqual(dest, self.peer)

    def test_aaaa_gets_nsec(self):
        pkt, _ = self.r.answer(query("stove.local", T_AAAA), self.peer, "192.168.1.50")
        _, _, _, an, _, _ = struct.unpack("!6H", pkt[:12])
        self.assertEqual(records(pkt)[0][1], T_NSEC)
        self.assertEqual(an, 1)

    def test_service_discovery(self):
        pkt, _ = self.r.answer(query("_http._tcp.local", T_PTR), self.peer, "192.168.1.50")
        types = [r[1] for r in records(pkt)]
        self.assertEqual(types[0], T_PTR)
        self.assertIn(T_SRV, types)
        self.assertIn(T_TXT, types)
        srv = [r for r in records(pkt) if r[1] == T_SRV][0]
        self.assertEqual(struct.unpack("!HHH", pkt[srv[4]:srv[4] + 6])[2], 80)
        pkt, _ = self.r.answer(query("_services._dns-sd._udp.local", T_PTR), self.peer, "192.168.1.50")
        self.assertEqual(records(pkt)[0][1], T_PTR)
        pkt, _ = self.r.answer(query("Palazzetti stove._http._tcp.local", T_ANY), self.peer, "192.168.1.50")
        self.assertEqual({r[1] for r in records(pkt)}, {T_SRV, T_TXT, T_A})

    def test_ignores_other_names_and_responses(self):
        self.assertIsNone(self.r.answer(query("printer.local", T_A), self.peer, "192.168.1.50"))
        response = bytearray(query("stove.local", T_A))
        response[2] = 0x84
        self.assertIsNone(self.r.answer(bytes(response), self.peer, "192.168.1.50"))

    def test_known_answer_suppression(self):
        known = encode_name("_http._tcp.local") + struct.pack("!HHIH", T_PTR, 1, 4500, 0)
        rdata = encode_name("Palazzetti stove._http._tcp.local")
        known = known[:-2] + struct.pack("!H", len(rdata)) + rdata
        pkt = query("_http._tcp.local", T_PTR, known=known, ancount=1)
        self.assertIsNone(self.r.answer(pkt, self.peer, "192.168.1.50"))

    def test_never_multicasts_loopback(self):
        old = mdns.default_route_ip
        mdns.default_route_ip = lambda: "192.168.1.50"
        try:
            pkt, _ = self.r.answer(query("stove.local", T_A), self.peer, "127.0.0.1")
        finally:
            mdns.default_route_ip = old
        a = [r for r in records(pkt) if r[1] == T_A][0]
        self.assertEqual(socket.inet_ntoa(pkt[a[4]:a[4] + 4]), "192.168.1.50")


if __name__ == "__main__":
    unittest.main()
