// monastery/frontend/app.js
//
// Wiring between monastery/api.py and the calendar. Everything about
// which motes are dated -- which properties count as dates, how a
// recurrence expands, what a mote's span is -- comes back from Python
// already decided (core/calendar.py reads, monastery/payload.py shapes).
// This file draws that answer, takes edits, and owns exactly two things
// Python has no opinion about: where the calendar is scrolled to, and
// which bindings you have switched off.
//
// Lane packing (two meetings at the same hour sitting side by side) is
// also local, for the same reason Mora highlights its own source: it is
// a fact about the rectangle, not about the mote.

let api;
let view = "week";           // "week" | "month"
let anchor = null;           // Date; the Monday shown, or any day of the month
let payload = null;          // the last calendar() answer
let bindings = null;         // null until the first answer; then string[]
let hiddenSpecies = new Set();
let selectedId = null;
let panel = null;            // the last focus() answer
let draft = null;            // {dateKey, start, end, allDay, speciesId, rows, left, top}
let railOpen = true;
let toastTimer = null;
let pollTimer = null;
let refreshSeq = 0;          // guards against two refreshes landing out of order

const POLL_MS = 4000;
const HOUR_PX = 48;          // must match --hour in style.css
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"];
const MON_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const UNKNOWN_COLOR = "oklch(0.62 0 0)";

const $ = (id) => document.getElementById(id);

// ---- colour -------------------------------------------------------
//
// Byte-for-byte the hash ui/frontend/graph.js uses, the same copy Mora
// carries. Three windows disagreeing about what colour "Meeting" is
// would be worse than any of them being arbitrary.

function hashString(text) {
  let h = 1779033703 ^ text.length;
  for (let i = 0; i < text.length; i++) {
    h = Math.imul(h ^ text.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  h = Math.imul(h ^ (h >>> 16), 2246822519);
  h = Math.imul(h ^ (h >>> 13), 3266489917);
  return (h ^= h >>> 16) >>> 0;
}

function hueFor(seed) {
  return hashString(seed) % 360;
}

/** A mote's colour: its species' hue, brighter if it is itself a
 *  species, grey if it has no type at all. */
function colorOf(entry) {
  if (!entry) return UNKNOWN_COLOR;
  if ((entry.kinds || []).includes("species")) return `oklch(0.80 0.13 ${hueFor(entry.id || "")})`;
  if (!entry.speciesId) return UNKNOWN_COLOR;
  return `oklch(0.72 0.11 ${hueFor(entry.speciesId)})`;
}

function hueOf(entry) {
  if (!entry) return null;
  if ((entry.kinds || []).includes("species")) return hueFor(entry.id || "");
  return entry.speciesId ? hueFor(entry.speciesId) : null;
}

/** The event rectangle's fill and edge: the mote's own hue, dimmed.
 *  A typeless mote gets grey rather than an arbitrary hue -- "no
 *  species" is worth seeing as itself. */
function eventTint(entry, selected) {
  const hue = hueOf(entry);
  if (hue === null) {
    return {
      bg: selected ? "oklch(0.62 0 0 / 0.3)" : "oklch(0.62 0 0 / 0.16)",
      border: selected ? UNKNOWN_COLOR : "oklch(0.62 0 0 / 0.32)",
      color: UNKNOWN_COLOR,
    };
  }
  return {
    bg: `oklch(0.74 0.1 ${hue} / ${selected ? 0.3 : 0.16})`,
    border: `oklch(0.74 0.1 ${hue} / ${selected ? 0.85 : 0.32})`,
    color: `oklch(0.74 0.1 ${hue})`,
  };
}

/** What a mote *is*: the kinds core says it is, else its species, else
 *  plain "mote". The same order ui/frontend/app.js::kindLabel uses. */
function kindOf(entry) {
  if (!entry) return "mote";
  const kinds = entry.kinds || [];
  if (kinds.length) return kinds.map((k) => k.replace(/_/g, " ")).join(" · ");
  return entry.speciesName || "mote";
}

// ---- dates ---------------------------------------------------------
//
// All local, all naive: every date a garden stores is naive local time
// (core/calendar.py says why), so nothing here touches UTC.

const pad = (n) => String(n).padStart(2, "0");
const keyOf = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const addDays = (d, n) => new Date(d.getFullYear(), d.getMonth(), d.getDate() + n);
const mondayOf = (d) => addDays(d, -((d.getDay() + 6) % 7));
const startOfDay = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate());
const fmtTime = (d) => `${pad(d.getHours())}:${pad(d.getMinutes())}`;
const minutesInto = (d) => d.getHours() * 60 + d.getMinutes();

function parseKey(key) {
  const [y, m, d] = key.split("-").map(Number);
  return new Date(y, m - 1, d);
}

/** An ISO string from Python back into a local Date. `new Date(s)` would
 *  read a bare "2026-08-24" as UTC midnight and land it on the 23rd in
 *  any western timezone -- which is exactly the kind of off-by-one a
 *  calendar cannot afford. */
function fromIso(text) {
  const [datePart, timePart] = text.split("T");
  const [y, m, d] = datePart.split("-").map(Number);
  if (!timePart) return new Date(y, m - 1, d);
  const [hh, mm, ss] = timePart.split(":").map(Number);
  return new Date(y, m - 1, d, hh || 0, mm || 0, Math.floor(ss || 0));
}

function todayKey() {
  return keyOf(new Date());
}

/** The half-open range currently on screen. The month view asks for the
 *  whole six-week grid it draws, not just the month, so a placement in
 *  the leading or trailing week is not silently missing. */
function visibleRange() {
  if (view === "week") {
    const start = mondayOf(anchor);
    return [start, addDays(start, 7)];
  }
  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const start = mondayOf(first);
  return [start, addDays(start, 42)];
}

// ---- small DOM helpers --------------------------------------------

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function clear(node) {
  node.replaceChildren();
  return node;
}

function button(className, text) {
  const node = el("button", className, text);
  node.type = "button";
  return node;
}

function toast(message, isError) {
  const node = $("toast");
  node.textContent = message;
  node.classList.toggle("error", !!isError);
  node.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), 4200);
}

/** Every api call goes through here: a bridge error is a toast, never a
 *  silently half-drawn calendar. */
async function call(promise, failure) {
  try {
    return await promise;
  } catch (error) {
    toast(failure || String(error && error.message ? error.message : error), true);
    return null;
  }
}

// ---- loading -------------------------------------------------------

async function refresh() {
  const seq = ++refreshSeq;
  const [start, end] = visibleRange();
  const answer = await call(
    api.calendar(isoLocal(start), isoLocal(end), bindings),
    "Could not read the calendar.",
  );
  // A poll that lands after the user has already navigated elsewhere
  // must not paint the range they left.
  if (answer === null || seq !== refreshSeq) return;
  payload = answer;
  bindings = answer.bindings.filter((b) => b.active).map((b) => b.key);
  draw();
}

/** The visible range as the naive local ISO strings Python parses.
 *  toISOString() would convert to UTC first, shifting the window by the
 *  local offset -- the same trap fromIso() exists for. */
function isoLocal(d) {
  return `${keyOf(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

// ---- drawing -------------------------------------------------------

function draw() {
  drawTopbar();
  drawRail();
  if (view === "week") drawWeek(); else drawMonth();
  drawStatus();
}

function visibleOccurrences() {
  if (!payload) return [];
  return payload.occurrences.filter((o) => !(o.speciesId && hiddenSpecies.has(o.speciesId)));
}

function byDay() {
  const map = new Map();
  visibleOccurrences().forEach((o) => {
    const start = fromIso(o.start);
    const end = fromIso(o.end);
    // An all-day occurrence spanning several days belongs to each of
    // them: "away Tue to Thu" should be visible on Wednesday, not only
    // where it started.
    if (o.allDay) {
      let day = startOfDay(start);
      const last = startOfDay(addDays(end, -1));
      for (let guard = 0; guard < 400 && day <= last; guard++) {
        push(map, keyOf(day), { ...o, _start: start, _end: end });
        day = addDays(day, 1);
      }
      return;
    }
    push(map, keyOf(start), { ...o, _start: start, _end: end });
  });
  return map;
}

function push(map, key, value) {
  if (!map.has(key)) map.set(key, []);
  map.get(key).push(value);
}

function drawTopbar() {
  $("range-title").textContent = rangeTitle();
  $("range-sub").textContent = rangeSub();
  document.querySelectorAll("#modes button").forEach((b) => {
    b.classList.toggle("on", b.dataset.view === view);
  });
  $("rail-toggle").classList.toggle("on", railOpen);
  $("rail").classList.toggle("hidden", !railOpen);
}

function rangeTitle() {
  if (view === "month") return `${MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}`;
  const start = mondayOf(anchor);
  const end = addDays(start, 6);
  return `${start.getDate()} ${MON_SHORT[start.getMonth()]} – ${end.getDate()} ${MON_SHORT[end.getMonth()]} ${end.getFullYear()}`;
}

function rangeSub() {
  if (!payload) return "";
  if (view === "month") return `${visibleOccurrences().length} placements`;
  const start = mondayOf(anchor);
  return `${MONTHS[start.getMonth()]} ${start.getFullYear()}`;
}

// ---- rail ----

function drawRail() {
  drawMini();
  drawBindingRows();
  drawSpeciesRows();
}

function drawMini() {
  $("mini-title").textContent = `${MONTHS[anchor.getMonth()]} ${anchor.getFullYear()}`;
  const grid = clear($("mini"));
  DOW.forEach((label) => grid.append(el("div", "mini-head", view === "month" ? label : label[0])));

  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const start = mondayOf(first);
  const days = byDay();
  const weekStart = mondayOf(anchor);
  const weekEnd = addDays(weekStart, 6);

  for (let i = 0; i < 42; i++) {
    const day = addDays(start, i);
    const key = keyOf(day);
    const node = button("mini-day");
    node.append(el("span", null, String(day.getDate())));
    if (day.getMonth() !== anchor.getMonth()) node.classList.add("other");
    if (view === "week" && day >= weekStart && day <= weekEnd) node.classList.add("in-view");
    if (key === todayKey()) node.classList.add("today");
    if ((days.get(key) || []).length) node.append(el("span", "mini-dot"));
    node.onclick = () => {
      anchor = view === "month" ? day : mondayOf(day);
      if (view === "month") view = "week";
      closeDraft();
      refresh();
    };
    grid.append(node);
  }
}

function drawBindingRows() {
  const host = clear($("binding-rows"));
  const rows = payload ? payload.bindings : [];
  if (!rows.length) {
    host.append(el("div", "panel-empty", "No dated properties in this garden yet."));
    return;
  }
  rows.forEach((b) => {
    const row = button("toggle-row");
    if (!b.active) row.classList.add("off");
    row.append(el("span", "toggle-box"));
    row.append(el("span", "toggle-name mono", b.key));
    row.append(el("span", "toggle-count", String(b.count)));
    row.onclick = () => {
      bindings = b.active ? bindings.filter((k) => k !== b.key) : [...bindings, b.key];
      refresh();
    };
    host.append(row);
  });
}

function drawSpeciesRows() {
  const host = clear($("species-rows"));
  const species = payload ? payload.species : [];
  if (!species.length) {
    host.append(el("div", "panel-empty", "No species in this garden yet."));
    return;
  }
  const counts = new Map();
  (payload.occurrences || []).forEach((o) => {
    if (o.speciesId) counts.set(o.speciesId, (counts.get(o.speciesId) || 0) + 1);
  });
  species.forEach((s) => {
    const hidden = hiddenSpecies.has(s.id);
    const row = button("toggle-row");
    if (hidden) row.classList.add("off");
    const dot = el("span", "toggle-dot");
    dot.style.background = `oklch(0.72 0.11 ${hueFor(s.id)})`;
    row.append(dot);
    row.append(el("span", "toggle-name plain", s.name));
    row.append(el("span", "toggle-count", String(counts.get(s.id) || 0)));
    row.onclick = () => {
      if (hidden) hiddenSpecies.delete(s.id); else hiddenSpecies.add(s.id);
      draw();
    };
    host.append(row);
  });
}

// ---- week view ----

function drawWeek() {
  $("week-view").classList.remove("hidden");
  $("month-view").classList.add("hidden");

  const start = mondayOf(anchor);
  const days = Array.from({ length: 7 }, (_, i) => addDays(start, i));
  const placed = byDay();

  const head = $("week-head");
  clear(head).append(el("div", "gutter"));
  days.forEach((day) => {
    const cell = el("div", "day-head");
    if (keyOf(day) === todayKey()) cell.classList.add("today");
    const inner = el("div", "day-head-inner");
    inner.append(el("span", "day-dow", DOW[(day.getDay() + 6) % 7]));
    inner.append(el("span", "day-num", String(day.getDate())));
    cell.append(inner);
    head.append(cell);
  });

  const allday = $("allday-row");
  clear(allday);
  const gutter = el("div", "gutter");
  gutter.append(el("span", "gutter-label", "all day"));
  allday.append(gutter);
  days.forEach((day) => {
    const cell = el("div", "allday-cell");
    (placed.get(keyOf(day)) || []).filter((o) => o.allDay).forEach((o) => cell.append(pinFor(o)));
    allday.append(cell);
  });

  const grid = $("week-grid");
  clear(grid);
  const hours = el("div", "gutter");
  hours.id = "hours";
  hours.style.height = `${24 * HOUR_PX}px`;
  for (let h = 0; h <= 24; h++) {
    const label = el("span", "hour-label", `${pad(h % 24)}:00`);
    label.style.top = `${h * HOUR_PX}px`;
    hours.append(label);
  }
  grid.append(hours);
  grid.style.height = `${24 * HOUR_PX}px`;

  days.forEach((day) => {
    const key = keyOf(day);
    const col = el("div", "day-col");
    if (key === todayKey()) col.classList.add("today");
    for (let h = 1; h < 24; h++) {
      const line = el("div", "hour-line");
      line.style.top = `${h * HOUR_PX}px`;
      col.append(line);
    }
    if (key === todayKey()) {
      const now = el("div");
      now.id = "now-line";
      now.style.top = `${(minutesInto(new Date()) / 60) * HOUR_PX}px`;
      col.append(now);
    }
    lanes((placed.get(key) || []).filter((o) => !o.allDay)).forEach((o) => col.append(eventFor(o)));
    col.onclick = (event) => {
      if (event.target.closest(".event")) return;
      openDraftAt(event, key, minutesAt(event, col));
    };
    grid.append(col);
  });
}

function minutesAt(event, col) {
  const rect = col.getBoundingClientRect();
  const minutes = ((event.clientY - rect.top) / HOUR_PX) * 60;
  return Math.max(0, Math.min(23 * 60 + 45, Math.round(minutes / 15) * 15));
}

/** Side-by-side columns for overlapping events: the first lane whose
 *  last event has already ended, else a new one. Local because it is a
 *  fact about the rectangles, not about the motes. */
function lanes(list) {
  const sorted = list.slice().sort((a, b) => a._start - b._start || b._end - a._end);
  const laneEnds = [];
  sorted.forEach((o) => {
    let lane = laneEnds.findIndex((end) => end <= o._start);
    if (lane === -1) { lane = laneEnds.length; laneEnds.push(0); }
    laneEnds[lane] = o._end;
    o._lane = lane;
  });
  const count = Math.max(1, laneEnds.length);
  sorted.forEach((o) => { o._laneCount = count; });
  return sorted;
}

function eventFor(o) {
  const selected = o.id === selectedId;
  const tint = eventTint(o, selected);
  const top = (minutesInto(o._start) / 60) * HOUR_PX;
  const minutes = Math.max(15, (o._end - o._start) / 60000);
  const height = Math.max(20, (minutes / 60) * HOUR_PX - 2);
  const width = 100 / o._laneCount;
  const compact = height < 42;

  const node = button("event");
  if (compact) node.classList.add("compact");
  if (selected) node.classList.add("selected");
  node.style.top = `${Math.round(top)}px`;
  node.style.height = `${Math.round(height)}px`;
  node.style.left = `${(o._lane * width).toFixed(2)}%`;
  node.style.width = `calc(${width.toFixed(2)}% - 3px)`;
  node.style.background = tint.bg;
  node.style.borderColor = tint.border;
  node.style.borderLeftColor = tint.color;

  const time = el("div", "event-time", compact
    ? fmtTime(o._start)
    : `${fmtTime(o._start)}–${fmtTime(o._end)}`);
  time.style.color = tint.color;
  node.append(time);

  const name = el("div", "event-name", o.name);
  if (!compact) name.style.webkitLineClamp = String(Math.max(1, Math.floor((height - 16) / 15)));
  node.append(name);

  node.title = `${o.name} — placed by ${o.binding}`;
  node.onclick = (event) => { event.stopPropagation(); select(o.id); };
  return node;
}

function pinFor(o) {
  const tint = eventTint(o, o.id === selectedId);
  const node = button("pin");
  node.style.background = tint.bg;
  node.style.borderColor = tint.border;
  const dot = el("span", "pin-dot");
  dot.style.background = tint.color;
  node.append(dot);
  node.append(el("span", "pin-name", o.name));
  node.append(el("span", "pin-binding", o.binding));
  node.title = `${o.name} — placed by ${o.binding}`;
  node.onclick = (event) => { event.stopPropagation(); select(o.id); };
  return node;
}

// ---- month view ----

function drawMonth() {
  $("month-view").classList.remove("hidden");
  $("week-view").classList.add("hidden");

  const head = clear($("month-head"));
  DOW.forEach((label) => head.append(el("div", "month-head-cell", label)));

  const first = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  const start = mondayOf(first);
  const placed = byDay();
  const grid = clear($("month-grid"));

  for (let w = 0; w < 6; w++) {
    const row = el("div", "month-week");
    for (let d = 0; d < 7; d++) {
      const day = addDays(start, w * 7 + d);
      const key = keyOf(day);
      const cell = el("div", "month-cell");
      if (day.getMonth() !== anchor.getMonth()) cell.classList.add("other");
      if (key === todayKey()) cell.classList.add("today");

      const cellHead = el("div", "month-cell-head");
      const num = button("month-num", String(day.getDate()));
      num.onclick = (event) => {
        event.stopPropagation();
        view = "week";
        anchor = mondayOf(day);
        closeDraft();
        refresh();
      };
      cellHead.append(num);
      if (day.getDate() === 1) cellHead.append(el("span", "month-tag", MON_SHORT[day.getMonth()]));
      cell.append(cellHead);

      const all = (placed.get(key) || []).slice().sort((a, b) => {
        if (a.allDay !== b.allDay) return a.allDay ? -1 : 1;
        return a._start - b._start;
      });
      all.slice(0, 3).forEach((o) => cell.append(chipFor(o)));
      if (all.length > 3) {
        const more = button("month-more", `+${all.length - 3} more`);
        more.onclick = (event) => {
          event.stopPropagation();
          view = "week";
          anchor = mondayOf(day);
          closeDraft();
          refresh();
        };
        cell.append(more);
      }

      cell.onclick = (event) => {
        if (event.target.closest(".chip") || event.target.closest(".month-num")) return;
        openDraftAt(event, key, 9 * 60);
      };
      row.append(cell);
    }
    grid.append(row);
  }
}

function chipFor(o) {
  const tint = eventTint(o, o.id === selectedId);
  const node = button("chip");
  node.style.background = tint.bg;
  node.style.borderColor = tint.border;
  const dot = el("span", "pin-dot");
  dot.style.background = tint.color;
  node.append(dot);
  node.append(el("span", "chip-time", o.allDay ? o.binding : fmtTime(o._start)));
  node.append(el("span", "chip-name", o.name));
  node.title = `${o.name} — placed by ${o.binding}`;
  node.onclick = (event) => { event.stopPropagation(); select(o.id); };
  return node;
}

// ---- status bar ----

function drawStatus() {
  const stats = payload ? payload.stats : { undated: 0 };
  const visible = new Set(visibleOccurrences().map((o) => o.id)).size;
  $("stat-placed").textContent = `${visible} placed`;
  $("stat-undated").textContent =
    `${stats.undated} mote${stats.undated === 1 ? "" : "s"} without a date property`;
  $("stat-bindings").textContent = bindings && bindings.length
    ? `bindings: ${bindings.join(", ")}`
    : "no bindings active";
}

// ---- detail panel ----

async function select(moteId) {
  selectedId = moteId;
  closeDraft();
  panel = await call(api.focus(moteId), "Could not read that mote.");
  drawPanel();
  draw();
}

function closePanel() {
  selectedId = null;
  panel = null;
  $("panel").classList.add("hidden");
  draw();
}

function drawPanel() {
  const node = $("panel");
  if (!panel) { node.classList.add("hidden"); return; }
  node.classList.remove("hidden");

  $("panel-dot").style.background = colorOf(panel);
  $("panel-kind-text").style.color = colorOf(panel);
  $("panel-kind-text").textContent = kindOf(panel);
  $("panel-name").textContent = panel.name;
  $("panel-id").textContent = panel.id;

  const placed = clear($("panel-placed"));
  const occurrence = (payload ? payload.occurrences : []).find((o) => o.id === panel.id);
  if (occurrence) {
    const row = el("div", "placed-by");
    row.append(el("span", "key", occurrence.binding));
    row.append(el("span", "eq", "="));
    row.append(el("span", "val", String(panel.properties[occurrence.binding] ?? "")));
    placed.append(row);
  } else {
    placed.append(el("div", "panel-empty", "Not placed in this range."));
  }

  const props = clear($("panel-props"));
  const keys = Object.keys(panel.properties).sort();
  if (!keys.length) props.append(el("div", "panel-empty", "No properties."));
  keys.forEach((key) => {
    const row = el("div", "prop-row");
    const label = el("span", "prop-key", key.replace(/_/g, " "));
    if ((panel.calendarKeys || []).includes(key)) label.classList.add("reserved");
    row.append(label);
    const value = panel.properties[key];
    row.append(el("span", "prop-val", typeof value === "string" ? value : JSON.stringify(value)));
    props.append(row);
  });

  const rels = clear($("panel-rels"));
  if (!panel.relationships.length) rels.append(el("div", "panel-empty", "No relationships."));
  panel.relationships.forEach((r) => {
    const row = button("rel-row");
    row.append(el("span", "rel-arrow", r.outgoing ? "→" : "←"));
    const dot = el("span", "rel-dot");
    dot.style.background = colorOf(r.other);
    row.append(dot);
    row.append(el("span", "rel-name", r.other.name));
    row.append(el("span", "rel-type", r.type.replace(/_/g, " ")));
    row.onclick = () => select(r.other.id);
    rels.append(row);
  });
}

// ---- the draft popover ----

function openDraftAt(event, dateKey, minutes) {
  const host = $("cal").getBoundingClientRect();
  const left = Math.max(10, Math.min(host.width - 316, event.clientX - host.left + 12));
  const top = Math.max(10, Math.min(Math.max(10, host.height - 420), event.clientY - host.top - 40));
  const start = minutes === null ? null : Math.round(minutes / 15) * 15;

  selectedId = null;
  panel = null;
  $("panel").classList.add("hidden");

  draft = {
    dateKey,
    allDay: start === null,
    start: start === null ? "" : fmtMinutes(start),
    end: start === null ? "" : fmtMinutes(Math.min(23 * 60 + 59, start + 60)),
    speciesId: null,
    rows: [],
    left,
    top,
  };
  drawDraft();
  draw();
  setTimeout(() => $("draft-name").focus(), 30);
}

function fmtMinutes(m) {
  return `${pad(Math.floor(m / 60))}:${pad(m % 60)}`;
}

function closeDraft() {
  draft = null;
  $("draft").classList.add("hidden");
}

function drawDraft() {
  const node = $("draft");
  if (!draft) { node.classList.add("hidden"); return; }
  node.classList.remove("hidden");
  node.style.left = `${Math.round(draft.left)}px`;
  node.style.top = `${Math.round(draft.top)}px`;

  $("draft-anchor").textContent = `date · ${draft.dateKey}`;
  $("draft-date").value = draft.dateKey;
  $("draft-start").value = draft.start;
  $("draft-end").value = draft.end;
  $("draft-allday").classList.toggle("on", draft.allDay);
  $("draft-times").classList.toggle("hidden", draft.allDay);
  $("draft-duration").textContent = draftDuration();

  const species = clear($("draft-species"));
  const list = payload ? payload.species : [];
  if (!list.length) {
    species.append(el("div", "panel-empty", "No species yet — this will be a plain mote."));
  }
  list.forEach((s) => {
    const chip = button("species-chip");
    const dot = el("span", "species-dot");
    const hue = hueFor(s.id);
    dot.style.background = `oklch(0.72 0.11 ${hue})`;
    chip.append(dot);
    chip.append(el("span", null, s.name));
    if (draft.speciesId === s.id) chip.style.borderColor = `oklch(0.72 0.11 ${hue})`;
    chip.onclick = () => {
      draft.speciesId = draft.speciesId === s.id ? null : s.id;
      drawDraft();
    };
    species.append(chip);
  });

  const rows = clear($("draft-rows"));
  draft.rows.forEach((row, index) => {
    const line = el("div", "draft-line draft-row");
    const key = el("input", "k");
    key.type = "text";
    key.placeholder = "key";
    key.value = row.key;
    key.oninput = () => { draft.rows[index].key = key.value; };
    const value = el("input", "v");
    value.type = "text";
    value.placeholder = "value";
    value.value = row.value;
    value.oninput = () => { draft.rows[index].value = value.value; };
    const remove = button("icon-button", "×");
    remove.onclick = () => { draft.rows.splice(index, 1); drawDraft(); };
    line.append(key, value, remove);
    rows.append(line);
  });
}

function draftDuration() {
  if (!draft || draft.allDay) return "";
  const start = parseHm(draft.start);
  const end = parseHm(draft.end);
  if (start === null || end === null || end <= start) return "";
  const minutes = end - start;
  return minutes % 60 === 0 ? `${minutes / 60}h` : `${minutes}m`;
}

function parseHm(text) {
  const match = /^(\d{1,2}):(\d{2})$/.exec((text || "").trim());
  if (!match) return null;
  const minutes = Number(match[1]) * 60 + Number(match[2]);
  return minutes >= 0 && minutes < 24 * 60 ? minutes : null;
}

/** The draft as properties. Always `date`, never the binding the
 *  calendar happens to be filtered by: reading is general, but there is
 *  one right answer to where a new date belongs, and it is the reserved
 *  key Calendar export already uses. */
function draftProperties() {
  if (!draft) return null;
  const name = $("draft-name").value.trim();
  const dateKey = $("draft-date").value.trim();
  const properties = {};
  if (name) properties.name = name;

  if (draft.allDay) {
    properties.date = dateKey;
  } else {
    const start = parseHm($("draft-start").value);
    const end = parseHm($("draft-end").value);
    if (start === null) return null;
    properties.date = `${dateKey}T${fmtMinutes(start)}:00`;
    if (end !== null && end > start) properties.duration = String(end - start);
  }

  draft.rows.forEach((row) => {
    const key = row.key.trim();
    if (key) properties[key] = row.value;
  });
  return properties;
}

async function createFromDraft() {
  if (!draft) return;
  const properties = draftProperties();
  if (properties === null) { toast("That start time isn't readable — try 09:15.", true); return; }
  if (!properties.date) { toast("A new mote needs a date.", true); return; }

  const speciesId = draft.speciesId;
  const created = await call(api.create_mote(properties, speciesId), "Could not create that mote.");
  if (created === null) return;
  closeDraft();
  await refresh();
  selectedId = created.id;
  panel = created;
  drawPanel();
  draw();
  toast(`${created.name} created — placed by date.`);
}

// ---- garden menu ----

async function openGardenMenu() {
  const menu = $("garden-menu");
  if (!menu.classList.contains("hidden")) { closeGardenMenu(); return; }
  const gardens = await call(api.list_gardens(), "Could not list gardens.");
  if (gardens === null) return;
  const current = $("garden-name").textContent;
  clear(menu);
  if (!gardens.length) menu.append(el("div", "menu-empty", "No gardens."));
  gardens.forEach((name) => {
    const item = button("menu-item", name);
    if (name === current) item.classList.add("on");
    item.onclick = async () => {
      closeGardenMenu();
      const answer = await call(api.switch_garden(name), "Could not switch garden.");
      if (answer === null) return;
      $("garden-name").textContent = answer.garden;
      bindings = null;
      hiddenSpecies = new Set();
      closePanel();
      closeDraft();
      await refresh();
    };
    menu.append(item);
  });
  menu.classList.remove("hidden");
  $("garden-btn").setAttribute("aria-expanded", "true");
}

function closeGardenMenu() {
  $("garden-menu").classList.add("hidden");
  $("garden-btn").setAttribute("aria-expanded", "false");
}

// ---- navigation ----

function step(n) {
  anchor = view === "week"
    ? addDays(mondayOf(anchor), 7 * n)
    : new Date(anchor.getFullYear(), anchor.getMonth() + n, 1);
  closeDraft();
  refresh();
}

function setView(next) {
  if (view === next) return;
  view = next;
  closeDraft();
  refresh();
}

// ---- polling -------------------------------------------------------
//
// Nothing in MonOSS pushes: the graph window, the CLI and the MCP server
// all write the same file and none of them announces it (see
// docs/architecture.md). So the calendar asks again, on a timer, and
// only while the window is focused -- a background window polling a
// SQLite file every few seconds is work nobody is looking at.

function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => {
    // A poll must never fight the person typing: reopening the draft's
    // inputs underneath them would lose the keystroke in flight.
    if (draft) return;
    refresh();
  }, POLL_MS);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

// ---- wiring --------------------------------------------------------

function wire() {
  $("prev").onclick = () => step(-1);
  $("next").onclick = () => step(1);
  $("today").onclick = () => { anchor = new Date(); closeDraft(); refresh(); };
  document.querySelectorAll("#modes button").forEach((b) => {
    b.onclick = () => setView(b.dataset.view);
  });
  $("rail-toggle").onclick = () => { railOpen = !railOpen; draw(); };
  $("garden-btn").onclick = openGardenMenu;

  $("panel-close").onclick = closePanel;

  $("draft-allday").onclick = () => {
    draft.allDay = !draft.allDay;
    drawDraft();
  };
  $("draft-add-row").onclick = () => { draft.rows.push({ key: "", value: "" }); drawDraft(); };
  $("draft-cancel").onclick = () => { closeDraft(); draw(); };
  $("draft-create").onclick = createFromDraft;
  ["draft-start", "draft-end"].forEach((id) => {
    $(id).oninput = () => {
      draft.start = $("draft-start").value;
      draft.end = $("draft-end").value;
      $("draft-duration").textContent = draftDuration();
    };
  });
  $("draft-name").onkeydown = (event) => {
    if (event.key === "Enter") { event.preventDefault(); createFromDraft(); }
  };

  // Whether a click started inside the draft, answered in the capture
  // phase -- which runs top-down, before the clicked control's own
  // handler. Asking afterwards with closest("#draft") does not work: a
  // chip or a row's × re-renders the popover from inside its own
  // handler, so by the time the click reaches the document the node it
  // targeted is detached and reports no ancestors at all. That read as
  // the user having clicked *outside*, and closed the draft they were
  // filling in.
  let clickedInDraft = false;
  $("draft").addEventListener("click", () => { clickedInDraft = true; }, true);

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".topbar-menu")) closeGardenMenu();
    if (draft && !clickedInDraft && !event.target.closest(".day-col")
        && !event.target.closest(".month-cell")) {
      closeDraft();
      draw();
    }
    clickedInDraft = false;
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeGardenMenu();
      if (draft) { closeDraft(); draw(); }
      else if (selectedId) closePanel();
      return;
    }
    // Not while typing into the draft -- these are navigation, and a
    // name with a "t" in it is not a request to jump to today.
    if (event.target.tagName === "INPUT") return;
    if (event.key === "ArrowLeft") step(-1);
    else if (event.key === "ArrowRight") step(1);
    else if (event.key.toLowerCase() === "t") { anchor = new Date(); closeDraft(); refresh(); }
    else if (event.key.toLowerCase() === "w") setView("week");
    else if (event.key.toLowerCase() === "m") setView("month");
  });

  window.addEventListener("focus", () => { refresh(); startPolling(); });
  window.addEventListener("blur", stopPolling);
}

async function init() {
  api = window.pywebview.api;
  anchor = new Date();
  wire();
  $("garden-name").textContent = await api.current_garden();
  await refresh();

  // Open on the working day rather than on midnight, which is what the
  // top of a 24-hour grid otherwise shows.
  const scroll = $("week-scroll");
  scroll.scrollTop = Math.max(0, 8 * HOUR_PX - 8);

  const focus = await api.initial_focus();
  if (focus) await select(focus);

  startPolling();
}

window.addEventListener("pywebviewready", init);
