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
let draft = null;            // {dateKey, start, end, allDay, days, speciesId, rows, rule, left, top}
let railW = 214;             // the bindings rail's width, in px -- 0 means pulled shut
let railResize = null;       // { sx, orig } while the rail's edge is being dragged
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
const STORE_KEY = "monastery.bindings";
const HOUR_PX = 48;          // must match --hour in style.css
const DRAG_PX = 4;           // how far a press moves before it counts as a drag
const EDGE_PX = 6;           // how deep an event's resize edges reach
const SNAP_MIN = 15;         // what a dragged time rounds to
const MIN_MINUTES = 15;      // the shortest event a drag can leave behind
const EVENT_MIN_PX = 6;      // shortest an event's highlight is drawn, regardless of duration
const INLINE_LABEL_MIN_PX = 16; // shortest an event can be and still hold its own label inline
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"];
const MON_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const UNKNOWN_COLOR = "oklch(0.62 0 0)";
// Monday-first, which is how this window draws a week. The shared repeat
// editor works in RRULE's own Sunday-first order; only this one lookup
// needs the difference, so it stays here rather than becoming an option.
const DAY_CODES = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"];

const $ = (id) => document.getElementById(id);

// ---- colour -------------------------------------------------------
//
// Byte-for-byte the hash monarch/frontend/graph.js uses, the same copy Mora
// carries. Windows disagreeing about what colour "Meeting" is would be
// worse than any of them being arbitrary.

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

// ---- the species palette ----
//
// The palette monarch/frontend/graph.js defines -- the same constants
// and the same two functions, carried byte for byte, so a species is the
// same colour in every window. Only the slot map differs: the graph
// hangs its off the canvas object, which has a colour cache to
// invalidate; here it is one module-level Map.
//
// Hashing a hue straight out of the species id gave every species an
// independent random hue, which at this lightness and chroma meant a
// garden's tenth species routinely looked like its third -- 360 hues read
// as about five colours. Instead each species claims one entry of a fixed
// palette of deliberately separated colours: hues walk the wheel by the
// golden angle so consecutive entries land far apart, and each lap round
// the wheel changes lightness and chroma so the second time past a hue is
// still visibly a different colour.
//
// Which entry a species claims depends on which other species exist, so
// the universe has to be the *garden's* species list (api.species_ids,
// api_common.py) rather than the ones this window happens to be showing.
// A window claiming from what it can see would give a species one colour
// here and another in Monarch under a hop limit.

const SPECIES_HUE_STEP = 137.508;    // golden angle
const SPECIES_LAP = 12;              // entries before the tone changes
const SPECIES_TONES = [
  { l: 0.72, c: 0.13 },
  { l: 0.85, c: 0.09 },
  { l: 0.60, c: 0.16 },
  { l: 0.79, c: 0.20 },
];
const SPECIES_PALETTE_SIZE = SPECIES_LAP * SPECIES_TONES.length;

let speciesSlots = new Map();        // species id -> palette entry

function paletteEntry(slot) {
  const index = ((slot % SPECIES_PALETTE_SIZE) + SPECIES_PALETTE_SIZE) % SPECIES_PALETTE_SIZE;
  return {
    h: (index * SPECIES_HUE_STEP + 25) % 360,
    ...SPECIES_TONES[Math.floor(index / SPECIES_LAP)],
  };
}

function paletteColor(slot, emphatic) {
  const entry = paletteEntry(slot);
  // The species mote itself is the brighter variant of its instances'
  // colour, as it was before -- same hue, one step more presence.
  const l = emphatic ? Math.min(0.9, entry.l + 0.07) : entry.l;
  const c = emphatic ? entry.c + 0.02 : entry.c;
  return `oklch(${l.toFixed(3)} ${c.toFixed(3)} ${entry.h.toFixed(1)})`;
}

/** species id -> palette entry. Each species prefers the entry its id
 *  hashes to -- so a colour is stable across sessions and unaffected by
 *  most later additions -- and probes forward from there when that entry
 *  is already claimed, which is what stops two species sharing a colour
 *  until the palette is genuinely full. Sorted ids keep the claiming
 *  order independent of the order they arrived in. */
function claimSlots(ids) {
  const slots = new Map();
  const taken = new Set();
  for (const id of [...ids].sort()) {
    let slot = hashString(id) % SPECIES_PALETTE_SIZE;
    for (let step = 0; step < SPECIES_PALETTE_SIZE && taken.has(slot); step++) {
      slot = (slot + 1) % SPECIES_PALETTE_SIZE;
    }
    taken.add(slot);
    slots.set(id, slot);
  }
  return slots;
}

/** A species created since the last refresh has claimed nothing yet; it
 *  falls back to its unprobed entry, which is the one it will almost
 *  always end up claiming anyway. */
function slotOf(speciesId) {
  return speciesSlots.has(speciesId)
    ? speciesSlots.get(speciesId)
    : hashString(speciesId) % SPECIES_PALETTE_SIZE;
}

/** The palette universe travels with the answer this window already
 *  fetches (api_common.py::species_ids), so a species claims the same
 *  entry here as in every other window and the bridge is asked nothing
 *  extra. Asking for it separately on each refresh was the first
 *  attempt: it doubled every poll's traffic for an answer that changes
 *  only when somebody makes a new species, and the added round trip was
 *  enough to make a freshly created mote miss the redraw that should
 *  have shown it. */
function adoptPalette(ids) {
  if (ids) speciesSlots = claimSlots(ids);
}

/** The palette entry behind a mote's colour, for the places that need to
 *  build a translucent variant of it rather than the colour itself. */
function paletteOf(entry) {
  if (!entry) return null;
  if ((entry.kinds || []).includes("species")) return paletteEntry(slotOf(entry.id || ""));
  return entry.speciesId ? paletteEntry(slotOf(entry.speciesId)) : null;
}

/** The event rectangle's fill and edge: the mote's own colour, dimmed.
 *  A typeless mote gets grey rather than an arbitrary colour -- "no
 *  species" is worth seeing as itself. */
function eventTint(entry, selected) {
  const tone = paletteOf(entry);
  if (tone === null) {
    return {
      bg: selected ? "oklch(0.62 0 0 / 0.3)" : "oklch(0.62 0 0 / 0.16)",
      border: selected ? UNKNOWN_COLOR : "oklch(0.62 0 0 / 0.32)",
      color: UNKNOWN_COLOR,
    };
  }
  const face = `${tone.l.toFixed(3)} ${tone.c.toFixed(3)} ${tone.h.toFixed(1)}`;
  return {
    bg: `oklch(${face} / ${selected ? 0.3 : 0.16})`,
    border: `oklch(${face} / ${selected ? 0.85 : 0.32})`,
    color: `oklch(${face})`,
  };
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

// ---- the bindings preference -----------------------------------------
//
// Which properties count as dates is the person's answer, not the
// garden's, so it lives here rather than in a mote -- remembered in
// localStorage, and sent back on every call so a poll can never quietly
// re-guess one you switched off.

function loadBindings() {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (error) {
    return null;
  }
}

function saveBindings() {
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(bindings));
  } catch (error) {
    /* a window that cannot remember still works; it just re-guesses. */
  }
}

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
  adoptPalette(answer.speciesIds);
  payload = answer;
  bindings = answer.bindings.filter((b) => b.active).map((b) => b.key);
  saveBindings();
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
    dot.style.background = paletteColor(slotOf(s.id), false);
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
 *  last event has already ended, else a new one. Lanes are assigned per
 *  cluster of mutually-touching events, so a stack of two doesn't narrow
 *  unrelated events later in the same day. Local because it is a fact
 *  about the rectangles, not about the motes. */
function lanes(list) {
  const sorted = list.slice().sort((a, b) => a._start - b._start || b._end - a._end);

  const clusters = [];
  let cluster = [];
  let clusterEnd = -Infinity;
  sorted.forEach((o) => {
    if (cluster.length && o._start >= clusterEnd) {
      clusters.push(cluster);
      cluster = [];
      clusterEnd = -Infinity;
    }
    cluster.push(o);
    clusterEnd = Math.max(clusterEnd, o._end);
  });
  if (cluster.length) clusters.push(cluster);

  clusters.forEach((group) => {
    const laneEnds = [];
    group.forEach((o) => {
      let lane = laneEnds.findIndex((end) => end <= o._start);
      if (lane === -1) { lane = laneEnds.length; laneEnds.push(0); }
      laneEnds[lane] = o._end;
      o._lane = lane;
    });
    const count = Math.max(1, laneEnds.length);
    group.forEach((o) => { o._laneCount = count; });
  });

  return sorted;
}

function eventFor(o) {
  const selected = o.id === selectedId;
  const tint = eventTint(o, selected);
  const top = (minutesInto(o._start) / 60) * HOUR_PX;
  const minutes = Math.max(15, (o._end - o._start) / 60000);
  const height = Math.max(EVENT_MIN_PX, (minutes / 60) * HOUR_PX - 2);
  const width = 100 / o._laneCount;
  const compact = height < 42;
  // Tall enough to hold its own label inline, the way an event always
  // has; too short, and the label floats off the highlight instead.
  const inline = height >= INLINE_LABEL_MIN_PX;
  // A block near the top of the column has no room for a floating label
  // above it, so its label drops below instead.
  const labelBelow = top < 26;

  const node = button("event");
  if (inline) node.classList.add("inline");
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
  const name = el("div", "event-name", o.name);
  if (inline && !compact) name.style.webkitLineClamp = String(Math.max(1, Math.floor((height - 16) / 15)));

  if (inline) {
    node.append(time, name);
  } else {
    // Too small to hold text of its own -- the label floats off the
    // highlight and only shows up when someone's actually looking.
    const label = el("div", `event-label${labelBelow ? " below" : ""}`);
    label.append(time, name);
    node.append(label);
  }

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
  // A placement is both a mote and an occurrence of one, so the menu
  // offers both -- the inspector edits the mote, and "Remove this one"
  // is about the rectangle that was actually clicked.
  node.oncontextmenu = (event) => {
    event.preventDefault();
    event.stopPropagation();
    ContextMenu.open(event.clientX, event.clientY, [
      ...ContextMenu.inspectItems(o.id, node),
      "-",
      { label: "Remove this occurrence", danger: true, run: () => deleteOccurrence(o) },
    ]);
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

// ---- bindings rail width --------------------------------------------
//
// Pulled open or shut from its own right edge, the way Motion's rail
// works, rather than through a button elsewhere in the topbar.

function railWidthKey() { return "monastery.railW"; }

function loadRailWidth() {
  try {
    const raw = window.localStorage.getItem(railWidthKey());
    if (raw !== null) railW = Math.max(0, parseInt(raw, 10) || 0);
  } catch (error) {
    // A window that cannot remember its rail width just uses the default.
  }
}

function saveRailWidth() {
  try {
    window.localStorage.setItem(railWidthKey(), String(railW));
  } catch (error) {
    // Not fatal -- see loadRailWidth.
  }
}

function applyRailWidth() {
  $("rail").style.width = `${railW}px`;
}

// Below half the usable minimum, a resize collapses the rail shut
// rather than settling on some odd sliver width -- pulling back out
// from the edge (where the handle still sits) reopens it at the same
// minimum, matching Motion's rail.
function snapRailWidth(value) {
  const min = 180, max = 420;
  if (value < min / 2) return 0;
  return Math.max(min, Math.min(max, value));
}

function onRailResizeDown(event) {
  event.preventDefault();
  $("rail-resize").classList.add("active");
  railResize = { sx: event.clientX, orig: railW };
  window.addEventListener("pointermove", onRailResizeMove);
  window.addEventListener("pointerup", onRailResizeUp);
}

function onRailResizeMove(event) {
  if (!railResize) return;
  railW = snapRailWidth(railResize.orig + (event.clientX - railResize.sx));
  applyRailWidth();
}

function onRailResizeUp() {
  window.removeEventListener("pointermove", onRailResizeMove);
  window.removeEventListener("pointerup", onRailResizeUp);
  $("rail-resize").classList.remove("active");
  railResize = null;
  saveRailWidth();
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
    `${Math.max(EVENT_MIN_PX, Math.round(((end - start) / 3600000) * HOUR_PX) - 2)}px`;
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
    toast(`${answer.name} split off from its series.`);
  }
  await refresh();
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
    // Delete key -- happens inside a click that is still on its way
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

/** The placement the keyboard and the × button act on: the one that was
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
  if (answer.scope === "series" && selectedId === o.id) deselect();
  await refresh();
  toast(
    answer.scope === "occurrence"
      ? `One occurrence of ${answer.name} removed.`
      : `${answer.name} deleted.`,
    false,
    { label: "Undo", run: () => Undo.undo() },
  );
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

// ---- selection ----

async function select(moteId, occurrenceKey, options = {}) {
  selectedId = moteId;
  // Which *placement* was clicked, not just which mote: a weekly standup
  // draws five rectangles, and deleting "this one" has to know which one
  // you meant.
  selectedKey = occurrenceKey || null;
  closeDraft();
  draw();
  await Inspector.open(moteId, { anchor: options.anchor, newTab: !!options.newTab });
}

function deselect() {
  selectedId = null;
  selectedKey = null;
  Inspector.close();
  draw();
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

  draft = {
    dateKey,
    allDay: start === null,
    days: Math.max(1, days || 1),
    start: start === null ? "" : fmtMinutes(start),
    end: start === null ? "" : fmtMinutes(end),
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
}

/** The repeat controls, drawn by the shared interval widget
 *  (frontend_shared/interval.js::repeatEditor).
 *
 *  This editor was written here, and for a while Monastery was the only
 *  window that had one -- the picker in every other window had been
 *  reduced to showing an RRULE as raw text, on the reasoning that
 *  Monastery is the app that actually reads a `recurring` mote back
 *  (core/calendar.py, via dateutil) and so should be the app that
 *  authors the rule. That held right up until the inspector became one
 *  surface in six windows, at which point "edit this date properly"
 *  had to work wherever you were standing. The editor moved into the
 *  shared widget; this is now one of its two callers.
 *
 *  draft.rule is the same shape either way, so composeRepeatLines below
 *  is unchanged. */
function drawRepeat() {
  const detail = clear($("draft-repeat-detail"));
  clear($("draft-freq-pills"));

  if (draft.rule.freq === "NONE") {
    const add = button("repeat-add", "+ Add a repeat rule");
    add.onclick = () => {
      const day = DAY_CODES[(new Date(`${draft.dateKey}T00:00`).getDay() + 6) % 7];
      draft.rule = { ...draft.rule, freq: "WEEKLY", interval: 1, byday: [day] };
      drawDraft();
    };
    detail.append(add);
    return;
  }

  detail.append(IntervalPicker.repeatEditor(draft.rule, drawDraft));

  const stop = button("repeat-add", "Remove repeat");
  stop.onclick = () => {
    draft.rule = { ...draft.rule, freq: "NONE", byday: [], count: null, until: null };
    drawDraft();
  };
  detail.append(stop);
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

  return properties;
}

async function createFromDraft() {
  if (!draft) return;
  const properties = draftProperties();
  if (properties === null) { toast("That start time isn't readable — try 09:15.", true); return; }
  if (!properties.date) { toast("A new mote needs a date.", true); return; }

  const created = await call(api.create_mote(properties), "Could not create that mote.");
  if (created === null) return;
  closeDraft();
  await refresh();
  selectedId = created.id;
  draw();
  await Inspector.open(created.id);
  toast(`${created.name} created — placed by date.`);
}

// ---- garden menu ----

/** The "Manage gardens…" footer every one of the six garden menus
 *  carries, byte for byte the same in each -- the shared Gardens window
 *  is only standard if the way in is. */
function gardenMenuManageItem(close) {
  const sep = document.createElement("div");
  sep.className = "menu-sep";
  const item = document.createElement("button");
  item.type = "button";
  item.className = "menu-item";
  item.textContent = "Manage gardens…";
  item.title = "Create, rename, import, export and sync gardens";
  item.onclick = () => { close(); api.open_gardens(); };
  const frag = document.createDocumentFragment();
  frag.append(sep, item);
  return frag;
}

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
      deselect();
      closeDraft();
      await refresh();
    };
    menu.append(item);
  });

  // Every window ends its garden menu with the same two lines: the
  // gardens you can switch to, then the one window where gardens
  // themselves are made, renamed, imported, exported and synced
  // (gardens_app/). Switching is per window; managing is not, which is
  // why this opens a window instead of a modal.
  menu.append(gardenMenuManageItem(closeGardenMenu));
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
  $("rail-resize").onpointerdown = onRailResizeDown;
  $("garden-btn").onclick = openGardenMenu;
  $("switch-app-btn").onclick = openLauncher;
  $("launcher-overlay").addEventListener("mousedown", (event) => {
    if (event.target === $("launcher-overlay")) closeLauncher();
  });
  $("launcher-input").addEventListener("input", (event) => renderLauncherResults(event.target.value));
  $("launcher-input").addEventListener("keydown", (event) => {
    const list = launcherFiltered($("launcher-input").value);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (launcherHighlight + LAUNCHER_COLUMNS < list.length) launcherHighlight += LAUNCHER_COLUMNS;
      updateLauncherHighlight();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (launcherHighlight - LAUNCHER_COLUMNS >= 0) launcherHighlight -= LAUNCHER_COLUMNS;
      updateLauncherHighlight();
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      if (launcherHighlight % LAUNCHER_COLUMNS < LAUNCHER_COLUMNS - 1 && launcherHighlight + 1 < list.length) launcherHighlight++;
      updateLauncherHighlight();
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      if (launcherHighlight % LAUNCHER_COLUMNS > 0) launcherHighlight--;
      updateLauncherHighlight();
    } else if (event.key === "Enter" && list[launcherHighlight]) {
      launchApp(list[launcherHighlight].id);
    }
  });

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

  $("chooser-one").onclick = () => answerChooser("occurrence");
  $("chooser-all").onclick = () => answerChooser("series");
  $("chooser-cancel").onclick = () => answerChooser(null);

  $("draft-allday").onclick = () => {
    draft.allDay = !draft.allDay;
    drawDraft();
  };
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
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "j") {
      event.preventDefault();
      openLauncher();
      return;
    }
    if ((event.metaKey || event.ctrlKey) && /^[1-5]$/.test(event.key)) {
      const app = launcherChoices()[Number(event.key) - 1];
      if (app) {
        event.preventDefault();
        launchApp(app.id);
      }
      return;
    }
    if (event.key === "Escape") {
      closeGardenMenu();
      closeLauncher();
      if (chooser) answerChooser(null);
      else if (draft) { closeDraft(); draw(); }
      else if (selectedId) deselect();
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

// ---- app switcher (⌘J) ----
//
// Every sibling app but this one, in a fixed order -- so "Monolith is
// ⌘3" holds regardless of what's typed into the filter box. See
// api_common.py::APPS for the id each maps to on the Python side.
const LAUNCHER_APPS = [
  {
    id: "monarch", name: "Monarch", kind: "graph",
    desc: "Motes and relationships in a live force field.",
    icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
      <circle cx="20" cy="20" r="12" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></circle>
      <ellipse cx="20" cy="20" rx="12" ry="4.5" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6" transform="rotate(-28 20 20)"></ellipse>
      <circle cx="20" cy="20" r="4" fill="var(--accent)"></circle>
    </svg>`,
  },
  {
    id: "mora", name: "Mora", kind: "notes",
    desc: "Write and link long-form .modoc notes.",
    icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
      <rect x="9" y="6" width="22" height="28" rx="3" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></rect>
      <rect x="14" y="14" width="12" height="1.8" rx="0.9" fill="var(--fg)" opacity="0.7"></rect>
      <rect x="14" y="20" width="12" height="1.8" rx="0.9" fill="var(--fg)" opacity="0.7"></rect>
      <rect x="14" y="26" width="7" height="1.8" rx="0.9" fill="var(--accent)"></rect>
    </svg>`,
  },
  {
    id: "monastery", name: "Monastery", kind: "calendar",
    desc: "See and schedule a garden's dated motes.",
    icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
      <rect x="7" y="9" width="26" height="24" rx="3" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></rect>
      <rect x="7" y="15" width="26" height="1.6" fill="var(--fg)" opacity="0.5"></rect>
      <rect x="12" y="20" width="5" height="5" rx="1" fill="var(--fg)" opacity="0.55"></rect>
      <rect x="21" y="20" width="5" height="5" rx="1" fill="var(--accent)"></rect>
    </svg>`,
  },
  {
    id: "motion", name: "Motion", kind: "simulation",
    desc: "Run the garden's rules and scrub the result tick by tick.",
    icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
      <circle cx="20" cy="20" r="10.5" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.55"></circle>
      <ellipse cx="20" cy="20" rx="10.5" ry="4" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.55" transform="rotate(-28 20 20)"></ellipse>
      <circle cx="20" cy="20" r="3.2" fill="var(--accent)"></circle>
      <circle cx="29.4" cy="15.3" r="2" fill="var(--accent)"></circle>
    </svg>`,
  },
];
const THIS_APP = "monastery";

let launcherHighlight = 0;
const LAUNCHER_COLUMNS = 2; // matches #launcher-results grid-template-columns

function launcherChoices() {
  return LAUNCHER_APPS.filter((a) => a.id !== THIS_APP);
}

function launcherFiltered(query) {
  const q = query.trim().toLowerCase();
  const choices = launcherChoices();
  if (!q) return choices;
  return choices.filter((a) => (a.name + " " + a.kind + " " + a.desc).toLowerCase().includes(q));
}

function renderLauncherResults(query) {
  const choices = launcherChoices();
  const list = launcherFiltered(query);
  launcherHighlight = Math.min(launcherHighlight, Math.max(list.length - 1, 0));
  const container = $("launcher-results");
  container.innerHTML = "";
  list.forEach((app, i) => {
    const card = document.createElement("a");
    card.href = "#";
    card.className = "launcher-card" + (i === launcherHighlight ? " highlighted" : "");
    card.innerHTML =
      `<div class="launcher-icon">${app.icon}</div>` +
      `<div class="launcher-text">` +
      `<div class="launcher-name-row"><span class="launcher-name">${app.name}</span><span class="launcher-kind">${app.kind}</span></div>` +
      `<div class="launcher-desc">${app.desc}</div>` +
      `</div>` +
      `<span class="launcher-key">⌘${choices.indexOf(app) + 1}</span>`;
    card.addEventListener("mouseenter", () => { launcherHighlight = i; updateLauncherHighlight(); });
    card.addEventListener("mousedown", (e) => { e.preventDefault(); launchApp(app.id); });
    container.appendChild(card);
  });
  const empty = $("launcher-empty");
  if (list.length === 0) {
    empty.textContent = `no app matches "${query}"`;
    empty.classList.remove("hidden");
  } else {
    empty.classList.add("hidden");
  }
}

function updateLauncherHighlight() {
  document.querySelectorAll("#launcher-results .launcher-card").forEach((card, i) => {
    card.classList.toggle("highlighted", i === launcherHighlight);
  });
}

function openLauncher() {
  $("launcher-overlay").classList.remove("hidden");
  $("launcher-input").value = "";
  $("launcher-garden").textContent = "garden · " + $("garden-name").textContent;
  launcherHighlight = 0;
  renderLauncherResults("");
  $("launcher-input").focus();
}

function closeLauncher() { $("launcher-overlay").classList.add("hidden"); }

async function launchApp(appId) {
  closeLauncher();
  const app = LAUNCHER_APPS.find((a) => a.id === appId);
  await call(api.open_app(appId));
  toast(`Opening ${app.name}…`);
}

async function init() {
  api = window.pywebview.api;
  Inspector.install({ api, onChanged: refresh, onOpenMora: () => toast("Opening Mora…") });
  CommandBar.install({
    api,
    onChanged: refresh,
    onNavigate: (moteId) => Inspector.open(moteId),
  });
  Undo.install({ api, toast, onChanged: refresh });
  anchor = new Date();
  bindings = loadBindings();
  loadRailWidth();
  applyRailWidth();
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
