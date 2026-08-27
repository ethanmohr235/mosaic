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
let draft = null;            // {dateKey, start, end, allDay, days, speciesId, rows, rule, left, top}
let railOpen = true;
let toastTimer = null;
let pollTimer = null;
let refreshSeq = 0;          // guards against two refreshes landing out of order
let selectedKey = null;      // which occurrence of the selected mote was clicked
let drag = null;             // the pointer gesture in flight, if any
let chooser = null;          // the this-one-or-the-series question, while it is up
let justDragged = false;     // swallows the click a finished drag leaves behind
let chooserFresh = false;    // ... and the one that put the chooser up
let lastPointer = { x: 0, y: 0 };

const POLL_MS = 4000;
const HOUR_PX = 48;          // must match --hour in style.css
const DRAG_PX = 4;           // how far a press moves before it counts as a drag
const EDGE_PX = 6;           // how deep an event's resize edges reach
const SNAP_MIN = 15;         // what a dragged time rounds to
const MIN_MINUTES = 15;      // the shortest event a drag can leave behind
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"];
const MON_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const UNKNOWN_COLOR = "oklch(0.62 0 0)";
const DAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];
const DAY_MIN = { MO: "M", TU: "T", WE: "W", TH: "T", FR: "F", SA: "S", SU: "S" };
const FREQ_OPTS = [
  { id: "NONE", label: "Once" }, { id: "DAILY", label: "Daily" }, { id: "WEEKLY", label: "Weekly" },
  { id: "MONTHLY", label: "Monthly" }, { id: "YEARLY", label: "Yearly" },
];
const FREQ_UNIT = { DAILY: "day", WEEKLY: "week", MONTHLY: "month", YEARLY: "year" };

const $ = (id) => document.getElementById(id);

// ---- colour -------------------------------------------------------
//
// Byte-for-byte the hash monarch/frontend/graph.js uses, the same copy Mora
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
 *  plain "mote". The same order monarch/frontend/app.js::kindLabel uses. */
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

const addMinutes = (d, n) => new Date(d.getTime() + n * 60000);
const atMinutes = (day, minutes) =>
  new Date(day.getFullYear(), day.getMonth(), day.getDate(), 0, minutes);
const snap = (minutes) => Math.round(minutes / SNAP_MIN) * SNAP_MIN;
const daysBetween = (from, to) => Math.round((startOfDay(to) - startOfDay(from)) / 86400000);
// addDays() builds from the date parts alone and so drops the time of
// day; a month chip dragged to Thursday has to keep its 14:00.
const shiftDays = (d, n) => new Date(
  d.getFullYear(), d.getMonth(), d.getDate() + n, d.getHours(), d.getMinutes(), d.getSeconds(),
);

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

/** `action` is an optional {label, run}. It is what makes a delete
 *  undoable without a modal in the way: the sentence saying what
 *  happened is also where you take it back. */
function toast(message, isError, action) {
  const node = clear($("toast"));
  node.append(el("span", null, message));
  if (action) {
    const undo = button("toast-action", action.label);
    undo.onclick = () => { node.classList.add("hidden"); action.run(); };
    node.append(undo);
  }
  node.classList.toggle("error", !!isError);
  node.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), action ? 8000 : 4200);
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
    const key = keyOf(day);
    const cell = el("div", "allday-cell");
    cell.dataset.day = key;
    (placed.get(key) || []).filter((o) => o.allDay).forEach((o) => cell.append(pinFor(o, key)));
    cell.onpointerdown = (event) => {
      if (event.target.closest(".pin")) return;
      beginDrag(event, { kind: "sweep-days", dayKey: key });
    };
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
    // Every droppable surface says which day it is, so a drag can ask
    // the DOM where the pointer ended up instead of recomputing the
    // week's arithmetic a second time.
    col.dataset.day = key;
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
    col.onpointerdown = (event) => {
      if (event.target.closest(".event")) return;
      // `host` is the column the sweep began in: a rectangle drawn
      // across two days says two different things and means neither, so
      // the drag stays in the column it started from.
      beginDrag(event, {
        kind: "sweep-time", dayKey: key, minutes: minutesAt(event, col), host: col,
      });
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

  node.append(killFor(o));
  node.title = `${o.name} — placed by ${o.binding}`;
  node.onpointerdown = (event) => {
    if (event.target.closest(".kill")) return;
    event.stopPropagation();
    beginDrag(event, {
      kind: "event", o, node,
      mode: edgeMode(event, node), grip: gripMinutes(event, node),
    });
  };
  node.onclick = (event) => {
    event.stopPropagation();
    if (justDragged) return;
    select(o.id, o.key);
  };
  return node;
}

/** The × that removes a placement. Hidden until the rectangle is hovered
 *  or selected (style.css), because a calendar covered in delete buttons
 *  is a calendar you read past rather than read. */
function killFor(o) {
  const node = button("kill", "×");
  node.title = "Delete";
  node.onpointerdown = (event) => event.stopPropagation();
  node.onclick = (event) => { event.stopPropagation(); deleteOccurrence(o); };
  return node;
}

/** Which part of an event the press landed on. A short rectangle keeps
 *  its whole body draggable -- edges that met in the middle would leave
 *  a 15-minute event with no way to move it at all. */
function edgeMode(event, node) {
  const rect = node.getBoundingClientRect();
  const reach = Math.min(EDGE_PX, Math.max(3, rect.height / 3));
  const offset = event.clientY - rect.top;
  if (rect.height >= 24 && offset <= reach) return "top";
  if (offset >= rect.height - reach) return "bottom";
  return "move";
}

/** How far into the event the grab was, in minutes -- so a moved event
 *  keeps the grip you took it by instead of jumping its top edge to the
 *  cursor. */
function gripMinutes(event, node) {
  const rect = node.getBoundingClientRect();
  return ((event.clientY - rect.top) / HOUR_PX) * 60;
}

function pinFor(o, dayKey) {
  const tint = eventTint(o, o.id === selectedId);
  const node = button("pin");
  node.style.background = tint.bg;
  node.style.borderColor = tint.border;
  const dot = el("span", "pin-dot");
  dot.style.background = tint.color;
  node.append(dot);
  node.append(el("span", "pin-name", o.name));
  node.append(el("span", "pin-binding", o.binding));
  node.append(killFor(o));
  node.title = `${o.name} — placed by ${o.binding}`;
  node.onpointerdown = (event) => {
    if (event.target.closest(".kill")) return;
    event.stopPropagation();
    // Only the right edge resizes an all-day pin. Dragging its left edge
    // would move the start and change the length at once, and there is
    // no honest single answer to which of the two you meant.
    const rect = node.getBoundingClientRect();
    const mode = event.clientX >= rect.right - EDGE_PX ? "bottom" : "move";
    beginDrag(event, { kind: "day", o, node, mode, dayKey });
  };
  node.onclick = (event) => {
    event.stopPropagation();
    if (justDragged) return;
    select(o.id, o.key);
  };
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
      cell.dataset.day = key;
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
      all.slice(0, 3).forEach((o) => cell.append(chipFor(o, key)));
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

      cell.onpointerdown = (event) => {
        if (event.target.closest(".chip") || event.target.closest(".month-num")) return;
        beginDrag(event, { kind: "sweep-days", dayKey: key });
      };
      row.append(cell);
    }
    grid.append(row);
  }
}

function chipFor(o, dayKey) {
  const tint = eventTint(o, o.id === selectedId);
  const node = button("chip");
  node.style.background = tint.bg;
  node.style.borderColor = tint.border;
  const dot = el("span", "pin-dot");
  dot.style.background = tint.color;
  node.append(dot);
  node.append(el("span", "chip-time", o.allDay ? o.binding : fmtTime(o._start)));
  node.append(el("span", "chip-name", o.name));
  node.append(killFor(o));
  node.title = `${o.name} — placed by ${o.binding}`;
  node.onpointerdown = (event) => {
    if (event.target.closest(".kill")) return;
    event.stopPropagation();
    // A month chip moves by whole days and keeps its time of day: the
    // cell is a day, so there is nothing finer for it to mean.
    beginDrag(event, { kind: "day", o, node, mode: "move", dayKey });
  };
  node.onclick = (event) => {
    event.stopPropagation();
    if (justDragged) return;
    select(o.id, o.key);
  };
  return node;
}

// ---- dragging ------------------------------------------------------
//
// One pointer layer for every gesture on the grid: moving a placement,
// dragging its edge, and sweeping empty space to draw a new one. It
// listens on `window` for the length of the gesture rather than calling
// setPointerCapture, because scripts/monastery_smoke_test.py drives this
// window with synthetic events and a synthetic pointerId makes capture
// throw.
//
// Nothing is written until the pointer comes up and nothing is
// re-rendered while it is down: a refresh mid-gesture would replace the
// very node being dragged. Everything before commitPlacement() is
// pixels.

function beginDrag(event, gesture) {
  // A press with the draft open belongs to the draft -- the document's
  // click handler closes it, and starting a drag underneath would fight
  // the popover for the same pointer.
  if (event.button !== 0 || chooser || draft) return;
  lastPointer = { x: event.clientX, y: event.clientY };
  drag = {
    ...gesture,
    x0: event.clientX, y0: event.clientY,
    x: event.clientX, y: event.clientY,
    moved: false,
  };
  window.addEventListener("pointermove", onDragMove);
  window.addEventListener("pointerup", onDragUp);
}

function onDragMove(event) {
  if (!drag) return;
  drag.x = event.clientX;
  drag.y = event.clientY;
  if (!drag.moved) {
    // Below the threshold this is still a click: a calendar where a
    // one-pixel wobble moves a meeting is a calendar you cannot select
    // anything in.
    if (Math.abs(drag.x - drag.x0) < DRAG_PX && Math.abs(drag.y - drag.y0) < DRAG_PX) return;
    drag.moved = true;
    if (drag.node) drag.node.classList.add("dragging");
    document.body.classList.add("dragging-on");
  }
  event.preventDefault();
  paintDrag();
}

function onDragUp(event) {
  window.removeEventListener("pointermove", onDragMove);
  window.removeEventListener("pointerup", onDragUp);
  const gesture = drag;
  drag = null;
  document.body.classList.remove("dragging-on");
  clearHints();
  if (!gesture) return;
  lastPointer = { x: event.clientX, y: event.clientY };
  if (!gesture.moved) { finishTap(gesture); return; }
  // A click still follows this pointerup, and without this the drag
  // would count as one too -- reselecting, or reopening what it just
  // dropped. The timer runs after that click has been delivered.
  justDragged = true;
  setTimeout(() => { justDragged = false; }, 0);
  finishDrag(gesture);
}

function clearHints() {
  document.querySelectorAll(".drop-target").forEach((n) => n.classList.remove("drop-target"));
  document.querySelectorAll(".sweep").forEach((n) => n.remove());
  document.querySelectorAll(".dragging").forEach((n) => n.classList.remove("dragging"));
}

/** Which of these nodes the pointer is over -- nearest rather than
 *  strictly inside, so a drag that wanders past the edge of the grid
 *  still lands on the day it was heading for. */
function nearestNode(nodes, x, y) {
  let best = null;
  let bestDistance = Infinity;
  nodes.forEach((node) => {
    const rect = node.getBoundingClientRect();
    const dx = Math.max(rect.left - x, 0, x - rect.right);
    const dy = Math.max(rect.top - y, 0, y - rect.bottom);
    const distance = dx * dx + dy * dy;
    if (distance < bestDistance) { bestDistance = distance; best = node; }
  });
  return best;
}

const timeColumns = () => [...document.querySelectorAll("#week-grid .day-col")];
const dayCells = () => [...document.querySelectorAll(
  view === "month" ? ".month-cell" : ".allday-cell",
)];

function minutesFrom(node, y) {
  const rect = node.getBoundingClientRect();
  return snap(((y - rect.top) / HOUR_PX) * 60);
}

/** Where a timed gesture currently says its event begins and ends.
 *  An edge drag stays on its own day: only the body of an event moves
 *  it, so a shaky hand on the bottom edge cannot also change the date. */
function timedPlacement(gesture) {
  const start = fromIso(gesture.o.start);
  const end = fromIso(gesture.o.end);
  const column = gesture.node.closest(".day-col");

  if (gesture.mode === "move") {
    const target = nearestNode(timeColumns(), gesture.x, gesture.y) || column;
    const rect = target.getBoundingClientRect();
    const raw = ((gesture.y - rect.top) / HOUR_PX) * 60 - gesture.grip;
    const minutes = Math.max(0, Math.min(24 * 60 - MIN_MINUTES, snap(raw)));
    const moved = atMinutes(parseKey(target.dataset.day), minutes);
    return [moved, addMinutes(moved, Math.max(MIN_MINUTES, (end - start) / 60000))];
  }

  const day = startOfDay(start);
  const pointer = minutesFrom(column, gesture.y);
  if (gesture.mode === "bottom") {
    const minutes = Math.max(minutesInto(start) + MIN_MINUTES, Math.min(24 * 60, pointer));
    return [start, atMinutes(day, minutes)];
  }
  return [atMinutes(day, Math.max(0, Math.min(minutesInto(end) - MIN_MINUTES, pointer))), end];
}

/** Where a whole-day gesture says its placement goes: the cell under
 *  the pointer, and the start and end that follow from it. */
function dayPlacement(gesture) {
  const cell = nearestNode(dayCells(), gesture.x, gesture.y);
  const target = parseKey(cell.dataset.day);
  const start = fromIso(gesture.o.start);
  const end = fromIso(gesture.o.end);
  if (gesture.mode === "bottom") {
    // The right edge of an all-day pin names the last day it covers,
    // and the end the bridge wants is the day after that.
    const last = Math.max(0, daysBetween(start, target));
    return [cell, start, addDays(startOfDay(start), last + 1)];
  }
  const delta = daysBetween(parseKey(gesture.dayKey), target);
  return [cell, shiftDays(start, delta), shiftDays(end, delta)];
}

/** A sweep down an empty column, as [from, to] minutes. It stays in the
 *  column it began in: a rectangle drawn across two days says two
 *  different things and means neither. */
function sweepTimePlacement(gesture) {
  const pointer = Math.max(0, Math.min(24 * 60, minutesFrom(gesture.host, gesture.y)));
  // Either direction: sweeping upwards from 14:00 to 12:00 draws
  // 12:00-14:00, not 12:00 to wherever the press happened to be plus a
  // quarter hour.
  const from = Math.min(gesture.minutes, pointer);
  return [from, Math.max(from + MIN_MINUTES, gesture.minutes, pointer)];
}

/** A sweep across empty cells, as the [first, last] day it covers --
 *  either direction, since dragging backwards is a range too. */
function sweepDaysPlacement(gesture) {
  const cell = nearestNode(dayCells(), gesture.x, gesture.y);
  const from = parseKey(gesture.dayKey);
  const to = parseKey(cell.dataset.day);
  return from <= to ? [from, to] : [to, from];
}

function paintDrag() {
  clearHints();
  if (drag.node) drag.node.classList.add("dragging");
  if (drag.kind === "event") paintTimedDrag();
  else if (drag.kind === "day") paintDayDrag();
  else if (drag.kind === "sweep-time") paintSweepTime();
  else paintSweepDays();
}

function paintTimedDrag() {
  const [start, end] = timedPlacement(drag);
  const column = drag.node.closest(".day-col");
  const target = drag.mode === "move"
    ? (nearestNode(timeColumns(), drag.x, drag.y) || column)
    : column;
  drag.node.style.top = `${Math.round((minutesInto(start) / 60) * HOUR_PX)}px`;
  drag.node.style.height =
    `${Math.max(20, Math.round(((end - start) / 3600000) * HOUR_PX) - 2)}px`;
  const shift = target.getBoundingClientRect().left - column.getBoundingClientRect().left;
  drag.node.style.transform = `translateX(${Math.round(shift)}px)`;
  const time = drag.node.querySelector(".event-time");
  if (time) time.textContent = drag.node.classList.contains("compact")
    ? fmtTime(start)
    : `${fmtTime(start)}–${fmtTime(end)}`;
}

function paintDayDrag() {
  const [cell] = dayPlacement(drag);
  if (cell) cell.classList.add("drop-target");
}

function paintSweepTime() {
  const [from, to] = sweepTimePlacement(drag);
  const preview = el("div", "sweep");
  preview.style.top = `${Math.round((from / 60) * HOUR_PX)}px`;
  preview.style.height = `${Math.max(12, Math.round(((to - from) / 60) * HOUR_PX))}px`;
  preview.append(el("span", "sweep-label", `${fmtMinutes(from)}–${fmtMinutes(to)}`));
  drag.host.append(preview);
}

function paintSweepDays() {
  const [from, to] = sweepDaysPlacement(drag);
  dayCells().forEach((cell) => {
    const day = parseKey(cell.dataset.day);
    if (day >= from && day <= to) cell.classList.add("drop-target");
  });
}

/** A press that never became a drag. An event, pin or chip handles its
 *  own click (selection); empty space drafts a mote, which is what a
 *  click on the grid has always done. */
function finishTap(gesture) {
  if (gesture.kind === "sweep-time") {
    openDraftAt(gesture, gesture.dayKey, gesture.minutes);
  } else if (gesture.kind === "sweep-days") {
    // A tapped month cell drafts a working-hours event, as it did before
    // there were gestures; a tapped all-day cell drafts an all-day one,
    // because that is the row you tapped.
    openDraftAt(gesture, gesture.dayKey, view === "month" ? 9 * 60 : null);
  }
}

function finishDrag(gesture) {
  if (gesture.kind === "event") {
    const [start, end] = timedPlacement(gesture);
    commitPlacement(gesture.o, start, end);
  } else if (gesture.kind === "day") {
    const [, start, end] = dayPlacement(gesture);
    commitPlacement(gesture.o, start, end);
  } else if (gesture.kind === "sweep-time") {
    const [from, to] = sweepTimePlacement(gesture);
    openDraftAt(gesture, gesture.dayKey, from, to);
  } else {
    const [from, to] = sweepDaysPlacement(gesture);
    openDraftAt(gesture, keyOf(from), null, null, daysBetween(from, to) + 1);
  }
}

/** Where a rectangle came to rest, written down. The bridge decides what
 *  that means in properties (monastery/api.py::place_occurrence); this
 *  only says which placement was grabbed and where it ended up. */
async function commitPlacement(o, start, end) {
  if (isoLocal(start) === isoLocal(fromIso(o.start))
      && isoLocal(end) === isoLocal(fromIso(o.end))) {
    draw();
    return;
  }
  const scope = await askScope(o);
  if (scope === null) { draw(); return; }

  const answer = await call(
    api.place_occurrence(o.id, o.binding, o.start, isoLocal(start), isoLocal(end), scope),
    "Could not move that mote.",
  );
  if (answer === null) { await refresh(); return; }

  if (scope === "occurrence") {
    selectedId = answer.id;
    selectedKey = null;
    panel = answer;
    toast(`${answer.name} split off from its series.`);
  } else if (selectedId === o.id) {
    panel = answer;
  }
  await refresh();
  drawPanel();
}

// ---- this occurrence, or the series? -------------------------------
//
// One mote is the whole series (docs/architecture.md), so an edit to a
// repeating placement is genuinely ambiguous and the window asks rather
// than guessing. `recurring` comes from core, which parsed the rule
// while expanding it -- no JavaScript reads an RRULE.

function askScope(o) {
  if (!o.recurring) return Promise.resolve("series");
  return new Promise((resolve) => {
    chooser = { name: o.name, resolve };
    // Every way of asking this -- a drop, the × on a rectangle, the
    // panel's Delete -- happens inside a click that is still on its way
    // to the document handler below, where it would read as "clicked
    // away" and cancel the very thing being asked about. The timer runs
    // after that click has been delivered.
    chooserFresh = true;
    setTimeout(() => { chooserFresh = false; }, 0);
    drawChooser();
  });
}

function answerChooser(scope) {
  const pending = chooser;
  chooser = null;
  chooserFresh = false;
  $("chooser").classList.add("hidden");
  if (pending) pending.resolve(scope);
}

function drawChooser() {
  const node = $("chooser");
  if (!chooser) { node.classList.add("hidden"); return; }
  $("chooser-title").textContent = `“${chooser.name}” repeats.`;
  node.classList.remove("hidden");
  const host = $("cal").getBoundingClientRect();
  const width = node.offsetWidth || 240;
  node.style.left = `${Math.round(
    Math.max(10, Math.min(host.width - width - 10, lastPointer.x - host.left - width / 2)),
  )}px`;
  node.style.top = `${Math.round(
    Math.max(10, Math.min(Math.max(10, host.height - 150), lastPointer.y - host.top + 14)),
  )}px`;
}

// ---- deleting ------------------------------------------------------

/** The placement the panel and the keyboard act on: the one that was
 *  clicked, else any of the selected mote's that is on screen. */
function selectedOccurrence() {
  if (!payload || !selectedId) return null;
  return payload.occurrences.find((o) => o.key === selectedKey)
    || payload.occurrences.find((o) => o.id === selectedId)
    || null;
}

async function deleteOccurrence(o) {
  const scope = await askScope(o);
  if (scope === null) return;

  const answer = await call(
    api.delete_occurrence(o.id, o.binding, o.start, scope),
    "Could not delete that mote.",
  );
  if (answer === null) return;
  if (answer.scope === "series" && selectedId === o.id) closePanel();
  await refresh();
  toast(
    answer.scope === "occurrence"
      ? `One occurrence of ${answer.name} removed.`
      : `${answer.name} deleted.`,
    false,
    { label: "Undo", run: () => undoDelete(answer.undo) },
  );
}

async function undoDelete(token) {
  const restored = await call(api.undo_delete(token), "Could not undo that.");
  if (restored === null) return;
  await refresh();
  toast(`${restored.name} restored.`);
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

async function select(moteId, occurrenceKey) {
  selectedId = moteId;
  // Which *placement* was clicked, not just which mote: a weekly standup
  // draws five rectangles, and deleting "this one" has to know which one
  // you meant.
  selectedKey = occurrenceKey || null;
  closeDraft();
  panel = await call(api.focus(moteId), "Could not read that mote.");
  drawPanel();
  draw();
}

function closePanel() {
  selectedId = null;
  selectedKey = null;
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
  const occurrence = selectedOccurrence();
  $("panel-delete").classList.toggle("hidden", !occurrence);
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

/** The popover a new mote is written in. `point` is where the gesture
 *  ended; `minutes`/`endMinutes` and `days` are what it drew -- a sweep
 *  down a column arrives with both times already decided, and a sweep
 *  across cells with a count of days, so the drawn rectangle and the
 *  drafted mote cannot disagree. */
function openDraftAt(point, dateKey, minutes, endMinutes, days) {
  const host = $("cal").getBoundingClientRect();
  const left = Math.max(10, Math.min(host.width - 316, point.x - host.left + 12));
  const top = Math.max(10, Math.min(Math.max(10, host.height - 420), point.y - host.top - 40));
  const start = minutes === null || minutes === undefined ? null : snap(minutes);
  const end = start === null
    ? null
    : Math.min(23 * 60 + 59, endMinutes ? snap(endMinutes) : start + 60);

  selectedId = null;
  selectedKey = null;
  panel = null;
  $("panel").classList.add("hidden");

  draft = {
    dateKey,
    allDay: start === null,
    days: Math.max(1, days || 1),
    start: start === null ? "" : fmtMinutes(start),
    end: start === null ? "" : fmtMinutes(end),
    speciesId: null,
    rows: [],
    rule: { freq: "NONE", interval: 1, byday: [], count: null, until: null },
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

/** Minutes as an ISO 8601 duration -- `PT1H30M`, `PT45M`. The same
 *  notation core/interval.py::format_duration writes, because a value
 *  composed here and one composed there have to be the same string. */
function isoDuration(minutes) {
  const total = Math.max(1, Math.round(minutes));
  const hours = Math.floor(total / 60);
  const rest = total % 60;
  if (hours && rest) return `PT${hours}H${rest}M`;
  if (hours) return `PT${hours}H`;
  return `PT${rest}M`;
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

  $("draft-anchor").textContent = draft.allDay && draft.days > 1
    ? `date · ${draft.dateKey} · ${draft.days} days`
    : `date · ${draft.dateKey}`;
  $("draft-date").value = draft.dateKey;
  $("draft-start").value = draft.start;
  $("draft-end").value = draft.end;
  $("draft-allday").classList.toggle("on", draft.allDay);
  $("draft-times").classList.toggle("hidden", draft.allDay);
  $("draft-duration").textContent = draftDuration();
  drawRepeat();

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

/** The repeat row's frequency pills plus, once one is chosen, the
 *  interval/weekday/ends controls beneath it. Ported from Monarch's
 *  interval picker, which used to own this: Monastery is the app that
 *  actually reads a `recurring` mote back (core/calendar.py, via
 *  dateutil), so it's the app that should author the rule too. Every
 *  control just mutates draft.rule and redraws -- same pattern as the
 *  species chips above. */
function drawRepeat() {
  const pills = clear($("draft-freq-pills"));
  for (const f of FREQ_OPTS) {
    const pill = button("freq-pill" + (draft.rule.freq === f.id ? " on" : ""), f.label);
    pill.dataset.freq = f.id;
    pill.onclick = () => {
      const byday = f.id === "WEEKLY" && !draft.rule.byday.length
        ? [DAY_CODES[(new Date(`${draft.dateKey}T00:00`).getDay() + 6) % 7]]
        : draft.rule.byday;
      draft.rule = { ...draft.rule, freq: f.id, byday };
      drawDraft();
    };
    pills.append(pill);
  }

  const detail = clear($("draft-repeat-detail"));
  if (draft.rule.freq === "NONE") return;

  const intervalRow = el("div", "repeat-stepper-row");
  intervalRow.append(el("span", "draft-label", "Every"));
  const intervalStepper = el("div", "repeat-stepper");
  const intervalDec = button(null, "−");
  intervalDec.onclick = () => { draft.rule.interval = Math.max(1, draft.rule.interval - 1); drawDraft(); };
  const intervalInc = button(null, "+");
  intervalInc.onclick = () => { draft.rule.interval = Math.min(30, draft.rule.interval + 1); drawDraft(); };
  intervalStepper.append(intervalDec, el("span", null, draft.rule.interval), intervalInc);
  intervalRow.append(intervalStepper);
  intervalRow.append(el("span", "repeat-unit", FREQ_UNIT[draft.rule.freq] + (draft.rule.interval > 1 ? "s" : "")));
  detail.append(intervalRow);

  if (draft.rule.freq === "WEEKLY") {
    const weekdayRow = el("div", "repeat-weekday-row");
    weekdayRow.append(el("span", "draft-label", "On"));
    const weekdayPills = el("div", "repeat-weekday-pills");
    for (const c of DAY_CODES) {
      const on = draft.rule.byday.includes(c);
      const p = button("weekday-pill" + (on ? " on" : ""), DAY_MIN[c]);
      p.dataset.day = c;
      p.onclick = () => {
        draft.rule.byday = on
          ? draft.rule.byday.filter((x) => x !== c)
          : DAY_CODES.filter((x) => draft.rule.byday.includes(x) || x === c);
        drawDraft();
      };
      weekdayPills.append(p);
    }
    weekdayRow.append(weekdayPills);
    detail.append(weekdayRow);
  }

  // "Ends" is a 3-way choice -- never, after N times, or on a date --
  // and RRULE only ever carries one of COUNT/UNTIL at a time, so
  // picking a mode clears whichever of the two isn't its own.
  const endMode = draft.rule.until ? "UNTIL" : draft.rule.count ? "COUNT" : "NEVER";
  const endsRow = el("div", "repeat-stepper-row");
  endsRow.append(el("span", "draft-label", "Ends"));
  const endsPills = el("div", "repeat-ends-pills");
  for (const [id, label] of [["NEVER", "Never"], ["COUNT", "After"], ["UNTIL", "On date"]]) {
    const p = button("freq-pill" + (endMode === id ? " on" : ""), label);
    p.dataset.end = id;
    p.onclick = () => {
      if (id === "NEVER") { draft.rule.count = null; draft.rule.until = null; }
      else if (id === "COUNT") { draft.rule.count = draft.rule.count || 5; draft.rule.until = null; }
      else { draft.rule.until = draft.rule.until || draft.dateKey; draft.rule.count = null; }
      drawDraft();
    };
    endsPills.append(p);
  }
  endsRow.append(endsPills);
  detail.append(endsRow);

  if (endMode === "COUNT") {
    const countRow = el("div", "repeat-stepper-row");
    countRow.append(el("span", "draft-label", ""));
    const countStepper = el("div", "repeat-stepper");
    const countDec = button(null, "−");
    countDec.onclick = () => { draft.rule.count = Math.max(1, draft.rule.count - 1); drawDraft(); };
    const countInc = button(null, "+");
    countInc.onclick = () => { draft.rule.count = draft.rule.count + 1; drawDraft(); };
    countStepper.append(countDec, el("span", null, `${draft.rule.count}×`), countInc);
    countRow.append(countStepper);
    detail.append(countRow);
  } else if (endMode === "UNTIL") {
    const untilRow = el("div", "repeat-stepper-row");
    untilRow.append(el("span", "draft-label", ""));
    const untilInput = el("input");
    untilInput.type = "date";
    untilInput.value = draft.rule.until;
    untilInput.onchange = () => {
      if (!untilInput.value) return;
      draft.rule.until = untilInput.value;
      draft.rule.count = null;
      drawDraft();
    };
    untilRow.append(untilInput);
    detail.append(untilRow);
  }
}

/** draft.rule as RRULE lines, the same grammar core/calendar.py's
 *  dateutil expansion reads (core/interval.py owns it). Mirrors the
 *  subset Monarch's old picker composed, minus the raw-text fallback --
 *  a fresh draft here is always this shape, never an existing rule that
 *  might use something wider. */
function composeRepeatLines(rule) {
  if (rule.freq === "NONE") return [];
  const bits = [`FREQ=${rule.freq}`];
  if (rule.interval > 1) bits.push(`INTERVAL=${rule.interval}`);
  if (rule.freq === "WEEKLY" && rule.byday.length) bits.push(`BYDAY=${rule.byday.join(",")}`);
  if (rule.until) bits.push(`UNTIL=${rule.until.replace(/-/g, "")}`);
  else if (rule.count) bits.push(`COUNT=${rule.count}`);
  return [`RRULE:${bits.join(";")}`];
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

  // One property, carrying its own span: an ISO 8601 interval
  // (core/interval.py). There is no separate `duration` to write, and so
  // no way for the two halves to end up disagreeing.
  if (draft.allDay) {
    properties.date = draft.days > 1 ? `${dateKey}/P${draft.days}D` : dateKey;
  } else {
    const start = parseHm($("draft-start").value);
    const end = parseHm($("draft-end").value);
    if (start === null) return null;
    const startText = `${dateKey}T${fmtMinutes(start)}:00`;
    properties.date = (end !== null && end > start)
      ? `${startText}/${isoDuration(end - start)}`
      : startText;
  }

  const repeatLines = composeRepeatLines(draft.rule);
  if (repeatLines.length) properties.date = [properties.date, ...repeatLines].join("\n");

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
// Nothing in MonOSS pushes: Monarch, the CLI and the MCP server
// all write the same file and none of them announces it (see
// docs/architecture.md). So the calendar asks again, on a timer, and
// only while the window is focused -- a background window polling a
// SQLite file every few seconds is work nobody is looking at.

function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => {
    // A poll must never fight the person typing -- reopening the draft's
    // inputs underneath them would lose the keystroke in flight -- nor
    // the person dragging, whose rectangle a redraw would replace
    // mid-gesture, nor a question that is still on screen.
    if (draft || drag || chooser) return;
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

  $("refresh-btn").onclick = async () => {
    const btn = $("refresh-btn");
    btn.disabled = true;
    btn.classList.add("spinning");
    try {
      const result = await call(api.refresh());
      await refresh();
      toast(result.synced ? "Synced and refreshed." : "Refreshed.");
    } finally {
      btn.disabled = false;
      btn.classList.remove("spinning");
    }
  };

  $("panel-close").onclick = closePanel;
  $("panel-delete").onclick = (event) => {
    lastPointer = { x: event.clientX, y: event.clientY };
    const occurrence = selectedOccurrence();
    if (occurrence) deleteOccurrence(occurrence);
  };

  $("chooser-one").onclick = () => answerChooser("occurrence");
  $("chooser-all").onclick = () => answerChooser("series");
  $("chooser-cancel").onclick = () => answerChooser(null);

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
    // A question is answered by answering it, not by clicking past it --
    // except by the click that asked it (see askScope).
    if (chooser && !chooserFresh && !event.target.closest("#chooser")) answerChooser(null);
    if (draft && !clickedInDraft && !event.target.closest(".day-col")
        && !event.target.closest(".month-cell") && !event.target.closest(".allday-cell")) {
      closeDraft();
      draw();
    }
    clickedInDraft = false;
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeGardenMenu();
      if (chooser) answerChooser(null);
      else if (draft) { closeDraft(); draw(); }
      else if (selectedId) closePanel();
      return;
    }
    // Not while typing into the draft -- these are navigation, and a
    // name with a "t" in it is not a request to jump to today.
    if (event.target.tagName === "INPUT") return;
    if (event.key === "Delete" || event.key === "Backspace") {
      const occurrence = selectedOccurrence();
      if (occurrence) { event.preventDefault(); deleteOccurrence(occurrence); }
      return;
    }
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
