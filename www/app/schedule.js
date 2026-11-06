"use strict";
/* Schedule screen: week timeline, slot list and the slot editor. */

const WEEK_MIN = 7 * 1440;
const TEMPLATES = [
  { name: "Weekday mornings", days: [0, 1, 2, 3, 4], start: "06:00", end: "08:30", power: 3, fan: 7, setpoint: 21.5 },
  { name: "Evenings", days: [0, 1, 2, 3, 4, 5, 6], start: "17:00", end: "22:00", power: 3, fan: 7, setpoint: 22 },
  { name: "Weekend days", days: [5, 6], start: "08:00", end: "22:00", power: 2, fan: 7, setpoint: 21 },
];

function durationText(e) {
  const s = toMin(e.start), en = toMin(e.end);
  const m = en > s ? en - s : en + 1440 - s;
  const hh = Math.floor(m / 60), mm = m % 60;
  return hh && mm ? `${hh} h ${mm} min` : hh ? `${hh} h` : `${mm} min`;
}

function weekIntervals(e) {
  const s = toMin(e.start), en = toMin(e.end);
  const dur = en > s ? en - s : en + 1440 - s;
  const out = [];
  for (const d of e.days) {
    const a = d * 1440 + s, b = a + dur;
    if (b <= WEEK_MIN) out.push([a, b, d]);
    else out.push([a, WEEK_MIN, d], [0, b - WEEK_MIN, d]);
  }
  return out;
}

/* Which other enabled slots overlap e, and on which days. */
function overlapsWith(e, entries) {
  const mine = weekIntervals(e);
  const hits = [];
  for (const o of entries) {
    if (o.id === e.id || !o.enabled) continue;
    const days = new Set();
    for (const [a1, b1, d] of mine) {
      for (const [a2, b2] of weekIntervals(o)) if (a1 < b2 && a2 < b1) days.add(d);
    }
    if (days.size) hits.push(`'${o.name || "Slot"}' on ${daysText([...days])}`);
  }
  return hits;
}

Stove.views.schedule = async function viewSchedule(root, arg, alive) {
  let data = await rpc("schedule.get");
  if (!alive()) return;
  const screen = h("div", { class: "screen" });
  const head = h("section", { class: "card" });
  const week = h("section", { class: "card" });
  const list = h("div", { class: "stack" });
  const fab = h("button", { class: "fab", type: "button", onclick: () => edit(null) }, icon("plus"), "New slot");
  screen.append(h("div", { class: "page-title" }, "Schedule"), head, week, list);
  root.append(screen, fab);

  function renderHead() {
    const info = data.info;
    const sw = toggle(data.enabled, async (on, input) => {
      input.disabled = true;
      try {
        data = await rpc("schedule.enable", { enabled: on });
        renderAll();
        toast(on ? "Schedule resumed" : "Schedule paused");
      } catch (e) {
        input.checked = !on;
        input.disabled = false;
        toast(e.message, "bad");
      }
    }, "Automatic schedule");
    const lines = [];
    if (!data.enabled) {
      lines.push(h("div", { class: "notice warn" }, icon("pause", "sm"),
        h("span", {}, "Paused: nothing is switched automatically until you turn it back on.")));
    } else {
      if (info.active) {
        const a = info.active;
        lines.push(h("div", { class: "foot-line" }, icon("flame", "sm"),
          h("span", {}, "Now: ", h("b", {}, a.name), ` until ${a.end}`)));
      }
      if (info.next) {
        lines.push(h("div", { class: "foot-line" }, icon("clock", "sm"),
          h("span", {}, "Next: ", h("b", {}, `${info.next.day} ${info.next.start}`), ` · ${info.next.name}`)));
      }
    }
    head.replaceChildren(
      h("div", { class: "spread" },
        h("div", {}, h("b", {}, "Automatic schedule"),
          h("div", { class: "small muted" }, `Hub time ${data.hub_time} ${data.timezone}`)),
        sw),
      ...lines);
  }

  function renderWeek() {
    const today = DAYS.indexOf((data.hub_time || "").slice(0, 3));
    const nowMin = toMin((data.hub_time || "").slice(4));
    const axis = h("div", { class: "tl-axis" }, h("div"), h("div", {}, ...[0, 6, 12, 18, 24].map(t => {
      const s = h("span", {}, `${t}`);
      s.style.left = (t / 24 * 100) + "%";
      return s;
    })));
    const rows = DAYS.map((d, i) => {
      const track = h("div", { class: "tl-track" });
      for (const t of [6, 12, 18]) {
        const g = h("div", { class: "grid" });
        g.style.left = (t / 24 * 100) + "%";
        track.append(g);
      }
      for (const e of data.entries) {
        for (const [from, to] of segmentsForDay(e, i)) {
          const b = h("button", {
            class: `blk bg-p${e.power}` + (e.enabled ? "" : " off"), type: "button",
            "aria-label": `${e.name || "Slot"}, ${DAYS[i]} ${e.start} to ${e.end}`,
            title: `${e.name || "Slot"} ${e.start}–${e.end} · power ${e.power} · fan ${e.fan_text} · ${fmtTemp(e.setpoint)}°`,
            onclick: () => edit(e),
          }, to - from >= 180 ? (e.name || e.start) : "");
          b.style.left = (from / 1440 * 100) + "%";
          b.style.width = ((to - from) / 1440 * 100) + "%";
          track.append(b);
        }
      }
      if (i === today && !isNaN(nowMin)) {
        const n = h("div", { class: "now", title: "Now" });
        n.style.left = (nowMin / 1440 * 100) + "%";
        track.append(n);
      }
      return h("div", { class: "tl-row" + (i === today ? " today" : "") }, h("b", {}, d), track);
    });
    week.replaceChildren(
      h("div", { class: "card-head" }, h("span", { class: "label" }, "This week")),
      h("div", { class: "tl" }, axis, ...rows),
      h("div", { class: "legend" }, "Power", ...[1, 2, 3, 4, 5].map(p => h("i", { class: `bg-p${p}`, title: `Power ${p}` })),
        h("span", {}, "low → high")));
  }

  function renderList() {
    const entries = [...data.entries].sort((a, b) => a.start.localeCompare(b.start) || a.id - b.id);
    if (!entries.length) {
      list.replaceChildren(h("section", { class: "card empty" },
        h("div", { class: "art" }, icon("calendar")),
        h("h3", {}, "Plan your week"),
        h("p", {}, "A slot switches the stove on at its start with its own power, fan and temperature, and off at its end."),
        h("div", { class: "templates" }, ...TEMPLATES.map(t => h("button", { class: "template", type: "button", onclick: () => edit(null, t) },
          h("div", {}, h("b", {}, t.name), h("span", {}, `${daysText(t.days)} · ${t.start}–${t.end} · ${fmtTemp(t.setpoint)}°`)),
          icon("chevron"))),
        h("button", { class: "template", type: "button", onclick: () => edit(null) },
          h("div", {}, h("b", {}, "Start from scratch"), h("span", {}, "Pick days, times and settings yourself")),
          icon("chevron")))));
      return;
    }
    const activeId = data.enabled && data.info.active ? data.info.active.id : null;
    list.replaceChildren(...entries.map(e => {
      const sw = toggle(e.enabled, async (on, input) => {
        input.disabled = true;
        try {
          await save(e.id, Object.assign({}, e, { enabled: on }), on ? "Slot on" : "Slot off");
        } catch (err) {
          input.checked = !on;
          input.disabled = false;
          toast(err.message, "bad");
        }
      }, "Slot enabled");
      const live = e.id === activeId;
      const card = h("section", { class: "card slot" + (e.enabled ? "" : " disabled") + (live ? " live" : "") },
        h("div", { class: `bar bg-p${e.power}` }),
        h("div", { class: "body" },
          h("div", { class: "title" }, h("div", { class: "name" }, e.name || "Slot"),
            live ? h("span", { class: "chip accent" }, icon("flame"), "Now") : null, sw,
            h("button", { class: "icon-btn", type: "button", "aria-label": `More for ${e.name || "slot"}`,
                          onclick: () => more(e) }, icon("more"))),
          h("div", { class: "time num" }, `${e.start} – ${e.end}`,
            h("small", {}, e.overnight ? `${durationText(e)} · ends next day` : durationText(e))),
          h("div", { class: "dayrow", "aria-label": daysText(e.days) },
            ...DAY_LETTERS.map((l, i) => h("span", { class: e.days.includes(i) ? "on" : null }, l))),
          h("div", { class: "chips" },
            h("span", { class: "chip" }, icon("flame"), `Power ${e.power}`),
            h("span", { class: "chip" }, icon("fan"), `Fan ${e.fan_text}`),
            h("span", { class: "chip" }, icon("thermo"), `${fmtTemp(e.setpoint)}°`))));
      card.addEventListener("click", ev => {
        if (ev.target.closest("button, label, input")) return;
        edit(e);
      });
      return card;
    }));
  }

  function renderAll() {
    renderHead();
    renderWeek();
    renderList();
  }

  async function save(id, e, msg) {
    const body = { name: e.name, days: e.days, start: e.start, end: e.end, power: e.power, fan: e.fan,
                   setpoint: e.setpoint, enabled: e.enabled };
    const j = await rpc("schedule.save", id ? { id, entry: body } : { entry: body });
    data = j;
    renderAll();
    toast(j.active_now ? "Saved. This slot is running now, the stove follows it in a moment." : (msg || "Saved"));
    return j;
  }

  function more(e) {
    sheet(e.name || "Slot", (panel, close) => {
      panel.append(h("div", { class: "menu-list" },
        h("button", { class: "btn", type: "button", onclick: () => { close(); edit(e); } }, icon("edit"), "Edit"),
        h("button", {
          class: "btn", type: "button",
          onclick: async () => {
            close();
            try {
              await save(null, Object.assign({}, e, { name: (e.name || "Slot") + " (copy)" }), "Duplicated");
            } catch (err) { toast(err.message, "bad"); }
          },
        }, icon("copy"), "Duplicate"),
        h("button", { class: "btn danger", type: "button", onclick: () => { close(); remove(e); } }, icon("trash"), "Delete")));
    });
  }

  async function remove(e) {
    const ok = await confirmSheet({ title: `Delete '${e.name || "slot"}'?`,
      text: `${daysText(e.days)} ${e.start}–${e.end}. A slot that is running right now leaves the stove as it is.`,
      ok: "Delete", danger: true, iconName: "trash" });
    if (!ok) return;
    try {
      data = await rpc("schedule.delete", { id: e.id });
      renderAll();
      toast("Slot deleted");
    } catch (err) {
      toast(err.message, "bad");
    }
  }

  function edit(e, template) {
    const isNew = !e;
    const f = e ? Object.assign({}, e, { days: [...e.days] })
      : Object.assign({ name: "", days: [0, 1, 2, 3, 4], start: "06:00", end: "08:00", power: 3, fan: 7, setpoint: 21, enabled: true },
        template ? JSON.parse(JSON.stringify(template)) : {});
    sheet(isNew ? "New slot" : "Edit slot", (panel, close) => {
      const err = h("div", { class: "err" });
      const warn = h("div", { class: "warnbox", hidden: true });
      const name = h("input", { type: "text", maxlength: "40", value: f.name, placeholder: "e.g. Morning" });
      const start = h("input", { type: "time", value: f.start, required: true });
      const end = h("input", { type: "time", value: f.end, required: true });
      const span = h("div", { class: "time-hint" });
      const dayBox = h("div", { class: "seg c7" });
      const bars = h("div", { class: "bars", role: "group", "aria-label": "Flame power" });
      const fanSeg = segmented(FAN, f.fan, v => { f.fan = v; fanSeg.draw(v); }, "c4");
      const tempVal = h("div", { class: "big num" });
      const enabled = toggle(f.enabled, on => { f.enabled = on; }, "Slot enabled");
      const saveBtn = h("button", { class: "btn primary", type: "submit" }, isNew ? "Add slot" : "Save");

      function drawDays() {
        dayBox.replaceChildren(...DAYS.map((d, i) => h("button", {
          type: "button", "aria-pressed": String(f.days.includes(i)), "aria-label": d,
          onclick: () => {
            f.days = f.days.includes(i) ? f.days.filter(x => x !== i) : [...f.days, i].sort((a, b) => a - b);
            drawDays();
            check();
          },
        }, d.slice(0, 2))));
      }
      function drawBars() {
        bars.replaceChildren(...[1, 2, 3, 4, 5].map(n => {
          const b = h("button", { type: "button", class: n <= f.power ? "lit" : null, "aria-pressed": String(n === f.power),
                                  "aria-label": `Power ${n}`, onclick: () => { f.power = n; drawBars(); } }, h("i"), String(n));
          b.firstChild.style.height = `${24 + n * 10}%`;
          return b;
        }));
      }
      function drawTemp() { tempVal.replaceChildren(fmtTemp(f.setpoint), h("small", {}, " °C")); }
      function check() {
        f.start = start.value;
        f.end = end.value;
        span.textContent = f.start && f.end && f.start !== f.end
          ? `${durationText(f)}${toMin(f.end) <= toMin(f.start) ? " · ends the next day" : ""}` : "";
        const hits = f.days.length && f.start && f.end && f.start !== f.end ? overlapsWith(Object.assign({}, f, { id: e && e.id }), data.entries) : [];
        warn.hidden = !hits.length;
        warn.replaceChildren(icon("info", "sm"), h("span", {},
          `Overlaps ${hits.join("; ")}. While both apply, the slot that started later is followed.`));
      }
      const step = d => () => { f.setpoint = Math.min(40, Math.max(5, Math.round((f.setpoint + d) * 2) / 2)); drawTemp(); };
      const preset = days => h("button", { class: "btn sm", type: "button", onclick: () => { f.days = days; drawDays(); check(); } },
        daysText(days));
      start.addEventListener("input", check);
      end.addEventListener("input", check);
      drawDays();
      drawBars();
      drawTemp();
      check();

      const form = h("form", {
        onsubmit: async ev => {
          ev.preventDefault();
          err.textContent = "";
          f.name = name.value.trim();
          if (!f.days.length) { err.textContent = "Pick at least one day."; return; }
          if (!f.start || !f.end) { err.textContent = "Enter a start and an end time."; return; }
          if (f.start === f.end) { err.textContent = "Start and end must differ."; return; }
          saveBtn.disabled = true;
          try {
            await save(isNew ? null : e.id, f);
            close();
          } catch (x) {
            err.textContent = x.message;
            saveBtn.disabled = false;
          }
        },
      },
        field("Name", name),
        h("div", { class: "section-title" }, icon("calendar", "sm"), "Days"), dayBox,
        h("div", { class: "presets" }, preset([0, 1, 2, 3, 4]), preset([5, 6]), preset([0, 1, 2, 3, 4, 5, 6])),
        h("div", { class: "section-title" }, icon("clock", "sm"), "Time"),
        h("div", { class: "times" }, field("Start", start), field("End", end)), span, warn,
        h("div", { class: "section-title" }, icon("flame", "sm"), "Flame power"), bars,
        h("div", { class: "section-title" }, icon("fan", "sm"), "Room fan"), fanSeg,
        h("div", { class: "section-title" }, icon("thermo", "sm"), "Target temperature"),
        h("div", { class: "stepper" },
          h("button", { class: "round-btn", type: "button", "aria-label": "Lower", onclick: step(-0.5) }, icon("minus")),
          tempVal,
          h("button", { class: "round-btn", type: "button", "aria-label": "Raise", onclick: step(0.5) }, icon("plus"))),
        h("div", { class: "spread mt" }, h("div", {}, h("b", {}, "Enabled"),
          h("div", { class: "small muted" }, "Off = kept but not run")), enabled),
        err,
        isNew ? null : h("button", { class: "link-btn danger-link", type: "button", onclick: () => { close(); remove(e); } },
          "Delete this slot"),
        h("div", { class: "sheet-foot" },
          h("button", { class: "btn", type: "button", onclick: () => close() }, "Cancel"), saveBtn));
      panel.append(form);
    });
  }

  renderAll();
  if (arg === "new") edit(null);
  // changes made elsewhere (another phone, the scheduler) arrive from the hub
  onLink("schedule", j => {
    if (!alive()) return;
    data = j;
    if (openSheet) { renderHead(); renderWeek(); } else renderAll();
  });
  viewTimer(async () => {                 // the "now" line and "Next" move with the clock
    try { data = await rpc("schedule.get"); if (alive() && !openSheet) { renderHead(); renderWeek(); } } catch (e) { /* offline */ }
  }, 60000);
};
