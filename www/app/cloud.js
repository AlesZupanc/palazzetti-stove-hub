"use strict";
/* Website only (https://example.com/mystove/): accounts, the encrypted vault with
   the stoves and their keys, choosing a stove, adding one with its token, the
   account page and the administrators' Users page. See docs/protocol.md. */

const Cloud = {
  vaultKey: null,         // CryptoKey, never leaves the browser
  vaultVersion: 0,
  devices: [],            // from the website: which stoves are linked and online

  async init() {
    try {
      const j = await api("GET", "me");
      if (j.user) {
        Stove.me = j.user;
        Cloud.meta = { salt: j.salt, iterations: j.iterations };
        const stored = await KeyStore.get("vault");
        if (stored && stored.user === j.user.id && stored.key) {
          try {
            await Cloud.openVault(stored.key, j.vault);
          } catch (e) {
            Cloud.vaultKey = null;      // password changed elsewhere: ask for it again
          }
        }
      }
    } catch (e) {
      Stove.loadError = e;
    }
    if (Stove.vault) Cloud.pickDefault();
    Push.init();
  },

  async openVault(key, vault) {
    Stove.vault = await Vault.decrypt(key, vault.blob);
    Cloud.vaultKey = key;
    Cloud.vaultVersion = vault.version;
  },

  /* Which screen may be shown (sign in first, then unlock the vault). */
  gate(name) {
    if (!Stove.me) return PUBLIC.has(name) ? name : "login";
    if (!Cloud.vaultKey) return ["reset", "approve", "setup", "unlock"].includes(name) ? name : "unlock";
    if (["login", "register", "forgot", "unlock"].includes(name)) return "";
    return name;
  },

  pickDefault() {
    let last = null;
    try { last = localStorage.getItem("stove-device"); } catch (e) { /* no storage */ }
    const list = Stove.vault.devices;
    const dev = list.find(d => d.id === last) || list[0];
    if (dev) Cloud.select(dev);
  },

  select(dev) {
    if (Stove.link) Stove.link.close();
    Stove.device = dev;
    try { localStorage.setItem("stove-device", dev.id); } catch (e) { /* not remembered */ }
    const link = new RemoteLink(dev.id, "link", dev.link, Bytes.unb64(dev.key));
    Stove.link = link;
    watchLink(link);
    setBrand(dev.name);
    link.on("hello", d => {
      if (d.device && d.device.name && d.device.name !== dev.name) {      // renamed at home: keep the vault current
        Cloud.updateVault(v => { const e = v.devices.find(x => x.id === dev.id); if (e) e.name = d.device.name; })
          .catch(() => {});
        dev.name = d.device.name;
      }
    });
    link.on("state", st => {
      if (link !== Stove.link) return;
      if (st.state === "open") Push.onStoveOpen(link);
      if (st.state === "revoked") toast(`${dev.name}: ${st.text}`, "bad");
      if (st.state === "signed-out") Cloud.signedOut();
    });
  },

  /* Change the vault and store it again; retries on top of a newer copy from another device. */
  async updateVault(change) {
    for (let attempt = 0; attempt < 3; attempt++) {
      const copy = JSON.parse(JSON.stringify(Stove.vault));
      change(copy);
      const blob = await Vault.encrypt(Cloud.vaultKey, copy);
      try {
        const j = await api("PUT", "vault", { blob, version: Cloud.vaultVersion });
        Stove.vault = copy;
        Cloud.vaultVersion = j.version;
        return copy;
      } catch (e) {
        if (e.status !== 409) throw e;
        await Cloud.openVault(Cloud.vaultKey, e.data.vault);      // somebody else saved first: merge on top
      }
    }
    throw new Error("Your stoves were changed on another device at the same time. Try again.");
  },

  async signIn(login, password) {
    const pre = await api("POST", "prelogin", { login });
    const keys = await Account.derive(password, pre.salt, pre.iterations);
    const j = await api("POST", "login", { login, auth: keys.auth });
    Stove.me = j.user;
    Cloud.meta = { salt: pre.salt, iterations: pre.iterations };
    await Cloud.openVault(keys.vaultKey, j.vault);
    await KeyStore.set("vault", { user: j.user.id, key: keys.vaultKey });
    Cloud.pickDefault();
    Push.init();
  },

  async signOut(everywhere) {
    try { await api("POST", everywhere ? "sessions/revoke" : "logout"); } catch (e) { /* signed out here anyway */ }
    await Cloud.signedOut();
  },

  async signedOut() {
    if (!Stove.me && !Cloud.vaultKey) return;
    if (Stove.link) Stove.link.close();
    Object.assign(Stove, { me: null, link: null, device: null, vault: null });
    Cloud.vaultKey = null;
    await KeyStore.del("vault");
    setBrand("Stove");
    go("#/login");
  },

  async loadDevices() {
    try { Cloud.devices = (await api("GET", "devices")).devices; } catch (e) { /* shown as unknown */ }
    return Cloud.devices;
  },
};

/* ------------------------------------------------------------ sign in etc. */
Stove.views.login = async function viewLogin(root) {
  if (Stove.me) { go("#/"); return; }
  const f = Stove.env;
  const login = h("input", { type: "text", autocomplete: "username", autocapitalize: "none", spellcheck: "false",
                             required: true, placeholder: "Username or e-mail" });
  const pw = passwordInput({ autocomplete: "current-password", placeholder: "Password" });
  authPage(root, "Welcome", "Sign in to reach your stove from anywhere.",
    authForm([field("Username or e-mail", withIcon("user", login)), field("Password", pw.el)], "Sign in", async () => {
      try {
        await Cloud.signIn(login.value.trim(), pw.input.value);
      } catch (e) {
        pw.input.select();
        throw e;
      }
      const target = Stove.after || "#/";
      Stove.after = null;
      go(target);
    }, h("div", { class: "auth-links" },
      f.registration ? h("a", { href: "#/register" }, "Create an account") : h("span"),
      f.mail ? h("a", { href: "#/forgot" }, "Forgot password?") : null)),
    Stove.after && Stove.after.startsWith("#/add/")
      ? h("div", { class: "notice" }, icon("info", "sm"), h("span", {}, "Sign in (or create an account) to add the stove."))
      : null,
    h("div", { class: "auth-foot" }, h("span", { class: "row" }, icon("lock", "sm"),
      "Your password never leaves this device; your stoves are end-to-end encrypted.")));
  login.focus();
};

Stove.views.register = async function viewRegister(root) {
  if (!Stove.env.registration) {
    authPage(root, "Create an account", null,
      doneCard("lock", "Registration is closed", "Ask the administrator to create an account for you."));
    return;
  }
  const user = h("input", { type: "text", autocomplete: "username", autocapitalize: "none", spellcheck: "false",
                            required: true, minlength: "3", maxlength: "32", placeholder: "e.g. ana" });
  const email = h("input", { type: "email", autocomplete: "email", required: true, placeholder: "you@example.com" });
  const pw = passwordInput({ autocomplete: "new-password", minlength: "8" });
  const pw2 = passwordInput({ autocomplete: "new-password" });
  const content = h("div");
  content.append(authForm([
    field("Username", withIcon("user", user), "3–32 letters, digits, dot, dash or underscore."),
    field("E-mail", withIcon("mail", email), "You get a message here once you are approved."),
    field("Password", pw.el, "At least 8 characters. It also protects your stoves' keys: if you forget it, "
      + "you add the stoves again."),
    field("Repeat the password", pw2.el)], "Request account", async () => {
    if (pw.input.value.length < 8) throw new Error("The password needs at least 8 characters.");
    if (pw.input.value !== pw2.input.value) throw new Error("The passwords do not match.");
    const salt = Account.newSalt();
    const keys = await Account.derive(pw.input.value, salt);
    const j = await api("POST", "register", { username: user.value.trim(), email: email.value.trim(), salt,
                                              auth: keys.auth, iterations: Account.ITERATIONS });
    content.replaceChildren(doneCard("check", "Request sent", j.message));
  }, h("div", { class: "auth-links" }, h("a", { href: "#/login" }, "I already have an account"))));
  authPage(root, "Create an account", "An administrator approves every new account.", content);
  user.focus();
};

Stove.views.forgot = async function viewForgot(root) {
  const login = h("input", { type: "text", autocomplete: "username", autocapitalize: "none", spellcheck: "false",
                             required: true, placeholder: "Username or e-mail" });
  const content = h("div");
  content.append(authForm([field("Username or e-mail", withIcon("user", login))], "Send reset link", async () => {
    const j = await api("POST", "password/forgot", { login: login.value.trim() });
    content.replaceChildren(doneCard("mail", "Check your e-mail", j.message));
  }, h("div", { class: "auth-links" }, h("a", { href: "#/login" }, "Back to sign in"))));
  authPage(root, "Reset your password", "We e-mail you a link to choose a new one.", content);
  login.focus();
};

function newPasswordPage(root, title, subtitle, warning, submitText, send) {
  const pw = passwordInput({ autocomplete: "new-password", minlength: "8" });
  const pw2 = passwordInput({ autocomplete: "new-password" });
  const content = h("div");
  content.append(authForm([warning, field("New password", pw.el, "At least 8 characters."), field("Repeat it", pw2.el)],
    submitText, async () => {
      if (pw.input.value.length < 8) throw new Error("The password needs at least 8 characters.");
      if (pw.input.value !== pw2.input.value) throw new Error("The passwords do not match.");
      const salt = Account.newSalt();
      const keys = await Account.derive(pw.input.value, salt);
      await send(content, { salt, auth: keys.auth, iterations: Account.ITERATIONS }, pw.input.value);
    }));
  authPage(root, title, subtitle, content);
  pw.input.focus();
}

Stove.views.reset = async function viewReset(root, token) {
  if (!token) {
    authPage(root, "Reset your password", null,
      doneCard("warn", "Link incomplete", "Request a new link and open it from the e-mail.", "Request a new link", "#/forgot"));
    return;
  }
  newPasswordPage(root, "Choose a new password", null,
    h("div", { class: "notice warn" }, icon("info", "sm"), h("span", {}, "Your stoves' keys were protected by the old "
      + "password, so you add the stoves again afterwards (with their tokens).")),
    "Set new password", async (content, keys) => {
      const j = await api("POST", "password/reset", Object.assign({ token }, keys));
      await Cloud.signedOut().catch(() => {});
      content.replaceChildren(doneCard("check", "Password changed", j.message, "Sign in"));
    });
};

Stove.views.setup = async function viewSetup(root, token) {
  newPasswordPage(root, "Welcome, administrator", "Choose the password for your account.", null, "Set password",
    async (content, keys, password) => {
      const j = await api("POST", "setup", Object.assign({ token }, keys));
      await Cloud.signIn(j.username, password);
      go("#/stoves");
    });
};

Stove.views.unlock = async function viewUnlock(root) {
  const pw = passwordInput({ autocomplete: "current-password", placeholder: "Password" });
  authPage(root, "Unlock your stoves", `Signed in as ${Stove.me.username}. Enter your password once on this device `
    + "to unlock the keys of your stoves.",
    authForm([field("Password", pw.el)], "Unlock", async () => {
      try {
        await Cloud.signIn(Stove.me.username, pw.input.value);
      } catch (e) {
        pw.input.select();
        throw e;
      }
      const target = Stove.after || "#/";
      Stove.after = null;
      go(target);
    }, h("div", { class: "auth-links" }, h("button", { class: "link-btn", type: "button", onclick: () => Cloud.signOut() },
      "Sign in with another account"))));
  pw.input.focus();
};

Stove.views.approve = async function viewApprove(root, token, alive) {
  let info;
  try {
    info = await api("POST", "approval", { token });
  } catch (e) {
    authPage(root, "Account request", null, doneCard("warn", "Link not valid", e.message,
      Stove.me && Stove.me.role === "admin" ? "Open Users" : "Sign in", Stove.me && Stove.me.role === "admin" ? "#/users" : "#/login"));
    return;
  }
  if (!alive()) return;
  const u = info.user;
  const content = h("div");
  async function decide(decision) {
    if (decision === "reject") {
      const ok = await confirmSheet({ title: `Reject ${u.username}?`, text: "The request is deleted.", ok: "Reject",
        danger: true, iconName: "x" });
      if (!ok) return;
    }
    content.querySelectorAll("button").forEach(b => { b.disabled = true; });
    try {
      const j = await api("POST", "approval/decide", { token, decision });
      content.replaceChildren(doneCard(decision === "approve" ? "check" : "x",
        decision === "approve" ? "Approved" : "Rejected", j.message,
        Stove.me ? "Open the app" : "Sign in", Stove.me ? "#/" : "#/login"));
    } catch (e) {
      toast(e.message, "bad");
      content.querySelectorAll("button").forEach(b => { b.disabled = false; });
    }
  }
  content.append(h("section", { class: "card" },
    h("div", { class: "row" }, avatar(u.username, "lg"), h("div", {}, h("div", { class: "hero-state" }, u.username),
      h("div", { class: "muted" }, u.email))),
    h("dl", { class: "kv mt" }, h("dt", {}, "Asked"), h("dd", {}, u.created || "–"), h("dt", {}, "From"), h("dd", {}, u.ip || "–")),
    h("div", { class: "btns mt" },
      h("button", { class: "btn danger", type: "button", onclick: () => decide("reject") }, "Reject"),
      h("button", { class: "btn primary", type: "button", onclick: () => decide("approve") }, icon("check", "sm"), "Approve"))));
  authPage(root, "Account request", "Someone would like to use the stove app.", content);
};

/* ------------------------------------------------------------------ stoves */
Stove.views.stoves = async function viewStoves(root, arg, alive) {
  const list = h("div", { class: "stack" });
  root.append(h("div", { class: "screen" },
    h("div", { class: "page-title" }, "Stoves"),
    h("p", { class: "page-sub" }, "The stoves your account can reach."),
    list,
    h("a", { class: "btn primary block mt", href: "#/add" }, icon("plus", "sm"), "Add a stove")));

  function draw() {
    const vault = Stove.vault.devices;
    if (!vault.length) {
      list.replaceChildren(h("section", { class: "card empty" }, h("div", { class: "art" }, icon("flame")),
        h("h3", {}, "No stove yet"),
        h("p", {}, "At home, open the stove's page (", h("b", {}, "http://stove.local"), ") → ", h("b", {}, "Online"),
          ". Use its link, or copy the token and tap ", h("b", {}, "Add a stove"), " here.")));
      return;
    }
    list.replaceChildren(...vault.map(d => {
      const known = Cloud.devices.find(x => x.id === d.id);
      const state = !known ? (Cloud.devices.length || Cloud.loaded ? "gone" : "?") : known.online ? "online" : "offline";
      const chip = { online: h("span", { class: "chip ok" }, "online"), offline: h("span", { class: "chip" }, "offline"),
                     gone: h("span", { class: "chip bad" }, "no access"), "?": null }[state];
      const current = Stove.device && Stove.device.id === d.id;
      return h("section", { class: "card stove-card" + (current ? " current" : "") },
        h("div", { class: "row" },
          h("div", { class: "stove-ico" }, icon("flame")),
          h("div", { class: "who" }, h("div", { class: "nm" }, d.name || "Stove", chip),
            h("div", { class: "em" }, `Id ${d.id}` + (known && known.last_seen && state !== "online"
              ? ` · last seen ${timeAgo(known.last_seen * 1000)}` : ""))),
          h("button", { class: "icon-btn", type: "button", "aria-label": `More for ${d.name}`, onclick: () => more(d) },
            icon("more"))),
        state === "gone"
          ? h("p", { class: "small muted mt" }, "This stove no longer gives your account access (removed at home). "
            + "Remove it here, or add it again with its token.")
          : h("button", { class: "btn block mt" + (current ? "" : " primary"), type: "button",
                          onclick: () => { Cloud.select(d); go("#/"); } },
            current ? "Open (current)" : "Open"));
    }));
  }

  function more(d) {
    sheet(d.name || "Stove", (panel, close) => {
      panel.append(h("div", { class: "menu-list" },
        h("button", { class: "btn danger", type: "button", onclick: () => { close(); remove(d); } }, icon("trash"),
          "Remove from my account")));
    });
  }

  async function remove(d) {
    const ok = await confirmSheet({ title: `Remove ${d.name}?`, text: "You can no longer reach it from here. To get it "
      + "back you need its token again.", ok: "Remove", danger: true, iconName: "trash" });
    if (!ok) return;
    try {
      if (Stove.device && Stove.device.id === d.id && Stove.link && Stove.link.state === "open") {
        await rpc("link.leave", {}, 8000).catch(() => {});        // tell the stove too, if it is reachable
      }
      await api("DELETE", `devices/${d.id}`).catch(() => {});
      await Cloud.updateVault(v => { v.devices = v.devices.filter(x => x.id !== d.id); });
      if (Stove.device && Stove.device.id === d.id) {
        Stove.link.close();
        Stove.link = null;
        Stove.device = null;
        setBrand("Stove");
        Cloud.pickDefault();
      }
      toast(`${d.name} removed`);
      draw();
    } catch (e) {
      toast(e.message, "bad");
    }
  }

  draw();
  await Cloud.loadDevices();
  Cloud.loaded = true;
  if (alive()) draw();
  viewTimer(async () => { await Cloud.loadDevices(); if (alive()) draw(); }, 30000);
};

/* Add a stove: its token (typed, pasted, or from the link on the local dashboard). */
Stove.views.add = async function viewAdd(root, arg, alive) {
  const input = h("input", { type: "text", autocomplete: "off", autocapitalize: "characters", spellcheck: "false",
                             placeholder: "XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX-XXXX", class: "num", value: arg ? Token.pretty(arg) : "" });
  const content = h("div");
  let pairing = null;
  viewUnsubs.push(() => { if (pairing) pairing.close(); });

  function ask() {
    content.replaceChildren(authForm([
      h("p", { class: "muted small" }, "At home, open the stove's page (http://stove.local) → Online, and copy its token."),
      field("Token", withIcon("key", input))], "Connect", connect,
    h("div", { class: "auth-links" }, h("a", { href: "#/stoves" }, "Cancel"))));
  }

  async function connect() {
    const t = Token.parse(input.value);
    if (Stove.vault.devices.some(d => d.id === t.deviceId)) {
      const ok = await confirmSheet({ title: "Already added", text: "This stove is in your account already. Add it again "
        + "(for example after a password reset)?", ok: "Add again", iconName: "info" });
      if (!ok) return;
    }
    if (pairing) pairing.close();
    pairing = new RemoteLink(t.deviceId, "pair", null, t.secret);
    const hello = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(pairing.state === "offline" ? pairing.stateText
        : "The stove did not answer. Is its hub at home connected?")), 20000);
      pairing.on("hello", d => { clearTimeout(timer); resolve(d); });
      pairing.on("state", st => {
        if (["failed", "revoked", "signed-out"].includes(st.state)) { clearTimeout(timer); reject(new Error(st.text)); }
        if (st.state === "offline") {           // the website knows no connected stove with this id
          clearTimeout(timer);
          reject(new Error(`Stove ${t.deviceId} is not connected to the website right now. Check the token, and that `
            + "the hub at home is running and online."));
        }
      });
    }).catch(e => { pairing.close(); pairing = null; throw e; });
    if (!alive()) return;
    confirm(t, hello);
  }

  function confirm(t, hello) {
    const name = (hello.device && hello.device.name) || "Stove";
    let added = null;     // pair.finish works once per session; a retry only saves to the vault again
    content.replaceChildren(h("section", { class: "card center" },
      h("div", { class: "done-icon" }, icon("flame")),
      h("h2", {}, `Add ${name}?`),
      h("p", { class: "muted" }, `Stove id ${t.deviceId}. It proved it has this token, and everything between it `
        + "and this device is end-to-end encrypted."),
      h("div", { class: "btns mt" },
        h("button", { class: "btn", type: "button", onclick: () => { pairing.close(); pairing = null; ask(); } }, "Cancel"),
        h("button", {
          class: "btn primary", type: "button",
          onclick: async ev => {
            const button = ev.currentTarget;
            button.disabled = true;
            try {
              const r = added || (added = await pairing.call("pair.finish", {}));
              const entry = { id: r.device.id, link: r.link, key: r.key, name: r.device.name || name,
                              added: Math.floor(Date.now() / 1000) };
              await Cloud.updateVault(v => { v.devices = v.devices.filter(x => x.id !== entry.id).concat([entry]); });
              pairing.close();
              pairing = null;
              Cloud.select(entry);
              toast(`${entry.name} added`);
              go("#/");
            } catch (e) {
              toast(e.message, "bad");
              button.disabled = false;
            }
          },
        }, icon("check", "sm"), "Add"))));
  }

  ask();
  authPage(root, "Add a stove", "Connect your account to a stove with its token.", content);
  if (arg) setTimeout(() => content.querySelector("form").requestSubmit(), 50);
};

/* ----------------------------------------------------------------- account */
Stove.views.account = async function viewAccount(root) {
  const me = Stove.me;
  const err = h("div", { class: "err" });
  const old = passwordInput({ autocomplete: "current-password" });
  const nw = passwordInput({ autocomplete: "new-password", minlength: "8" });
  const nw2 = passwordInput({ autocomplete: "new-password" });
  const btn = h("button", { class: "btn primary block", type: "submit" }, "Change password");
  const pushCard = h("section", { class: "card" });
  root.append(h("div", { class: "screen" },
    h("div", { class: "page-title" }, "Account"),
    h("section", { class: "card" }, h("div", { class: "row" }, avatar(me.username, "lg"),
      h("div", {}, h("div", { class: "hero-state" }, me.username), h("div", { class: "muted" }, me.email),
        h("div", { class: "chips mt" }, h("span", { class: "chip " + (me.role === "admin" ? "accent" : "") },
          icon(me.role === "admin" ? "shield" : "user"), me.role === "admin" ? "Administrator" : "User"))))),
    me.role === "admin" ? h("a", { class: "card link-card", href: "#/users" }, icon("users"),
      h("span", {}, h("b", {}, "Users"), h("span", { class: "muted small" }, "Approve requests, manage accounts")),
      icon("chevron")) : null,
    pushCard,
    h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "label" }, "Appearance")),
      themePicker()),
    h("form", {
      class: "card",
      onsubmit: async ev => {
        ev.preventDefault();
        err.textContent = "";
        if (nw.input.value.length < 8) { err.textContent = "The new password needs at least 8 characters."; return; }
        if (nw.input.value !== nw2.input.value) { err.textContent = "The new passwords do not match."; return; }
        btn.disabled = true;
        try {
          const pre = await api("POST", "prelogin", { login: me.username });
          const oldKeys = await Account.derive(old.input.value, pre.salt, pre.iterations);
          const salt = Account.newSalt();
          const keys = await Account.derive(nw.input.value, salt);
          const vault = await Vault.encrypt(keys.vaultKey, Stove.vault);      // the stoves move to the new key
          const j = await api("POST", "password/change", { old_auth: oldKeys.auth, salt, auth: keys.auth,
                                                           iterations: Account.ITERATIONS, vault });
          Cloud.vaultKey = keys.vaultKey;
          Cloud.vaultVersion = j.vault.version;
          await KeyStore.set("vault", { user: me.id, key: keys.vaultKey });
          toast(j.message);
          old.input.value = nw.input.value = nw2.input.value = "";
        } catch (e) {
          err.textContent = e.message;
        }
        btn.disabled = false;
      },
    }, h("div", { class: "card-head" }, h("span", { class: "label" }, "Password")),
    field("Current password", old.el), field("New password", nw.el, "At least 8 characters."),
    field("Repeat the new password", nw2.el), err, btn),
    h("section", { class: "card" }, h("div", { class: "card-head" }, h("span", { class: "label" }, "Sessions")),
      h("div", { class: "btns" },
        h("button", { class: "btn", type: "button", onclick: () => Cloud.signOut(false) }, icon("logout", "sm"), "Sign out"),
        h("button", {
          class: "btn", type: "button",
          onclick: async () => {
            const ok = await confirmSheet({ title: "Sign out everywhere?", text: "All your phones and computers have to sign in again.",
              ok: "Sign out everywhere", danger: true, iconName: "logout" });
            if (ok) Cloud.signOut(true);
          },
        }, "Everywhere"))),
    h("div", { class: "auth-foot" }, `Stove website ${Stove.env.version || ""}`)));
  Push.card(pushCard);
};

/* ------------------------------------------------------------ users (admin) */
Stove.views.users = async function viewUsers(root, arg, alive) {
  if (!Stove.me || Stove.me.role !== "admin") { go("#/account"); return; }
  const pendingCard = h("section", { class: "card" });
  const usersCard = h("section", { class: "card" });
  const sysCard = h("section", { class: "card" }, h("div", { class: "skel line" }), h("div", { class: "skel line short" }));
  root.append(h("div", { class: "screen" }, h("a", { class: "back-link", href: "#/account" }, icon("back", "sm"), "Account"),
    h("div", { class: "page-title" }, "Users"), pendingCard, usersCard, sysCard));

  async function act(u, action, question) {
    if (question) {
      const ok = await confirmSheet(question);
      if (!ok) return;
    }
    try {
      const j = await api("POST", `users/${u.id}`, { action });
      render(j.users);
      toast({ approve: `${u.username} can sign in now`, reject: "Request rejected", delete: "User deleted" }[action] || "Done");
    } catch (e) {
      toast(e.message, "bad");
    }
  }

  function actions(u) {
    sheet(u.username, (panel, close) => {
      const run = (action, q) => () => { close(); act(u, action, q); };
      panel.append(h("div", { class: "row mt" }, avatar(u.username, "lg"), h("div", {},
        h("b", {}, u.username), h("div", { class: "small muted" }, u.email))),
      h("div", { class: "menu-list mt" },
        u.role === "admin"
          ? h("button", { class: "btn", type: "button", onclick: run("make_user") }, icon("user"), "Remove administrator role")
          : h("button", { class: "btn", type: "button", onclick: run("make_admin", { title: `Make ${u.username} an administrator?`,
              text: "Administrators approve new accounts and manage users.", ok: "Make administrator", iconName: "shield" }) },
            icon("shield"), "Make administrator"),
        u.status === "active"
          ? h("button", { class: "btn", type: "button", onclick: run("disable", { title: `Disable ${u.username}?`,
              text: "They are signed out at once and cannot sign in until enabled again.", ok: "Disable", danger: true, iconName: "lock" }) },
            icon("lock"), "Disable")
          : h("button", { class: "btn", type: "button", onclick: run("enable") }, icon("check"), "Enable"),
        h("button", { class: "btn danger", type: "button", onclick: run("delete", { title: `Delete ${u.username}?`,
          text: "The account and its stove links are removed for good.", ok: "Delete", danger: true, iconName: "trash" }) },
          icon("trash"), "Delete")));
    });
  }

  function render(users) {
    const pending = users.filter(u => u.status === "pending" && u.has_password);
    pendingCard.hidden = !pending.length;
    pendingCard.replaceChildren(h("div", { class: "card-head" }, h("span", { class: "label" }, "Waiting for approval"),
      h("span", { class: "chip warn" }, String(pending.length))),
    ...pending.map(u => h("div", { class: "request" },
      h("div", { class: "row" }, avatar(u.username), h("div", {}, h("b", {}, u.username), h("div", { class: "small" }, u.email))),
      h("div", { class: "meta" }, `Asked ${u.created}${u.ip ? " from " + u.ip : ""}`),
      h("div", { class: "btns" },
        h("button", { class: "btn sm danger", type: "button", onclick: () => act(u, "reject", { title: `Reject ${u.username}?`,
          text: "The request is deleted.", ok: "Reject", danger: true, iconName: "x" }) }, "Reject"),
        h("button", { class: "btn sm primary", type: "button", onclick: () => act(u, "approve") }, icon("check", "sm"), "Approve")))));
    const others = users.filter(u => !(u.status === "pending" && u.has_password));
    usersCard.replaceChildren(h("div", { class: "card-head" }, h("span", { class: "label" }, "People"),
      h("span", { class: "value-tag" }, String(others.length))),
    ...others.map(u => h("div", { class: "person" }, avatar(u.username),
      h("div", { class: "who" },
        h("div", { class: "nm" }, u.username, u.me ? h("span", { class: "chip" }, "you") : null,
          u.role === "admin" ? h("span", { class: "chip accent" }, "admin") : null,
          u.status === "disabled" ? h("span", { class: "chip bad" }, "disabled") : null,
          !u.has_password ? h("span", { class: "chip warn" }, "no password yet") : null),
        h("div", { class: "em" }, u.email),
        h("div", { class: "em" }, `${u.stoves} stove${u.stoves === 1 ? "" : "s"} · `
          + (u.last ? `last sign-in ${u.last}` : "never signed in"))),
      u.me ? null : h("button", { class: "icon-btn", type: "button", "aria-label": `Actions for ${u.username}`, onclick: () => actions(u) },
        icon("more")))));
  }

  // "Update now" for the website itself (tools/auto-update.sh on the VPS)
  const updBox = h("div", { class: "mt" });
  let updPoll = null, upd = null;
  function drawUpdate(u) {
    const wasBusy = updateBusy(upd);
    upd = u;
    if (wasBusy && !updateBusy(u) && u && u.last) {
      toast(u.last.result === "failed" ? "The update failed" : u.last.result === "updated" ? "Website updated — reload the page for the new app"
        : "Already up to date", u.last.result === "failed" ? "bad" : undefined);
    }
    updBox.replaceChildren(updateBox(u, {
      howToEnable: "sudo www/deploy.sh --auto-update",
      onRequest: async () => {
        const ok = await confirmSheet({ title: "Update the website now?", text: "It fetches the newest tested version "
          + "from GitHub. If there is one, it installs it and restarts — the stoves and this page reconnect by themselves.",
          ok: "Update", iconName: "refresh" });
        if (!ok) return;
        try { drawUpdate(await api("POST", "system/update")); toast("Update started"); } catch (e) { toast(e.message, "bad"); }
      },
    }));
    if (updateBusy(u) && !updPoll) {
      updPoll = setInterval(async () => {
        if (!alive()) { clearInterval(updPoll); updPoll = null; return; }
        try { drawUpdate((await api("GET", "system")).update); } catch (e) { /* restarting */ }
      }, 3000);
    } else if (!updateBusy(u) && updPoll) {
      clearInterval(updPoll);
      updPoll = null;
    }
  }

  const j = await api("GET", "users");
  if (!alive()) return;
  render(j.users);
  try {
    const s = await api("GET", "system");
    if (!alive()) return;
    sysCard.replaceChildren(h("div", { class: "card-head" }, h("span", { class: "label" }, "Website")),
      h("dl", { class: "kv" },
        kvRow("Address", s.public_url),
        kvRow("Stoves online", `${s.devices_online} of ${s.devices_known} known`, s.devices_online ? "ok" : "warn"),
        kvRow("E-mail", s.mail.configured ? `Sending as ${s.mail.sender}` : "Not set up (links go to the log)",
          s.mail.configured ? "ok" : "warn"),
        kvRow("Push notifications", s.push ? "Available" : "Off (install python3-cryptography)", s.push ? "ok" : "warn"),
        kvRow("Registration", s.registration ? "Open (with approval)" : "Closed"),
        kvRow("Version", `${s.version} · app ${s.app} · up ${fmtUptime(s.uptime_s)}`)), updBox);
    drawUpdate(s.update);
  } catch (e) {
    sysCard.replaceChildren(h("div", { class: "err" }, e.message));
  }
};
