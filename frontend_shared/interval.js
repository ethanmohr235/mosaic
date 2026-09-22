/* The interval picker -- one calendar, seven windows.
 *
 * A date in MonOSS is an ISO 8601 interval (core/interval.py): a start,
 * an optional span after a "/", and optional RRULE lines beneath it. A
 * text box can hold one; only a calendar can edit one. This is that
 * calendar.
 *
 * It lived in monarch/frontend/app.js, which meant the graph window
 * could edit a date properly and the other five got a text box -- the
 * same split the inspector itself was extracted to end. It is a
 * separate file from inspector.js because two callers need it: the
 * inspector's `date`-shaped property rows, and Monastery's draft editor,
 * which is not an inspector at all.
 *
 * core/interval.py owns the real grammar; what follows mirrors just
 * enough of it in JS to decompose a value into an editable draft and
 * recompose one back. It never expands a recurring value into
 * occurrences -- that is core/calendar.py's job, via dateutil, and "no
 * JavaScript reads an RRULE" stays true for *that*. A piece of a value
 * this cannot safely decompose (a multi-day span, two RRULE lines) is
 * marked `simple: false` and kept as raw, editable text rather than
 * silently rewritten or dropped.
 *
 * Symlinked into each app's frontend/, like inspector.js; see
 * docs/architecture.md. */

const IntervalPicker = (() => {
  "use strict";

  const DAY_CODES = ["SU", "MO", "TU", "WE", "TH", "FR", "SA"];
  const DAY_LABEL = { SU: "Sun", MO: "Mon", TU: "Tue", WE: "Wed", TH: "Thu", FR: "Fri", SA: "Sat" };
  const DAY_MIN = { SU: "S", MO: "M", TU: "T", WE: "W", TH: "T", FR: "F", SA: "S" };
  const MONTH_NAMES = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
  ];
  const MONTH_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  const FREQ_UNIT = { DAILY: "day", WEEKLY: "week", MONTHLY: "month", YEARLY: "year" };
  const ISO_DURATION_RE = /^P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?)?$/i;

  // The one open picker, if any. Module-level rather than per-field: two
  // calendars over each other is never what was meant.
  let openPicker = null;

  /** Closes whichever picker is open. Exported so a host can put it in
   *  its own Escape chain ahead of closing something larger. */
  function closeOpen() {
    if (openPicker) openPicker();
  }

  // The popup is fixed-positioned outside its row's flow but stays a
  // child of that row in the DOM, so composedPath() still contains it
  // for a click on the summary button or anywhere inside the calendar --
  // only a genuine outside click gets here. Registered once, by the
  // module, rather than once per host window.
  document.addEventListener("click", (event) => {
    if (!openPicker) return;
    const inside = event.composedPath().some(
      (el) => el.classList && el.classList.contains("interval-field"));
    if (!inside) closeOpen();
  });

  document.addEventListener("keydown", (event) => {
    // Ahead of whatever else Escape means in this window: the calendar is
    // the frontmost thing on screen while it is open.
    if (event.key === "Escape" && openPicker) {
      event.stopPropagation();
      closeOpen();
    }
  }, true);

  const pad2 = (n) => String(n).padStart(2, "0");
  const isoOf = (dt) => `${dt.getFullYear()}-${pad2(dt.getMonth() + 1)}-${pad2(dt.getDate())}`;
  const minutesOf = (hhmm) => { const [h, m] = hhmm.split(":").map(Number); return h * 60 + m; };
  const hhmmOf = (total) => {
    const m = ((total % 1440) + 1440) % 1440;
    return `${pad2(Math.floor(m / 60))}:${pad2(m % 60)}`;
  };

  function parseIsoDurationMinutes(text) {
    const m = ISO_DURATION_RE.exec(String(text || "").trim());
    if (!m || (!m[1] && !m[2] && !m[3])) return null;
    return (m[1] ? parseInt(m[1], 10) * 1440 : 0) + (m[2] ? parseInt(m[2], 10) * 60 : 0) + (m[3] ? parseInt(m[3], 10) : 0);
  }

  function formatDurationISO(totalMinutes) {
    const mins = Math.max(1, Math.round(totalMinutes));
    const h = Math.floor(mins / 60), m = mins % 60;
    if (h && m) return `PT${h}H${m}M`;
    if (h) return `PT${h}H`;
    return `PT${m}M`;
  }

  /** The span after "/" decomposed into a same-day end time, when it fits
   *  that shape -- a duration up to 24h, or an explicit end landing on the
   *  same calendar day as the start. Anything else (a multi-day range, a
   *  longer duration, an end on a different day) is `simple: false`: the
   *  picker shows it as raw text instead of silently truncating it the
   *  moment some other control on the row is touched. */
  function decomposeSpan(date, time, allDay, tail) {
    if (tail === null) return { simple: true, endTime: null };
    if (allDay) return { simple: false, raw: tail };
    const durationMinutes = parseIsoDurationMinutes(tail);
    if (durationMinutes !== null) {
      if (durationMinutes > 1440) return { simple: false, raw: tail };
      return { simple: true, endTime: hhmmOf(minutesOf(time || "00:00") + durationMinutes) };
    }
    const endTimed = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?$/.exec(tail.trim());
    if (endTimed && endTimed[1] === date) return { simple: true, endTime: endTimed[2] };
    return { simple: false, raw: tail };
  }

  /** A bare RRULE UNTIL value (`20261209`, `20261209T000000`, or
   *  `20261209T000000Z`) as the date-only string the picker's "ends on"
   *  control edits, or null if it's some other shape entirely. The time
   *  and any UTC `Z` are dropped rather than preserved -- for a weekly
   *  class or task series, the day the repetition stops is the part
   *  anyone actually means, and a bare date is also the one UNTIL form
   *  that never hits dateutil's naive/aware comparison error (see
   *  core/calendar.py::_rule_text, which strips a `Z` for the same
   *  reason on the read side). */
  function decomposeUntilValue(raw) {
    const m = /^(\d{4})(\d{2})(\d{2})(?:T\d{6}Z?)?$/.exec(raw);
    return m ? `${m[1]}-${m[2]}-${m[3]}` : null;
  }

  /** The rule lines beneath the start decomposed into repeat controls, when
   *  there is exactly one RRULE: line and it uses only FREQ/INTERVAL/BYDAY/
   *  COUNT/UNTIL, with at most one of COUNT/UNTIL present (RRULE permits
   *  only one). Any EXDATE:/RDATE:/EXRULE: lines (what Monastery writes
   *  when detaching one occurrence) are preserved verbatim as `otherLines`
   *  and re-appended on every write, never shown or edited here. Two or
   *  more RRULE lines, both COUNT and UNTIL together, or any other
   *  unsupported param, is `simple: false`: the whole rule block becomes
   *  raw, editable text instead of a structured editor that would drop
   *  what it can't parse. */
  function decomposeRuleLines(lines) {
    const rruleLines = lines.filter((l) => /^RRULE:/i.test(l));
    const otherLines = lines.filter((l) => !/^RRULE:/i.test(l));
    if (rruleLines.length === 0) {
      return { simple: true, freq: "NONE", interval: 1, byday: [], count: null, until: null, otherLines };
    }
    if (rruleLines.length > 1) return { simple: false, raw: lines.join("\n") };

    const body = rruleLines[0].slice(rruleLines[0].indexOf(":") + 1);
    const parts = {};
    for (const bit of body.split(";")) {
      const eq = bit.indexOf("=");
      if (eq === -1) continue;
      parts[bit.slice(0, eq).toUpperCase()] = bit.slice(eq + 1);
    }
    const known = new Set(["FREQ", "INTERVAL", "BYDAY", "COUNT", "UNTIL"]);
    const freq = (parts.FREQ || "").toUpperCase();
    if (
      Object.keys(parts).some((k) => !known.has(k)) ||
      !["DAILY", "WEEKLY", "MONTHLY", "YEARLY"].includes(freq) ||
      (parts.COUNT && parts.UNTIL)
    ) {
      return { simple: false, raw: lines.join("\n") };
    }
    const until = parts.UNTIL ? decomposeUntilValue(parts.UNTIL) : null;
    if (parts.UNTIL && until === null) return { simple: false, raw: lines.join("\n") };
    const interval = parts.INTERVAL ? Math.max(1, parseInt(parts.INTERVAL, 10) || 1) : 1;
    const byday = parts.BYDAY ? parts.BYDAY.toUpperCase().split(",").filter((c) => DAY_CODES.includes(c)) : [];
    const count = parts.COUNT ? Math.max(1, parseInt(parts.COUNT, 10) || 1) : null;
    return { simple: true, freq, interval, byday, count, until, otherLines };
  }

  /** Parses an ISO 8601 interval (core/interval.py) into an editable draft:
   *  the start (date/time/allDay), plus `span` and `rule`, each either
   *  structured or a `simple: false` raw-text fallback. Returns null for
   *  anything that isn't an interval at all, so callers fall back to plain
   *  text rather than force a picker onto an unparseable value. */
  function parseIntervalValue(text) {
    if (text === "") return null;
    const lines = String(text).split("\n").map((l) => l.trim()).filter((l) => l !== "");
    if (lines.length === 0) return null;
    const head = lines[0];
    const slash = head.indexOf("/");
    const startText = slash === -1 ? head : head.slice(0, slash);
    const tail = slash === -1 ? null : head.slice(slash + 1);

    const timed = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?$/.exec(startText);
    if (!timed && !/^\d{4}-\d{2}-\d{2}$/.test(startText)) return null;

    const date = timed ? timed[1] : startText;
    const time = timed ? timed[2] : "";
    const allDay = !timed;
    return {
      date, time, allDay,
      span: decomposeSpan(date, time, allDay, tail),
      rule: decomposeRuleLines(lines.slice(1)),
    };
  }

  function composeHead(draft) {
    if (draft.allDay) return draft.date;
    const start = `${draft.date}T${draft.time || "00:00"}:00`;
    if (!draft.span.simple) return `${start}/${draft.span.raw}`;
    if (draft.span.endTime === null) return start;
    let mins = minutesOf(draft.span.endTime) - minutesOf(draft.time || "00:00");
    if (mins <= 0) mins += 1440;
    return `${start}/${formatDurationISO(mins)}`;
  }

  function composeRuleLines(rule) {
    if (!rule.simple) return rule.raw ? rule.raw.split("\n").map((s) => s.trim()).filter(Boolean) : [];
    const lines = [];
    if (rule.freq !== "NONE") {
      const bits = [`FREQ=${rule.freq}`];
      if (rule.interval > 1) bits.push(`INTERVAL=${rule.interval}`);
      if (rule.freq === "WEEKLY" && rule.byday.length) bits.push(`BYDAY=${rule.byday.join(",")}`);
      if (rule.until) bits.push(`UNTIL=${rule.until.replace(/-/g, "")}`);
      else if (rule.count) bits.push(`COUNT=${rule.count}`);
      lines.push(`RRULE:${bits.join(";")}`);
    }
    return [...lines, ...(rule.otherLines || [])];
  }

  function serializeInterval(draft) {
    return [composeHead(draft), ...composeRuleLines(draft.rule)].join("\n");
  }

  function fmtTime12h(hhmm) {
    const [h, mi] = hhmm.split(":").map(Number);
    const ap = h >= 12 ? "PM" : "AM";
    return `${h % 12 === 0 ? 12 : h % 12}:${pad2(mi)} ${ap}`;
  }

  function durationLabelFor(startTime, endTime) {
    let mins = minutesOf(endTime) - minutesOf(startTime || "00:00");
    if (mins <= 0) mins += 1440;
    const h = Math.floor(mins / 60), m = mins % 60;
    return h ? (m ? `${h}h ${m}m` : `${h}h`) : `${m}m`;
  }

  function humanRepeat(rule) {
    const unit = FREQ_UNIT[rule.freq];
    let s = rule.interval > 1 ? `every ${rule.interval} ${unit}s` : `every ${unit}`;
    if (rule.freq === "WEEKLY" && rule.byday.length) s += " on " + rule.byday.map((c) => DAY_LABEL[c]).join(", ");
    if (rule.until) {
      const dt = new Date(`${rule.until}T00:00`);
      s += `, until ${MONTH_SHORT[dt.getMonth()]} ${dt.getDate()}, ${dt.getFullYear()}`;
    } else if (rule.count) {
      s += `, ${rule.count}×`;
    }
    return s;
  }

  function humanSummary(draft) {
    const dt = new Date(`${draft.date}T00:00`);
    let s = `${MONTH_SHORT[dt.getMonth()]} ${dt.getDate()}, ${dt.getFullYear()}`;
    if (draft.allDay) {
      if (!draft.span.simple) s += " · custom span";
    } else {
      s += ` · ${fmtTime12h(draft.time || "00:00")}`;
      if (draft.span.simple && draft.span.endTime) {
        s += ` – ${fmtTime12h(draft.span.endTime)} (${durationLabelFor(draft.time, draft.span.endTime)})`;
      } else if (!draft.span.simple) {
        s += " · custom span";
      }
    }
    if (draft.rule.simple && draft.rule.freq !== "NONE") s += ` · ${humanRepeat(draft.rule)}`;
    else if (!draft.rule.simple) s += " · custom repeat";
    return s;
  }

  function buildCalendarDays(monthStr, selectedDate, todayStr) {
    const [y, mo] = monthStr.split("-").map(Number);
    const first = new Date(y, mo - 1, 1);
    const start = new Date(y, mo - 1, 1 - first.getDay());
    const days = [];
    for (let i = 0; i < 42; i++) {
      const dt = new Date(start.getFullYear(), start.getMonth(), start.getDate() + i);
      const iso = isoOf(dt);
      days.push({
        iso, num: dt.getDate(), inMonth: dt.getMonth() === mo - 1,
        isToday: iso === todayStr, isSelected: iso === selectedDate,
      });
    }
    return days;
  }


  // ---- shared building blocks ----

  function blankDraft() {
    return {
      date: isoOf(new Date()), time: "", allDay: true,
      span: { simple: true, endTime: null },
      rule: { simple: true, freq: "NONE", interval: 1, byday: [], count: null, until: null, otherLines: [] },
    };
  }

  /** A text box that grows into a textarea once the value wraps -- used
   *  for the two raw-text fallbacks, where the value is an RRULE line or
   *  a span this file declined to decompose. */
  function plainField(text, onCommit) {
    const multiline = text.length > 38 || text.includes("\n");
    const field = document.createElement(multiline ? "textarea" : "input");
    if (!multiline) field.type = "text";
    field.className = "interval-raw-field";
    field.spellcheck = false;
    field.value = text;
    if (multiline) field.rows = Math.min(4, text.split("\n").length + 1);
    field.addEventListener("change", () => onCommit(field.value));
    return field;
  }

  function rawRuleFallback(draft, rerender, commit) {
    const box = document.createElement("div");
    const note = document.createElement("div");
    note.className = "interval-fallback-note";
    note.textContent = "This rule uses something the editor can't show — edit it as text:";
    const field = plainField(draft.rule.raw, (v) => {
      draft.rule = { simple: false, raw: v };
      commit();
    });
    const reset = document.createElement("button");
    reset.type = "button";
    reset.className = "interval-fallback-reset";
    reset.textContent = "Remove repeat";
    reset.addEventListener("click", () => {
      draft.rule = { simple: true, freq: "NONE", interval: 1, byday: [], count: null, until: null, otherLines: [] };
      rerender();
      commit();
    });
    box.append(note, field, reset);
    return box;
  }

  function pill(label, on, onClick, className = "interval-pill") {
    const b = document.createElement("button");
    b.type = "button";
    b.className = className + (on ? " on" : "");
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  function stepper(value, onChange, { min = 1 } = {}) {
    const wrap = document.createElement("div");
    wrap.className = "interval-stepper";
    const down = document.createElement("button");
    down.type = "button"; down.className = "interval-step"; down.textContent = "\u2212";
    const input = document.createElement("input");
    input.type = "text"; input.className = "interval-step-value";
    input.value = String(value);
    const up = document.createElement("button");
    up.type = "button"; up.className = "interval-step"; up.textContent = "+";
    const to = (n) => onChange(Math.max(min, n));
    down.addEventListener("click", () => to(value - 1));
    up.addEventListener("click", () => to(value + 1));
    input.addEventListener("change", () => to(parseInt(input.value, 10) || min));
    wrap.append(down, input, up);
    return wrap;
  }

  /** The structured repeat editor: frequency, interval, weekdays, and
   *  how it ends.
   *
   *  This lived in monastery/frontend/app.js and nowhere else, which is
   *  why the picker in every other window had been reduced to showing an
   *  RRULE as raw text. It writes into `rule` in place -- the same shape
   *  decomposeRuleLines produces and composeRuleLines serializes -- and
   *  calls `changed()` after each edit.
   *
   *  RRULE carries at most one of COUNT/UNTIL, so the ends control is
   *  three exclusive choices rather than two independent fields. */
  function repeatEditor(rule, changed) {
    const box = document.createElement("div");
    box.className = "interval-repeat-editor";

    const freqRow = document.createElement("div");
    freqRow.className = "interval-pill-row";
    for (const [value, label] of [
      ["DAILY", "Day"], ["WEEKLY", "Week"], ["MONTHLY", "Month"], ["YEARLY", "Year"],
    ]) {
      freqRow.appendChild(pill(label, rule.freq === value, () => {
        rule.freq = value;
        if (value !== "WEEKLY") rule.byday = [];
        changed();
      }));
    }
    box.appendChild(freqRow);

    const everyRow = document.createElement("div");
    everyRow.className = "interval-stepper-row";
    const everyLabel = document.createElement("span");
    everyLabel.className = "interval-row-label";
    everyLabel.textContent = "Every";
    const unit = document.createElement("span");
    unit.className = "interval-unit";
    unit.textContent = FREQ_UNIT[rule.freq] + (rule.interval > 1 ? "s" : "");
    everyRow.append(everyLabel, stepper(rule.interval, (n) => { rule.interval = n; changed(); }), unit);
    box.appendChild(everyRow);

    if (rule.freq === "WEEKLY") {
      const dayRow = document.createElement("div");
      dayRow.className = "interval-pill-row";
      for (const code of DAY_CODES) {
        dayRow.appendChild(pill(DAY_MIN[code], rule.byday.includes(code), () => {
          rule.byday = rule.byday.includes(code)
            ? rule.byday.filter((c) => c !== code)
            : DAY_CODES.filter((c) => c === code || rule.byday.includes(c));
          changed();
        }, "interval-day-pill"));
      }
      box.appendChild(dayRow);
    }

    const endsRow = document.createElement("div");
    endsRow.className = "interval-pill-row";
    const endsLabel = document.createElement("span");
    endsLabel.className = "interval-row-label";
    endsLabel.textContent = "Ends";
    endsRow.appendChild(endsLabel);
    const mode = rule.until ? "on" : rule.count ? "after" : "never";
    endsRow.appendChild(pill("Never", mode === "never", () => {
      rule.count = null; rule.until = null; changed();
    }));
    endsRow.appendChild(pill("After", mode === "after", () => {
      rule.until = null; rule.count = rule.count || 10; changed();
    }));
    endsRow.appendChild(pill("On", mode === "on", () => {
      rule.count = null;
      rule.until = rule.until || isoOf(new Date(Date.now() + 30 * 86400000));
      changed();
    }));
    box.appendChild(endsRow);

    if (mode === "after") {
      const row = document.createElement("div");
      row.className = "interval-stepper-row";
      const times = document.createElement("span");
      times.className = "interval-unit";
      times.textContent = rule.count === 1 ? "time" : "times";
      row.append(stepper(rule.count, (n) => { rule.count = n; changed(); }), times);
      box.appendChild(row);
    } else if (mode === "on") {
      const row = document.createElement("div");
      row.className = "interval-stepper-row";
      const input = document.createElement("input");
      input.type = "date";
      input.className = "interval-until-input";
      input.value = rule.until;
      input.addEventListener("change", () => {
        if (input.value) { rule.until = input.value; changed(); }
      });
      row.appendChild(input);
      box.appendChild(row);
    }

    return box;
  }

  /** Drops the picker below its summary button, as a fixed-position popup
   *  rather than an inline block: the calendar grid needs real width to
   *  stay usable, and the panel hosting it is user-resizable down to
   *  sizes that would crush it.
   *
   *  Measured from the anchor every time it is called, and called again
   *  on scroll, resize, and whenever the host panel moves -- the old
   *  version measured once on open, so dragging the inspector afterwards
   *  left the calendar behind at the coordinates the field used to be
   *  at. That is the "appears in weird places" bug.
   *
   *  It is also placed *before* being measured for overflow, and
   *  measured with the panel already visible, because a display:none
   *  element has no dimensions -- reading them while hidden is what made
   *  it sometimes not appear at all. */
  function positionIntervalPanel(panel, anchor) {
    if (!anchor.isConnected) return;
    panel.classList.remove("hidden");
    // Reset before measuring, or a previous nudge is read back as the
    // natural size and the popup walks up the screen on every reposition.
    panel.style.left = "0px";
    panel.style.top = "0px";
    const anchorRect = anchor.getBoundingClientRect();
    const rect = panel.getBoundingClientRect();
    const margin = 8;

    let left = anchorRect.left;
    if (left + rect.width > window.innerWidth - margin) {
      left = window.innerWidth - rect.width - margin;
    }
    let top = anchorRect.bottom + 4;
    if (top + rect.height > window.innerHeight - margin) {
      // Above the field instead, unless there is even less room there.
      const above = anchorRect.top - rect.height - 4;
      top = above >= margin ? above : Math.max(margin, window.innerHeight - rect.height - margin);
    }
    panel.style.left = Math.max(margin, left) + "px";
    panel.style.top = Math.max(margin, top) + "px";
  }

  /** A collapsed summary button that expands into a full calendar grid,
   *  time/end-time, and repeat controls -- Monarch's Observatory design
   *  (docs/ui-setup.md). Defines its own `value` getter/setter so it can
   *  stand in for a plain <input> anywhere one is expected -- which is how
   *  the shared inspector accepts it from monarchValueEditor.
   *
   *  Writes on every control change rather than only on Done, and always
   *  in the duration form (`start/PT_H_M`) -- this editor doesn't need
   *  core/interval.py's format_like style-preservation, which exists for
   *  Monastery's drag/resize paths where exactly one dimension may change
   *  at a time. moteId is used only by Clear, to rebuild the row the same
   *  way the per-property delete (×) button already does once the value
   *  stops being interval-shaped. */
  /** `onCommit(value)` is called on every control change, always in the
   *  duration form (`start/PT_H_M`). `onClear()` is optional -- the host
   *  decides what "this is not a date any more" means for it; without
   *  one, the Clear button is not drawn. */
  function intervalField(rawValue, onCommit, options = {}) {
    const onClear = typeof options === "function" ? null : options.onClear;
    let draft = parseIntervalValue(rawValue) || blankDraft();
    let month = draft.date.slice(0, 7);
    let open = false;
    let moveWatcher = null;

    const wrap = document.createElement("div");
    wrap.className = "interval-field";

    const summaryBtn = document.createElement("button");
    summaryBtn.type = "button";
    summaryBtn.className = "interval-summary-btn";

    const panel = document.createElement("div");
    // Also "interval-field": while open, this panel is promoted to a
    // direct child of <body> (see openPanel) so its `position: fixed`
    // measures against the viewport rather than getting clipped/
    // repositioned by an ancestor's backdrop-filter (the shared
    // inspector's glass panel has one -- frontend_shared/inspector.css
    // -- and that establishes a containing block for fixed descendants,
    // which is why the calendar used to only show up, half-cut-off, near
    // wherever the inspector happened to be). Carrying this class onto
    // the panel too keeps the module's outside-click composedPath()
    // check (above) matching once it's no longer inside `wrap`.
    panel.className = "interval-panel interval-field hidden";

    function divider() {
      const d = document.createElement("div");
      d.className = "interval-divider";
      return d;
    }

    // ---- calendar ----
    const calHeader = document.createElement("div");
    calHeader.className = "interval-cal-header";
    const prevBtn = document.createElement("button");
    prevBtn.type = "button"; prevBtn.className = "interval-cal-nav"; prevBtn.textContent = "‹";
    const monthLabel = document.createElement("span");
    monthLabel.className = "interval-cal-month";
    const nextBtn = document.createElement("button");
    nextBtn.type = "button"; nextBtn.className = "interval-cal-nav"; nextBtn.textContent = "›";
    calHeader.append(prevBtn, monthLabel, nextBtn);

    const weekdays = document.createElement("div");
    weekdays.className = "interval-cal-weekdays";
    for (const c of DAY_CODES) {
      const s = document.createElement("span");
      s.textContent = DAY_MIN[c];
      weekdays.appendChild(s);
    }

    const grid = document.createElement("div");
    grid.className = "interval-cal-grid";

    const jumpChips = document.createElement("div");
    jumpChips.className = "interval-jump-chips";
    const jumpToday = document.createElement("button");
    jumpToday.type = "button"; jumpToday.className = "interval-jump-chip"; jumpToday.textContent = "Today";
    const jumpTomorrow = document.createElement("button");
    jumpTomorrow.type = "button"; jumpTomorrow.className = "interval-jump-chip"; jumpTomorrow.textContent = "Tomorrow";
    const jumpNextWeek = document.createElement("button");
    jumpNextWeek.type = "button"; jumpNextWeek.className = "interval-jump-chip"; jumpNextWeek.textContent = "Next week";
    jumpChips.append(jumpToday, jumpTomorrow, jumpNextWeek);

    // ---- time / end time ----
    const timeRow = document.createElement("div");
    timeRow.className = "interval-time-row";
    const allDayLabel = document.createElement("label");
    allDayLabel.className = "picker-checkbox";
    const allDayCheckbox = document.createElement("input");
    allDayCheckbox.type = "checkbox";
    allDayLabel.append(allDayCheckbox, document.createTextNode("All day"));
    const timeInput = document.createElement("input");
    timeInput.type = "time";
    timeRow.append(allDayLabel, timeInput);

    const endRow = document.createElement("div");
    endRow.className = "interval-end-row";

    // ---- repeat ----
    // Structured repeat authoring (frequency, weekdays, ends-on/-after)
    // lives in Monastery now -- it's the calendar app, and it's the one
    // that reads `recurring` off core/calendar.py's expansion. Monarch
    // still has to round-trip an existing rule without dropping it, so a
    // rule is shown and edited as raw RRULE text here rather than losing
    // the picker's ability to represent one at all.
    const repeatRow = document.createElement("div");
    repeatRow.className = "interval-repeat-row";
    const repeatLabel = document.createElement("span");
    repeatLabel.className = "interval-row-label";
    repeatLabel.textContent = "Repeat";
    repeatRow.append(repeatLabel);

    const repeatDetail = document.createElement("div");
    repeatDetail.className = "interval-repeat-detail";

    // ---- footer ----
    const footer = document.createElement("div");
    footer.className = "interval-footer";
    const summaryText = document.createElement("div");
    summaryText.className = "interval-summary-text";
    const rawValueText = document.createElement("div");
    rawValueText.className = "interval-raw-value";
    const footerBtns = document.createElement("div");
    footerBtns.className = "interval-footer-btns";
    const clearBtn = document.createElement("button");
    clearBtn.type = "button"; clearBtn.className = "interval-clear-btn"; clearBtn.textContent = "Clear";
    const doneBtn = document.createElement("button");
    doneBtn.type = "button"; doneBtn.className = "interval-done-btn"; doneBtn.textContent = "Done";
    footerBtns.append(clearBtn, doneBtn);
    footer.append(summaryText, rawValueText, footerBtns);

    panel.append(
      calHeader, weekdays, grid, jumpChips,
      divider(), timeRow, endRow,
      divider(), repeatRow, repeatDetail,
      divider(), footer,
    );
    wrap.append(summaryBtn, panel);

    // ---- render passes ----

    function commit() {
      onCommit(serializeInterval(draft));
      refreshText();
    }

    function refreshText() {
      summaryBtn.textContent = humanSummary(draft);
      summaryText.textContent = humanSummary(draft);
      rawValueText.textContent = serializeInterval(draft);
    }

    function renderCalendar() {
      const [y, mo] = month.split("-").map(Number);
      monthLabel.textContent = `${MONTH_NAMES[mo - 1]} ${y}`;
      grid.innerHTML = "";
      const todayStr = isoOf(new Date());
      for (const day of buildCalendarDays(month, draft.date, todayStr)) {
        const cell = document.createElement("button");
        cell.type = "button";
        cell.className = "interval-cal-day";
        cell.textContent = day.num;
        if (!day.inMonth) cell.classList.add("out");
        if (day.isToday) cell.classList.add("today");
        if (day.isSelected) cell.classList.add("selected");
        cell.addEventListener("click", () => {
          draft.date = day.iso;
          month = draft.date.slice(0, 7);
          renderCalendar();
          commit();
        });
        grid.appendChild(cell);
      }
    }

    function renderTimeRow() {
      allDayCheckbox.checked = draft.allDay;
      timeInput.value = draft.time || "09:00";
      timeInput.classList.toggle("hidden", draft.allDay);
      endRow.classList.toggle("hidden", draft.allDay);
      renderEndRow();
    }

    function renderEndRow() {
      endRow.innerHTML = "";
      if (draft.allDay) return;
      if (!draft.span.simple) {
        const note = document.createElement("div");
        note.className = "interval-fallback-note";
        note.textContent = "Custom span — edit as text:";
        const field = plainField(draft.span.raw, (v) => {
          draft.span = { simple: false, raw: v };
          commit();
        });
        const reset = document.createElement("button");
        reset.type = "button"; reset.className = "interval-fallback-reset"; reset.textContent = "Use a simple end time";
        reset.addEventListener("click", () => {
          draft.span = { simple: true, endTime: null };
          renderEndRow();
          commit();
        });
        endRow.append(note, field, reset);
        return;
      }
      if (draft.span.endTime === null) {
        const addBtn = document.createElement("button");
        addBtn.type = "button"; addBtn.className = "interval-end-ghost"; addBtn.textContent = "+ Add an end time";
        addBtn.addEventListener("click", () => {
          const mins = (minutesOf(draft.time || "09:00") + 60) % 1440;
          draft.span = { simple: true, endTime: hhmmOf(mins) };
          renderEndRow();
          commit();
        });
        endRow.appendChild(addBtn);
        return;
      }
      const label = document.createElement("span");
      label.className = "interval-row-label";
      label.textContent = "Ends at";
      const endInput = document.createElement("input");
      endInput.type = "time";
      endInput.value = draft.span.endTime;
      endInput.addEventListener("change", () => {
        draft.span = { simple: true, endTime: endInput.value || draft.span.endTime };
        renderEndRow();
        commit();
      });
      const durationLabel = document.createElement("span");
      durationLabel.className = "interval-end-duration";
      durationLabel.textContent = durationLabelFor(draft.time, draft.span.endTime);
      const clear = document.createElement("button");
      clear.type = "button"; clear.className = "interval-end-clear"; clear.textContent = "×";
      clear.addEventListener("click", () => {
        draft.span = { simple: true, endTime: null };
        renderEndRow();
        commit();
      });
      endRow.append(label, endInput, durationLabel, clear);
    }

    /** No frequency pills or weekday/ends steppers here any more -- see the
     *  comment above `repeatRow`. An existing rule is shown as raw RRULE
     *  text (seeded from composeRuleLines when it's still `simple: true`,
     *  so switching to text doesn't reformat a rule the user hasn't
     *  touched), and a fresh one starts from a plain weekly template. */
    function renderRepeatDetail() {
      repeatDetail.innerHTML = "";
      const hasRule = !draft.rule.simple || draft.rule.freq !== "NONE" || (draft.rule.otherLines || []).length > 0;
      if (!hasRule) {
        const addBtn = document.createElement("button");
        addBtn.type = "button"; addBtn.className = "interval-end-ghost"; addBtn.textContent = "+ Add a repeat rule";
        addBtn.addEventListener("click", () => {
          // Weekly on the start's own weekday: the overwhelmingly common
          // intent, and it means the first thing shown is already right.
          const day = DAY_CODES[new Date(`${draft.date}T00:00`).getDay()];
          draft.rule = {
            simple: true, freq: "WEEKLY", interval: 1, byday: [day],
            count: null, until: null, otherLines: draft.rule.otherLines || [],
          };
          renderRepeatDetail();
          commit();
        });
        repeatDetail.appendChild(addBtn);
        return;
      }

      // A rule this file could not safely decompose stays raw text --
      // structured controls that silently drop an EXDATE or a BYMONTHDAY
      // would be worse than an honest textarea.
      if (!draft.rule.simple) {
        repeatDetail.append(rawRuleFallback(draft, renderRepeatDetail, commit));
        return;
      }
      repeatDetail.appendChild(repeatEditor(draft.rule, () => {
        renderRepeatDetail();
        commit();
      }));
    }

    function renderAll() {
      renderCalendar();
      renderTimeRow();
      renderRepeatDetail();
      refreshText();
    }

    prevBtn.addEventListener("click", () => {
      const [y, mo] = month.split("-").map(Number);
      const dt = new Date(y, mo - 2, 1);
      month = `${dt.getFullYear()}-${pad2(dt.getMonth() + 1)}`;
      renderCalendar();
    });
    nextBtn.addEventListener("click", () => {
      const [y, mo] = month.split("-").map(Number);
      const dt = new Date(y, mo, 1);
      month = `${dt.getFullYear()}-${pad2(dt.getMonth() + 1)}`;
      renderCalendar();
    });
    const jumpTo = (offsetDays) => {
      const dt = new Date();
      dt.setDate(dt.getDate() + offsetDays);
      draft.date = isoOf(dt);
      month = draft.date.slice(0, 7);
      renderCalendar();
      commit();
    };
    jumpToday.addEventListener("click", () => jumpTo(0));
    jumpTomorrow.addEventListener("click", () => jumpTo(1));
    jumpNextWeek.addEventListener("click", () => jumpTo(7));

    allDayCheckbox.addEventListener("change", () => {
      draft.allDay = allDayCheckbox.checked;
      if (!draft.allDay && !draft.time) draft.time = "09:00";
      if (draft.allDay) draft.span = { simple: true, endTime: null };
      renderTimeRow();
      commit();
    });
    timeInput.addEventListener("change", () => {
      // Moving the start has to move the end with it -- the span is what a
      // start edit must preserve, not the end's own clock time (which
      // would otherwise silently shrink or invert the moment the start
      // crosses it).
      const oldTime = draft.time || "09:00";
      const newTime = timeInput.value || "09:00";
      if (draft.span.simple && draft.span.endTime !== null) {
        let duration = minutesOf(draft.span.endTime) - minutesOf(oldTime);
        if (duration <= 0) duration += 1440;
        draft.span = { simple: true, endTime: hhmmOf(minutesOf(newTime) + duration) };
      }
      draft.time = newTime;
      renderEndRow();
      commit();
    });

    function closePanel() {
      open = false;
      panel.classList.add("hidden");
      summaryBtn.classList.remove("open");
      if (openPicker === closePanel) openPicker = null;
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
      if (moveWatcher) { moveWatcher.disconnect(); moveWatcher = null; }
      // Undo the openPanel portal: put it back next to summaryBtn so a
      // closed field leaves nothing dangling off <body>.
      if (panel.parentNode === document.body) wrap.appendChild(panel);
    }

    function reposition() {
      if (open) positionIntervalPanel(panel, summaryBtn);
    }

    function openPanel() {
      // One picker at a time: two calendars over each other is never what
      // was meant, and the second would cover the first's Done button.
      if (openPicker) openPicker();
      open = true;
      summaryBtn.classList.add("open");
      // Promote out of `wrap` to escape any ancestor's backdrop-filter/
      // transform/filter -- each establishes a containing block for
      // `position: fixed` descendants, so left in place this panel would
      // position and clip against the nearest one of those (the shared
      // inspector panel, in practice) instead of the viewport.
      document.body.appendChild(panel);
      positionIntervalPanel(panel, summaryBtn);
      openPicker = closePanel;
      // Anything that can move the field under the popup: the window
      // resizing, the panel scrolling, or the panel being dragged (which
      // is a plain style change, so only an observer sees it).
      window.addEventListener("resize", reposition);
      window.addEventListener("scroll", reposition, true);
      moveWatcher = new MutationObserver(reposition);
      const host = summaryBtn.closest(".ins") || document.body;
      moveWatcher.observe(host, { attributes: true, attributeFilter: ["style"] });
    }

    summaryBtn.addEventListener("click", () => (open ? closePanel() : openPanel()));
    doneBtn.addEventListener("click", closePanel);
    clearBtn.addEventListener("click", async () => {
      closePanel();
      await onCommit("");
      if (onClear) await onClear();
    });

    renderAll();

    Object.defineProperty(wrap, "value", {
      get: () => serializeInterval(draft),
      set(v) {
        draft = parseIntervalValue(v) || {
          date: isoOf(new Date()), time: "", allDay: true,
          span: { simple: true, endTime: null },
          rule: { simple: true, freq: "NONE", interval: 1, byday: [], count: null, until: null, otherLines: [] },
        };
        month = draft.date.slice(0, 7);
        renderAll();
      },
    });
    return wrap;
  }

  return {
    field: intervalField,
    closeOpen,
    isOpen: () => openPicker !== null,
    parse: parseIntervalValue,
    serialize: serializeInterval,
    summary: humanSummary,
    blankDraft,
    // Monastery drives its own repeat row off the same structured
    // editor, so that one rule editor exists rather than two.
    repeatEditor,
    composeRuleLines,
    isoOf,
  };
})();
