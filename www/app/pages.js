"use strict";
/* Activity, and helpers shared by the other pages (local.js, cloud.js). */

const LOGO = `<svg viewBox="0 0 24 24" aria-hidden="true" focusable="false"><path fill="#fff" d="M12 2.5c.7 3.6-1.3 5.5-3 7.4C7.5 11.6 6.5 13.2 6.5 15.4a5.5 5.5 0 0 0 11 0c0-2.8-1.6-4.6-2.9-6.2-.3 1.5-1.1 2.5-2.3 3 .6-3.4.2-6.5-.3-9.7z"/></svg>`;

/* ---------------------------------------------------------------- activity */
const EVENT_KIND = {
  on: ["power", "accent", "Switched on", "stove"], off: ["power", "cool", "Switched off", "stove"],
  setpoint: ["thermo", "", "Target temperature", "stove"], power: ["flame", "", "Flame power", "stove"],
  fan: ["fan", "", "Room fan", "stove"], temperatures: ["thermo", "", "Temperature probes", "stove"],
  "slot start": ["calendar", "accent", "Slot started", "schedule"], "slot end": ["calendar", "cool", "Slot ended", "schedule"],
  "slot added": ["calendar", "", "Slot added", "schedule"], "slot changed": ["calendar", "", "Slot changed", "schedule"],
  "slot deleted": ["calendar", "", "Slot deleted", "schedule"], "schedule paused": ["pause", "", "Schedule paused", "schedule"],
  "schedule resumed": ["play", "", "Schedule resumed", "schedule"], "schedule imported": ["calendar", "", "Schedule imported", "schedule"],
  paused: ["pause", "", "Schedule paused", "schedule"], manual: ["user", "", "Manual override", "schedule"],
  clock: ["clock", "", "Clock", "system"], start: ["bolt", "ok", "Hub started", "system"], mail: ["mail", "", "E-mail", "system"],
  login: ["key", "", "Signed in", "accounts"], "login failed": ["lock", "", "Sign-in failed", "accounts"],
  "logout everywhere": ["logout", "", "Signed out everywhere", "accounts"], register: ["users", "accent", "Account requested", "accounts"],
  "approve user": ["check", "ok", "Account approved", "accounts"], "reject user": ["x", "", "Request rejected", "accounts"],
  "disable user": ["lock", "", "User disabled", "accounts"], "enable user": ["check", "", "User enabled", "accounts"],
  "make admin": ["shield", "", "Made administrator", "accounts"], "remove admin": ["user", "", "Administrator removed", "accounts"],
  "delete user": ["trash", "", "User deleted", "accounts"], "password changed": ["key", "", "Password changed", "accounts"],
  "password reset": ["key", "", "Password reset", "accounts"], "password reset requested": ["mail", "", "Password reset requested", "accounts"],
  "password set": ["key", "", "Password set", "accounts"], "admin created": ["shield", "ok", "Administrator created", "accounts"],
  "user added": ["users", "", "User added", "accounts"], role: ["shield", "", "Role changed", "accounts"],
  alarm: ["warn", "bad", "Stove alarm", "stove"], "pellet tank": ["bag", "", "Pellet tank", "stove"],
  "pellets refilled": ["bag", "ok", "Tank refilled", "stove"], "pellets reset": ["bag", "", "Pellet counter reset", "stove"],
  "pellet level": ["bag", "", "Pellet level set", "stove"],
  "pellets low": ["bag", "accent", "Pellets running low", "stove"], "pellets empty": ["bag", "bad", "Pellet tank empty", "stove"],
  "box offline": ["router", "", "Connection Box not answering", "system"],
  "box online": ["router", "ok", "Connection Box answers again", "system"], renamed: ["edit", "", "Stove renamed", "system"],
  "account added": ["globe", "accent", "Account added", "online"], "account removed": ["globe", "", "Account removed", "online"],
  "new token": ["key", "", "New token", "online"],
};
const VIA = { lan: "at home", remote: "online", cli: "command line", system: "", scheduler: "" };

function dayLabel(dateStr) {
  const d = new Date(dateStr + "T12:00:00");
  const today = new Date();
  const diff = Math.round((new Date(today.toDateString()) - new Date(d.toDateString())) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Yesterday";
  return d.toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
}

Stove.views.log = async function viewLog(root, arg, alive) {
  let filter = "all", limit = 150, events = [];
  const filters = [["all", "All"], ["stove", "Stove"], ["schedule", "Schedule"], ["online", "Online"], ["system", "System"]];
  const filterBar = h("div", { class: "filters", role: "group", "aria-label": "Filter" });
  const list = h("div", { class: "stack" });
  const more = h("button", { class: "btn block", type: "button", hidden: true, onclick: async () => { limit = 500; await load(); } },
    "Show older entries");
  root.append(h("div", { class: "screen" }, h("div", { class: "page-title" }, "Activity"),
    h("p", { class: "page-sub" }, "Everything the stove, the schedule and people did."), filterBar, list, more));

  function drawFilters() {
    filterBar.replaceChildren(...filters.map(([k, label]) => h("button", { type: "button", "aria-pressed": String(k === filter),
      onclick: () => { filter = k; drawFilters(); draw(); } }, label)));
  }

  function draw() {
    const shown = events.filter(ev => filter === "all" || (EVENT_KIND[ev.action] || [])[3] === filter);
    if (!shown.length) {
      list.replaceChildren(h("section", { class: "card empty" }, h("div", { class: "art" }, icon("activity")),
        h("h3", {}, "Nothing here yet"), h("p", {}, "Actions show up here as soon as something happens.")));
      return;
    }
    const groups = [];
    for (const ev of shown) {
      const day = (ev.time || "").slice(0, 10);
      if (!groups.length || groups[groups.length - 1].day !== day) groups.push({ day, items: [] });
      groups[groups.length - 1].items.push(ev);
    }
    list.replaceChildren(...groups.flatMap(g => [
      h("div", { class: "day-head" }, dayLabel(g.day)),
      h("section", { class: "card" }, ...g.items.map(ev => {
        const [ic, tone, label] = EVENT_KIND[ev.action] || ["info", "", ev.action];
        const via = VIA[ev.via] !== undefined ? VIA[ev.via] : ev.via;
        return h("div", { class: "ev" + (ev.ok ? "" : " fail") },
          h("div", { class: "ico " + (ev.ok ? tone : "bad") }, icon(ev.ok ? ic : "warn")),
          h("div", { class: "txt" },
            h("div", { class: "what" }, label),
            ev.detail && ev.detail.toLowerCase() !== label.toLowerCase() ? h("div", { class: "detail" }, ev.detail) : null,
            h("div", { class: "meta" }, [(ev.time || "").slice(11), ev.actor === "-" ? "unknown" : ev.actor, via]
              .filter(Boolean).join(" · "))));
      })),
    ]));
  }

  async function load() {
    const j = await rpc("events.list", { limit });
    if (!alive()) return;
    events = j.events;
    more.hidden = events.length < limit || limit >= 500;
    draw();
  }

  drawFilters();
  list.append(h("section", { class: "card" }, h("div", { class: "skel line" }), h("div", { class: "skel line short" })));
  onLink("activity", ev => {          // new entries arrive as they happen
    if (!alive() || events.some(e => e.id === ev.id)) return;
    events.unshift(ev);
    draw();
  });
  onLink("state", st => { if (st.state === "open") load().catch(() => {}); });
  await load();
};

/* Lets long URLs wrap after a "/" instead of in the middle of a word. */
function breakable(s) {
  return (String(s).match(/[^/]*\/+|[^/]+$/g) || [String(s)]).flatMap((p, i) => i ? [h("wbr"), p] : [p]);
}

function fmtUptime(s) {
  const d = Math.floor(s / 86400), hh = Math.floor(s % 86400 / 3600), m = Math.floor(s % 3600 / 60);
  return d ? `${d} d ${hh} h` : hh ? `${hh} h ${m} min` : `${m} min`;
}

/* "Update now" (tools/auto-update.sh): what the last run did, for the Pi and the website alike. */
function updateBusy(u) {
  return !!(u && u.available && !u.stuck && (u.requested || (u.last && u.last.result === "running")));
}

function updateBox(u, opts) {
  const { howToEnable, onRequest } = opts;
  if (!u || !u.available) {
    return h("dl", { class: "kv" }, ...kvRow("Updates", `By hand. Automatic updates (with a button here): ${howToEnable}`, "warn"));
  }
  const L = u.last;
  let text = "Automatic, every minute", state = "ok";
  if (u.stuck) [text, state] = [u.stuck, "bad"];
  else if (updateBusy(u)) [text, state] = [L && L.result === "running" && !u.requested ? `${L.message}…` : "Checking GitHub…", "warn"];
  else if (L && L.result === "updated") text = `${L.message} (${ago(L.at)})`;
  else if (L && L.result === "current") text = `Up to date, checked ${ago(L.at)}`;
  else if (L && L.result === "failed") [text, state] = [`Last update failed ${ago(L.at)}: ${L.message}`, "bad"];
  return h("div", {},
    h("dl", { class: "kv" }, ...kvRow("Updates", text, state)),
    h("button", { class: "btn sm mt", type: "button", disabled: updateBusy(u), onclick: onRequest },
      icon("refresh", "sm"), updateBusy(u) ? "Updating…" : "Update now"));
}

function themePicker() {
  const seg = segmented([["light", h("span", { class: "row" }, icon("sun", "sm"), "Light")],
    ["dark", h("span", { class: "row" }, icon("moon", "sm"), "Dark")],
    ["system", h("span", { class: "row" }, icon("contrast", "sm"), "System")]], Theme.get(), v => Theme.set(v), "c3");
  Theme.onchange = () => seg.draw(Theme.get());   // the app bar button changes it too
  return seg;
}

/* ------------------------------------------------------------ public pages */
function authPage(root, title, subtitle, ...content) {
  root.append(h("div", { class: "auth" }, h("div", { class: "auth-card" },
    h("div", { class: "auth-hero" }, h("div", { class: "logo" }, svg(LOGO)), h("h1", {}, title),
      subtitle ? h("p", {}, subtitle) : null),
    ...content)));
}

function authForm(fields, submitText, onSubmit, footer) {
  const err = h("div", { class: "err", role: "alert" });
  const btn = h("button", { class: "btn primary block", type: "submit" }, submitText);
  const form = h("form", {
    class: "card",
    onsubmit: async ev => {
      ev.preventDefault();
      err.textContent = "";
      btn.disabled = true;
      btn.replaceChildren(h("span", { class: "spinner" }));
      try {
        await onSubmit();
      } catch (e) {
        err.textContent = e.message;
      }
      btn.disabled = false;
      btn.replaceChildren(submitText);
    },
  }, ...fields, err, btn, footer || null);
  return form;
}

function doneCard(iconName, title, text, linkText, href) {
  return h("section", { class: "card center" }, h("div", { class: "done-icon" }, icon(iconName)),
    h("h2", {}, title), h("p", { class: "muted" }, text),
    h("a", { class: "btn block", href: href || "#/login" }, linkText || "Back to sign in"));
}

