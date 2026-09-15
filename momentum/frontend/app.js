// momentum/frontend/app.js
//
// Wiring between momentum/api.py and the board. Everything about what a
// garden's tasks *are* -- which property means status, which means an
// estimate, where a task sits in time, which day has room -- comes back
// from Python already decided (core/task_candidates.py and
// core/calendar.py read, momentum/payload.py shapes). This file draws
// that answer, takes edits, and owns exactly two things Python has no
// opinion about: which tab is showing, and which key the person picked
// for each role.
//
// That second one is remembered in localStorage, the same way Monastery
// remembers which bindings you switched off, and sent back on every
// call -- so a poll can never quietly re-guess a key you corrected.

let api;
let tab = "board";           // "board" | "days" | "focus"
let keyChoice = null;             // {status, priority, estimate, bindings} once known
let data = null;             // the last answer for the current tab
let anchor = null;           // Date; any day of the week shown in Days
let selectedId = null;
let panel = null;            // the last focus() answer
let activeId = null;         // which mote the timer is running against
let railOpen = true;
let toastTimer = null;
let pollTimer = null;
let tickTimer = null;
let refreshSeq = 0;
let dragState = null;        // {id, node, x0, y0, moved} while a card is pressed
let justDragged = false;     // true for one tick after a drop, so the trailing click is a no-op
let dropZones = [];          // {node, onDrop} for wherever a dragged card can land, this render
let addingColumn = null;     // which column's inline "+ add mote" input is open
let sessionMinutes = 25;
let stopwatch = false;       // true: count up with no target, until stopped
let workHours = { start: "09:00", end: "17:00" }; // last answer from the "days" tab; edited from the rail
let elapsed = 0;             // seconds since the current session began
let running = false;
let startedAt = null;        // when the current session began, for the log

const POLL_MS = 4000;
const DRAG_PX = 4;           // how far a press moves before it counts as a drag, not a click
const STORE_KEY = "momentum.keys";
const TIMER_STORE_KEY = "momentum.timer";
const DOW = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MON_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                   "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const UNKNOWN_COLOR = "oklch(0.62 0 0)";
const DONE_WORDS = new Set(["done", "complete", "completed", "shipped", "harvested"]);

const $ = (id) => document.getElementById(id);

// ---- colour -------------------------------------------------------
//
// Byte-for-byte the hash monarch/frontend/graph.js uses, the same copy
// Monastery and Mora carry. Four windows disagreeing about what colour
// "Meeting" is would be worse than any of them being arbitrary.

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

/** A mote's colour: its species' palette entry, brighter if it is itself
 *  a species, grey if it has no type at all. */
function colorOf(entry) {
  if (!entry) return UNKNOWN_COLOR;
  if ((entry.kinds || []).includes("species")) return paletteColor(slotOf(entry.id || ""), true);
  if (!entry.speciesId) return UNKNOWN_COLOR;
  return paletteColor(slotOf(entry.speciesId), false);
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
const parseKey = (k) => {
  const p = String(k).split("-");
  return new Date(+p[0], +p[1] - 1, +p[2]);
};
const isoLocal = (d) => `${keyOf(d)}T${pad(d.getHours())}:${pad(d.getMinutes())}:00`;
const todayKey = () => keyOf(new Date());

function clock(seconds) {
  const s = Math.max(0, Math.round(seconds));
  return `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
}

/** Minutes as a board reads them: `45m`, `4h`, `1.5h`. The same shape
 *  core/task_candidates.py::format_estimate produces, because a total
 *  computed here and one computed there must not look different. */
function humanMinutes(minutes) {
  if (!minutes) return "";
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  return `${Number(hours.toFixed(1))}h`;
}

// ---- DOM helpers ---------------------------------------------------

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
 *  silently half-drawn board. */
async function call(promise, failure) {
  try {
    return await promise;
  } catch (error) {
    toast(failure || String(error && error.message ? error.message : error), true);
    return null;
  }
}

// ---- the key preference --------------------------------------------
//
// Which property means status/priority/estimate is the person's answer,
// not the garden's, so it lives here rather than in a mote. `false` is
// how "switched off" is sent -- distinct from `null`, which means "you
// decide" and lets Python guess again (momentum/api.py::_resolve_keys).

function loadKeys() {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch (error) {
    return null;
  }
}

function saveKeys() {
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(keyChoice));
  } catch (error) {
    /* a window that cannot remember still works; it just re-guesses. */
  }
}

// ---- the timer preference -------------------------------------------
//
// How long a focus session runs, and whether it runs at all -- stopwatch
// mode counts up with no target. Same shape as the key preference: it's
// this window's own choice, so it's remembered in localStorage rather
// than sent to Python.

function loadTimerPrefs() {
  try {
    const raw = window.localStorage.getItem(TIMER_STORE_KEY);
    if (!raw) return;
    const saved = JSON.parse(raw);
    if (Number.isFinite(saved.minutes) && saved.minutes > 0) sessionMinutes = saved.minutes;
    stopwatch = !!saved.stopwatch;
  } catch (error) {
    /* fall back to the defaults declared above. */
  }
}

function saveTimerPrefs() {
  try {
    window.localStorage.setItem(TIMER_STORE_KEY, JSON.stringify({ minutes: sessionMinutes, stopwatch }));
  } catch (error) {
    /* a window that cannot remember still works; it just uses defaults. */
  }
}

/** The arguments every view call takes. Null on the very first call of
 *  a session, so Python picks defaults; the person's answer after that. */
function keyArgs() {
  if (!keyChoice) return [null, null, null, null];
  return [
    keyChoice.status === null ? false : keyChoice.status,
    keyChoice.priority === null ? false : keyChoice.priority,
    keyChoice.estimate === null ? false : keyChoice.estimate,
    keyChoice.bindings,
  ];
}

function adoptKeys(answer) {
  if (!answer || !answer.keys) return;
  keyChoice = answer.keys;
  saveKeys();
}

function setKey(role, value) {
  if (!keyChoice) return;
  keyChoice[role] = keyChoice[role] === value ? null : value;
  saveKeys();
  refresh();
}

function toggleBinding(key) {
  if (!keyChoice) return;
  const active = new Set(keyChoice.bindings || []);
  if (active.has(key)) active.delete(key); else active.add(key);
  keyChoice.bindings = [...active];
  saveKeys();
  refresh();
}

// ---- loading -------------------------------------------------------

function visibleRange() {
  const start = mondayOf(anchor);
  return [start, addDays(start, 7)];
}


async function refresh() {
  // Nothing is re-rendered while a card is being dragged: it would
  // replace the very node the pointer is holding onto. See dragging.
  if (dragState) return;
  const seq = ++refreshSeq;
  const args = keyArgs();
  let answer;
  if (tab === "board") {
    answer = await call(api.board(...args), "Could not read the board.");
  } else if (tab === "days") {
    const [start, end] = visibleRange();
    answer = await call(
      api.days(isoLocal(start), isoLocal(end), ...args), "Could not read the week.",
    );
  } else {
    answer = await call(api.focus_tab(...args), "Could not read the focus view.");
  }
  // A poll that lands after the user has already switched tabs is stale
  // by definition, and drawing it would flash the wrong view.
  if (answer === null || seq !== refreshSeq) return;
  adoptPalette(answer.speciesIds);
  data = answer;
  adoptKeys(answer);
  if (answer.workStart) workHours = { start: answer.workStart, end: answer.workEnd };
  render();
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(refresh, POLL_MS);
}

function stopPolling() {
  clearInterval(pollTimer);
  pollTimer = null;
}

// ---- rendering: shared ---------------------------------------------

function isDone(card) {
  return DONE_WORDS.has(String(card.status || "").toLowerCase());
}

/** One card, in whichever of the three views asked for it. Compact
 *  drops the type row and the progress bar, which is what makes a day
 *  column readable at seven-across. */
function cardNode(card, compact, draggable = true, withComplete = false) {
  const node = el("div", "card" + (compact ? " compact" : ""));
  const color = colorOf(card);
  node.style.borderLeftColor = color;
  node.dataset.id = card.id;
  if (card.id === selectedId) node.classList.add("selected");
  if (isDone(card)) node.classList.add("done");

  if (!compact) {
    const top = el("div", "card-top");
    const dot = el("span", "dot");
    dot.style.background = color;
    const type = el("span", "type", kindOf(card));
    type.style.color = color;
    top.append(dot, type, el("div", "spacer"));
    top.append(dayChip(card));
    node.append(top);
  }

  node.append(el("div", "card-name", card.name));

  const foot = el("div", "card-foot");
  if (compact) {
    const dot = el("span", "dot");
    dot.style.cssText = `width:5px;height:5px;border-radius:50%;background:${color};flex-shrink:0`;
    foot.append(dot);
  }
  foot.append(el("span", "owner", ownerLabel(card)));
  foot.append(el("div", "spacer"));
  if (card.blocked) foot.append(el("span", "flag", "blocked"));
  if (!compact && card.subtasks.length) {
    foot.append(el("span", "subs", `${card.subtasksDone}/${card.subtasks.length}`));
  }
  if (card.estimate) foot.append(el("span", "est", card.estimate));
  if (withComplete && !isDone(card)) {
    const done = button("card-complete", "✓");
    done.title = "Mark complete";
    done.onclick = (event) => { event.stopPropagation(); completeTask(card.id); };
    foot.append(done);
  }
  node.append(foot);

  if (!compact && card.subtasks.length) {
    const bar = el("div", "bar");
    const fill = el("i");
    fill.style.width = `${Math.round((card.subtasksDone / card.subtasks.length) * 100)}%`;
    fill.style.background = color;
    bar.append(fill);
    node.append(bar);
  }

  node.oncontextmenu = (event) => {
    event.preventDefault();
    ContextMenu.open(event.clientX, event.clientY, [
      ...ContextMenu.inspectItems(card.id, node),
      "-",
      { label: "Delete mote", danger: true, run: () => deleteMote(card.id, card.name) },
    ]);
  };

  if (draggable) node.addEventListener("pointerdown", (event) => beginCardDrag(event, card.id, node));
  node.addEventListener("click", () => {
    if (justDragged) return;
    select(card.id === selectedId ? null : card.id);
  });
  return node;
}

function ownerLabel(card) {
  if (card.owners && card.owners.length) {
    return card.owners.map((o) => o.name).join(", ");
  }
  return card.priority ? card.priority : "";
}

function dayChip(card) {
  const placement = card.scheduled;
  if (!placement) return el("span", "card-day none", "undated");
  const when = parseKey(placement.day);
  const label = `${DOW[(when.getDay() + 6) % 7]} ${when.getDate()}`;
  const chip = el("span", "card-day" + (placement.day === todayKey() ? " today" : ""), label);
  chip.title = `${placement.binding} = ${placement.start.replace("T", " ")}`;
  return chip;
}

/** Registers `node` as somewhere a dragged card can be released. Reset
 *  at the top of every render() (see below), so a target from the view
 *  before last cannot outlive the DOM node it pointed at. */
function dropTarget(node, onDrop) {
  dropZones.push({ node, onDrop });
}

// ---- dragging -------------------------------------------------------
//
// Pointer events rather than HTML5 drag-and-drop -- the same choice
// monastery/frontend/app.js makes for its own calendar, and for the same
// reason: native drag-and-drop inside pywebview's WebKitGTK view is
// unreliable enough that a card here simply would not move. One pointer
// layer covers the board's columns, the week's days and the pool alike,
// since all three only ever care which zone the pointer came up over.

function beginCardDrag(event, id, node) {
  if (event.button !== 0) return;
  dragState = { id, node, x0: event.clientX, y0: event.clientY, moved: false };
  window.addEventListener("pointermove", onCardDragMove);
  window.addEventListener("pointerup", onCardDragUp);
}

function onCardDragMove(event) {
  if (!dragState) return;
  if (!dragState.moved) {
    // Below the threshold this is still a click: a board where a
    // one-pixel wobble picks up a card is a board you cannot select
    // anything on.
    if (Math.abs(event.clientX - dragState.x0) < DRAG_PX
        && Math.abs(event.clientY - dragState.y0) < DRAG_PX) return;
    dragState.moved = true;
    dragState.node.classList.add("dragging");
    document.body.classList.add("dragging-on");
  }
  event.preventDefault();
  highlightZoneAt(event.clientX, event.clientY);
}

function onCardDragUp(event) {
  window.removeEventListener("pointermove", onCardDragMove);
  window.removeEventListener("pointerup", onCardDragUp);
  const state = dragState;
  dragState = null;
  clearZoneHighlight();
  if (!state) return;
  state.node.classList.remove("dragging");
  document.body.classList.remove("dragging-on");
  if (!state.moved) return;
  // A click still follows this pointerup, and without this it would
  // count as one too -- reopening or deselecting what the drag just
  // dropped.
  justDragged = true;
  setTimeout(() => { justDragged = false; }, 0);
  const zone = zoneAt(event.clientX, event.clientY);
  if (zone) zone.onDrop(state.id);
}

function zoneAt(x, y) {
  const under = document.elementFromPoint(x, y);
  if (!under) return null;
  return dropZones.find((z) => z.node.contains(under));
}

function highlightZoneAt(x, y) {
  clearZoneHighlight();
  const zone = zoneAt(x, y);
  if (zone) zone.node.classList.add("over");
}

function clearZoneHighlight() {
  dropZones.forEach((z) => z.node.classList.remove("over"));
}

// ---- rendering: the rail -------------------------------------------

function keyRow(label, count, on, onClick) {
  const row = button("key-row" + (on ? " on" : ""));
  row.append(el("span", "tick", on ? "✓" : ""));
  row.append(el("span", "name", label));
  if (count !== null) row.append(el("span", "n", String(count)));
  row.onclick = onClick;
  return row;
}

function renderRail() {
  const candidates = data && data.candidates;
  const enumKeys = (candidates && candidates.enum) || [];
  const estimates = (candidates && candidates.estimate) || [];
  const bindings = (candidates && candidates.bindings) || [];

  const status = clear($("status-rows"));
  const priority = clear($("priority-rows"));
  if (!enumKeys.length) {
    status.append(el("div", "empty", "no label-shaped properties yet"));
    priority.append(el("div", "empty", "no label-shaped properties yet"));
  }
  enumKeys.forEach((c) => {
    const hint = c.values.slice(0, 4).join(", ");
    const row = keyRow(c.key, c.count, keyChoice && keyChoice.status === c.key, () => setKey("status", c.key));
    row.title = hint ? `values: ${hint}` : "";
    status.append(row);
    const prow = keyRow(c.key, c.count, keyChoice && keyChoice.priority === c.key, () => setKey("priority", c.key));
    prow.title = row.title;
    priority.append(prow);
  });

  const estimate = clear($("estimate-rows"));
  if (!estimates.length) estimate.append(el("div", "empty", "no duration-shaped properties yet"));
  estimates.forEach((c) => estimate.append(
    keyRow(c.key, c.count, keyChoice && keyChoice.estimate === c.key, () => setKey("estimate", c.key)),
  ));

  const binding = clear($("binding-rows"));
  if (!bindings.length) binding.append(el("div", "empty", "nothing is dated yet"));
  bindings.forEach((b) => binding.append(
    keyRow(b.key, b.count, b.active, () => toggleBinding(b.key)),
  ));

  // Only overwritten when nothing is focused: a poll landing mid-edit
  // must not undo what the person is still typing.
  if (document.activeElement !== $("work-start-input")) $("work-start-input").value = workHours.start;
  if (document.activeElement !== $("work-end-input")) $("work-end-input").value = workHours.end;
}

// ---- rendering: the board ------------------------------------------

function renderBoard() {
  const host = clear($("columns"));
  (data.columns || []).forEach((column) => {
    const node = el("div", "column");
    const head = el("div", "column-head");
    const swatch = el("span", "swatch");
    swatch.style.background = column.key === null
      ? UNKNOWN_COLOR
      : `oklch(0.74 0.1 ${hueFor(column.key)})`;
    head.append(swatch, el("span", "label", column.label), el("span", "count", String(column.cards.length)));
    head.append(el("div", "spacer"));
    head.append(el("span", "load", humanMinutes(column.minutes)));
    node.append(head);

    const body = el("div", "column-body");
    column.cards.forEach((card) => body.append(cardNode(card)));

    if (addingColumn === column.key) {
      const input = el("input", "new-input");
      input.type = "text";
      input.placeholder = "new mote name";
      input.onkeydown = (event) => {
        if (event.key === "Enter") createTask(input.value, column.key);
        if (event.key === "Escape") { addingColumn = null; render(); }
      };
      body.append(input);
      setTimeout(() => input.focus(), 0);
    }

    const add = button("dashed", "+ add mote");
    add.onclick = () => { addingColumn = column.key; render(); };
    body.append(add);

    node.append(body);
    // A column with no status cannot be dropped into: there is no value
    // to write. Dragging *out* of it works, which is the useful half.
    if (column.key !== null) {
      dropTarget(node, (id) => moveTo(id, column.key));
    }
    host.append(node);
  });
}

// ---- rendering: the week -------------------------------------------

function renderDays() {
  const pool = clear($("pool-cards"));
  (data.pool || []).forEach((card) => pool.append(cardNode(card, true)));
  $("pool-count").textContent = String((data.pool || []).length);
  $("pool-note").textContent = (data.pool || []).length
    ? "Drag a mote onto a day to schedule it into the earliest free slot."
    : "Nothing undated. Drag a card back here to unschedule it.";

  const host = clear($("week"));
  (data.days || []).forEach((day) => {
    const when = parseKey(day.day);
    const isToday = day.day === todayKey();
    const node = el("div", "day" + (isToday ? " today" : ""));

    const head = el("div", "day-head");
    const top = el("div", "day-top");
    top.append(el("span", "dow", DOW[(when.getDay() + 6) % 7]));
    top.append(el("span", "num", String(when.getDate())));
    top.append(el("div", "spacer"));
    top.append(el("span", "load" + (day.over ? " over" : ""), humanMinutes(day.minutes) || "—"));
    head.append(top);

    const bar = el("div", "bar");
    const fill = el("i");
    const capacity = data.capacityMinutes || 0;
    fill.style.width = capacity ? `${Math.min(100, Math.round((day.minutes / capacity) * 100))}%` : "0%";
    fill.style.background = day.over ? "oklch(0.62 0.13 25)" : (isToday ? "var(--accent)" : "oklch(0.45 0 0)");
    bar.append(fill);
    head.append(bar);
    node.append(head);

    const body = el("div", "day-body");
    day.cards.forEach((card) => body.append(cardNode(card, true, true, true)));
    node.append(body);

    dropTarget(node, (id) => scheduleOn(id, day.day));
    host.append(node);
  });

  // The pool is a drop target too: dropping a scheduled card back into
  // it is how you unschedule, which is the same gesture in reverse.
  dropTarget($("pool"), (id) => unschedule(id));
}

// ---- rendering: focus ----------------------------------------------

function activeCard() {
  const queue = (data && data.queue) || [];
  return queue.find((c) => c.id === activeId) || queue[0] || null;
}

function renderFocus() {
  const queue = clear($("queue-cards"));
  (data.queue || []).forEach((card) => {
    // In the queue a click picks what the timer is about, rather than
    // opening the panel -- that is the one thing this list is for -- so
    // it is not a drag source either.
    const node = cardNode(card, true, false);
    node.onclick = () => { activeId = card.id; render(); };
    if (card.id === (activeCard() || {}).id) node.classList.add("selected");
    queue.append(node);
  });
  $("queue-note").classList.toggle("hidden", (data.queue || []).length > 0);

  const active = activeCard();
  const color = active ? colorOf(active) : UNKNOWN_COLOR;
  $("active-dot").style.background = color;
  $("active-kind").style.color = color;
  $("active-kind").textContent = active ? kindOf(active) : "—";
  $("active-name").textContent = active ? active.name : "Nothing scheduled for today";
  $("active-card").style.borderLeftColor = color;
  $("active-meta").textContent = active
    ? [ownerLabel(active), active.estimate, (active.scheduled || {}).day].filter(Boolean).join(" · ")
    : "";

  const subs = clear($("active-subs"));
  (active ? active.subtasks : []).forEach((sub, index) => {
    subs.append(subRow(sub, index, active.id));
  });
  const total = active ? active.subtasks.length : 0;
  const done = active ? active.subtasksDone : 0;
  $("active-sub-label").textContent = total ? `${done}/${total} subtasks` : "no subtasks";
  $("active-fill").style.width = total ? `${Math.round((done / total) * 100)}%` : "0%";
  $("active-fill").style.background = color;

  $("streak-days").textContent = String(data.streak || 0);
  $("streak-unit").textContent = (data.streak === 1) ? "day unbroken" : "days unbroken";

  const heat = clear($("heatmap"));
  (data.heatmap || []).forEach((cell) => {
    const node = el("div", "cell" + (cell.today ? " today" : ""));
    const n = cell.sessions;
    node.style.background = n >= 3 ? "var(--accent)"
      : n === 2 ? "oklch(0.74 0.1 68 / 0.55)"
      : n === 1 ? "oklch(0.74 0.1 68 / 0.25)" : "oklch(0.2 0 0)";
    node.title = `${cell.day} — ${humanMinutes(cell.minutes) || "nothing"}`;
    heat.append(node);
  });

  const today = todayKey();
  const todayMinutes = (data.sessions || [])
    .filter((s) => s.day === today)
    .reduce((sum, s) => sum + s.minutes, 0);
  $("streak-summary").textContent =
    `${humanMinutes(todayMinutes) || "0m"} logged today · ${(data.sessions || []).length} sessions total`;

  const log = clear($("sessions"));
  if (!(data.sessions || []).length) log.append(el("div", "empty", "no sessions logged yet"));
  (data.sessions || []).slice(0, 60).forEach((session) => {
    const row = el("div", "session");
    const when = session.start ? new Date(session.start) : null;
    const isToday = session.day === today;
    row.append(el("span", "time", when
      ? (isToday ? `${pad(when.getHours())}:${pad(when.getMinutes())}`
                 : `${MON_SHORT[when.getMonth()]} ${when.getDate()}`)
      : "—"));
    row.append(el("span", "name", session.subject ? session.subject.name : "untitled"));
    row.append(el("span", "dur" + (isToday ? " today" : ""), `${Math.round(session.minutes)}m`));
    log.append(row);
  });
}

function subRow(sub, index, moteId) {
  const row = el("div", "sub-row" + (sub.done ? " done" : ""));
  row.append(el("span", "box", sub.done ? "✓" : ""));
  row.append(el("span", "text", sub.text));
  row.onclick = async (event) => {
    event.stopPropagation();
    await call(api.set_subtask(moteId, index, !sub.done), "Could not update that subtask.");
    refresh();
    if (selectedId === moteId) select(moteId);
  };
  return row;
}

// ---- rendering: the panel ------------------------------------------

function renderPanel() {
  const node = $("panel");
  if (!panel) { node.classList.add("hidden"); return; }
  node.classList.remove("hidden");

  const color = colorOf(panel);
  $("panel-dot").style.background = color;
  $("panel-kind-text").style.color = color;
  $("panel-kind-text").textContent = kindOf(panel);
  $("panel-name").textContent = panel.name;
  $("panel-id").textContent = panel.id;

  // Status pills come from the board's own columns, so the panel offers
  // exactly the statuses this garden uses -- not a list this file made up.
  const statuses = clear($("panel-statuses"));
  const available = boardStatuses();
  $("panel-status-section").classList.toggle("hidden", !keyChoice || !keyChoice.status);
  available.forEach((status) => {
    const pill = button("pill" + (panel.status === status ? " on" : ""), status);
    pill.onclick = () => setStatus(panel.id, panel.status === status ? null : status);
    statuses.append(pill);
  });

  const sched = clear($("panel-sched"));
  if (panel.scheduled) {
    const row = el("div", "sched-row");
    row.append(el("span", "k", panel.scheduled.binding));
    row.append(el("span", "eq", "="));
    row.append(el("span", "v", panel.scheduled.start.replace("T", " ")));
    row.append(el("div", "spacer"));
    const clearBtn = button("close-button", "×");
    clearBtn.title = "Unschedule";
    clearBtn.onclick = () => unschedule(panel.id);
    row.append(clearBtn);
    sched.append(row);
  } else {
    sched.append(el("div", "sched-row none", "not scheduled"));
  }

  const props = clear($("panel-props"));
  const entries = Object.entries(panel.properties || {});
  if (!entries.length) props.append(el("div", "empty", "no properties"));
  entries.forEach(([key, value]) => {
    const isTaskKey = (panel.taskKeys || []).includes(key);
    const row = el("div", "prop-row" + (isTaskKey ? " task-key" : ""));
    row.append(el("span", "k", key));
    row.append(el("span", "v", typeof value === "object" ? JSON.stringify(value) : String(value)));
    props.append(row);
  });

  const subs = clear($("panel-subs"));
  if (!panel.subtasks.length) subs.append(el("div", "empty", "none yet"));
  panel.subtasks.forEach((sub, index) => subs.append(subRow(sub, index, panel.id)));

  const rels = clear($("panel-rels"));
  if (!panel.relationships.length) rels.append(el("div", "empty", "none"));
  panel.relationships.forEach((rel) => {
    const row = button("rel-row");
    row.append(el("span", "arrow", rel.outgoing ? "→" : "←"));
    const dot = el("span", "dot");
    dot.style.background = colorOf(rel.other);
    row.append(dot);
    row.append(el("span", "name", rel.other.name));
    row.append(el("span", "type", rel.type.replace(/_/g, " ")));
    row.onclick = () => select(rel.other.id);
    rels.append(row);
  });
}

function boardStatuses() {
  // The board knows the garden's statuses; the other two tabs do not,
  // so they fall back to whatever is already on screen plus the mote's
  // own. Nothing invents a vocabulary -- see core/task_candidates.py.
  if (data && data.columns) {
    return data.columns.filter((c) => c.key !== null).map((c) => c.key);
  }
  const found = new Set();
  const lists = [(data && data.pool) || [], (data && data.queue) || []];
  ((data && data.days) || []).forEach((d) => lists.push(d.cards));
  lists.flat().forEach((c) => { if (c.status) found.add(c.status); });
  if (panel && panel.status) found.add(panel.status);
  return [...found];
}

// ---- rendering: the chrome -----------------------------------------

function render() {
  if (!data) return;
  // Every drop target is registered fresh by whichever tab draws below,
  // so a zone from the view before last can never outlive its node.
  dropZones = [];
  $("board-view").classList.toggle("hidden", tab !== "board");
  $("days-view").classList.toggle("hidden", tab !== "days");
  $("focus-view").classList.toggle("hidden", tab !== "focus");
  $("rail").classList.toggle("hidden", !railOpen);
  // Only the week has a range to step through -- the board and focus
  // tabs have nothing for prev/next/today to mean.
  $("nav").classList.toggle("hidden", tab !== "days");

  if (tab === "board") renderBoard();
  else if (tab === "days") renderDays();
  else renderFocus();

  renderRail();
  renderPanel();
  renderChrome();
  renderTimer();
}

function renderChrome() {
  const stats = data.stats || {};
  if (tab === "days") {
    const [start, end] = visibleRange();
    const last = addDays(end, -1);
    $("range-title").textContent =
      `${start.getDate()} – ${last.getDate()} ${MON_SHORT[last.getMonth()]} ${last.getFullYear()}`;
    const scheduled = (data.days || []).reduce((n, d) => n + d.cards.length, 0);
    $("range-sub").textContent =
      `${scheduled} scheduled · ${(data.pool || []).length} undated · capacity ${humanMinutes(data.capacityMinutes)}/day`;
    $("hint").textContent = "drag a mote onto a day for the earliest free slot · drop it back to unschedule";
    $("stat-scheduled").textContent = `${scheduled} scheduled this week`;
  } else if (tab === "board") {
    $("range-title").textContent = "Board";
    $("range-sub").textContent = keyChoice && keyChoice.status
      ? `by ${keyChoice.status} · ${stats.open || 0} open of ${stats.total || 0}`
      : "no status key chosen — pick one in the sidebar";
    $("hint").textContent = "drag cards between columns to set status";
    $("stat-scheduled").textContent = `${(stats.total || 0) - (stats.undated || 0)} scheduled`;
  } else {
    $("range-title").textContent = "Focus";
    const active = activeCard();
    $("range-sub").textContent = active ? active.name : "nothing queued for today";
    $("hint").textContent = "space starts or pauses the timer";
    $("stat-scheduled").textContent = `${(data.queue || []).length} queued today`;
  }

  $("stat-open").textContent = `${stats.open || 0} open`;
  $("stat-undated").textContent = `${stats.undated || 0} undated`;

  const sessions = (data.sessions || []).filter((s) => s.day === todayKey());
  if (tab === "focus") {
    const minutes = sessions.reduce((sum, s) => sum + s.minutes, 0);
    $("stat-focus").textContent = `${humanMinutes(minutes) || "0m"} focused today`;
  }

  [...$("tabs").children].forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
}

function renderTimer() {
  const total = sessionMinutes * 60;
  const shown = stopwatch ? elapsed : Math.max(0, total - elapsed);
  $("clock").textContent = clock(shown);
  $("chip-time").textContent = clock(shown);
  $("chip-time").classList.toggle("running", running);
  $("chip-label").textContent = running ? "running" : (tab === "focus" ? "paused" : "focus");
  $("phase").textContent = running ? "in session" : "ready";
  $("phase").classList.toggle("running", running);

  // Stopwatch mode has no target to fill toward, so the ring just laps
  // once a minute -- motion without implying a duration that isn't there.
  const pct = stopwatch
    ? ((elapsed % 60) / 60) * 100
    : Math.max(0, Math.min(100, (elapsed / total) * 100));
  $("ring").style.background = `conic-gradient(var(--accent) ${pct.toFixed(1)}%, oklch(0.225 0 0) 0)`;
  $("run-btn").textContent = running ? "Pause"
    : (elapsed === 0 ? (stopwatch ? "Begin" : `Begin ${sessionMinutes}m`) : "Resume");

  $("duration-input").disabled = running || stopwatch;
  $("stopwatch-toggle").disabled = running;
  if (document.activeElement !== $("duration-input")) $("duration-input").value = sessionMinutes;
  $("stopwatch-toggle").checked = stopwatch;
}

// ---- actions -------------------------------------------------------

/** The one delete path -- the panel's own button and the card's context
 *  menu both call this, rather than the second growing an inline copy of
 *  the first. */
async function deleteMote(moteId, name = "That mote") {
  const ok = await call(api.delete_mote(moteId), "Could not delete that mote.");
  if (!ok) return;
  if (selectedId === moteId) select(null);
  toast(`${name} deleted.`);
  refresh();
}

async function select(moteId, options = {}) {
  selectedId = moteId;
  if (!moteId) { panel = null; Inspector.close(); render(); return; }
  panel = await call(api.focus(moteId), "Could not read that mote.");
  render();
  // The docked panel keeps what is Momentum's own -- status pills drawn
  // from the board's own columns, subtasks, scheduling -- and the mote
  // itself goes to the surface every window shares.
  await Inspector.open(moteId, { anchor: options.anchor, newTab: !!options.newTab });
}

async function moveTo(moteId, status) {
  if (!keyChoice || !keyChoice.status) {
    toast("Pick which property means status first — the sidebar lists the candidates.", true);
    return;
  }
  const [, , estimateArg] = keyArgs();
  const answer = await call(
    api.set_status(moteId, keyChoice.status, status, estimateArg), "Could not set that status.",
  );
  if (answer === null) return;
  if (selectedId === moteId) panel = answer;
  toast(`${answer.name} → ${status}`);
  refresh();
}

async function completeTask(moteId) {
  const [statusArg, , estimateArg] = keyArgs();
  const answer = await call(api.complete_task(moteId, statusArg, estimateArg), "Could not complete that task.");
  if (answer === null) return;
  if (selectedId === moteId) panel = answer;
  toast(`${answer.name} marked complete.`);
  refresh();
}

async function scheduleOn(moteId, day) {
  const answer = await call(api.schedule(moteId, day, ...keyArgs()), "Could not schedule that.");
  if (answer === null) return;
  if (selectedId === moteId) panel = answer;
  const when = answer.start ? answer.start.replace("T", " ").slice(0, 16) : day;
  if (answer.slotFull) {
    toast(`${answer.name} → ${when} — that day is already full, so it went at the start.`);
  } else {
    toast(`${answer.name} → ${answer.binding} = ${when}`);
  }
  refresh();
}

async function saveWorkingHours() {
  const start = $("work-start-input").value || workHours.start;
  const end = $("work-end-input").value || workHours.end;
  const answer = await call(api.set_working_hours(start, end), "Could not save working hours.");
  if (answer === null) return;
  workHours = answer;
  toast(`Working hours: ${answer.workStart}–${answer.workEnd}`);
  refresh();
}

async function unschedule(moteId) {
  const answer = await call(api.unschedule(moteId, keyChoice ? keyChoice.bindings : null), "Could not unschedule that.");
  if (answer === null) return;
  if (selectedId === moteId) panel = answer;
  toast(`${answer.name} — back in the pool.`);
  refresh();
}

async function setStatus(moteId, status) {
  if (!keyChoice || !keyChoice.status) return;
  const [, , estimateArg] = keyArgs();
  const answer = await call(
    api.set_status(moteId, keyChoice.status, status, estimateArg), "Could not set that status.",
  );
  if (answer === null) return;
  panel = answer;
  refresh();
}

async function createTask(name, status) {
  const trimmed = String(name || "").trim();
  addingColumn = null;
  if (!trimmed) { render(); return; }
  const answer = await call(
    api.create_task(trimmed, keyChoice ? keyChoice.status : null, status), "Could not create that mote.",
  );
  if (answer === null) { render(); return; }
  toast(`${answer.name} created.`);
  selectedId = answer.id;
  panel = answer;
  refresh();
}

// ---- the timer -----------------------------------------------------
//
// A running countdown is this window's own state and nothing is written
// until it stops -- the same preview/commit split core/simulation.py
// keeps. `startedAt` is captured when a session begins so the log says
// when the work happened, not when the timer happened to be stopped.

function toggleTimer() {
  if (tab !== "focus") setTab("focus");
  running = !running;
  if (running && startedAt === null) {
    startedAt = new Date();
    // Starting the timer against a task is the one moment this window
    // moves a status-bound property on its own, to "working" -- one of
    // the presets asked for. Fire-and-forget: a failure here is not
    // worth blocking the timer over, and the next poll shows whatever
    // actually happened.
    const active = activeCard();
    if (active && keyChoice && keyChoice.status) {
      call(api.set_status(active.id, keyChoice.status, "working"), "Could not update status.")
        .then(() => refresh());
    }
  }
  renderTimer();
}

function resetTimer() {
  running = false;
  elapsed = 0;
  startedAt = null;
  renderTimer();
}

async function finishSession(auto) {
  const minutes = Math.round(elapsed / 60);
  const active = activeCard();
  const began = startedAt;
  running = false;
  elapsed = 0;
  startedAt = null;
  renderTimer();

  if (minutes < 1) {
    if (!auto) toast("Nothing to log yet — that session is under a minute.");
    return;
  }
  const [statusArg, , estimateArg] = keyArgs();
  const answer = await call(
    api.log_session(active ? active.id : null, minutes, began ? isoLocal(began) : null, false, estimateArg, statusArg),
    "Could not log that session.",
  );
  if (answer === null) return;
  toast(`Session logged — ${minutes}m on ${active ? active.name : "nothing in particular"}.`);
  refresh();
}

/** The timer's own complete button: log whatever ran (if anything) and
 *  mark the task done in the same action, so a finished task is not a
 *  log followed by a separate trip to the board. Under a minute in, there
 *  is nothing worth logging as a session, so it goes straight to
 *  complete_task() -- the same closing-session accounting the days
 *  view's check button uses when no timer was running at all. */
async function completeSession() {
  const minutes = Math.round(elapsed / 60);
  const active = activeCard();
  const began = startedAt;
  running = false;
  elapsed = 0;
  startedAt = null;
  renderTimer();
  if (!active) return;

  const [statusArg, , estimateArg] = keyArgs();
  const answer = minutes >= 1
    ? await call(
        api.log_session(active.id, minutes, began ? isoLocal(began) : null, true, estimateArg, statusArg),
        "Could not complete that task.",
      )
    : await call(api.complete_task(active.id, statusArg, estimateArg), "Could not complete that task.");
  if (answer === null) return;
  toast(`${active.name} marked complete.`);
  refresh();
}

function tick() {
  if (!running) return;
  elapsed += 1;
  if (!stopwatch && elapsed >= sessionMinutes * 60) { finishSession(true); return; }
  renderTimer();
}

function setSessionMinutes(minutes) {
  const clamped = Math.max(1, Math.min(180, Math.round(minutes) || 25));
  sessionMinutes = clamped;
  saveTimerPrefs();
  if (!running) { elapsed = 0; startedAt = null; }
  renderTimer();
}

function setStopwatch(on) {
  stopwatch = on;
  saveTimerPrefs();
  if (!running) { elapsed = 0; startedAt = null; }
  renderTimer();
}

// ---- tabs ----------------------------------------------------------

function setTab(next) {
  if (tab === next) return;
  tab = next;
  addingColumn = null;
  data = null;
  render();
  refresh();
}

function step(direction) {
  if (tab !== "days") return;
  anchor = addDays(anchor, 7 * direction);
  refresh();
}

// ---- garden menu ---------------------------------------------------

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
  gardens.forEach((name) => {
    const item = button(name === current ? "on" : null, name);
    item.onclick = async () => {
      closeGardenMenu();
      const answer = await call(api.switch_garden(name), "Could not switch garden.");
      if (answer === null) return;
      $("garden-name").textContent = answer.garden;
      // Another garden is another set of properties, so the keyChoice chosen
      // for this one mean nothing there -- let Python guess afresh.
      keyChoice = null;
      selectedId = null;
      panel = null;
      refresh();
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

// ---- wiring --------------------------------------------------------

function wire() {
  [...$("tabs").children].forEach((b) => {
    b.onclick = () => setTab(b.dataset.tab);
  });

  $("rail-toggle").onclick = () => {
    railOpen = !railOpen;
    $("rail-toggle").classList.toggle("on", railOpen);
    render();
  };

  $("work-start-input").onchange = saveWorkingHours;
  $("work-end-input").onchange = saveWorkingHours;

  $("prev").onclick = () => step(-1);
  $("next").onclick = () => step(1);
  $("today").onclick = () => { anchor = new Date(); refresh(); };

  $("garden-btn").onclick = openGardenMenu;
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".topbar-menu")) closeGardenMenu();
  });

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
    const icon = $("refresh-btn");
    icon.classList.add("spinning");
    const answer = await call(api.refresh(), "Could not refresh.");
    icon.classList.remove("spinning");
    if (answer && answer.synced) toast("Synced.");
    refresh();
  };

  $("chip-time").onclick = toggleTimer;
  $("run-btn").onclick = toggleTimer;
  $("reset-btn").onclick = resetTimer;
  $("finish-btn").onclick = () => finishSession(false);
  $("complete-btn").onclick = () => completeSession();
  $("duration-input").onchange = (event) => setSessionMinutes(Number(event.target.value));
  $("stopwatch-toggle").onchange = (event) => setStopwatch(event.target.checked);

  $("panel-close").onclick = () => select(null);
  $("panel-focus").onclick = () => {
    if (!panel) return;
    activeId = panel.id;
    setTab("focus");
  };
  $("panel-delete").onclick = () => panel && deleteMote(panel.id, panel.name);
  $("panel-add-sub").onclick = async () => {
    if (!panel) return;
    const text = window.prompt("Subtask");
    if (!text || !text.trim()) return;
    const answer = await call(api.add_subtask(panel.id, text.trim()), "Could not add that subtask.");
    if (answer === null) return;
    panel = answer;
    render();
    refresh();
  };

  window.addEventListener("keydown", (event) => {
    const typing = event.target instanceof HTMLInputElement;
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
      if (launcherIsOpen()) { closeLauncher(); return; }
      if (addingColumn !== null) { addingColumn = null; render(); return; }
      select(null);
      return;
    }
    if (typing) return;
    if (event.key === " " && tab === "focus") { event.preventDefault(); toggleTimer(); }
    else if (event.key === "ArrowLeft") step(-1);
    else if (event.key === "ArrowRight") step(1);
    else if (event.key.toLowerCase() === "b") setTab("board");
    else if (event.key.toLowerCase() === "d") setTab("days");
    else if (event.key.toLowerCase() === "f") setTab("focus");
    else if (event.key.toLowerCase() === "t" && tab === "days") { anchor = new Date(); refresh(); }
  });

  window.addEventListener("focus", () => { refresh(); startPolling(); });
  window.addEventListener("blur", stopPolling);
}

// ---- app switcher (⌘J) ----
//
// Every sibling app but this one, in a fixed order -- so "Monastery is
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
    id: "momentum", name: "Momentum", kind: "tasks",
    desc: "Task board, week planner and focus timer.",
    icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
      <rect x="9" y="26" width="6" height="8" rx="1.5" fill="var(--fg)" opacity="0.5"></rect>
      <rect x="17" y="18" width="6" height="16" rx="1.5" fill="var(--fg)" opacity="0.65"></rect>
      <rect x="25" y="9" width="6" height="25" rx="1.5" fill="var(--accent)"></rect>
    </svg>`,
  },
  {
    id: "monolith", name: "Monolith", kind: "table",
    desc: "Every mote as a row: query, bulk-edit and link the garden.",
    icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
      <rect x="13" y="6" width="14" height="28" rx="2.5" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></rect>
      <rect x="16" y="11" width="8" height="3" rx="1" fill="var(--accent)"></rect>
      <rect x="16" y="18" width="8" height="3" rx="1" fill="var(--fg)" opacity="0.4"></rect>
      <rect x="16" y="25" width="8" height="3" rx="1" fill="var(--fg)" opacity="0.4"></rect>
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
const THIS_APP = "momentum";

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

function launcherIsOpen() { return !$("launcher-overlay").classList.contains("hidden"); }

async function launchApp(appId) {
  closeLauncher();
  const app = LAUNCHER_APPS.find((a) => a.id === appId);
  await call(api.open_app(appId), `Could not open ${app.name}.`);
  toast(`Opening ${app.name}…`);
}

async function init() {
  api = window.pywebview.api;
  Inspector.install({ api, onChanged: refresh });
  CommandBar.install({
    api,
    onChanged: refresh,
    onNavigate: (moteId) => Inspector.open(moteId),
  });
  anchor = new Date();
  keyChoice = loadKeys();
  loadTimerPrefs();
  wire();
  $("garden-name").textContent = await api.current_garden();
  await refresh();

  const focus = await api.initial_focus();
  if (focus) await select(focus);

  tickTimer = setInterval(tick, 1000);
  startPolling();
}

window.addEventListener("pywebviewready", init);
