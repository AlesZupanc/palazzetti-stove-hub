/* Service worker of the stove website: installable app, works offline (cached
   app files), shows push notifications. Not used on the local dashboard
   (service workers need https). The server fills in the version below, so a
   changed app file installs a new worker. */

const VERSION = "__APP_VERSION__";
const CACHE = "stove-" + VERSION;
const FILES = ["./", "index.html", "env.json", "app.css", "theme.js", "app.js", "link.js", "crypto.js", "home.js", "schedule.js",
  "pages.js", "local.js", "cloud.js", "push.js", "icon.svg", "manifest.webmanifest", "icons/icon-192.png"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(FILES)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", event => {
  event.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k.startsWith("stove-") && k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

self.addEventListener("fetch", event => {
  const req = event.request;
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== self.location.origin || !url.href.startsWith(self.registration.scope)) return;
  if (/\/(api|ws)\//.test(url.pathname)) return;   // always live
  // network first, so a reachable site is always current; the cache covers being offline
  event.respondWith(fetch(req).then(resp => {
    if (resp.ok) {
      const copy = resp.clone();
      caches.open(CACHE).then(c => c.put(req, copy));
    }
    return resp;
  }).catch(() => caches.match(req, { ignoreSearch: true }).then(r => r || caches.match("./"))));
});

self.addEventListener("push", event => {
  let d = {};
  try {
    d = event.data ? event.data.json() : {};
  } catch (e) {
    d = { body: event.data ? event.data.text() : "" };
  }
  event.waitUntil(self.registration.showNotification(d.title || "Stove", {
    body: d.body || "",
    tag: d.tag || undefined,
    renotify: !!d.tag,
    icon: "icons/icon-192.png",
    badge: "icons/badge-96.png",
    timestamp: d.ts ? d.ts * 1000 : Date.now(),
    data: { device: d.device || null },
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const scope = self.registration.scope;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) {
      if (c.url.startsWith(scope) && "focus" in c) return c.focus();
    }
    return self.clients.openWindow(scope + "#/");
  }));
});
