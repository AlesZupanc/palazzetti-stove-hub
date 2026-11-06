"use strict";
/* Cryptography in the browser (WebCrypto), see docs/protocol.md:
   - E2E:      the browser side of the end-to-end encrypted session with a hub
   - Token:    the stove's token (device id + secret)
   - Account:  password -> auth key (sent to the server) + vault key (never sent)
   - Vault:    the encrypted list of stoves and their link keys
   - KeyStore: keeps the vault key in IndexedDB (non-extractable) between visits
   WebCrypto only exists on https:// pages (and localhost), i.e. on the website. */

const Bytes = {
  utf8: s => new TextEncoder().encode(s),
  text: b => new TextDecoder().decode(b),
  concat(...parts) {
    const out = new Uint8Array(parts.reduce((n, p) => n + p.length, 0));
    let i = 0;
    for (const p of parts) { out.set(p, i); i += p.length; }
    return out;
  },
  b64(b) {
    let s = "";
    const u = new Uint8Array(b);
    for (let i = 0; i < u.length; i += 0x8000) s += String.fromCharCode.apply(null, u.subarray(i, i + 0x8000));
    return btoa(s);
  },
  unb64(s) {
    const bin = atob(String(s));
    const out = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
    return out;
  },
  b64url: b => Bytes.b64(b).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, ""),
  unb64url: s => Bytes.unb64(String(s).replace(/-/g, "+").replace(/_/g, "/") + "===".slice((String(s).length + 3) % 4)),
  random: n => crypto.getRandomValues(new Uint8Array(n)),
};

const subtle = () => {
  if (!globalThis.crypto || !crypto.subtle) {
    throw new Error("This page needs a secure connection (https) for encryption.");
  }
  return crypto.subtle;
};

async function hkdfBits(ikm, salt, info, bytes) {
  const key = await subtle().importKey("raw", ikm, "HKDF", false, ["deriveBits"]);
  return new Uint8Array(await subtle().deriveBits({ name: "HKDF", hash: "SHA-256", salt, info }, key, bytes * 8));
}

/* ------------------------------------------------------------------ E2E */
const E2E = {
  PROTO: "stove-e2e-v1",

  async open(mode, linkId) {
    const pair = await subtle().generateKey({ name: "ECDH", namedCurve: "P-256" }, false, ["deriveBits"]);
    const eb = new Uint8Array(await subtle().exportKey("raw", pair.publicKey));
    const nb = Bytes.random(16);
    return { msg: { t: "open", mode, link: linkId || null, eb: Bytes.b64(eb), nb: Bytes.b64(nb) },
             priv: pair.privateKey, eb, nb, mode, linkId: linkId || "" };
  },

  async transcript(mode, linkId, deviceId, eb, nb, ep, np) {
    const z = new Uint8Array([0]);
    const data = Bytes.concat(Bytes.utf8(E2E.PROTO), z, Bytes.utf8(mode), z, Bytes.utf8(linkId), z,
      Bytes.utf8(deviceId), z, eb, nb, ep, np);
    return new Uint8Array(await subtle().digest("SHA-256", data));
  },

  /* -> Session, or throws if the hub could not prove it knows psk. */
  async finish(state, accept, psk, deviceId) {
    const ep = Bytes.unb64(accept.ep), np = Bytes.unb64(accept.np), mac = Bytes.unb64(accept.mac);
    if (ep.length !== 65 || np.length !== 16 || mac.length !== 32) throw new Error("Invalid answer from the stove.");
    const peer = await subtle().importKey("raw", ep, { name: "ECDH", namedCurve: "P-256" }, false, []);
    const z = new Uint8Array(await subtle().deriveBits({ name: "ECDH", public: peer }, state.priv, 256));
    const th = await E2E.transcript(state.mode, state.linkId, deviceId, state.eb, state.nb, ep, np);
    const okm = await hkdfBits(z, psk, Bytes.concat(Bytes.utf8(E2E.PROTO + " keys"), th), 96);
    const kc = await subtle().importKey("raw", okm.slice(0, 32), { name: "HMAC", hash: "SHA-256" }, false, ["verify"]);
    const ok = await subtle().verify("HMAC", kc, mac, Bytes.concat(Bytes.utf8("H"), th));
    if (!ok) throw new Error("The stove could not prove its identity. Check the token.");
    const tx = await subtle().importKey("raw", okm.slice(32, 64), "AES-GCM", false, ["encrypt"]);
    const rx = await subtle().importKey("raw", okm.slice(64, 96), "AES-GCM", false, ["decrypt"]);
    return new E2ESession(tx, rx);
  },
};

/* AES-256-GCM both ways; the nonce counts messages. Calls are chained so the
   order of seal() equals the order of the nonces (and of the messages sent). */
class E2ESession {
  constructor(tx, rx) {
    this.tx = tx; this.rx = rx; this.txN = 0; this.rxN = 0;
    this.txChain = Promise.resolve();
    this.rxChain = Promise.resolve();
  }

  static nonce(n) {
    const iv = new Uint8Array(12);
    const v = new DataView(iv.buffer);
    v.setUint32(4, Math.floor(n / 0x100000000));
    v.setUint32(8, n >>> 0);
    return iv;
  }

  seal(obj) {
    const run = this.txChain.then(async () => {
      const iv = E2ESession.nonce(this.txN++);
      const c = await subtle().encrypt({ name: "AES-GCM", iv }, this.tx, Bytes.utf8(JSON.stringify(obj)));
      return Bytes.b64(new Uint8Array(c));
    });
    this.txChain = run.catch(() => {});
    return run;
  }

  open(text) {
    const run = this.rxChain.then(async () => {
      const iv = E2ESession.nonce(this.rxN);
      let plain;
      try {
        plain = await subtle().decrypt({ name: "AES-GCM", iv }, this.rx, Bytes.unb64(text));
      } catch (e) {
        throw new Error("A message from the stove did not decrypt.");
      }
      this.rxN++;
      return JSON.parse(Bytes.text(plain));
    });
    this.rxChain = run.catch(() => {});
    return run;
  }
}

/* ---------------------------------------------------------------- token */
const Token = {
  ALPHABET: "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567",

  clean: text => String(text || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase(),

  /* -> {deviceId, secret: Uint8Array(15)} or throws */
  parse(text) {
    const s = Token.clean(text);
    if (s.length !== 32 || [...s].some(c => !Token.ALPHABET.includes(c))) {
      throw new Error("A stove token has 32 letters and digits (A–Z, 2–7). Copy it from the stove's page at home.");
    }
    let bits = 0, value = 0;
    const out = [];
    for (const c of s.slice(8)) {
      value = (value << 5) | Token.ALPHABET.indexOf(c);
      bits += 5;
      if (bits >= 8) { out.push((value >>> (bits - 8)) & 0xff); bits -= 8; }
    }
    return { deviceId: s.slice(0, 8), secret: new Uint8Array(out) };
  },

  pretty: text => Token.clean(text).replace(/(.{4})(?=.)/g, "$1-"),
};

/* -------------------------------------------------------------- account */
const Account = {
  ITERATIONS: 600000,
  SALT: Bytes.utf8("stove-account-v1"),

  /* password -> {auth: base64 (sent to the server), vaultKey: CryptoKey (kept here)} */
  async derive(password, saltB64, iterations) {
    const pw = await subtle().importKey("raw", Bytes.utf8(password), "PBKDF2", false, ["deriveBits"]);
    const master = new Uint8Array(await subtle().deriveBits(
      { name: "PBKDF2", hash: "SHA-256", salt: Bytes.unb64(saltB64), iterations: iterations || Account.ITERATIONS },
      pw, 256));
    const auth = await hkdfBits(master, Account.SALT, Bytes.utf8("stove auth"), 32);
    const vk = await hkdfBits(master, Account.SALT, Bytes.utf8("stove vault"), 32);
    const vaultKey = await subtle().importKey("raw", vk, "AES-GCM", false, ["encrypt", "decrypt"]);
    return { auth: Bytes.b64(auth), vaultKey };
  },

  newSalt: () => Bytes.b64(Bytes.random(16)),
};

/* ---------------------------------------------------------------- vault */
const Vault = {
  empty: () => ({ v: 1, devices: [] }),

  async encrypt(key, obj) {
    const iv = Bytes.random(12);
    const c = await subtle().encrypt({ name: "AES-GCM", iv }, key, Bytes.utf8(JSON.stringify(obj)));
    return Bytes.b64(Bytes.concat(iv, new Uint8Array(c)));
  },

  async decrypt(key, blob) {
    if (!blob) return Vault.empty();
    const raw = Bytes.unb64(blob);
    let plain;
    try {
      plain = await subtle().decrypt({ name: "AES-GCM", iv: raw.slice(0, 12) }, key, raw.slice(12));
    } catch (e) {
      throw new Error("Wrong password.");
    }
    const v = JSON.parse(Bytes.text(plain));
    if (!Array.isArray(v.devices)) v.devices = [];
    return v;
  },
};

/* ------------------------------------------------------------- key store */
const KeyStore = {
  memory: {},

  open() {
    return new Promise((resolve, reject) => {
      if (!globalThis.indexedDB) { reject(new Error("no IndexedDB")); return; }
      const req = indexedDB.open("stove", 1);
      req.onupgradeneeded = () => req.result.createObjectStore("keys");
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  },

  async run(mode, fn) {
    const db = await KeyStore.open();
    try {
      return await new Promise((resolve, reject) => {
        const tx = db.transaction("keys", mode);
        const req = fn(tx.objectStore("keys"));
        tx.oncomplete = () => resolve(req && req.result);
        tx.onerror = () => reject(tx.error);
        tx.onabort = () => reject(tx.error);
      });
    } finally {
      db.close();
    }
  },

  async get(name) {
    try { return await KeyStore.run("readonly", s => s.get(name)); } catch (e) { return KeyStore.memory[name]; }
  },

  async set(name, value) {
    KeyStore.memory[name] = value;
    try { await KeyStore.run("readwrite", s => s.put(value, name)); } catch (e) { /* this visit only */ }
  },

  async del(name) {
    delete KeyStore.memory[name];
    try { await KeyStore.run("readwrite", s => s.delete(name)); } catch (e) { /* nothing stored */ }
  },
};

if (typeof module !== "undefined") module.exports = { Bytes, E2E, E2ESession, Token, Account, Vault };
