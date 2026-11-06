"""WebSocket protocol (RFC 6455): server upgrade, client connect, framing.

Standard library only. Used by the Raspberry Pi hub (ws:// on the home network,
wss:// to the VPS) and by the VPS server. www/stovecloud/ws.py is a
byte-identical copy (a test checks it), because www/ is deployed on its own.

One thread reads (recv), any thread may send: sends are serialised by a lock.
recv() also keeps the connection alive: it pings when the line is quiet and
gives up when nothing at all has arrived for idle_timeout seconds. Sender
queues outgoing messages for a writer thread, so a peer that stops reading
never blocks anybody else (and is dropped once too much is waiting).
"""

import base64
import collections
import hashlib
import os
import select
import socket
import ssl
import struct
import threading
import time
import urllib.parse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

CLOSE_NORMAL = 1000
CLOSE_GOING_AWAY = 1001
CLOSE_PROTOCOL = 1002
CLOSE_BAD_DATA = 1007
CLOSE_POLICY = 1008
CLOSE_TOO_BIG = 1009
CLOSE_ERROR = 1011

MAX_FRAGMENTS = 1024          # pieces of one message (browsers send our messages in one)
FRAME_OVERHEAD = 14           # counted per frame, so empty fragments still use up the size limit


class WebSocketError(Exception):
    """Protocol violation; code is the close code sent to the peer."""

    def __init__(self, message, code=CLOSE_PROTOCOL):
        super().__init__(message)
        self.code = code


class HandshakeError(Exception):
    """The server did not switch to WebSocket; status and body tell why."""

    def __init__(self, status, reason, body=b""):
        super().__init__(f"HTTP {status} {reason}".strip())
        self.status = status
        self.reason = reason
        self.body = body


def accept_key(key):
    return base64.b64encode(hashlib.sha1((key + GUID).encode("ascii")).digest()).decode("ascii")


def _mask(data, key):
    if not data:
        return data
    n = len(data)
    pad = (key * (n // 4 + 1))[:n]
    return (int.from_bytes(data, "big") ^ int.from_bytes(pad, "big")).to_bytes(n, "big")


class WebSocket:
    def __init__(self, sock, is_client, initial=b"", max_message=1 << 20, ping_interval=25.0,
                 idle_timeout=75.0, io_timeout=30.0):
        self.sock = sock
        self.is_client = is_client
        self.max_message = max_message
        self.ping_interval = ping_interval
        self.idle_timeout = idle_timeout
        self.closed = False
        self.close_code = None      # what the peer sent when it closed
        self.close_reason = ""
        self.error = ""             # why we closed (protocol error, timeout, ...)
        self._buf = bytearray(initial)
        self._send_lock = threading.Lock()
        self._close_sent = False
        self._last_rx = time.monotonic()
        sock.settimeout(io_timeout)   # a frame that stalls half-way for this long ends the connection

    # -- reading -------------------------------------------------------------
    def _fill(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("connection closed by the peer")
            self._buf += chunk

    def _take(self, n):
        self._fill(n)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _wait_readable(self):
        """Block until a frame starts arriving; ping while the line is quiet."""
        while True:
            if self._buf or (isinstance(self.sock, ssl.SSLSocket) and self.sock.pending()):
                return
            quiet = time.monotonic() - self._last_rx
            if quiet > self.idle_timeout:
                raise TimeoutError(f"nothing received for {int(quiet)} s")
            wait = max(0.5, min(self.ping_interval, self.idle_timeout - quiet))
            ready, _, _ = select.select([self.sock], [], [], wait)
            if ready:
                return
            if time.monotonic() - self._last_rx >= self.ping_interval:
                self.ping()

    def _read_frame(self):
        b1, b2 = self._take(2)
        fin, opcode = bool(b1 & 0x80), b1 & 0x0F
        if b1 & 0x70:
            raise WebSocketError("reserved bits set")
        masked, length = bool(b2 & 0x80), b2 & 0x7F
        if length == 126:
            length = struct.unpack("!H", self._take(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self._take(8))[0]
        if masked == self.is_client:
            raise WebSocketError("frames from a client must be masked, from a server not")
        if opcode >= 0x8 and (length > 125 or not fin):
            raise WebSocketError("invalid control frame")
        if length > self.max_message:
            raise WebSocketError("message too big", CLOSE_TOO_BIG)
        key = self._take(4) if masked else None
        payload = self._take(length)
        if key:
            payload = _mask(payload, key)
        return fin, opcode, payload

    def recv(self):
        """Next message: str (text) or bytes (binary); None once the connection is closed.

        Protocol errors close the connection (with the right close code) and also
        return None; self.error tells what went wrong.
        """
        try:
            return self._recv()
        except WebSocketError as e:
            self.error = str(e)
            self.close(e.code, str(e))
        except (OSError, ValueError) as e:   # includes timeouts and a socket closed by close()
            if not self.closed:
                self.error = str(e) or e.__class__.__name__
            self._drop()
        return None

    def _recv(self):
        parts, kind, size = [], None, 0
        while not self.closed:
            self._wait_readable()
            fin, opcode, payload = self._read_frame()
            self._last_rx = time.monotonic()
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self._on_close(payload)
                return None
            if opcode == OP_CONT:
                if kind is None:
                    raise WebSocketError("unexpected continuation frame")
            elif opcode in (OP_TEXT, OP_BINARY):
                if kind is not None:
                    raise WebSocketError("new message inside a fragmented one")
                kind = opcode
            else:
                raise WebSocketError(f"unknown opcode {opcode}")
            size += len(payload) + FRAME_OVERHEAD
            if size > self.max_message + FRAME_OVERHEAD or len(parts) >= MAX_FRAGMENTS:
                raise WebSocketError("message too big", CLOSE_TOO_BIG)
            parts.append(payload)
            if fin:
                data = b"".join(parts)
                if kind == OP_BINARY:
                    return data
                try:
                    return data.decode("utf-8")
                except UnicodeDecodeError:
                    raise WebSocketError("invalid UTF-8", CLOSE_BAD_DATA) from None
        return None

    def _on_close(self, payload):
        code, reason = CLOSE_GOING_AWAY, ""
        if len(payload) >= 2:
            code = struct.unpack("!H", payload[:2])[0]
            reason = payload[2:].decode("utf-8", "replace")
        self.close_code, self.close_reason = code, reason
        self.close(code if code in (CLOSE_NORMAL, CLOSE_GOING_AWAY) else CLOSE_NORMAL)

    # -- writing -------------------------------------------------------------
    def _frame(self, opcode, payload, fin=True):
        n = len(payload)
        head = bytearray([(0x80 if fin else 0) | opcode])
        bit = 0x80 if self.is_client else 0
        if n < 126:
            head.append(bit | n)
        elif n < 65536:
            head.append(bit | 126)
            head += struct.pack("!H", n)
        else:
            head.append(bit | 127)
            head += struct.pack("!Q", n)
        if self.is_client:
            key = os.urandom(4)
            head += key
            payload = _mask(payload, key)
        return bytes(head) + payload

    def _send_frame(self, opcode, payload, fin=True):
        frame = self._frame(opcode, payload, fin)
        with self._send_lock:
            if self._close_sent:
                raise ConnectionError("WebSocket is closed")
            self.sock.sendall(frame)

    def send(self, message):
        """Send a str as a text message or bytes as a binary message."""
        if isinstance(message, str):
            self._send_frame(OP_TEXT, message.encode("utf-8"))
        else:
            self._send_frame(OP_BINARY, bytes(message))

    def ping(self, payload=b""):
        self._send_frame(OP_PING, payload)

    def close(self, code=CLOSE_NORMAL, reason=""):
        """Send a close frame and drop the connection. Never waits long: when a
        send is stuck (the peer does not read), the close frame is skipped."""
        if self.closed:
            return
        frame = self._frame(OP_CLOSE, struct.pack("!H", code) + reason.encode("utf-8")[:120])
        if self._send_lock.acquire(timeout=1.0):
            try:
                _, writable, _ = select.select([], [self.sock], [], 0)
                if writable and not self._close_sent:
                    self._close_sent = True
                    self.sock.sendall(frame)
            except (OSError, ValueError):
                pass
            finally:
                self._send_lock.release()
        self._drop()

    def abort(self, error):
        """Drop the connection at once, without the closing handshake."""
        if not self.closed:
            self.error = error
        self._drop()

    def _drop(self):
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


class Sender:
    """Sends text messages from a queue in its own thread.

    send() never blocks: when more than max_bytes are waiting (the peer stopped
    reading), the connection is closed instead and send() returns False.
    """

    def __init__(self, sock, max_bytes=4 << 20, name="ws-writer"):
        self.sock = sock
        self.max_bytes = max_bytes
        self.closed = False
        self._q = collections.deque()
        self._bytes = 0
        self._cond = threading.Condition()
        threading.Thread(target=self._run, name=name, daemon=True).start()

    @property
    def pending(self):
        """Bytes waiting to be sent."""
        return self._bytes

    def send(self, text):
        with self._cond:
            if self.closed:
                return False
            if self._bytes + len(text) <= self.max_bytes:
                self._q.append(text)
                self._bytes += len(text)
                self._cond.notify()
                return True
            self.closed = True
            self._q.clear()
            self._cond.notify()
        self.sock.abort("too much unsent data - the other side does not keep up")
        return False

    def close(self):
        with self._cond:
            self.closed = True
            self._q.clear()
            self._cond.notify()

    def _run(self):
        while True:
            with self._cond:
                while not self._q and not self.closed:
                    self._cond.wait()
                if self.closed:
                    return
                text = self._q.popleft()
                self._bytes -= len(text)
            try:
                self.sock.send(text)
            except (OSError, ValueError):
                self.close()
                self.sock.close()
                return


# --------------------------------------------------------------------------
# server side
# --------------------------------------------------------------------------
def is_upgrade(headers):
    return (headers.get("Upgrade", "").lower() == "websocket"
            and "upgrade" in headers.get("Connection", "").lower())


def server_upgrade(handler, **options):
    """Answer a BaseHTTPRequestHandler's upgrade request with 101 and return the WebSocket.

    On a malformed request an error is sent and None is returned.
    """
    h = handler.headers
    key = h.get("Sec-WebSocket-Key", "").strip()
    try:
        valid_key = len(base64.b64decode(key, validate=True)) == 16
    except ValueError:
        valid_key = False
    if handler.command != "GET" or not is_upgrade(h) or not valid_key:
        _plain_error(handler, 400, "bad WebSocket request")
        return None
    if h.get("Sec-WebSocket-Version", "").strip() != "13":
        _plain_error(handler, 426, "WebSocket version 13 only", [("Sec-WebSocket-Version", "13")])
        return None
    handler.send_response(101, "Switching Protocols")
    handler.send_header("Upgrade", "websocket")
    handler.send_header("Connection", "Upgrade")
    handler.send_header("Sec-WebSocket-Accept", accept_key(key))
    handler.end_headers()
    handler.close_connection = True
    return WebSocket(handler.connection, is_client=False, **options)


def _plain_error(handler, status, text, headers=()):
    body = text.encode("utf-8")
    handler.send_response(status)
    for k, v in headers:
        handler.send_header(k, v)
    handler.send_header("Content-Type", "text/plain; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)
    handler.close_connection = True


# --------------------------------------------------------------------------
# client side
# --------------------------------------------------------------------------
def connect(url, headers=(), timeout=15.0, ssl_context=None, **options):
    """Open ws:// or wss:// (http/https are accepted too). Raises HandshakeError or OSError."""
    u = urllib.parse.urlsplit(url)
    secure = u.scheme in ("wss", "https")
    if u.scheme not in ("ws", "wss", "http", "https") or not u.hostname:
        raise ValueError(f"not a WebSocket URL: {url}")
    port = u.port or (443 if secure else 80)
    sock = socket.create_connection((u.hostname, port), timeout=timeout)
    try:
        if secure:
            ctx = ssl_context or ssl.create_default_context()
            sock = ctx.wrap_socket(sock, server_hostname=u.hostname)
        host = u.hostname if ":" not in u.hostname else f"[{u.hostname}]"
        if u.port and u.port != (443 if secure else 80):
            host += f":{u.port}"
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        target = (u.path or "/") + (f"?{u.query}" if u.query else "")
        lines = [f"GET {target} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines += [f"{k}: {v}" for k, v in headers]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("connection closed during the WebSocket handshake")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("WebSocket handshake answer too long")
        head, _, rest = buf.partition(b"\r\n\r\n")
        status_line, *header_lines = head.decode("latin-1").split("\r\n")
        parts = status_line.split(" ", 2)
        status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        got = {}
        for line in header_lines:
            k, _, v = line.partition(":")
            got[k.strip().lower()] = v.strip()
        if status != 101:
            body = rest
            length = int(got.get("content-length") or 0)
            while len(body) < min(length, 4096):
                chunk = sock.recv(4096)
                if not chunk:
                    break
                body += chunk
            raise HandshakeError(status, parts[2] if len(parts) > 2 else "", body[:4096])
        if got.get("sec-websocket-accept") != accept_key(key) or got.get("upgrade", "").lower() != "websocket":
            raise HandshakeError(status, "invalid WebSocket handshake answer")
        return WebSocket(sock, is_client=True, initial=rest, **options)
    except BaseException:
        sock.close()
        raise
