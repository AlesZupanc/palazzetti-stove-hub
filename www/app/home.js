"use strict";
/* Home screen: stove status, power, thermostat dial, flame power, room fan,
   today's schedule, the temperature probes and the pellet tank. */

const FLAME_SVG = `<svg class="flame" viewBox="0 0 64 64" aria-hidden="true" focusable="false">
  <defs><linearGradient id="flameGrad" x1="0" y1="1" x2="0" y2="0">
    <stop offset="0" stop-color="#e4572e"/><stop offset=".55" stop-color="#ff8b3d"/><stop offset="1" stop-color="#ffc35a"/>
  </linearGradient></defs>
  <path class="outer" d="M32 5c2.2 9.2-3.6 14.1-8.1 19.1C19.8 28.7 16 33.2 16 40c0 10.2 7.2 18 16 18s16-7.6 16-17.8c0-7.8-4.3-12.6-7.7-16.8-.8 4-2.8 6.9-6.1 8.6 1.5-9.5-.4-17.8-2.2-27z"/>
  <path class="inner" d="M32.6 31c.9 5-1.6 7.4-3.9 9.9-1.9 2.1-3.4 4.3-3.4 7.4 0 4.6 3.1 7.7 6.9 7.7s6.9-3.2 6.9-7.5c0-4.2-2.5-6.5-4.1-8.9-.5 1.9-1.4 3.1-2.8 3.8.6-4.4-.1-7.7.4-12.4z"/>
</svg>`;

const IGNITION = { 2: [1, "Test fire"], 3: [2, "Heat-up"], 4: [3, "Fuel ignition"], 5: [4, "Ignition test"] };
const PROBES = ["T1", "T2", "T3", "T4", "T5"];

const fmtKg = kg => (Math.round(kg * 10) / 10).toLocaleString(undefined, { maximumFractionDigits: 1 });
const fmtDay = t => new Date(t * 1000).toLocaleDateString(undefined, { day: "numeric", month: "short" });
const fmtHours = n => n < 1 ? "less than an hour" : n < 48 ? `about ${Math.round(n)} h` : `about ${Math.round(n / 24)} days`;
const NO_PELLETS = 253;     // the stove's "no pellets" alarm (STATUS)

/* kg typed with a decimal point or comma ("7.5", "7,5"): a type=number field would drop the comma. */
function kgInput(value, placeholder) {
  return h("input", { type: "text", inputmode: "decimal", autocomplete: "off", value, placeholder });
}
function parseKg(input) {
  const s = String(input.value).trim().replace(",", ".");
  return /^(\d+(\.\d*)?|\.\d+)$/.test(s) ? parseFloat(s) : NaN;
}

/* How full the tank is, in % (a slider in steps of 5 and quick picks). kgOf(pct) says what that is in kg. */
function levelPicker(pct, kgOf) {
  const read = h("div", { class: "level-read" });
  const range = h("input", { type: "range", class: "level", min: "0", max: "100", step: "5", value: String(pct),
                             "aria-label": "How full the tank is now, in %" });
  const quick = segmented([[25, "25 %"], [50, "50 %"], [75, "75 %"], [100, "Full"]], pct, v => set(v), "c4");
  function set(v) { pct = v; range.value = String(v); draw(); }
  function draw() {
    quick.draw(pct);
    read.replaceChildren(h("span", { class: "num" }, String(pct)), h("small", {}, ` %${kgOf(pct)}`));
  }
  range.addEventListener("input", () => { pct = Number(range.value); draw(); });
  draw();
  return { el: h("div", {}, read, range, quick), get: () => pct, draw };
}
const NEEDS_UPDATE = "The stove's hub needs an update for this (with auto-update on it updates itself within minutes).";

function stovePhase(S) {
  if (!S) return "unknown";
  if (S.alarm) return "alarm";
  if (IGNITION[S.status]) return "igniting";
  return S.phase || "unknown";
}

function roomTemp(S) {
  const room = S && S.probes && S.probes.room;
  return room && S.temps && S.temps[room] != null ? S.temps[room] : null;
}

function probeName(S, key) {
  return (S && S.probes && S.probes.names && S.probes.names[key]) || key;
}

Stove.views[""] = async function viewHome(root, arg, alive) {
  let S = null, sched = null, lastFetch = 0, failed = null;
  const busy = {};        // action -> value being sent
  const screen = h("div", { class: "screen" });
  screen.append(h("div", { class: "grid2" },
    h("div", { class: "col" },
      h("div", { class: "card" }, h("div", { class: "skel tall" })),
      h("div", { class: "card" }, h("div", { class: "skel dial" }))),
    h("div", { class: "col" },
      h("div", { class: "card" }, h("div", { class: "skel line short" }), h("div", { class: "skel block" })),
      h("div", { class: "card" }, h("div", { class: "skel line short" }), h("div", { class: "skel block" })))));
  root.append(screen);

  /* ------------------------------------------------------------ hero */
  const hero = (() => {
    const el = h("section", { class: "card hero", "data-phase": "unknown", "aria-live": "polite" });
    const flameBox = h("div", { class: "flame-box" });
    const state = h("div", { class: "hero-state" }, "…");
    const sub = h("div", { class: "hero-sub" });
    const btn = h("button", { class: "power-btn", type: "button", "aria-label": "Turn on" }, icon("power"));
    const stats = h("div", { class: "hero-stats" });
    const steps = h("div", { class: "steps", hidden: true });
    const notice = h("div", { class: "notice", hidden: true });
    const updatedText = h("span");
    const updated = h("div", { class: "updated" }, icon("clock", "sm"), updatedText,
      h("button", { class: "icon-btn", type: "button", "aria-label": "Refresh", onclick: () => refresh(true) },
        icon("refresh", "sm")));
    btn.addEventListener("click", togglePower);
    el.append(h("div", { class: "hero-top" }, flameBox, h("div", { class: "hero-text" }, state, sub), btn),
      stats, steps, notice, updated);
    let flameShown = null;

    function stat(iconName, k, v, unit) {
      return h("div", { class: "stat" }, h("div", { class: "k" }, icon(iconName), k),
        h("div", { class: "v num" }, v, unit ? h("small", {}, unit) : null));
    }

    function update() {
      const phase = failed && !S ? "unknown" : stovePhase(S);
      el.dataset.phase = phase;
      const want = phase === "alarm" ? "warn" : "flame";
      if (flameShown !== want) {
        flameBox.replaceChildren(want === "warn" ? icon("warn", "lg") : svg(FLAME_SVG));
        flameShown = want;
      }
      const on = phase === "running" || phase === "igniting";
      btn.classList.toggle("on", on);
      btn.setAttribute("aria-label", on ? "Turn the stove off" : "Turn the stove on");
      btn.disabled = !S || "on" in busy || "off" in busy;
      btn.replaceChildren(("on" in busy || "off" in busy) ? h("span", { class: "spinner" }) : icon("power"));

      steps.hidden = phase !== "igniting";
      notice.hidden = true;
      if (!S) {
        state.textContent = failed ? "No connection" : "…";
        sub.textContent = failed ? failed.message : (Stove.link && Stove.link.state !== "open" ? Stove.link.stateText : "");
        stats.replaceChildren();
      } else {
        const a = S.scheduler && S.scheduler.active, n = S.scheduler && S.scheduler.next;
        if (phase === "off") {
          state.textContent = "Off";
          sub.textContent = n && S.scheduler.enabled ? `Starts ${n.day} ${n.start} · ${n.name}` : "Ready to light";
        } else if (phase === "igniting") {
          const [step, label] = IGNITION[S.status];
          state.textContent = "Igniting";
          sub.textContent = `${label} · step ${step} of 4`;
          steps.replaceChildren(h("div", { class: "steps-bar" }, [1, 2, 3, 4].map(i =>
            h("i", { class: i < step ? "done" : i === step ? "now" : null }))),
          h("div", { class: "steps-label" }, h("span", {}, "Lighting"), h("span", {}, "Burning")));
        } else if (phase === "running") {
          state.textContent = S.status_text;
          sub.textContent = a ? `Schedule: ${a.name} until ${a.end}` : `Power ${S.power} · fan ${fanText(S.fan)}`;
        } else if (phase === "shutdown") {
          state.textContent = "Cooling down";
          sub.textContent = S.status_text;
          notice.className = "notice cool";
          notice.replaceChildren(icon("snow", "sm"),
            h("span", {}, "The fire burns out and the stove cools down. It can be lit again afterwards."));
          notice.hidden = false;
        } else if (phase === "alarm") {
          state.textContent = "Alarm";
          sub.textContent = S.status_text;
          notice.className = "notice bad";
          const noPellets = S.status === NO_PELLETS, P = S.pellets;
          const refilled = noPellets && P && P.tank && !P.tank.empty;   // only the panel is left
          notice.replaceChildren(...[icon("warn", "sm"),
            h("span", {}, !noPellets
              ? "Check the stove (pellets, door, ash). Alarms are usually reset on the stove's own panel."
              : refilled ? "Tank refilled. Now reset the alarm on the stove's own panel."
              : P ? "The pellet tank is empty. Fill it, tap Tank refilled, then reset the alarm on the stove's panel."
              : "The pellet tank is empty. Fill it, then reset the alarm on the stove's panel."),
            noPellets && P && !refilled ? h("button", { class: "link-btn push-end", type: "button", onclick: refillPellets },
              "Tank refilled") : null].filter(Boolean));
          notice.hidden = false;
        } else {
          state.textContent = S.status_text;
          sub.textContent = "";
        }
        const room = roomTemp(S);
        stats.replaceChildren(
          room != null ? stat("thermo", probeName(S, S.probes.room), fmtTemp(room), "°")
            : stat("thermo", "Room", "–", ""),
          stat("flame", "Target", fmtTemp(S.setpoint), "°"),
          S.pellets && S.pellets.tank ? stat("bag", "Pellets left", String(S.pellets.tank.pct), "%")
            : stat("bag", "Pellets", S.pellets ? fmtKg(S.pellets.used) : "–", "kg"));
      }
      updatedText.textContent = lastFetch ? `Updated ${timeAgo(lastFetch)}` : "Loading…";
      if (failed && S) updatedText.textContent += " · " + failed.message;
    }
    return { el, update };
  })();

  /* ------------------------------------------------------ thermostat */
  const thermo = (() => {
    const MIN = 10, MAX = 30, START = 135, SWEEP = 270, R = 100, C = 120;
    let local = null, dragging = false, sendTimer = null, savedTimer = null;
    const clampDial = v => Math.min(MAX, Math.max(MIN, v));
    const angle = v => START + (clampDial(v) - MIN) / (MAX - MIN) * SWEEP;
    const polar = (deg, r) => { const a = deg * Math.PI / 180; return [C + r * Math.cos(a), C + r * Math.sin(a)]; };
    const arc = (a1, a2, r) => {
      const [x1, y1] = polar(a1, r), [x2, y2] = polar(a2, r);
      return `M ${x1.toFixed(2)} ${y1.toFixed(2)} A ${r} ${r} 0 ${a2 - a1 > 180 ? 1 : 0} 1 ${x2.toFixed(2)} ${y2.toFixed(2)}`;
    };
    let ticks = "";
    for (let v = MIN; v <= MAX; v += 2.5) {
      const [x1, y1] = polar(angle(v), R - 22), [x2, y2] = polar(angle(v), R - (v % 5 === 0 ? 30 : 27));
      ticks += `<line class="tick" x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}"/>`;
    }
    // The colour belongs to the dial, not to the filled part: cold blue at the left end, warm
    // orange around 19 °C, red at the right end. SVG has no conic gradient, so the band is drawn
    // once as short solid segments and revealed up to the knob by a mask with round caps.
    const STOPS = [[0, [0x5b, 0x9b, 0xe0]], [0.45, [0xff, 0xb0, 0x66]], [1, [0xe4, 0x57, 0x2e]]];
    const colorAt = t => {
      t = Math.min(1, Math.max(0, t));
      let i = 1;
      while (i < STOPS.length - 1 && t > STOPS[i][0]) i++;
      const [t0, c0] = STOPS[i - 1], [t1, c1] = STOPS[i], k = (t - t0) / (t1 - t0);
      return "rgb(" + c0.map((c, j) => Math.round(c + (c1[j] - c) * k)).join(",") + ")";
    };
    const STEP = 3, PAD = 6;   // PAD: colour under the round caps beyond both ends of the band
    let band = "";
    for (let a = START - PAD; a < START + SWEEP + PAD; a += STEP) {
      const end = Math.min(a + STEP + 0.6, START + SWEEP + PAD);   // a slight overlap hides the seams
      band += `<path d="${arc(a, end, R)}" stroke="${colorAt((a + STEP / 2 - START) / SWEEP)}"/>`;
    }
    const dialSvg = svg(`<svg viewBox="0 0 240 240" aria-hidden="true" focusable="false">
      <defs><mask id="dialMask" maskUnits="userSpaceOnUse" x="-10" y="-10" width="260" height="260">
        <path class="fill" d="" fill="none" stroke="#fff" stroke-width="18" stroke-linecap="round"/>
      </mask></defs>
      <path class="track" d="${arc(START, START + SWEEP, R)}"/>
      <g class="band" mask="url(#dialMask)">${band}</g>${ticks}
      <circle class="room-mark" r="4" cx="120" cy="20" visibility="hidden"/>
      <circle class="knob" r="13" cx="120" cy="20"/>
    </svg>`);
    const fill = dialSvg.querySelector(".fill"), knob = dialSvg.querySelector(".knob");
    const roomMark = dialSvg.querySelector(".room-mark");
    const valueEl = h("div", { class: "dial-value num" }, "–");
    const labelEl = h("div", { class: "dial-label" }, "Target");
    const stateEl = h("div", { class: "dial-state" });
    const dial = h("div", { class: "dial", role: "slider", tabindex: "0", "aria-label": "Target temperature",
                            "aria-valuemin": "5", "aria-valuemax": "40" },
      dialSvg, h("div", { class: "dial-center" }, valueEl, labelEl, stateEl));
    const minus = h("button", { class: "round-btn", type: "button", "aria-label": "Lower by 0.5 degrees" }, icon("minus"));
    const plus = h("button", { class: "round-btn", type: "button", "aria-label": "Raise by 0.5 degrees" }, icon("plus"));
    const el = h("section", { class: "card" },
      h("div", { class: "card-head" }, h("span", { class: "label" }, "Target temperature")),
      h("div", { class: "dial-wrap" }, dial), h("div", { class: "dial-steps" }, minus, plus));

    function current() { return local != null ? local : (S && S.setpoint != null ? Number(S.setpoint) : 20); }
    function draw() {
      const v = current();
      const a = angle(v);
      fill.setAttribute("d", v > MIN ? arc(START, a, R) : "");
      const [kx, ky] = polar(a, R);
      knob.setAttribute("cx", kx.toFixed(2));
      knob.setAttribute("cy", ky.toFixed(2));
      valueEl.replaceChildren(fmtTemp(v), h("sup", {}, "°"));
      dial.setAttribute("aria-valuenow", String(v));
      dial.setAttribute("aria-valuetext", `${fmtTemp(v)} degrees`);
      const room = roomTemp(S);
      if (room != null) {
        const [rx, ry] = polar(angle(room), R + 17);
        roomMark.setAttribute("cx", rx.toFixed(2));
        roomMark.setAttribute("cy", ry.toFixed(2));
        roomMark.setAttribute("visibility", "visible");
        labelEl.textContent = `Target · ${probeName(S, S.probes.room)} ${fmtTemp(room)}°`;
      } else {
        roomMark.setAttribute("visibility", "hidden");
        labelEl.textContent = "Target";
      }
    }
    function setLocal(v) {
      local = Math.min(40, Math.max(5, Math.round(v * 2) / 2));
      stateEl.textContent = "";
      draw();
    }
    function scheduleSend(ms) {
      clearTimeout(sendTimer);
      sendTimer = setTimeout(send, ms);
    }
    async function send() {
      if (local == null || dragging) return;
      const v = local;
      if (S && Number(S.setpoint) === v) { local = null; draw(); return; }
      stateEl.textContent = "Setting…";
      const ok = await act("setpoint", v, { quiet: true });
      if (local === v) local = null;
      stateEl.textContent = ok ? "Saved" : "";
      draw();
      clearTimeout(savedTimer);
      savedTimer = setTimeout(() => { if (stateEl.textContent === "Saved") stateEl.textContent = ""; }, 1800);
    }
    function valueAt(e) {
      const r = dialSvg.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width * 240, y = (e.clientY - r.top) / r.height * 240;
      let deg = Math.atan2(y - C, x - C) * 180 / Math.PI;
      if (deg < 0) deg += 360;
      let rel = deg - START;
      if (rel < 0) rel += 360;
      if (rel > SWEEP) rel = rel - SWEEP < (360 - SWEEP) / 2 ? SWEEP : 0;
      return { value: MIN + rel / SWEEP * (MAX - MIN), dist: Math.hypot(x - C, y - C) };
    }
    dial.addEventListener("pointerdown", e => {
      if (!S) return;
      const p = valueAt(e);
      if (p.dist < R - 38 || p.dist > R + 34) return;   // only the ring, not the numbers
      dragging = true;
      dial.setPointerCapture(e.pointerId);
      setLocal(p.value);
      e.preventDefault();
    });
    dial.addEventListener("pointermove", e => { if (dragging) setLocal(valueAt(e).value); });
    const release = () => { if (dragging) { dragging = false; scheduleSend(500); } };
    dial.addEventListener("pointerup", release);
    dial.addEventListener("pointercancel", release);
    dial.addEventListener("keydown", e => {
      const step = { ArrowUp: .5, ArrowRight: .5, ArrowDown: -.5, ArrowLeft: -.5, PageUp: 1, PageDown: -1 }[e.key];
      if (step === undefined || !S) return;
      e.preventDefault();
      setLocal(current() + step);
      scheduleSend(800);
    });
    function hold(btn, step) {
      let t1 = null, t2 = null;
      const bump = () => { if (S) { setLocal(current() + step); scheduleSend(900); } };
      const stop = () => { clearTimeout(t1); clearInterval(t2); };
      btn.addEventListener("pointerdown", e => {
        e.preventDefault();
        bump();
        t1 = setTimeout(() => { t2 = setInterval(bump, 110); }, 450);
      });
      ["pointerup", "pointerleave", "pointercancel"].forEach(ev => btn.addEventListener(ev, stop));
      btn.addEventListener("keydown", e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); bump(); } });
    }
    hold(minus, -0.5);
    hold(plus, 0.5);
    return { el, update: () => { if (!dragging) draw(); }, busy: () => local != null || dragging };
  })();

  /* ----------------------------------------------------------- power */
  const power = (() => {
    const tag = h("span", { class: "value-tag num" });
    const bars = h("div", { class: "bars", role: "group", "aria-label": "Flame power" });
    const el = h("section", { class: "card" },
      h("div", { class: "card-head" }, h("span", { class: "label" }, "Flame power"), tag), bars);
    function update() {
      const cur = S ? S.power : null;
      const sending = busy.power;
      tag.textContent = cur ? `${sending || cur} of 5` : "–";
      bars.replaceChildren(...[1, 2, 3, 4, 5].map(n => {
        const shown = sending || cur;
        const b = h("button", { type: "button", class: n <= shown ? "lit" : null, "aria-pressed": String(n === cur),
                                "aria-label": `Power ${n}`, disabled: !S || sending !== undefined,
                                onclick: () => act("power", n, { quiet: true }) },
          h("i"), sending === n ? h("span", { class: "spinner" }) : String(n));
        b.firstChild.style.height = `${28 + n * 11}%`;
        return b;
      }));
    }
    return { el, update };
  })();

  /* ------------------------------------------------------------- fan */
  const fan = (() => {
    const tag = h("span", { class: "value-tag" });
    const seg = segmented(FAN, null, v => act("fan", v, { quiet: true }), "c4");
    const el = h("section", { class: "card" },
      h("div", { class: "card-head" }, h("span", { class: "label" }, "Room fan"), tag), seg);
    function update() {
      tag.textContent = S ? fanText(busy.fan !== undefined ? busy.fan : S.fan) : "–";
      seg.draw(S ? S.fan : null, busy.fan);
      if (!S) seg.querySelectorAll("button").forEach(b => { b.disabled = true; });
    }
    return { el, update };
  })();

  /* --------------------------------------------------- schedule card */
  const schedCard = (() => {
    const body = h("div");
    const sw = h("span");
    const el = h("section", { class: "card" },
      h("div", { class: "card-head" }, h("a", { class: "label", href: "#/schedule" }, "Schedule"), sw), body);
    function update() {
      const info = S ? S.scheduler : (sched && sched.info);
      if (!info) { body.replaceChildren(h("div", { class: "skel line" })); return; }
      sw.replaceChildren(toggle(info.enabled, async (on, input) => {
        input.disabled = true;
        try {
          const j = await rpc("schedule.enable", { enabled: on });
          sched = j;
          if (S) S.scheduler = j.info;
          toast(on ? "Schedule resumed" : "Schedule paused");
        } catch (e) {
          input.checked = !on;
          toast(e.message, "bad");
        }
        input.disabled = false;
        update();
      }, "Automatic schedule"));
      const kids = [];
      if (sched && sched.entries.length) kids.push(todayTrack(sched));
      if (!info.enabled) {
        kids.push(h("div", { class: "notice warn" }, icon("pause", "sm"), h("span", {}, "Paused. Slots are not run until you switch it back on.")));
      } else if (info.active) {
        const a = info.active;
        kids.push(h("div", { class: "foot-line" }, icon("flame", "sm"),
          h("span", {}, "Now: ", h("b", {}, a.name), ` until ${a.end}`)),
        h("div", { class: "chips mt" }, h("span", { class: "chip accent" }, icon("flame"), `Power ${a.power}`),
          h("span", { class: "chip" }, icon("fan"), `Fan ${a.fan_text}`),
          h("span", { class: "chip" }, icon("thermo"), `${fmtTemp(a.setpoint)}°`)));
        if (a.phase === "pending" && a.error) kids.push(h("div", { class: "small muted mt" }, a.error));
      }
      if (info.enabled && info.next) {
        kids.push(h("div", { class: "foot-line" }, icon("clock", "sm"),
          h("span", {}, "Next: ", h("b", {}, `${info.next.day} ${info.next.start}`), ` · ${info.next.name}`)));
      }
      if (info.enabled && !info.active && !info.next) {
        kids.push(h("div", { class: "foot-line" }, icon("calendar", "sm"),
          h("span", {}, "No slots yet. ", h("a", { href: "#/schedule" }, "Plan the week"))));
      }
      if (!info.clock_ok) kids.push(h("div", { class: "small muted mt" }, "Waiting for the hub clock to synchronise…"));
      body.replaceChildren(...kids);
    }
    return { el, update };
  })();

  function todayTrack(data) {
    const today = DAYS.indexOf((data.hub_time || "").slice(0, 3));
    const nowMin = toMin((data.hub_time || "").slice(4));
    const track = h("div", { class: "tl-track" });
    for (const t of [6, 12, 18]) {
      const g = h("div", { class: "grid" });
      g.style.left = (t / 24 * 100) + "%";
      track.append(g);
    }
    for (const e of data.entries) {
      for (const [from, to] of segmentsForDay(e, today)) {
        const b = h("div", { class: `blk bg-p${e.power}` + (e.enabled ? "" : " off"), title: `${e.name || "Slot"} ${e.start}–${e.end}` },
          to - from >= 180 ? (e.name || "") : "");
        b.style.left = (from / 1440 * 100) + "%";
        b.style.width = ((to - from) / 1440 * 100) + "%";
        track.append(b);
      }
    }
    if (!isNaN(nowMin)) {
      const n = h("div", { class: "now" });
      n.style.left = (nowMin / 1440 * 100) + "%";
      track.append(n);
    }
    return h("div", {}, track, h("div", { class: "steps-label" }, h("span", {}, "0:00"), h("span", {}, "Today"), h("span", {}, "24:00")));
  }

  /* ----------------------------------------------------- temperatures */
  const temps = (() => {
    const tiles = h("div", { class: "tiles" });
    const tip = h("div", { class: "small muted mt", hidden: true }, "Tip: tap ", icon("edit", "sm"),
      " to name the probes and choose which one is the room temperature.");
    tip.querySelector("svg").style.display = "inline";
    tip.querySelector("svg").style.verticalAlign = "-3px";
    const editBtn = h("button", { class: "icon-btn", type: "button", "aria-label": "Name the temperature probes",
                                  onclick: editProbes }, icon("edit"));
    const el = h("section", { class: "card" },
      h("div", { class: "card-head" }, h("span", { class: "label" }, "Temperatures"), editBtn), tiles, tip);
    function update() {
      const t = S ? Object.entries(S.temps || {}) : [];
      const room = S && S.probes ? S.probes.room : null;
      tiles.replaceChildren(...(t.length ? t.map(([k, v]) => h("div", { class: "tile" + (k === room ? " room" : "") },
        h("div", { class: "k" }, probeName(S, k)), h("div", { class: "v num" }, `${fmtTemp(v)}°`)))
        : [h("div", { class: "muted small" }, "No temperatures reported.")]));
      tip.hidden = !S || !!room || !t.length;
      editBtn.disabled = !S;
    }
    return { el, update };
  })();

  /* ---------------------------------------------------------- pellets */
  /* The stove only counts the pellets it burns; with the tank's size set, what is left in the tank
     is that size minus what was burnt since "Tank refilled" (or what a partial refill put in). */
  const pelletCard = (() => {
    const body = h("div");
    const editBtn = h("button", { class: "icon-btn", type: "button", "aria-label": "Pellet tank size",
                                  onclick: editTank }, icon("edit"));
    // (made once: it keeps the focus when the card is drawn again, and gets it back when its sheet closes)
    const refill = h("button", { class: "btn", type: "button", onclick: refillPellets });
    const levelBtn = h("button", { class: "btn", type: "button", onclick: () => setLevel() }, "Set level");
    const btns = h("div", { class: "btns mt" }, refill, levelBtn);
    const el = h("section", { class: "card" },
      h("div", { class: "card-head" }, h("span", { class: "label" }, "Pellets"), editBtn), body, btns);
    function update() {
      const P = S && S.pellets, T = P && P.tank;
      editBtn.disabled = !P || !S.pellet_tank;     // (a hub from before the tank does not send pellet_tank)
      btns.hidden = !P;
      if (!S) { body.replaceChildren(); return; }
      if (!P) {
        body.replaceChildren(h("div", { class: "muted small" }, "The stove does not report its pellet counter."));
        return;
      }
      refill.className = "btn" + (T && (T.low || T.empty || T.over) ? " primary" : "");
      refill.textContent = T ? "Tank refilled" : "Reset the counter";
      levelBtn.hidden = !T;
      const used = h("div", { class: "foot-line" }, icon("bag", "sm"), h("span", {},
        P.since ? `Burnt since ${fmtDay(P.since)}: ` : "Burnt: ", h("b", {}, `${fmtKg(P.used)} kg`),
        P.since ? ` · stove total ${fmtKg(P.total)} kg` : ""));
      if (!T) {
        const tip = h("div", { class: "small muted mt" }, "Tip: tap ", icon("edit", "sm"),
          " and give the size of the pellet tank to see how much is left.");
        Object.assign(tip.querySelector("svg").style, { display: "inline", verticalAlign: "-3px" });
        body.replaceChildren(...(S.pellet_tank ? [used, tip] : [used]));
        return;
      }
      const fill = h("i");
      fill.style.width = `${T.pct}%`;       // (through the CSSOM: the page's CSP refuses style attributes)
      const level = T.empty ? " out" : T.low ? " low" : "";
      const gauge = h("div", { class: "gauge" + level, role: "meter", "aria-label": "Pellets in the tank",
                               "aria-valuemin": "0", "aria-valuemax": String(T.size), "aria-valuenow": String(T.left) }, fill);
      const note = T.empty ? h("div", { class: "pellet-note bad" }, icon("warn", "sm"), "Empty: the stove ran out of pellets.")
        : T.over ? h("div", { class: "pellet-note warn" }, icon("warn", "sm"),
            "More was burnt than the tank holds. Filled up without tapping Tank refilled?")
        : T.low ? h("div", { class: "pellet-note warn" }, icon("warn", "sm"), "Running low: time to refill.")
        : null;
      const hours = T.hours_left != null && !T.empty ? h("div", { class: "small muted" },
        `${fmtHours(T.hours_left)[0].toUpperCase()}${fmtHours(T.hours_left).slice(1)} of burning left `
        + `at the recent ${fmtKg(T.rate)} kg/h.`) : null;
      body.replaceChildren(...[
        h("div", { class: "pellet-left" }, h("span", { class: "num" }, String(T.pct)),
          h("small", {}, ` % · ${fmtKg(T.left)} of ${fmtKg(T.size)} kg`)),
        gauge, note, hours, used].filter(Boolean));
    }
    return { el, update };
  })();

  function editTank() {
    const cur = (S && S.pellet_tank) || { size: null, low_pct: 20 };
    let low = cur.low_pct;
    sheet("Pellet tank", (panel, close) => {
      const size = kgInput(cur.size != null ? String(cur.size) : "", "e.g. 15");
      const kgOf = pct => {
        const v = parseKg(size);
        return v >= 1 && v <= 200 ? ` · about ${fmtKg(v * pct / 100)} of ${fmtKg(v)} kg` : "";
      };
      const now = cur.size == null ? levelPicker(100, kgOf) : null;     // the first size: what is in it now
      if (now) size.addEventListener("input", now.draw);
      const seg = segmented([[0, "Off"], [10, "10 %"], [20, "20 %"], [30, "30 %"]], low,
        v => { low = v; seg.draw(low); }, "c4");
      const err = h("div", { class: "err" });
      const saveBtn = h("button", { class: "btn primary", type: "submit" }, "Save");
      async function save(button, value) {
        err.textContent = "";
        button.disabled = true;
        try {
          const j = await rpc("pellets.tank", { size: value, low_pct: low });
          if (!alive()) return;
          gotStatus(j);
          if (value && now) {       // count from what is in it now, not from the stove's old counter
            try {
              gotStatus(await (now.get() === 100 ? rpc("pellets.refill") : rpc("pellets.level", { pct: now.get() })));
            } catch (e) {
              close();
              toast(`Saved, but the level could not be set: ${e.code === 404 ? NEEDS_UPDATE : e.message}`, "bad");
              return;
            }
          }
          close();
          toast(value ? "Saved" : "Tank size removed");
        } catch (e) {
          err.textContent = e.message;
          button.disabled = false;
        }
      }
      const form = h("form", {
        onsubmit: ev => {
          ev.preventDefault();
          const v = parseKg(size);
          if (!(v >= 1 && v <= 200)) { err.textContent = "The tank size must be 1 to 200 kg, e.g. 15 or 15.5."; return; }
          save(saveBtn, v);
        },
      },
        h("p", { class: "sheet-text" }, "How much the stove's pellet tank holds. What is left is that size minus what "
          + "the stove has burnt since you last tapped Tank refilled."),
        field("Tank size (kg)", size, "In the stove's manual; most pellet stoves hold 15 to 25 kg."),
        now ? h("div", { class: "section-title" }, icon("bag", "sm"), "How full is it now?") : null,
        now ? now.el : null,
        h("div", { class: "section-title" }, icon("warn", "sm"), "Running low below"), seg,
        h("p", { class: "small muted" }, "Phones with notifications for problems get one message per filling, "
          + "and another one when the stove stops for lack of pellets."),
        err,
        h("div", { class: "sheet-foot" },
          cur.size != null ? h("button", { class: "btn danger", type: "button",
                                           onclick: ev => save(ev.currentTarget, null) }, "Remove") : null,
          h("button", { class: "btn", type: "button", onclick: () => close() }, "Cancel"), saveBtn));
      panel.append(form);
    });
  }

  function editProbes() {
    const names = Object.assign({}, (S.probes && S.probes.names) || {});
    let room = S.probes ? S.probes.room : null;
    const present = PROBES.filter(k => S.temps && k in S.temps);
    sheet("Temperature probes", (panel, close) => {
      const inputs = {};
      const roomSeg = segmented([[null, "None"], ...present.map(k => [k, k])], room, v => { room = v; roomSeg.draw(room); },
        present.length + 1 <= 3 ? "c3" : "c4");
      const err = h("div", { class: "err" });
      panel.append(h("p", { class: "sheet-text" },
        "The Connection Box reports up to five probes (T1–T5). Give them names you recognise, and pick the one that measures the room."));
      for (const k of present) {
        inputs[k] = h("input", { type: "text", maxlength: "20", value: names[k] || "", placeholder: `e.g. ${k === "T1" ? "Room" : "Flue gas"}` });
        panel.append(field(`${k} · now ${fmtTemp(S.temps[k])}°`, inputs[k]));
      }
      panel.append(h("div", { class: "section-title" }, icon("thermo", "sm"), "Room temperature"), roomSeg, err,
        h("div", { class: "sheet-foot" },
          h("button", { class: "btn", type: "button", onclick: () => close() }, "Cancel"),
          h("button", {
            class: "btn primary", type: "button",
            onclick: async ev => {
              const button = ev.currentTarget;
              button.disabled = true;
              const out = {};
              for (const k of present) if (inputs[k].value.trim()) out[k] = inputs[k].value.trim();
              try {
                S.probes = await rpc("probes.set", { names: out, room });
                render();
                close();
                toast("Saved");
              } catch (e) {
                err.textContent = e.message;
                button.disabled = false;
              }
            },
          }, "Save")));
    });
  }

  async function refillPellets() {
    const P = S && S.pellets;
    if (!P) return;
    const T = P.tank;
    if (!T) {                       // no tank size: only the counter starts again
      const ok = await confirmSheet({ title: "Reset the pellet counter?",
        text: `It starts again from 0 kg (now ${fmtKg(P.used)} kg${P.since ? ` since ${fmtDay(P.since)}` : ""}), e.g. when `
          + "you have filled the tank or start a new bag. The stove's own total stays as it is.",
        ok: "Reset", iconName: "bag" });
      if (!ok) return;
      try {
        const j = await rpc("pellets.reset");      // (= a full refill; also on a hub from before the tank)
        if (!alive()) return;
        gotStatus(j);
        toast("Pellet counter reset");
      } catch (e) { toast(e.message, "bad"); }
      return;
    }
    sheet("Tank refilled", (panel, close) => {
      const err = h("div", { class: "err" });
      const kg = kgInput("", "e.g. 15");
      const addBtn = h("button", { class: "btn", type: "submit" }, "Add");
      async function send(params, button, done) {
        err.textContent = "";
        button.disabled = true;
        try {
          const j = await rpc("pellets.refill", params);
          if (!alive()) return;
          gotStatus(j);
          close();
          toast(done);
        } catch (e) {
          err.textContent = e.message;
          button.disabled = false;
        }
      }
      const form = h("form", {
        onsubmit: ev => {
          ev.preventDefault();
          const v = parseKg(kg);
          if (!(v > 0)) { err.textContent = "How many kg did you put in? E.g. 15 or 7.5."; return; }
          if (v > T.size) { err.textContent = `The tank holds only ${fmtKg(T.size)} kg.`; return; }
          send({ added: v }, addBtn, `${fmtKg(v)} kg added`);
        },
      },
        h("p", { class: "sheet-text" }, T.empty
          ? "The stove had run out of pellets. After filling it, reset the alarm on the stove's own panel."
          : `About ${fmtKg(T.left)} of ${fmtKg(T.size)} kg are in the tank now.`),
        h("button", { class: "btn primary block", type: "button",
                      onclick: ev => send({}, ev.currentTarget, "Pellet tank full") }, "Filled to the top"),
        h("div", { class: "section-title" }, icon("bag", "sm"), "Or only some pellets"),
        field("I added (kg)", kg, `The tank holds ${fmtKg(T.size)} kg; one bag is usually 15 kg.`),
        h("button", { class: "link-btn", type: "button", onclick: () => setLevel() }, "Or say how full it is now (%)"),
        err,
        h("div", { class: "sheet-foot" },
          h("button", { class: "btn", type: "button", onclick: () => close() }, "Cancel"), addBtn));
      panel.append(form);
    });
  }

  /* Somebody looked into the tank: how full it is, in %. Clearly more than the estimate counts as a refill. */
  function setLevel() {
    const T = S && S.pellets && S.pellets.tank;
    if (!T) return;
    sheet("Pellets in the tank", (panel, close) => {
      const pick = levelPicker(Math.round(T.pct / 5) * 5, pct => ` · about ${fmtKg(T.size * pct / 100)} of ${fmtKg(T.size)} kg`);
      const err = h("div", { class: "err" });
      const setBtn = h("button", { class: "btn primary", type: "submit" }, "Set");
      const form = h("form", {
        onsubmit: async ev => {
          ev.preventDefault();
          err.textContent = "";
          setBtn.disabled = true;
          try {
            const j = await rpc("pellets.level", { pct: pick.get() });
            if (!alive()) return;
            gotStatus(j);
            close();
            toast(`Pellets: ${pick.get()} %`);
          } catch (e) {
            err.textContent = e.code === 404 ? NEEDS_UPDATE : e.message;
            setBtn.disabled = false;
          }
        },
      },
        h("p", { class: "sheet-text" }, `Look into the tank and set how full it is; the app thought ${T.pct} %. `
          + "It goes on counting down from there. Clearly more than it thought counts as a refill."),
        pick.el,
        err,
        h("div", { class: "sheet-foot" },
          h("button", { class: "btn", type: "button", onclick: () => close() }, "Cancel"), setBtn));
      panel.append(form);
    });
  }

  /* --------------------------------------------------------- actions */
  async function togglePower() {
    const phase = stovePhase(S);
    const turnOff = phase === "running" || phase === "igniting";
    const ok = await confirmSheet(turnOff
      ? { title: "Turn the stove off?", text: "The fire burns out and the stove cools down for a while before it can be lit again.",
          ok: "Turn off", danger: true, iconName: "power" }
      : { title: "Turn the stove on?", text: phase === "shutdown"
            ? "It is still cooling down, so it may only light once that has finished."
            : "It runs through its ignition steps for a few minutes before it burns.",
          ok: "Turn on", iconName: "flame" });
    if (ok) act(turnOff ? "off" : "on");
  }

  async function act(action, value, opts) {
    busy[action] = value === undefined ? true : value;
    render();
    let ok = false;
    try {
      const j = await rpc("stove.action", { action, value });
      if (!alive()) return false;
      gotStatus(j);
      ok = true;
      if (!(opts && opts.quiet)) {
        toast(action === "on" ? "Stove is lighting up" : action === "off" ? "Stove is switching off" : "Done");
      }
    } catch (e) {
      toast(e.message, "bad");
    }
    delete busy[action];
    render();
    return ok;
  }

  /* ------------------------------------------------------ data + render */
  let built = false;
  function render() {
    if (!alive()) return;
    if (!built) {
      screen.replaceChildren(h("div", { class: "grid2" },
        h("div", { class: "col" }, hero.el, thermo.el),
        h("div", { class: "col" }, power.el, fan.el, schedCard.el, temps.el, pelletCard.el)));
      built = true;
    }
    hero.update();
    thermo.update();
    power.update();
    fan.update();
    schedCard.update();
    temps.update();
    pelletCard.update();
  }

  /* The hub pushes the status every few seconds while we watch, and at once after a change. */
  function gotStatus(j) {
    if (!alive()) return;
    lastFetch = Date.now();
    if (j.error) {          // the hub is fine but the Connection Box does not answer
      failed = new Error(j.error);
      if (S) Object.assign(S, { scheduler: j.scheduler, probes: j.probes, pellet_tank: j.pellet_tank });
      else if (j.scheduler) sched = Object.assign({}, sched || { entries: [] }, { info: j.scheduler });
      setConn("warn", "Stove not answering");
    } else {
      S = j;
      failed = null;
      if (Stove.link.state === "open") setConn("open", "Live");
    }
    render();
  }

  async function refresh(manual) {
    try {
      gotStatus(await rpc("status.get"));
      if (manual) toast(failed ? failed.message : "Updated", failed ? "bad" : undefined);
    } catch (e) {
      if (!alive()) return;
      if (!S) failed = e;
      render();
      if (manual) toast(e.message, "bad");
    }
  }

  async function loadSchedule() {
    try {
      sched = await rpc("schedule.get");
      if (alive()) render();
    } catch (e) { /* the status card shows problems */ }
  }

  onLink("status", j => { if (!thermo.busy()) gotStatus(j); });
  onLink("schedule", j => { sched = j; if (S) S.scheduler = j.info; render(); });
  onLink("state", st => {
    if (st.state === "open") { refresh(); loadSchedule(); }
    else if (!S && st.state !== "connecting") { failed = new Error(st.text); render(); }
  });
  viewTimer(() => { if (S) hero.update(); }, 10000);   // "Updated 30 s ago"
  await Promise.all([refresh(), loadSchedule()]);
};

/* Minutes [from, to) of a slot on a given weekday (0 = Monday), incl. the part after midnight. */
function segmentsForDay(e, day) {
  const s = toMin(e.start), en = toMin(e.end), out = [];
  if (e.days.includes(day)) out.push([s, en > s ? en : 1440]);
  if (en <= s && en > 0 && e.days.includes((day + 6) % 7)) out.push([0, en]);
  return out;
}
