"use strict";
/* Stove web app - core: helpers, icons, the account API, UI components and the router.
   The same files are served by the Raspberry Pi at home (http://stove.local/, "local"
   mode: no login, plain ws://) and by the website on the VPS
   (https://example.com/mystove/, "cloud" mode: accounts, end-to-end encrypted
   sessions). link.js and crypto.js talk to the stove; the screens are in home.js,
   schedule.js, pages.js, local.js and cloud.js. Plain JavaScript, no build step. */

const Stove = { views: {}, env: {}, me: null, link: null, device: null, vault: null, after: null };

const $ = (sel, root) => (root || document).querySelector(sel);

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
      else if (k in el && typeof v !== "string") el[k] = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}

/* Static, trusted markup only (icons, illustrations) - never data. */
function svg(markup) {
  const t = document.createElement("template");
  t.innerHTML = markup.trim();
  return t.content.firstChild;
}

const ICONS = {
  home: '<path d="M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5.5h-5V20H5a1 1 0 0 1-1-1z"/>',
  calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2.5"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
  activity: '<path d="M3 12.5h4l2.5-6.5 5 13 2.5-6.5H21"/>',
  users: '<circle cx="9" cy="8.5" r="3.2"/><path d="M3.5 19.5c.6-3.3 2.8-5.2 5.5-5.2s4.9 1.9 5.5 5.2"/><circle cx="17" cy="9.5" r="2.5"/><path d="M16.6 14.4c2.1.2 3.5 1.8 3.9 4.6"/>',
  user: '<circle cx="12" cy="8.5" r="3.6"/><path d="M5 20c.8-3.9 3.5-6.1 7-6.1s6.2 2.2 7 6.1"/>',
  power: '<path d="M12 3.5v8"/><path d="M7.1 6.6a7 7 0 1 0 9.8 0"/>',
  flame: '<path d="M12 3c.6 3.2-1.2 4.9-2.7 6.6C7.9 11.2 7 12.6 7 14.6a5 5 0 0 0 10 0c0-2.5-1.4-4.1-2.6-5.5-.3 1.3-1 2.2-2 2.7.5-3 .2-5.8-.4-8.8z"/>',
  fan: '<circle cx="12" cy="12" r="1.7"/><path d="M12 10.3c-.4-2.6.2-5.6 3-6.2 2.2-.4 3.1 2.2 1.3 3.7-1.2.9-2.7 1.6-4.3 2.5zM13.7 12c2.6-.4 5.6.2 6.2 3 .4 2.2-2.2 3.1-3.7 1.3-.9-1.2-1.6-2.7-2.5-4.3zM12 13.7c.4 2.6-.2 5.6-3 6.2-2.2.4-3.1-2.2-1.3-3.7 1.2-.9 2.7-1.6 4.3-2.5zM10.3 12c-2.6.4-5.6-.2-6.2-3-.4-2.2 2.2-3.1 3.7-1.3.9 1.2 1.6 2.7 2.5 4.3z"/>',
  thermo: '<path d="M14 14.6V5.5a2 2 0 0 0-4 0v9.1a4 4 0 1 0 4 0z"/><path d="M12 9.5v6"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
  x: '<path d="M6.5 6.5l11 11M17.5 6.5l-11 11"/>',
  edit: '<path d="M4.5 19.5h4L19 9a2.1 2.1 0 0 0-3-3L5.5 16.5z"/><path d="m14.5 7.5 3 3"/>',
  trash: '<path d="M4.5 7h15M10 11v6M14 11v6M6.2 7l.9 12.4A1.7 1.7 0 0 0 8.8 21h6.4a1.7 1.7 0 0 0 1.7-1.6L17.8 7M9 7V4.5h6V7"/>',
  copy: '<rect x="8.5" y="8.5" width="11.5" height="11.5" rx="2"/><path d="M15.5 8.5V5.5A1.5 1.5 0 0 0 14 4H5.5A1.5 1.5 0 0 0 4 5.5V14a1.5 1.5 0 0 0 1.5 1.5h3"/>',
  more: '<circle cx="5.5" cy="12" r="1.4" fill="currentColor"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/><circle cx="18.5" cy="12" r="1.4" fill="currentColor"/>',
  warn: '<path d="M10.3 4.6 2.9 17.5A2 2 0 0 0 4.6 20.5h14.8a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0z"/><path d="M12 10v4M12 17.2v.2"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.8v.2"/>',
  refresh: '<path d="M19.5 12a7.5 7.5 0 1 1-2.2-5.3L19.5 9"/><path d="M19.5 4.5V9H15"/>',
  logout: '<path d="M14 4.5H6.5A1.5 1.5 0 0 0 5 6v12a1.5 1.5 0 0 0 1.5 1.5H14"/><path d="M10 12h10M16.5 8.5 20 12l-3.5 3.5"/>',
  eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
  eyeOff: '<path d="M4 4l16 16M9.8 5.8c.7-.2 1.4-.3 2.2-.3 6 0 9.5 6.5 9.5 6.5a16.6 16.6 0 0 1-3 3.8M6.2 7.1C3.8 8.8 2.5 12 2.5 12S6 18.5 12 18.5c1.5 0 2.9-.4 4.1-1"/><path d="M9.9 9.9a2.9 2.9 0 0 0 4.1 4.1"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17M12 3.5c2.4 2.6 3.5 5.4 3.5 8.5s-1.1 5.9-3.5 8.5c-2.4-2.6-3.5-5.4-3.5-8.5s1.1-5.9 3.5-8.5z"/>',
  bag: '<path d="M6 8.5h12l-1 11a1.6 1.6 0 0 1-1.6 1.5H8.6A1.6 1.6 0 0 1 7 19.5z"/><path d="M9 8.5V7a3 3 0 0 1 6 0v1.5"/>',
  pause: '<path d="M9 5.5v13M15 5.5v13"/>',
  play: '<path d="M8 5.5v13l10.5-6.5z"/>',
  chevron: '<path d="m9.5 6 6 6-6 6"/>',
  back: '<path d="m14.5 6-6 6 6 6"/>',
  shield: '<path d="M12 3.5 5 6v5.5c0 4.3 3 7.6 7 9 4-1.4 7-4.7 7-9V6z"/><path d="m9 12 2.2 2.2L15.5 10"/>',
  mail: '<rect x="3.5" y="5.5" width="17" height="13" rx="2.2"/><path d="m4 7.5 8 5.8 8-5.8"/>',
  lock: '<rect x="5" y="10.5" width="14" height="10" rx="2.2"/><path d="M8 10.5V8a4 4 0 0 1 8 0v2.5"/>',
  key: '<circle cx="8" cy="15" r="3.5"/><path d="m10.5 12.5 8-8M16 7l2.5 2.5M14 9l1.8 1.8"/>',
  sun: '<circle cx="12" cy="12" r="3.8"/><path d="M12 2.8v2.1M12 19.1v2.1M2.8 12h2.1M19.1 12h2.1M5.5 5.5 7 7M17 17l1.5 1.5M5.5 18.5 7 17M17 7l1.5-1.5"/>',
  moon: '<path d="M19.5 14.6A8 8 0 0 1 9.4 4.5a8 8 0 1 0 10.1 10.1z"/>',
  contrast: '<circle cx="12" cy="12" r="8.5"/><path d="M12 3.5a8.5 8.5 0 0 1 0 17z" fill="currentColor"/>',
  snow: '<path d="M12 3v18M4.2 7.5l15.6 9M4.2 16.5l15.6-9"/><path d="m9.6 4.6 2.4 1.9 2.4-1.9M9.6 19.4l2.4-1.9 2.4 1.9"/>',
  spark: '<path d="M12 3.5v4M12 16.5v4M3.5 12h4M16.5 12h4M6 6l2.8 2.8M15.2 15.2 18 18M6 18l2.8-2.8M15.2 8.8 18 6"/>',
  bolt: '<path d="M13 3 5 13.5h6L10 21l8-10.5h-6z"/>',
  terminal: '<rect x="3" y="4.5" width="18" height="15" rx="2.2"/><path d="m7 9.5 3 2.5-3 2.5M12.5 15H17"/>',
  router: '<rect x="3" y="13" width="18" height="6.5" rx="2"/><path d="M7 16.3h.1M11 16.3h.1M8.5 9.5a5 5 0 0 1 7 0M6 7a8.5 8.5 0 0 1 12 0"/>',
};

function icon(name, cls) {
  return svg(`<svg class="i ${cls || ""}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${ICONS[name] || ""}</svg>`);
}

/* ---------------------------------------------------------- formatting */
const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const DAY_LETTERS = ["M", "T", "W", "T", "F", "S", "S"];
const FAN = [[0, "Off"], [1, "1"], [2, "2"], [3, "3"], [4, "4"], [5, "5"], [6, "High"], [7, "Auto"]];
const fanText = v => { const f = FAN.find(x => x[0] === v); return f ? f[1] : (v == null ? "–" : String(v)); };
const fmtTemp = v => (v == null || isNaN(v) ? "–" : Number(v).toFixed(1));
const toMin = t => { const [a, b] = String(t).split(":").map(Number); return a * 60 + b; };

function daysText(days) {
  const d = [...days].sort((a, b) => a - b);
  if (d.length === 7) return "Every day";
  if (d.join() === "0,1,2,3,4") return "Weekdays";
  if (d.join() === "5,6") return "Weekend";
  const runs = [];
  for (let i = 0; i < d.length;) {
    let j = i;
    while (j + 1 < d.length && d[j + 1] === d[j] + 1) j++;
    runs.push(j - i >= 2 ? `${DAYS[d[i]]}–${DAYS[d[j]]}` : d.slice(i, j + 1).map(x => DAYS[x]).join(", "));
    i = j + 1;
  }
  return runs.join(", ");
}

function timeAgo(ms) {
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (s < 8) return "just now";
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

function initials(name) {
  const parts = String(name || "?").split(/[\s._-]+/).filter(Boolean);
  return ((parts[0] || "?")[0] + (parts[1] ? parts[1][0] : "")).toUpperCase();
}

function avatarColor(name) {
  let n = 0;
  for (const c of String(name)) n = (n * 31 + c.charCodeAt(0)) % 360;
  return `hsl(${n} 55% 50%)`;
}

function avatar(name, cls) {
  const a = h("span", { class: "avatar " + (cls || ""), "aria-hidden": "true" }, initials(name));
  a.style.background = avatarColor(name);
  return a;
}

/* ------------------------------------------------- account API (website) */
class ApiError extends Error {
  constructor(status, message, data) { super(message); this.status = status; this.data = data || {}; }
}

async function api(method, path, body) {
  const opts = { method, headers: { "X-Requested-With": "stove" }, credentials: "same-origin", cache: "no-store" };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch("api/" + path, opts);
  } catch (e) {
    throw new ApiError(0, "Cannot reach the website. Check your connection.");
  }
  let data = null;
  if ((r.headers.get("Content-Type") || "").includes("application/json")) {
    try { data = await r.json(); } catch (e) { data = null; }
  }
  if (!r.ok) {
    const msg = (data && data.error) || `Request failed (HTTP ${r.status})`;
    if (r.status === 401 && Stove.me && typeof Cloud !== "undefined") setTimeout(() => Cloud.signedOut());
    throw new ApiError(r.status, msg, data);
  }
  return data || {};
}

/* ------------------------------------------------------- UI components */
let toastTimer = null;
function toast(msg, kind) {
  const t = $("#toast");
  t.replaceChildren(icon(kind === "bad" ? "warn" : "check"), h("span", {}, msg));
  t.className = "show" + (kind === "bad" ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { t.className = kind === "bad" ? "bad" : ""; }, kind === "bad" ? 5000 : 2600);
}

let openSheet = null;
/* Bottom sheet on phones, dialog on wide screens. build(body, close) fills it. */
function sheet(title, build, opts) {
  opts = opts || {};
  if (openSheet) openSheet.close();
  const back = h("div", { class: "backdrop" });
  const panel = h("div", { class: "sheet", role: "dialog", "aria-modal": "true", "aria-label": title });
  const prevFocus = document.activeElement;
  let closed = false;
  function close(result) {
    if (closed) return;
    closed = true;
    back.remove();
    document.removeEventListener("keydown", onKey);
    document.body.style.overflow = "";
    openSheet = null;
    if (prevFocus && prevFocus.focus) prevFocus.focus();
    if (opts.onClose) opts.onClose(result);
  }
  function onKey(e) { if (e.key === "Escape") close(); }
  back.addEventListener("click", e => { if (e.target === back) close(); });
  document.addEventListener("keydown", onKey);
  panel.append(h("div", { class: "grab" }));
  if (!opts.noHead) {
    panel.append(h("div", { class: "sheet-head" }, h("h2", {}, title),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: () => close() }, icon("x"))));
  }
  build(panel, close);
  back.append(panel);
  document.body.append(back);
  document.body.style.overflow = "hidden";
  // Move focus into the sheet - on touch screens not into a text field, which would pop up the keyboard.
  const typing = matchMedia("(pointer: fine)").matches;
  const first = typing ? panel.querySelector("input, select, textarea, .btn.primary, .btn") : panel;
  panel.tabIndex = -1;
  setTimeout(() => (first || panel).focus({ preventScroll: true }), 50);
  openSheet = { close };
  return openSheet;
}

function confirmSheet({ title, text, ok, danger, iconName }) {
  return new Promise(resolve => {
    let answer = false;
    sheet(title, (panel, close) => {
      panel.append(
        h("div", { class: "confirm-icon" + (danger ? " bad" : "") }, icon(iconName || (danger ? "warn" : "info"))),
        h("h2", { class: "sheet-title" }, title),
        text ? h("p", { class: "sheet-text" }, text) : null,
        h("div", { class: "btns" },
          h("button", { class: "btn", type: "button", onclick: () => close() }, "Cancel"),
          h("button", { class: "btn " + (danger ? "danger solid" : "primary"), type: "button",
                        onclick: () => { answer = true; close(); } }, ok || "OK")));
    }, { noHead: true, onClose: () => resolve(answer) });
  });
}

function toggle(checked, onchange, label) {
  const input = h("input", { type: "checkbox", checked, "aria-label": label || "On/off" });
  input.addEventListener("change", () => onchange(input.checked, input));
  return h("label", { class: "switch" }, input, h("span"));
}

/* items: [[value, label or node], ...] */
function segmented(items, current, onPick, cls) {
  const box = h("div", { class: "seg " + (cls || ""), role: "group" });
  function draw(cur, busyValue) {
    box.replaceChildren(...items.map(([v, label]) => h("button", {
      type: "button", "aria-pressed": String(v === cur), disabled: busyValue !== undefined,
      onclick: () => onPick(v),
    }, busyValue === v ? h("span", { class: "spinner" }) : label)));
  }
  draw(current);
  box.draw = draw;
  return box;
}

function passwordInput(attrs) {
  const input = h("input", Object.assign({ type: "password", required: true }, attrs));
  const btn = h("button", { class: "icon-btn", type: "button", "aria-label": "Show password" }, icon("eye"));
  btn.addEventListener("click", () => {
    const show = input.type === "password";
    input.type = show ? "text" : "password";
    btn.replaceChildren(icon(show ? "eyeOff" : "eye"));
    btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
  });
  return { input, el: h("div", { class: "input-wrap" }, icon("lock"), input, btn) };
}

function field(label, control, hint) {
  return h("label", { class: "field" }, h("span", { class: "lbl" }, label), control,
    hint ? h("span", { class: "hint" }, hint) : null);
}

function withIcon(name, input) {
  return h("div", { class: "input-wrap" }, icon(name), input);
}

function errorCard(e, retry) {
  return h("div", { class: "card empty" },
    h("div", { class: "art" }, icon("warn")),
    h("h3", {}, "Something went wrong"),
    h("p", {}, e.message || String(e)),
    h("button", { class: "btn", onclick: retry || (() => route()) }, icon("refresh", "sm"), "Try again"));
}

/* The pill in the top bar: state of the connection to the stove. */
function setConn(state, text) {
  const c = $("#conn");
  c.hidden = !Stove.link;
  c.className = "conn " + ({ open: "ok", connecting: "", warn: "warn" }[state] ?? "bad");
  c.lastChild.textContent = text;
  c.title = text;
}

function linkStateText(st) {
  return { open: "Live", connecting: "Connecting…", offline: "Offline", revoked: "No access",
           "signed-out": "Signed out", failed: "Error" }[st.state] || st.state;
}

/* A timer that belongs to the current screen (stopped when it changes). */
function viewTimer(fn, ms) {
  const t = setInterval(fn, ms);
  viewUnsubs.push(() => clearInterval(t));
}

function setBrand(name) {
  $(".brand span").textContent = name || "Stove";
  document.title = name && name !== "Stove" ? `${name} · Stove` : "Stove";
}

/* -------------------------------------------------------------- theme */
/* Dark is the default; Light and System (follow the phone) are options. */
const Theme = {
  get() {
    let t = "dark";
    try { t = localStorage.getItem("stove-theme") || "dark"; } catch (e) { /* storage blocked */ }
    return t === "light" || t === "system" ? t : "dark";
  },
  isDark() {
    const t = Theme.get();
    return t === "dark" || (t === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  },
  set(t) {
    try {
      if (t === "dark") localStorage.removeItem("stove-theme");
      else localStorage.setItem("stove-theme", t);
    } catch (e) { /* not stored, still applied for this page */ }
    if (t === "system") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", t);
    Theme.paint();
  },
  paint() {
    const dark = Theme.isDark();
    const meta = $('meta[name="theme-color"]');
    if (meta) meta.setAttribute("content", dark ? "#12100e" : "#f5f1ec");
    const btn = $("#themeBtn");
    if (btn) {
      btn.replaceChildren(icon(dark ? "sun" : "moon"));
      btn.setAttribute("aria-label", dark ? "Light mode" : "Dark mode");
      btn.title = dark ? "Light mode" : "Dark mode";
    }
    if (Theme.onchange) Theme.onchange();
  },
  onchange: null,
};

/* ------------------------------------------------------------- router */
const LOCAL_TABS = [["", "home", "Stove"], ["schedule", "calendar", "Schedule"], ["log", "activity", "Activity"],
  ["online", "globe", "Online"]];
const CLOUD_TABS = [["", "home", "Stove"], ["schedule", "calendar", "Schedule"], ["log", "activity", "Activity"],
  ["stoves", "flame", "Stoves"], ["account", "user", "Account"]];
const PUBLIC = new Set(["login", "register", "forgot", "reset", "approve", "setup"]);
const NEEDS_STOVE = new Set(["", "schedule", "log"]);
const isLocal = () => Stove.env.mode === "local";

function go(hash) {
  if (location.hash === hash) route();
  else location.hash = hash;
}

function buildNav(name) {
  const tabs = isLocal() ? LOCAL_TABS : CLOUD_TABS;
  const current = name === "users" ? "account" : name;
  $("#nav").replaceChildren(...tabs.map(([tab, ic, label]) =>
    h("a", { href: "#/" + tab, class: tab === current ? "on" : null, "aria-current": tab === current ? "page" : null },
      h("span", { class: "pill" }, icon(ic)), label)));
}

let routeSeq = 0;
async function route() {
  const seq = ++routeSeq;
  const alive = () => seq === routeSeq;
  dropViewHandlers();
  Theme.onchange = null;
  if (openSheet) openSheet.close();
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  let name = parts[0] || "", arg = parts.slice(1).join("/");
  if (!(name in Stove.views)) name = "";
  let inApp = true;
  if (!isLocal()) {
    // website: sign in, unlock the vault, pick a stove
    const target = Cloud.gate(name);
    if (target !== name) {
      if (!PUBLIC.has(name) && name !== "unlock") Stove.after = location.hash;
      history.replaceState(null, "", "#/" + target);
      name = target;
      arg = "";
    }
    inApp = !PUBLIC.has(name) && name !== "unlock" && name !== "add";
    if (NEEDS_STOVE.has(name) && !Stove.device) name = "stoves";
  }
  const nav = $("#nav");
  nav.hidden = !inApp;
  if (inApp) buildNav(name);
  $("#conn").hidden = !inApp || !Stove.link || !NEEDS_STOVE.has(name) && !isLocal();
  const main = $("#view");
  main.className = inApp ? "with-nav" : "bare";
  const view = h("div");
  main.replaceChildren(view);
  window.scrollTo(0, 0);
  try {
    await Stove.views[name](view, arg, alive);
  } catch (e) {
    if (alive()) view.replaceChildren(errorCard(e));
  }
}

/* Follow the connection to the current stove in the top bar. */
function watchLink(link) {
  link.on("state", st => {
    if (link !== Stove.link) return;
    setConn(st.state, st.state === "open" ? "Live" : linkStateText(st));
    $("#conn").title = st.text || "";
  });
  link.on("hello", d => { if (link === Stove.link && d.device) setBrand(d.device.name); });
  setConn(link.state, linkStateText(link));
}

async function boot() {
  Theme.paint();
  $("#themeBtn").addEventListener("click", () => Theme.set(Theme.isDark() ? "light" : "dark"));
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => { if (Theme.get() === "system") Theme.paint(); });
  window.addEventListener("hashchange", route);
  try {
    const r = await fetch("env.json", { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    Stove.env = await r.json();
  } catch (e) {
    $("#view").className = "bare";
    $("#view").replaceChildren(errorCard(new Error("Cannot reach the stove app: " + e.message), () => location.reload()));
    return;
  }
  document.documentElement.dataset.mode = Stove.env.mode;
  if (isLocal()) {
    setBrand(Stove.env.device && Stove.env.device.name);
    Stove.link = new LanLink();
    watchLink(Stove.link);
  } else {
    await Cloud.init();
    if (Stove.loadError) {          // offline (the app itself came from the cache)
      $("#view").className = "bare";
      $("#view").replaceChildren(errorCard(Stove.loadError, () => location.reload()));
      return;
    }
  }
  route();
}

document.addEventListener("DOMContentLoaded", boot);
