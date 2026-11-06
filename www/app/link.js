"use strict";
/* Connections to a stove hub. Every screen talks to Stove.link the same way:
     await Stove.link.call("stove.action", {action: "on"})
     onLink("status", s => ...)            (events pushed by the hub)
   At home the link is a plain ws:// connection to the Pi (LanLink); from outside
   it is an end-to-end encrypted session through the VPS (RemoteLink, crypto.js).
   See docs/protocol.md. */

class RpcError extends Error {
  constructor(message, code) { super(message); this.code = code || 0; }
}

class Link {
  constructor() {
    this.handlers = {};
    this.pending = new Map();
    this.nextId = 1;
    this.state = "connecting";
    this.stateText = "Connecting…";
    this.hello = null;
    this.closedForGood = false;
  }

  on(event, fn) {
    (this.handlers[event] = this.handlers[event] || new Set()).add(fn);
    return () => this.handlers[event].delete(fn);
  }

  emit(event, data) {
    for (const fn of [...(this.handlers[event] || [])]) {
      try { fn(data); } catch (e) { console.error(e); }
    }
  }

  setState(state, text) {
    this.state = state;
    this.stateText = text;
    this.emit("state", { state, text });
  }

  /* Request -> Promise of the result. Waits for the connection for a while. */
  call(method, params, timeout) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new RpcError(this.state === "open" ? "The stove hub did not answer in time." : this.offlineText(), 0));
      }, timeout || 25000);
      this.pending.set(id, { resolve, reject, timer, msg: { id, m: method, p: params || {} }, sent: false });
      if (this.state === "open") this.flush();
    });
  }

  offlineText() { return this.stateText || "Not connected to the stove."; }

  flush() {
    for (const p of this.pending.values()) {
      if (!p.sent) { p.sent = true; this.transmit(p.msg); }
    }
  }

  deliver(msg) {
    if (msg && msg.id != null && this.pending.has(msg.id)) {
      const p = this.pending.get(msg.id);
      this.pending.delete(msg.id);
      clearTimeout(p.timer);
      if ("e" in msg) p.reject(new RpcError(msg.e, msg.code));
      else p.resolve(msg.r);
    } else if (msg && msg.ev) {
      if (msg.ev === "hello") this.hello = msg.d;
      this.emit(msg.ev, msg.d);
    }
  }

  /* Requests sent on a connection that died never get an answer: send them again on the next one. */
  resetSent() {
    for (const p of this.pending.values()) p.sent = false;
  }

  failAll(text) {
    for (const [id, p] of this.pending) {
      clearTimeout(p.timer);
      p.reject(new RpcError(text, 0));
      this.pending.delete(id);
    }
  }
}

/* Reconnecting WebSocket with back-off; subclasses handle messages. */
class SocketLink extends Link {
  constructor(url) {
    super();
    this.url = url;
    this.ws = null;
    this.delay = 1000;
    this.timer = null;
    document.addEventListener("visibilitychange", () => {
      // phones freeze background tabs; reconnect at once when the app comes back
      if (document.visibilityState === "visible" && !this.closedForGood && this.state !== "open"
          && (!this.ws || this.ws.readyState > 1)) {
        clearTimeout(this.timer);
        this.connect();
      }
    });
  }

  connect() {
    if (this.closedForGood) return;
    clearTimeout(this.timer);
    let sock;
    try {
      sock = new WebSocket(this.url);
    } catch (e) {
      this.retry("Cannot connect: " + e.message);
      return;
    }
    this.ws = sock;
    sock.onopen = () => this.opened(sock);
    sock.onmessage = e => { if (sock === this.ws) this.received(e.data); };
    sock.onclose = e => { if (sock === this.ws) this.lost(e); };
  }

  retry(text) {
    if (this.closedForGood) return;
    this.setState("offline", text);
    this.resetSent();
    this.timer = setTimeout(() => this.connect(), this.delay);
    this.delay = Math.min(this.delay * 2, 15000);
  }

  close() {
    this.closedForGood = true;
    clearTimeout(this.timer);
    if (this.ws) { const s = this.ws; this.ws = null; s.close(); }
    this.failAll("Connection closed.");
  }

  sendRaw(obj) {
    if (this.ws && this.ws.readyState === 1) this.ws.send(JSON.stringify(obj));
  }
}

/* At home: ws://stove.local/ws, plain JSON. */
class LanLink extends SocketLink {
  constructor() {
    super((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    this.connect();
  }

  opened() {
    this.delay = 1000;
    this.setState("open", "Live");
    this.flush();
  }

  received(data) {
    let msg;
    try { msg = JSON.parse(data); } catch (e) { return; }
    this.deliver(msg);
  }

  lost() { this.retry("The stove hub is not reachable."); }

  transmit(msg) { this.sendRaw(msg); }
}

/* From outside: wss://example.com/mystove/ws/client?dev=ID, end-to-end encrypted.
   psk: Uint8Array (token secret when pairing, the link key afterwards). */
class RemoteLink extends SocketLink {
  constructor(deviceId, mode, linkId, psk) {
    const u = new URL("ws/client?dev=" + encodeURIComponent(deviceId), location.href);
    u.protocol = location.protocol === "https:" ? "wss:" : "ws:";
    super(u.href);
    this.deviceId = deviceId;
    this.mode = mode;
    this.linkId = linkId;
    this.psk = psk;
    this.session = null;
    this.handshake = null;
    this.inbox = Promise.resolve();   // decrypt strictly in order
    this.connect();
  }

  async opened(sock) {
    this.setState("connecting", "Connecting to the stove…");
    this.session = null;
    this.handshake = await E2E.open(this.mode, this.linkId);
    if (sock === this.ws) this.sendRaw(this.handshake.msg);
  }

  received(data) {
    let msg;
    try { msg = JSON.parse(data); } catch (e) { return; }
    this.inbox = this.inbox.then(() => this.handle(msg)).catch(e => this.fatal(e.message || String(e)));
  }

  async handle(msg) {
    if (msg.t === "accept" && this.handshake) {
      this.session = await E2E.finish(this.handshake, msg, this.psk, this.deviceId);
      this.handshake = null;
      this.delay = 1000;
      this.setState("open", "Live");
      this.flush();
    } else if (msg.t === "data" && this.session) {
      this.deliver(await this.session.open(msg.c));
    } else if (msg.t === "offline") {
      this.session = null;
      this.resetSent();
      this.setState("offline", "The stove's hub is not connected to the internet right now.");
    } else if (msg.t === "online") {
      this.opened(this.ws);       // the hub is back: new handshake on the same connection
    } else if (msg.t === "closed") {
      this.session = null;
      const why = msg.reason || "";
      if (/unknown link|access removed/.test(why)) {
        this.fatal("This stove no longer gives your account access.", "revoked");
      } else if (/too many attempts/.test(why)) {
        this.fatal("Too many attempts with this token. Wait a few minutes and try again.");
      } else if (msg.code === 404) {
        this.fatal(why || "This stove is not linked to your account.", "revoked");
      } else {
        this.resetSent();
        this.setState("offline", why || "The stove closed the connection.");
      }
    } else if (msg.t === "error") {
      this.setState("offline", msg.message || "The server refused the connection.");
    }
  }

  fatal(text, kind) {
    this.closedForGood = true;
    clearTimeout(this.timer);
    this.setState(kind || "failed", text);
    if (this.ws) { const s = this.ws; this.ws = null; s.close(); }
    this.failAll(text);
  }

  lost(e) {
    this.session = null;
    if (e && e.code === 4401) { this.fatal("Your session has ended. Please sign in again.", "signed-out"); return; }
    this.retry("No connection to the server.");
  }

  transmit(msg) {
    if (!this.session) { this.resetSent(); return; }   // sent again once the session is up
    this.session.seal(msg).then(c => this.sendRaw({ t: "data", c })).catch(e => this.fatal(e.message));
  }
}

/* Event handlers registered by the current screen; dropped when it changes. */
let viewUnsubs = [];
function onLink(event, fn) {
  if (Stove.link) viewUnsubs.push(Stove.link.on(event, fn));
}
function dropViewHandlers() {
  viewUnsubs.forEach(u => u());
  viewUnsubs = [];
}

function rpc(method, params, timeout) {
  if (!Stove.link) return Promise.reject(new RpcError("No stove selected.", 0));
  return Stove.link.call(method, params, timeout);
}
