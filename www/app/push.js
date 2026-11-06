"use strict";
/* Push notifications and the installable app (website only).
   The service worker (sw.js) shows the notifications; the subscription's keys go
   to the stoves through the end-to-end encrypted sessions, the website only
   learns the push service's address. See docs/protocol.md, section 6. */

const Push = {
  reg: null,

  async init() {
    if (!("serviceWorker" in navigator) || !window.isSecureContext) return;
    try {
      Push.reg = await navigator.serviceWorker.register("sw.js");
    } catch (e) {
      console.warn("service worker:", e);
    }
  },

  supported() {
    return !!(Push.reg && "PushManager" in window && "Notification" in window && Stove.env.push_key);
  },

  isApple() {
    return /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === "MacIntel" && navigator.maxTouchPoints > 1);
  },

  installed() {
    return matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
  },

  state() {
    try { return JSON.parse(localStorage.getItem("stove-push") || "null"); } catch (e) { return null; }
  },

  save(state) {
    try {
      if (state) localStorage.setItem("stove-push", JSON.stringify(state));
      else localStorage.removeItem("stove-push");
    } catch (e) { /* kept for this visit only */ }
  },

  /* Run fn(link) on every stove of the account: the open one, or a short extra session. */
  async eachStove(fn) {
    const results = [];
    for (const d of Stove.vault.devices) {
      if (Stove.device && Stove.device.id === d.id && Stove.link && Stove.link.state === "open") {
        results.push(await fn(Stove.link, d).then(() => [d, null], e => [d, e]));
        continue;
      }
      const link = new RemoteLink(d.id, "link", d.link, Bytes.unb64(d.key));
      try {
        await new Promise((resolve, reject) => {
          const t = setTimeout(() => reject(new Error(link.stateText || "not reachable")), 15000);
          link.on("state", st => {
            if (st.state === "open") { clearTimeout(t); resolve(); }
            if (["failed", "revoked", "signed-out"].includes(st.state)) { clearTimeout(t); reject(new Error(st.text)); }
          });
        });
        await fn(link, d);
        results.push([d, null]);
      } catch (e) {
        results.push([d, e]);
      } finally {
        link.close();
      }
    }
    return results;
  },

  async enable(schedule) {
    const perm = await Notification.requestPermission();
    if (perm !== "granted") {
      throw new Error(perm === "denied" ? "Notifications are blocked for this site. Allow them in the browser's settings."
        : "Notifications were not allowed.");
    }
    let sub = await Push.reg.pushManager.getSubscription();
    if (!sub) {
      sub = await Push.reg.pushManager.subscribe({ userVisibleOnly: true,
                                                   applicationServerKey: Bytes.unb64url(Stove.env.push_key) });
    }
    const j = sub.toJSON();
    const r = await api("POST", "push/subscriptions", { endpoint: j.endpoint });
    const state = { sub: r.sub, p256dh: j.keys.p256dh, auth: j.keys.auth, schedule: !!schedule, user: Stove.me.id };
    Push.save(state);
    return Push.share(state);
  },

  /* Give the subscription's keys to the stoves (again, e.g. after adding a stove). */
  share(state) {
    const categories = state.schedule ? ["problems", "schedule"] : ["problems"];
    return Push.eachStove(link => link.call("push.subscribe", { sub: state.sub, p256dh: state.p256dh, auth: state.auth,
                                                                categories }, 10000));
  },

  async disable() {
    const state = Push.state();
    Push.save(null);
    if (state) {
      await Push.eachStove(link => link.call("push.unsubscribe", { sub: state.sub }, 10000));
      await api("DELETE", `push/subscriptions/${state.sub}`).catch(() => {});
    }
    const sub = Push.reg && await Push.reg.pushManager.getSubscription();
    if (sub) await sub.unsubscribe();
  },

  /* When a stove's session opens, make sure it knows this phone's subscription. */
  onStoveOpen(link) {
    const state = Push.state();
    if (!state || !Stove.me || state.user !== Stove.me.id) return;
    link.call("push.get", { sub: state.sub }, 10000).then(r => {
      if (!r.subscribed) {
        link.call("push.subscribe", { sub: state.sub, p256dh: state.p256dh, auth: state.auth,
                                      categories: state.schedule ? ["problems", "schedule"] : ["problems"] }, 10000);
      }
    }).catch(() => {});
  },

  /* The Notifications card on the Account page. */
  card(el) {
    const head = h("div", { class: "card-head" }, h("span", { class: "label" }, "Notifications"));
    if (!Push.supported()) {
      const text = Push.isApple() && !Push.installed()
        ? ["On iPhone and iPad, notifications work in the installed app: tap ", h("b", {}, "Share → Add to Home Screen"),
           ", open the Stove app from the home screen and turn them on here (iOS 16.4 or newer)."]
        : !Stove.env.push_key ? ["The website has no push key (the server needs python3-cryptography)."]
          : ["This browser does not support push notifications."];
      el.replaceChildren(head, h("div", { class: "notice" }, icon("info", "sm"), h("span", {}, ...text)));
      return;
    }
    const state = Push.state();
    const on = !!(state && state.user === Stove.me.id && Notification.permission === "granted");
    const msg = h("div", { class: "small muted mt" });
    const sched = toggle(!!(state && state.schedule), async (value, input) => {
      input.disabled = true;
      try {
        const st = Object.assign(Push.state() || {}, { schedule: value });
        Push.save(st);
        report(await Push.share(st));
      } catch (e) { toast(e.message, "bad"); }
      input.disabled = false;
    }, "When the schedule switches the stove on or off");
    const main = toggle(on, async (value, input) => {
      input.disabled = true;
      try {
        if (value) report(await Push.enable(false));
        else { await Push.disable(); toast("Notifications off on this device"); }
      } catch (e) {
        input.checked = !value;
        toast(e.message, "bad");
      }
      input.disabled = false;
      Push.card(el);
    }, "Notifications on this device");

    function report(results) {
      const failed = results.filter(r => r[1]);
      msg.textContent = failed.length
        ? `Not reached right now: ${failed.map(r => r[0].name).join(", ")}. They are told the next time you open them.`
        : results.length ? `On for ${results.map(r => r[0].name).join(", ")}.` : "Add a stove to get its notifications.";
    }

    el.replaceChildren(...[head,
      h("div", { class: "setting" }, h("div", {}, h("b", {}, "On this device"),
        h("div", { class: "small muted" }, "Alarms, a stove that does not light, a hub that goes offline, pellets running low, new accounts.")), main),
      on ? h("div", { class: "setting" }, h("div", {}, h("b", {}, "Schedule"),
        h("div", { class: "small muted" }, "Also when the schedule switches the stove on or off.")), sched) : null,
      on ? h("button", {
        class: "btn sm mt", type: "button",
        onclick: async () => {
          try {
            await rpc("push.test", { sub: Push.state().sub }, 10000);
            toast("Test sent - it should arrive in a moment");
          } catch (e) { toast(e.message, "bad"); }
        },
      }, icon("bolt", "sm"), "Send a test") : null,
      msg].filter(Boolean));
  },
};
