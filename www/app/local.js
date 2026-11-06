"use strict";
/* Local dashboard only (http://stove.local): the "Online" page - remote access,
   the stove's token for adding it to an online account, the accounts that have
   added it, the stove's name and the hub's system status. */

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);   // only on https pages
    return true;
  } catch (e) { /* fall back below */ }
  const ta = h("textarea", { readonly: true, "aria-hidden": "true" });
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.append(ta);
  ta.select();
  let ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  ta.remove();
  return ok;
}

function kvRow(k, v, state) {
  return [h("dt", {}, k), h("dd", {}, state !== undefined ? h("i", { class: "dot " + state }) : null, h("span", {}, breakable(v)))];
}

function ago(ts) {
  return ts ? timeAgo(ts * 1000) : "never";
}

Stove.views.online = async function viewOnline(root, arg, alive) {
  const statusCard = h("section", { class: "card" });
  const tokenCard = h("section", { class: "card" });
  const linksCard = h("section", { class: "card" });
  const stoveCard = h("section", { class: "card" });
  const apiCard = h("section", { class: "card" });
  root.append(h("div", { class: "screen" },
    h("div", { class: "page-title" }, "Online"),
    h("p", { class: "page-sub" }, "Reach this stove from anywhere through your website."),
    h("div", { class: "grid2" },
      h("div", { class: "col" }, statusCard, tokenCard, apiCard),
      h("div", { class: "col" }, linksCard, stoveCard,
        h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "label" }, "Appearance")),
          themePicker())))));
  let info = null, sys = null, showToken = false, api = null, showKey = false;

  // the local API: plain URLs on the home network (stovemaster/urlapi.py)
  function drawApi() {
    if (!api) return;
    const token = api.token;
    const url = q => `${api.base}/api/control?token=${token}&${q}`;
    const examples = [["Switch on", url("state=on")], ["Switch off", url("state=off")],
      ["Set 22.5 °C", url("temp=22.5")], ["Level 3, fan auto", url("level=3&fan=auto")],
      ["A sentence", `${api.base}/api/command?token=${token}&format=text&text=`
        + encodeURIComponent("turn on and set the temperature to 23 degrees")],
      ["Siri shortcut", `${api.base}/api/command?token=${token}&format=text&text=`],
      ["State (no token)", `${api.base}/api/state`]];
    const copyBtn = (text, what, label) => h("button", {
      class: label ? "btn sm" : "icon-btn", type: "button", "aria-label": `Copy ${what}`,
      onclick: async () => toast(await copyText(text) ? `${what[0].toUpperCase()}${what.slice(1)} copied`
        : "Copy it by hand, please"),
    }, icon("copy", label ? "sm" : undefined), label || null);
    apiCard.replaceChildren(h("div", { class: "card-head" }, h("span", { class: "label" }, "API")),
      h("p", { class: "small muted" }, "Control the stove with plain URLs from the home network — Siri Shortcuts, "
        + "Home Assistant, scripts. Every control URL needs this secret token."),
      h("div", { class: "token" },
        h("code", { class: "num", "aria-label": "Secret token" }, showKey ? token : "•".repeat(16)),
        h("button", { class: "icon-btn", type: "button", "aria-label": showKey ? "Hide the token" : "Show the token",
                      onclick: () => { showKey = !showKey; drawApi(); } }, icon(showKey ? "eyeOff" : "eye")),
        copyBtn(token, "the secret token")),
      h("dl", { class: "kv mt" }, ...examples.flatMap(([label, u]) => [h("dt", {}, label),
        h("dd", {}, copyBtn(u, `the URL: ${label}`, "Copy URL"))])),
      h("p", { class: "small muted mt" }, "GET or POST ", h("code", {}, "/api/control"), " with token= and any of "
        + "state=on|off, temp=5–40, level=1–5, fan=0–5|high|auto. ", h("code", {}, "/api/state"),
        " tells what the stove does. Add format=text for a sentence instead of JSON (Siri can speak it)."),
      h("p", { class: "small muted" }, h("code", {}, "/api/command"), " takes a sentence in text= — "
        + "“turn on and set the temperature to 23 degrees”, “fan auto”, “two degrees warmer”, “how is the stove”. "
        + "For Siri: a shortcut “Control the stove” — Ask for Input, URL Encode, Get Contents of URL (the Siri "
        + "shortcut URL with the encoded text after text=), Speak Text. Ready-made shortcuts and the steps: README."),
      h("p", { class: "small muted" }, "Keep the token secret: anyone at home who has it can control the stove."),
      h("button", {
        class: "link-btn", type: "button",
        onclick: async () => {
          const ok = await confirmSheet({ title: "Make a new secret token?", text: "URLs and shortcuts with the "
            + "current token stop working until you give them the new one.", ok: "New token", iconName: "key" });
          if (!ok) return;
          try { api = await rpc("urlapi.regenerate"); showKey = true; drawApi(); toast("New secret token made"); } catch (e) { toast(e.message, "bad"); }
        },
      }, "Make a new secret token"));
  }

  function drawStatus() {
    if (!info.configured) {
      statusCard.replaceChildren(h("div", { class: "card-head" }, h("span", { class: "label" }, "Remote access")),
        h("div", { class: "notice warn" }, icon("info", "sm"), h("span", {},
          "Off. Install the website on your VPS (", h("b", {}, "www/deploy.sh"), "), then enter its address on the Pi with ",
          h("b", {}, "sudo stove-master setup"), ".")));
      return;
    }
    statusCard.replaceChildren(h("div", { class: "card-head" }, h("span", { class: "label" }, "Remote access"),
      h("span", { class: "chip " + (info.connected ? "ok" : "bad") }, info.connected ? "Connected" : "Not connected")),
    h("dl", { class: "kv" },
      kvRow("Website", info.url, info.connected ? "ok" : "bad"),
      info.connected ? kvRow("Since", timeAgo(info.since * 1000)) : kvRow("Problem", info.last_error || "connecting…", "bad"),
      kvRow("Device id", info.device.id)));
  }

  function drawToken() {
    tokenCard.hidden = !info.configured;
    if (!info.configured) return;
    const tok = info.token;
    const shown = showToken ? tok : tok.slice(0, 9) + tok.slice(9).replace(/[A-Z2-7]/g, "•");
    tokenCard.replaceChildren(h("div", { class: "card-head" }, h("span", { class: "label" }, "Add to your account")),
      h("ol", { class: "steps-list" },
        h("li", {}, "Open ", h("a", { href: info.url, target: "_blank", rel: "noopener" }, breakable(info.url)),
          " and sign in, or create an account."),
        h("li", {}, "Choose ", h("b", {}, "Stoves → Add stove"), " and enter this token:")),
      h("div", { class: "token" }, h("code", { class: "num", "aria-label": "Token" }, shown),
        h("button", { class: "icon-btn", type: "button", "aria-label": showToken ? "Hide the token" : "Show the token",
                      onclick: () => { showToken = !showToken; drawToken(); } }, icon(showToken ? "eyeOff" : "eye")),
        h("button", { class: "icon-btn", type: "button", "aria-label": "Copy the token",
                      onclick: async () => toast(await copyText(tok) ? "Token copied" : "Copy it by hand, please", undefined) },
          icon("copy"))),
      h("a", { class: "btn primary block mt", href: info.add_url, target: "_blank", rel: "noopener" }, icon("globe", "sm"),
        "Open the website with the token"),
      h("p", { class: "small muted mt" }, "Anyone with this token can add the stove to their account. Accounts that "
        + "added it keep working when you make a new one."),
      h("button", {
        class: "link-btn", type: "button",
        onclick: async () => {
          const ok = await confirmSheet({ title: "Make a new token?", text: "The current token stops working. Accounts "
            + "that already added the stove keep their access.", ok: "New token", iconName: "key" });
          if (!ok) return;
          try { info = await rpc("cloud.regenerate"); showToken = true; draw(); toast("New token made"); } catch (e) { toast(e.message, "bad"); }
        },
      }, "Make a new token"));
  }

  function drawLinks() {
    linksCard.hidden = !info.configured;
    if (!info.configured) return;
    const links = info.links || [];
    linksCard.replaceChildren(h("div", { class: "card-head" }, h("span", { class: "label" }, "Accounts with access"),
      h("span", { class: "value-tag" }, String(links.length))),
    ...(links.length ? links.map(l => h("div", { class: "person" }, avatar(l.account),
      h("div", { class: "who" },
        h("div", { class: "nm" }, l.account, l.online ? h("span", { class: "chip ok" }, "online now") : null),
        h("div", { class: "em" }, `Added ${new Date(l.added * 1000).toLocaleDateString()} · used ${ago(l.last_seen)}`)),
      h("button", {
        class: "icon-btn", type: "button", "aria-label": `Remove ${l.account}`,
        onclick: async () => {
          const ok = await confirmSheet({ title: `Remove ${l.account}?`, text: "This account can no longer reach the "
            + "stove from outside. It can be added again with the token.", ok: "Remove", danger: true, iconName: "trash" });
          if (!ok) return;
          try { info = await rpc("cloud.unlink", { link: l.id }); draw(); toast(`${l.account} removed`); } catch (e) { toast(e.message, "bad"); }
        },
      }, icon("trash"))))
      : [h("p", { class: "muted small" }, "No account has added this stove yet.")]));
  }

  function drawStove() {
    const name = h("input", { type: "text", maxlength: "40", value: (sys && sys.device.name) || "Stove", "aria-label": "Name" });
    const save = h("button", {
      class: "btn sm", type: "button",
      onclick: async () => {
        try { const j = await rpc("device.rename", { name: name.value }); setBrand(j.name); toast("Renamed"); } catch (e) { toast(e.message, "bad"); }
      },
    }, "Save");
    const rows = [];
    if (sys) {
      const ifaces = Object.entries(sys.mdns.interfaces || {}).map(([k, v]) => `${k} ${v}`).join(", ");
      rows.push(
        kvRow("Connection Box", (sys.box.host || "searching…") + (sys.box.auto ? " (found automatically)" : "")
          + (sys.box.last_error ? ` — ${sys.box.last_error}` : ""), sys.box.last_error ? "bad" : sys.box.host ? "ok" : "warn"),
        kvRow("At home", sys.lan_url + (sys.mdns.enabled ? (ifaces ? ` (${ifaces})` : "") : " (name not announced)"),
          sys.mdns.enabled ? "ok" : "warn"),
        kvRow("Hub clock", sys.time + (sys.clock_ok ? "" : " (waiting for NTP)"), sys.clock_ok ? "ok" : "warn"),
        kvRow("Version", `${sys.version} · Python ${sys.python} · up ${fmtUptime(sys.uptime_s)}`));
    }
    stoveCard.replaceChildren(h("div", { class: "card-head" }, h("span", { class: "label" }, "This stove")),
      h("div", { class: "row-input" }, withIcon("flame", name), save),
      h("p", { class: "small muted" }, "The name shown in the app and in notifications."),
      h("dl", { class: "kv mt" }, ...rows), updBox);
    drawUpdate();
  }

  // "Update now": the hub asks systemd to run tools/auto-update.sh; it restarts when there is something new
  const updBox = h("div", { class: "mt" });
  let updPoll = null;
  function drawUpdate() {
    if (!sys) return;
    updBox.replaceChildren(updateBox(sys.update, {
      howToEnable: "sudo pi/install.sh --auto-update",
      onRequest: async () => {
        const ok = await confirmSheet({ title: "Update now?", text: "The hub fetches the newest tested version "
          + "from GitHub. If there is one, it installs it and restarts — this page reconnects by itself.",
          ok: "Update", iconName: "refresh" });
        if (!ok) return;
        try { sys.update = await rpc("system.update"); drawUpdate(); toast("Update started"); } catch (e) { toast(e.message, "bad"); }
      },
    }));
    if (updateBusy(sys.update) && !updPoll) {
      updPoll = setInterval(async () => {
        if (!alive()) { clearInterval(updPoll); updPoll = null; return; }
        try {
          const s = await rpc("system.get");
          const wasBusy = updateBusy(sys.update);
          sys = s;
          drawUpdate();
          if (wasBusy && !updateBusy(s.update) && s.update.last) {
            toast(s.update.last.result === "failed" ? "The update failed" : s.update.last.result === "updated"
              ? "Updated" : "Already up to date", s.update.last.result === "failed" ? "bad" : undefined);
          }
        } catch (e) { /* restarting: the next try reaches the new version */ }
      }, 3000);
    } else if (!updateBusy(sys.update) && updPoll) {
      clearInterval(updPoll);
      updPoll = null;
    }
  }

  function draw() {
    if (!alive() || !info) return;
    drawStatus();
    drawToken();
    drawLinks();
    if (!stoveCard.firstChild) drawStove();
  }

  async function load() {
    [info, sys, api] = await Promise.all([rpc("cloud.info"), rpc("system.get"), rpc("urlapi.info")]);
    stoveCard.replaceChildren();
    draw();
    drawApi();
  }

  statusCard.append(h("div", { class: "skel line" }), h("div", { class: "skel line short" }));
  onLink("online", d => { info = d; draw(); });
  onLink("state", st => { if (st.state === "open") load().catch(() => {}); });
  viewTimer(() => { if (info) drawStatus(); }, 30000);
  await load();
};
