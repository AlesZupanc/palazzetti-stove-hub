# How the pieces talk to each other

This file describes the protocols between the browser, the Raspberry Pi hub
("the hub") and the VPS ("the cloud server"). The README explains how to use
everything; this is the reference for anyone changing the code.

```
 at home                                             anywhere
 ┌─────────┐ ws://stove.local/ws  ┌──────────┐      ┌─────────┐
 │ browser │ ───────────────────▶ │ Pi hub   │      │ browser │
 └─────────┘  plain JSON RPC      │          │      └────┬────┘
              (no login)          │          │           │ https: app files, accounts
                                  │          │           │ wss:   /ws/client  (E2E encrypted)
                                  │          │ wss  ┌────▼─────────────┐
                                  │          │ ───▶ │ VPS cloud server │
                                  └──────────┘      └──────────────────┘
                                   /ws/device: one always-on connection,
                                   carries every remote session (E2E encrypted)
```

## 1. The RPC protocol (browser ⇄ hub)

The same JSON messages are used at home and through the VPS. At home they
travel as plain WebSocket text frames on `ws://stove.local/ws`; from outside
they are encrypted end to end (section 3) and the VPS cannot read them.

Request (browser → hub):

```json
{"id": 7, "m": "stove.action", "p": {"action": "setpoint", "value": 21.5}}
```

Answer (hub → browser): `{"id": 7, "r": {...}}` or `{"id": 7, "e": "message", "code": 400}`.

Events (hub → browser, unrequested):

| event      | when                                   | data                                  |
|------------|----------------------------------------|---------------------------------------|
| `hello`    | right after connecting                 | device id/name, version, who you are  |
| `status`   | stove status changed (checked every 5 s) | status summary (with `pellets`) + scheduler + probes + `pellet_tank` |
| `schedule` | a slot or the pause switch changed     | the whole schedule                    |
| `activity` | something was logged                   | the new event                         |
| `online`   | at home only: cloud connection/links changed | same as `cloud.info`            |

In the status, `pellets` is `{total, used, since}` (kg; `used` since the last refill) and, once a
tank size is set, `pellets.tank` is `{size, left, pct, low_pct, low, empty, over, rate, hours_left}`
(`rate` in kg/h, both null until the stove has burnt an hour); `pellet_tank` is the setting
`{size, low_pct}`. `pellets.refill`, `pellets.level`, `pellets.reset` and `pellets.tank` answer
with that status.

Methods: `status.get`, `stove.action`, `schedule.get`, `schedule.save`,
`schedule.delete`, `schedule.enable`, `probes.set`, `pellets.refill` (`{}` = filled up,
`{"added": kg}` = some put in), `pellets.level` (`{"pct": 0..100}`: how full the tank is
now), `pellets.tank` (`{"size": kg or null, "low_pct": 0..90}`),
`pellets.reset` (the old name of a full refill), `events.list`,
`system.get`, `device.rename`; at home only: `cloud.info`, `cloud.regenerate`,
`cloud.unlink`, `urlapi.info`, `urlapi.regenerate`, `system.update`; remote only: `pair.finish`, `link.leave`, `push.subscribe`,
`push.unsubscribe`, `push.get`, `push.test`.

## 2. Home network (no login)

The local dashboard needs no password: the home network is trusted, and a
login over plain `http://` would not protect anything. Instead the hub
refuses anything that does not come from the home network:

* the client address must be private (10/8, 172.16/12, 192.168/16, 100.64/10,
  169.254/16, loopback, fc00::/7, fe80::/10) unless `[web] lan_only = no`;
* the `Host` header must be an IP address, `<name>.local`, the Pi's host name,
  `localhost` or a name listed in `[web] allowed_hosts` (stops DNS rebinding);
* a WebSocket must come from a page of the hub itself (`Origin` check), so a
  web page on the internet cannot steer the stove through your browser.

### Behind a reverse proxy

Normally the hub listens on port 80 itself and nothing is in front of it. A
reverse proxy of your own on the Pi (README, "Behind your own reverse proxy")
takes port 80 instead, with the hub on, say, `127.0.0.1:8080`
(`[web] bind = 127.0.0.1`, `port = 8080`, `public_port = 80`,
`trust_proxy = yes`). The proxy passes the browser's `Host` unchanged (so the
checks above still see the real name) and sets `X-Real-IP: <client>`,
overwriting anything the client sent. With `[web] trust_proxy = yes` the hub
uses that header as the client address, only on connections from 127.0.0.1; a
value that is not an IP address is refused (403). `X-Forwarded-For` is never
used: proxies append to it, so its first entries come from the client.
`public_port` is the port in the addresses the hub shows (the dashboard's
links, the log) and in stove.local's DNS-SD record. An unknown `Host` gets 421
"Unknown host name. Open <the hub's address> instead." (without a proxy: "... or
the hub's IP address"). The proxy should listen on IPv4 only, as the hub does
(a global IPv6 address would not count as the home network; browsers fall back
to IPv4), and leave query strings out of its logs, which can hold the local
API's secret token.

### The local API and the local token

`/api/state`, `/api/control` and `/api/command` (`stovemaster/urlapi.py`) take the
dashboard's secret token as `token=`, `X-Stove-Token` or `Authorization: Bearer`.
Programs on the Pi itself (scripts, other services) have a second one, the local
token: the file `[web] local_token_file` (`/etc/stove-master/local-api-token`,
made once by `pi/install.sh`, `root:stove` 0640), read when needed and again when
the file changes; no file (or a token shorter than 16 characters) means no local
token. The hub accepts it only when the TCP peer is a loopback address and the
request has no `X-Real-IP` header, i.e. from a program on the Pi, never through
a proxy. It is never shown and the dashboard's "new token" does not touch it.
`X-Stove-Actor: <text>` (printable, at most 40 characters) names the caller in
the Activity log as `Local program (<text>)`, else `Local program`; with the
dashboard's token the header is ignored and the actor stays
`API (<client address>)`.

```
POST http://127.0.0.1/api/control             X-Stove-Token: <local token>
     state=on&temp=21.5&level=3&fan=7          X-Stove-Actor: cron job
-> 200 {"ok": true, "done": ["setpoint", "power", "fan", "on"], "state": {...}, "say": "..."}
   400 bad value · 401 wrong token · 413 body over 4 KB · 502 the stove did not answer:
       {"ok": false, "error": "...", "done": ["setpoint"], "say": "..."}
       "done": the commands sent before the one that failed (they stay done; the
       rest was not sent); [] when nothing was sent
```

The commands go one by one in that order (settings, then on/off) and stop at the
first one that fails. The names in `done` are `setpoint`, `power` (the fire
power level), `fan`, `on` and `off`.

## 3. End-to-end encryption (browser ⇄ hub through the VPS)

### The stove's token

Every hub has a token, shown on the local dashboard under **Online**:

```
ABCD-EFGH-2345-6789-ABCD-EFGH-2345-6789
└ device id ┘└──────── secret (120 bits) ───────┘
```

32 characters of base32 (A–Z, 2–7). The first 8 are the device id (the VPS
needs it to route messages), the other 24 are a 15-byte secret that never
leaves the browser and the hub. The local dashboard also offers a link
`https://example.com/mystove/#/add/<token>`; the part after `#` is not sent to
any server. "New token" makes a new secret; already added accounts keep working.

### Handshake

Both sides share a pre-shared key `psk`: the token secret when adding the
stove (`mode = "pair"`), afterwards the link key the hub handed out
(`mode = "link"`, 32 bytes).

```
browser:  eB, nB = new P-256 key pair, 16 random bytes
browser → hub   {"t":"open", "mode", "link", "eb": eB_pub, "nb": nB}
hub:      eP, nP = new P-256 key pair, 16 random bytes
          Z  = ECDH(eP, eB_pub)
          th = SHA-256("stove-e2e-v1" 0 mode 0 link 0 device_id 0 eB_pub nB eP_pub nP)
          kc | k_b2h | k_h2b = HKDF-SHA256(ikm=Z, salt=psk, info="stove-e2e-v1 keys" th, 96 bytes)
hub → browser   {"t":"accept", "ep": eP_pub, "np": nP, "mac": HMAC-SHA256(kc, "H" th)}
browser:  same derivation; checks mac (the hub knows psk); else the token was wrong
```

After that every message is `{"t":"data", "c": base64(AES-256-GCM(key, nonce, json))}`
with `k_b2h` or `k_h2b` and a 96-bit nonce that counts 0, 1, 2, … in each
direction (no replays, no reordering). The hub accepts a message only if it
decrypts, which proves the browser knows `psk` too. The VPS sees who talks to
which device and how much, never the content; it cannot pretend to be either
side without `psk`.

### Adding a stove ("Add stove" on the website)

1. The browser opens a `pair` session with the token secret.
2. The hub answers with its name; the browser asks "Add <name>?".
3. On "Add", the browser calls `pair.finish`; the hub creates a link
   (`link id`, 32-byte `key`), tells the VPS that this account may now open
   sessions to it, and returns both. `pair.finish` works once per session;
   the VPS accepts the hub's `link add` only for an account that has a `pair`
   session open to that hub, and a new token ends all `pair` sessions.
4. The browser stores `{device id, link id, key, name}` in the account's
   encrypted vault (section 4). Every later session uses `mode = "link"`.

The hub keeps one link per account; adding again replaces it. The local
dashboard lists linked accounts and can remove them; the website can remove a
stove from the account (`link.leave`).

## 4. Accounts on the VPS

Accounts (profiles) live on the VPS, because there is no login at home any
more. The password never leaves the browser:

```
master   = PBKDF2-SHA256(password, salt, 600000 iterations, 32 bytes)
auth key = HKDF(master, info "stove auth")    sent to the VPS instead of the password
vault key= HKDF(master, info "stove vault")   AES-256-GCM key, never sent
```

The VPS stores `salt` and a salted hash of the auth key. The vault (the list
of stoves with their link keys) is stored on the VPS encrypted with the vault
key, so signing in on a new phone brings the stoves along, and the VPS still
cannot use them. Forgetting the password means a reset by e-mail; the vault
cannot be decrypted any more, so the stoves are added again with their tokens.

The browser keeps the vault key in IndexedDB as a non-extractable key, so a
reload does not ask for the password again (until "Sign out").

New accounts need an administrator's approval (e-mail with a link). The first
administrator is created by `deploy.sh`: it prints a one-time link to set the
password.

## 5. VPS endpoints

HTTPS (`/mystove/...`): the app files, `env.json`, `sw.js` and the JSON API
under `api/` (accounts, vault, devices, admin, push subscriptions).

`wss://…/mystove/ws/client?dev=<device id>` (session cookie, same origin):
one connection per stove the browser looks at. Messages: `open`, `data`,
`close` from the browser; `accept`, `data`, `closed`, `online`, `offline`
from the VPS.

`wss://…/mystove/ws/device` (header `Authorization: Device <id>.<secret>`):
the hub's always-on connection. The first connection registers the device id
with that secret (trust on first use); later ones must present the same
secret. Messages carry a session id `sid` chosen by the VPS: `open`, `data`,
`close` towards the hub; `accept`, `data`, `close`, `link` (add/remove an
account), `push` from the hub; `welcome` and `links` tell the hub which
accounts the VPS still routes, so links removed on the website disappear on
the hub as well.

Ping every 25 s keeps NAT routers and nginx from closing idle connections.

Limits: a WebSocket message is at most 1 MB (the local dashboard: 256 KB); the
VPS forwards a browser's `c` up to 256 KB and the hub sends at most 768 KB, so
nothing one side sends can make the other drop the connection. A bigger `c`
ends that browser's session (`closed`, "message too big") instead of being
lost, which would break the nonce order. Every connection sends from a queue
bounded in bytes (`ws.Sender`): a peer that stops reading is disconnected
instead of blocking anybody else, and while a hub's queue holds more than
2 MB the VPS ends sessions that send more instead of dropping the hub.

## 6. Push notifications

The VPS has a VAPID key pair (made on first start). A browser that turns
notifications on subscribes with the VAPID public key, registers the endpoint
URL with the VPS (`POST api/push/subscriptions` → `sub` id) and gives the
subscription keys (`p256dh`, `auth`) only to the hubs, through the encrypted
session (`push.subscribe`). When something happens, the hub encrypts the
notification for that browser (RFC 8291, aes128gcm) and asks the VPS to
deliver it (`{"t":"push", "sub", "body"}`); the VPS adds the VAPID signature
(RFC 8292) and posts it to the push service (Google, Apple, Mozilla). So the
VPS and the push service only see ciphertext.

Notifications: stove alarm, schedule problems (stove did not light, could not
switch off), Connection Box unreachable, a new account added the stove, and,
if wanted, the schedule switching the stove on and off.

On iPhone and iPad (iOS 16.4+) notifications only work for the web app added
to the home screen (Share → Add to Home Screen).
