"""WebSocket module: handshake, framing, fragmentation, keepalive, limits, interop with Node."""

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from helpers import ROOT

from stovemaster import ws


def make_server(**options):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if self.path != "/echo":
                body = b"nothing here"
                self.send_response(404)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            sock = ws.server_upgrade(self, **options)
            if sock is None:
                return
            server.sockets.append(sock)
            while True:
                msg = sock.recv()
                if msg is None:
                    break
                sock.send(msg)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    server.sockets = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def raw_upgrade(port, path="/echo", version="13"):
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: {version}\r\n\r\n").encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        buf += s.recv(1024)
    head, _, rest = buf.partition(b"\r\n\r\n")
    return s, head.decode(), rest


def frame(opcode, payload, fin=True, mask=True):
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    if n < 126:
        head.append((0x80 if mask else 0) | n)
    else:
        head.append((0x80 if mask else 0) | 126)
        head += struct.pack("!H", n)
    if mask:
        key = os.urandom(4)
        return bytes(head) + key + ws._mask(payload, key)
    return bytes(head) + payload


def read_frame(s):
    s.settimeout(5)
    b1, b2 = s.recv(2)
    n = b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", s.recv(2))[0]
    data = b""
    while len(data) < n:
        data += s.recv(n - len(data))
    return b1 & 0x0F, data


class Handshake(unittest.TestCase):
    def test_accept_key_rfc_example(self):
        self.assertEqual(ws.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_mask_roundtrip(self):
        data = os.urandom(1001)
        key = os.urandom(4)
        self.assertEqual(ws._mask(ws._mask(data, key), key), data)
        self.assertEqual(ws._mask(b"abcd", b"\x00\x00\x00\x00"), b"abcd")


class EchoServer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = make_server(max_message=300_000)
        cls.port = cls.server.server_address[1]
        cls.url = f"ws://127.0.0.1:{cls.port}/echo"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def test_text_binary_large_unicode(self):
        c = ws.connect(self.url)
        for msg in ("hello", "Žižek ☃ 🔥", b"\x00\x01\x02", "x" * 70_000, os.urandom(200_000)):
            c.send(msg)
            self.assertEqual(c.recv(), msg)
        c.close()
        self.assertTrue(c.closed)

    def test_fragmented_message_and_ping(self):
        s, head, _ = raw_upgrade(self.port)
        self.assertIn("101", head.split("\r\n")[0])
        s.sendall(frame(ws.OP_TEXT, b"hel", fin=False) + frame(ws.OP_PING, b"p1")
                  + frame(ws.OP_CONT, b"lo ", fin=False) + frame(ws.OP_CONT, b"world"))
        self.assertEqual(read_frame(s), (ws.OP_PONG, b"p1"))      # control frames may come in between
        self.assertEqual(read_frame(s), (ws.OP_TEXT, b"hello world"))
        s.sendall(frame(ws.OP_CLOSE, struct.pack("!H", 1000)))
        op, payload = read_frame(s)
        self.assertEqual((op, struct.unpack("!H", payload[:2])[0]), (ws.OP_CLOSE, 1000))
        s.close()

    def test_protocol_errors_close_the_connection(self):
        cases = [(frame(ws.OP_TEXT, b"unmasked", mask=False), 1002),
                 (frame(ws.OP_CONT, b"orphan"), 1002),
                 (frame(0x3, b"reserved opcode"), 1002),
                 (frame(ws.OP_TEXT, b"\xff\xfe"), 1007)]
        for data, code in cases:
            s, _, _ = raw_upgrade(self.port)
            s.sendall(data)
            op, payload = read_frame(s)
            self.assertEqual((op, struct.unpack("!H", payload[:2])[0]), (ws.OP_CLOSE, code), data)
            s.close()

    def test_too_big(self):
        c = ws.connect(self.url)
        c.send("y" * 300_001)
        self.assertIsNone(c.recv())
        self.assertEqual(c.close_code, ws.CLOSE_TOO_BIG)

    def test_empty_fragments_are_limited(self):
        s, _, _ = raw_upgrade(self.port)
        s.sendall(frame(ws.OP_TEXT, b"", fin=False) + frame(ws.OP_CONT, b"", fin=False) * (ws.MAX_FRAGMENTS + 5))
        op, payload = read_frame(s)
        self.assertEqual((op, struct.unpack("!H", payload[:2])[0]), (ws.OP_CLOSE, ws.CLOSE_TOO_BIG))
        s.close()

    def test_bad_requests(self):
        s, head, _ = raw_upgrade(self.port, version="8")
        self.assertIn("426", head.split("\r\n")[0])
        self.assertIn("Sec-WebSocket-Version: 13", head)
        s.close()
        with self.assertRaises(ws.HandshakeError) as ctx:
            ws.connect(f"ws://127.0.0.1:{self.port}/nope")
        self.assertEqual((ctx.exception.status, ctx.exception.body), (404, b"nothing here"))
        with self.assertRaises(ValueError):
            ws.connect("ftp://example.com/")

    def test_server_sees_close_from_client(self):
        before = len(self.server.sockets)
        c = ws.connect(self.url)
        c.send("hi")
        c.recv()
        server_side = self.server.sockets[before]
        c.close(ws.CLOSE_GOING_AWAY, "bye")
        deadline = time.time() + 3
        while not server_side.closed and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(server_side.closed)
        self.assertEqual((server_side.close_code, server_side.close_reason), (ws.CLOSE_GOING_AWAY, "bye"))


def tcp_pair():
    """Two connected TCP sockets with small buffers, so a peer that does not read blocks the writer soon."""
    lst = socket.socket()
    lst.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
    lst.bind(("127.0.0.1", 0))
    lst.listen(1)
    a = socket.socket()
    a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 16384)
    a.connect(lst.getsockname())
    b, _ = lst.accept()
    lst.close()
    return a, b


def thread_gone(name, timeout=3):
    deadline = time.time() + timeout
    while any(t.name == name for t in threading.enumerate()):
        if time.time() > deadline:
            return False
        time.sleep(0.02)
    return True


class SenderTests(unittest.TestCase):
    def test_messages_arrive_in_order(self):
        server = make_server()
        try:
            c = ws.connect(f"ws://127.0.0.1:{server.server_address[1]}/echo")
            sender = ws.Sender(c)
            for i in range(200):
                self.assertTrue(sender.send(f"m{i}"))
            self.assertEqual([c.recv() for _ in range(200)], [f"m{i}" for i in range(200)])
            sender.close()
            c.close()
        finally:
            server.shutdown()
            server.server_close()

    def test_a_peer_that_does_not_read_is_dropped_without_blocking(self):
        a, b = tcp_pair()
        sock = ws.WebSocket(a, is_client=False)
        sender = ws.Sender(sock, max_bytes=256 * 1024, name="test-writer-1")
        started = time.monotonic()
        results = [sender.send("x" * 32768) for _ in range(100)]    # 3 MB; b never reads
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertIn(False, results)
        self.assertTrue(sock.closed and sender.closed)
        self.assertIn("keep up", sock.error)
        self.assertFalse(sender.send("more"))
        self.assertTrue(thread_gone("test-writer-1"))                # the stuck writer was released too
        b.close()

    def test_close_does_not_wait_for_a_stuck_writer(self):
        a, b = tcp_pair()
        sock = ws.WebSocket(a, is_client=False)
        sender = ws.Sender(sock, max_bytes=8 << 20, name="test-writer-2")
        sender.send("y" * (2 << 20))              # far more than the buffers hold: the writer blocks
        time.sleep(0.3)
        started = time.monotonic()
        sock.close(ws.CLOSE_GOING_AWAY, "bye")
        self.assertLess(time.monotonic() - started, 2.0)
        self.assertTrue(sock.closed)
        self.assertTrue(thread_gone("test-writer-2"))
        b.close()


class Keepalive(unittest.TestCase):
    def test_silent_peer_is_dropped_live_peer_is_kept(self):
        server = make_server(ping_interval=0.2, idle_timeout=0.8)
        port = server.server_address[1]
        try:
            # a peer that never answers pings is dropped after idle_timeout
            s, _, _ = raw_upgrade(port)
            time.sleep(1.5)
            s.settimeout(2)
            got = b""
            try:
                while True:
                    chunk = s.recv(4096)
                    if not chunk:
                        break
                    got += chunk
            except OSError:
                pass
            self.assertIn(bytes([0x80 | ws.OP_PING]), got)   # it was pinged ...
            s.close()                                         # ... and then the server hung up
            # a real client answers the pings inside recv() and stays connected
            c = ws.connect(f"ws://127.0.0.1:{port}/echo")
            replies = []
            t = threading.Thread(target=lambda: replies.append(c.recv()), daemon=True)
            t.start()
            time.sleep(1.5)
            self.assertFalse(c.closed)
            c.send("still here")
            t.join(3)
            self.assertEqual(replies, ["still here"])
            c.close()
        finally:
            server.shutdown()
            server.server_close()


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class NodeInterop(unittest.TestCase):
    def test_node_websocket_client(self):
        server = make_server()
        port = server.server_address[1]
        script = r"""
        const ws = new WebSocket(process.argv[1]);
        const out = [];
        ws.onopen = () => { ws.send("hello from node"); ws.send("z".repeat(100000)); ws.send(new Uint8Array([1, 2, 3])); };
        ws.onmessage = async e => {
          out.push(typeof e.data === "string" ? e.data.slice(0, 20) + ":" + e.data.length : "bin:" + (await e.data.arrayBuffer()).byteLength);
          if (out.length === 3) { ws.close(1000, "done"); }
        };
        ws.onclose = e => { console.log(JSON.stringify({ out, code: e.code })); };
        ws.onerror = e => { console.log(JSON.stringify({ error: String(e.message || e) })); };
        """
        try:
            r = subprocess.run(["node", "-e", script, f"ws://127.0.0.1:{port}/echo"], capture_output=True,
                               text=True, timeout=30, cwd=ROOT)
            result = json.loads(r.stdout.strip().splitlines()[-1])
            self.assertEqual(result["out"], ["hello from node:15", "zzzzzzzzzzzzzzzzzzzz:100000", "bin:3"])
            self.assertEqual(result["code"], 1000)
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
