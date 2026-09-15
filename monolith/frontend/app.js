// monolith/frontend/app.js
//
// Wiring between monolith/api.py and the table. Everything about *what*
// the table contains comes back from Python already decided: which
// columns a garden has (monolith/payload.py::Schema discovers them),
// which rows a query selects, how they group, and how each value reads
// (core/query.py). This file draws that answer and takes edits.
//
// Three things Python has no opinion about, and this file owns:
// the draft query before it is saved as a view, which rows are selected,
// and where the popover opens. The draft lives in localStorage so the
// window reopens on the table you left rather than on a default one.
//
// The selection is the reason a table beats the other four windows for
// bulk work, and also the thing most easily got wrong: it is a set of
// mote *ids*, never row indices, so it survives a refetch that reorders
// or refilters the rows under it. A stale id is not an error --
// monolith/api.py::_existing drops it and writes the rest.

const THIS_APP = "monolith";
const ROW_PAGE = 200;       // rows drawn per page -- see the scroll handler and "show more"
const SEARCH_DEBOUNCE_MS = 140;
const CHIP_VALUES = 8;      // a column with at most this many distinct values draws as chips
const GROUP_CHIPS = 4;      // how many visible columns the topbar offers as group keys
const UNKNOWN_COLOR = "oklch(0.62 0 0)";

let api;
let schema = null;          // the discovered columns, from the last answer
let data = null;            // the last table() answer
let draft = null;           // the query being edited, in core/query.py's JSON shape
let viewId = null;          // the saved view it was loaded from, if any
let saved = null;           // that view's query as stored -- the dirty check
let sel = new Set();        // selected mote ids
let lastRow = null;         // anchor for shift-click ranges
let limit = ROW_PAGE;
let matrixOpen = false;
let paintType = null;       // which relationship type a matrix click writes
let columnFilter = "";
let popState = null;        // the popover that is up, if any
let toastTimer = null;
let refreshSeq = 0;         // guards against two refreshes landing out of order
let launcherHighlight = 0;
let layout = {                                  // sizes the user drags -- see wireResizers()
  railWidth: 232, matrixHeight: 250, railColumnsHeight: 240, columnWidths: {},
};

const $ = (id) => document.getElementById(id);

// ---- layout (resizable rail, matrix and columns) --------------------
//
// Independent of the draft query's storeKey: these are window chrome,
// not garden data, so they hold across gardens and views.

function loadLayout() {
  try {
    const raw = window.localStorage.getItem("monolith.layout");
    if (!raw) return;
    const state = JSON.parse(raw);
    layout = { ...layout, ...state, columnWidths: { ...(state.columnWidths || {}) } };
  } catch (error) {
    // A window that cannot remember its last sizes just uses the defaults.
  }
}

function saveLayout() {
  try {
    window.localStorage.setItem("monolith.layout", JSON.stringify(layout));
  } catch (error) {
    // Not fatal -- see loadLayout.
  }
}

function applyLayout() {
  $("rail").style.width = `${layout.railWidth}px`;
  $("rail-columns").style.height = `${layout.railColumnsHeight}px`;
  $("matrix").style.height = `${layout.matrixHeight}px`;
}

/** One drag gesture, generic over the four things in this window that
 *  resize by dragging a handle: it reads a start size, tracks the mouse
 *  delta along one axis, and writes the clamped result back through
 *  `set` on every move so the caller decides what "applying" a size
 *  means (an inline style here, a style plus a live table repaint there). */
function attachResizer(handle, axis, sign, get, set, min, max) {
  handle.addEventListener("mousedown", (event) => {
    event.preventDefault();
    const start = axis === "x" ? event.clientX : event.clientY;
    const startSize = get();
    handle.classList.add("dragging");
    document.body.style.cursor = axis === "x" ? "col-resize" : "row-resize";
    document.body.style.userSelect = "none";

    function onMove(moveEvent) {
      const cur = axis === "x" ? moveEvent.clientX : moveEvent.clientY;
      const size = Math.min(max, Math.max(min, startSize + sign * (cur - start)));
      set(size);
    }
    function onUp() {
      handle.classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      saveLayout();
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
}

/** A column header's resize handle. Unlike the other three, this one has
 *  no single element to restyle -- every row shares the grid template --
 *  so a drag recomputes it and repaints every `.trow` directly rather
 *  than going through render(), which would also re-run the query. */
function attachColumnResizer(handle, column) {
  handle.addEventListener("mousedown", (event) => {
    event.preventDefault();
    event.stopPropagation();
    const startWidth = handle.parentElement.getBoundingClientRect().width;
    const startX = event.clientX;
    handle.classList.add("dragging");
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";

    function onMove(moveEvent) {
      const width = Math.max(60, Math.round(startWidth + (moveEvent.clientX - startX)));
      layout.columnWidths[column.key] = width;
      const template = rowTemplate();
      document.querySelectorAll("#table .trow").forEach((node) => {
        node.style.gridTemplateColumns = template;
      });
    }
    function onUp() {
      handle.classList.remove("dragging");
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      saveLayout();
    }
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  });
  handle.addEventListener("click", (event) => event.stopPropagation());
}

function wireResizers() {
  attachResizer($("rail-resizer"), "x", 1,
    () => layout.railWidth,
    (v) => { layout.railWidth = v; $("rail").style.width = `${v}px`; },
    160, 480);
  attachResizer($("rail-split-resizer"), "y", -1,
    () => layout.railColumnsHeight,
    (v) => { layout.railColumnsHeight = v; $("rail-columns").style.height = `${v}px`; },
    80, 640);
  attachResizer($("matrix-resizer"), "y", -1,
    () => layout.matrixHeight,
    (v) => { layout.matrixHeight = v; $("matrix").style.height = `${v}px`; },
    120, 900);
}

// ---- colour -------------------------------------------------------
//
// Byte-for-byte the hash monarch/frontend/graph.js uses, the same copy
// Mora, Monastery and Momentum carry. Five windows disagreeing about
// what colour "Task" is would be worse than any of them being arbitrary.

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

/** A relationship type's colour -- the plain hash, not the species
 *  palette: a type is not a species, and the palette's entries are
 *  claimed by species. Stable per type, so `depends_on` reads the same
 *  in the matrix, in its chip and in a cell's link list. */
function typeColor(type) {
  return `oklch(0.74 0.11 ${hueFor(type)})`;
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
 *  silently half-drawn table. */
async function call(promise, failure) {
  try {
    return await promise;
  } catch (error) {
    toast(failure || String(error && error.message ? error.message : error), true);
    return null;
  }
}

// ---- fields and columns --------------------------------------------
//
// A field is {source, key, direction}; its string form is the same one
// monolith/payload.py::column_key builds, so the two sides agree on what
// "this column" means without sending an index that a refetch could move.

function fieldKey(field) {
  if (!field) return "";
  if (field.source === "rel") return `rel:${field.direction}:${field.key}`;
  return `${field.source}:${field.key}`;
}

function columnFor(field) {
  const key = fieldKey(field);
  return (schema ? schema.columns : []).find((c) => c.key === key) || null;
}

function labelFor(field) {
  const column = columnFor(field);
  return column ? column.label : (field ? field.key : "");
}

function opsFor(field) {
  const column = columnFor(field);
  if (column) return column.ops;
  return schema ? schema.ops.text : [];
}

function isEmptyOp(op) {
  return op === "is empty" || op === "is not empty";
}

// ---- the draft query -----------------------------------------------

function storeKey() {
  return `monolith.draft.${data ? data.garden : "default"}`;
}

function persist() {
  try {
    window.localStorage.setItem(storeKey(), JSON.stringify({
      draft, viewId, saved, matrixOpen, paintType,
    }));
  } catch (error) {
    // A window that cannot remember its last query is still a window.
  }
}

function restore(garden) {
  try {
    const raw = window.localStorage.getItem(`monolith.draft.${garden}`);
    if (!raw) return;
    const state = JSON.parse(raw);
    draft = state.draft || null;
    viewId = state.viewId || null;
    saved = state.saved || null;
    matrixOpen = !!state.matrixOpen;
    paintType = state.paintType || null;
  } catch (error) {
    draft = null;
  }
}

function dirty() {
  if (!viewId || !saved) return false;
  return JSON.stringify(draft) !== JSON.stringify(saved);
}

/** Every change to the query goes through here: it re-runs the query on
 *  the Python side rather than filtering locally, so a condition means
 *  exactly what core/query.py says it means and nothing is evaluated
 *  twice in two languages. */
function edit(change) {
  draft = { ...draft, ...change };
  limit = ROW_PAGE;
  refresh();
}


async function refresh() {
  const seq = ++refreshSeq;
  const answer = await call(api.table(draft), "Could not read the garden.");
  if (answer === null || seq !== refreshSeq) return;
  adoptPalette(answer.speciesIds);
  schema = answer.schema;
  data = answer;
  draft = answer.query;
  // A selection is a set of ids, but it is only actionable on rows that
  // are on screen: keeping an id the query no longer matches would mean
  // a bulk edit writing motes nobody can see it write.
  const present = new Set(visibleIds());
  [...sel].forEach((id) => { if (!present.has(id)) sel.delete(id); });
  if (!paintType) paintType = answer.matrix.types[0] || "depends_on";
  persist();
  render();
}

// ---- rendering ------------------------------------------------------

function render() {
  if (!data) return;
  renderHead();
  renderWhere();
  renderViews();
  renderColumns();
  renderTable();
  renderMatrix();
  renderFooter();
  renderBulk();
}

function renderHead() {
  const view = (data.views || []).find((v) => v.id === viewId);
  $("view-title").textContent = view ? view.name : "All motes";
  $("view-sub").textContent = `${data.matched} rows · ${data.summary}`;
  $("garden-name").textContent = data.garden;

  const chips = clear($("group-chips"));
  const none = button(draft.group ? null : "on", "none");
  none.onclick = () => edit({ group: null });
  chips.append(none);

  // Name, id and last-updated are near-unique per mote: grouping by one
  // makes a header per row, so they are columns but never chips.
  const poor = new Set(["builtin:name", "builtin:id", "builtin:updated"]);
  const offered = draft.columns.filter((f) => !poor.has(fieldKey(f))).slice(0, GROUP_CHIPS);
  if (draft.group && !offered.some((f) => fieldKey(f) === fieldKey(draft.group))) {
    offered.push(draft.group);
  }
  offered.forEach((field) => {
    const chip = button(draft.group && fieldKey(draft.group) === fieldKey(field) ? "on" : null,
                        labelFor(field));
    chip.onclick = () => edit({ group: field });
    chips.append(chip);
  });

  const more = button(null, "…");
  more.title = "Group by any column";
  more.onclick = (event) => pickColumn(event.currentTarget, "group by", () => true,
                                       (field) => edit({ group: field }));
  chips.append(more);

  $("matrix-toggle").classList.toggle("on", matrixOpen);
}

function renderWhere() {
  const bar = clear($("conditions"));
  draft.conditions.forEach((condition, index) => {
    bar.append(conditionChip(condition, index));
    if (index < draft.conditions.length - 1) {
      const join = button("join", draft.join);
      join.onclick = () => edit({ join: draft.join === "and" ? "or" : "and" });
      bar.append(join);
    }
  });
  const isDirty = dirty();
  $("dirty-note").classList.toggle("hidden", !isDirty);
  $("update-view").classList.toggle("hidden", !isDirty);
  $("save-view").classList.remove("hidden");
}

function conditionChip(condition, index) {
  const chip = el("div", "cond");
  const field = button("cond-field", labelFor(condition.field));
  field.onclick = (event) => pickColumn(event.currentTarget, "property", () => true,
                                        (picked) => setCondition(index, resetCondition(picked)));
  const op = button("cond-op", condition.op);
  op.onclick = (event) => pickOp(event.currentTarget, condition, index);
  chip.append(field, op);

  if (!isEmptyOp(condition.op)) {
    const value = button("cond-value", condition.value || "…");
    value.onclick = (event) => pickValue(event.currentTarget, condition, index);
    chip.append(value);
  }
  const remove = button("cond-remove", "×");
  remove.onclick = () => edit({ conditions: draft.conditions.filter((_, i) => i !== index) });
  chip.append(remove);
  return chip;
}

/** A fresh condition on a newly picked column. `contains` is the right
 *  first guess for a column of free text and the wrong one for a column
 *  holding four repeated values -- nobody filtering a status wants a
 *  substring match -- so a column that reads as an enum starts on `is`. */
function resetCondition(field) {
  const ops = opsFor(field);
  const column = columnFor(field);
  const enumish = column && column.values.length > 0 && column.values.length <= CHIP_VALUES;
  return { field, op: enumish && ops.includes("is") ? "is" : ops[0], value: "" };
}

function setCondition(index, condition) {
  edit({ conditions: draft.conditions.map((c, i) => (i === index ? condition : c)) });
}

function renderViews() {
  const list = clear($("views"));
  (data.views || []).forEach((view) => {
    const node = button("view" + (view.id === viewId ? " on" : ""));
    node.dataset.id = view.id;
    const row = el("div", "view-row");
    const dot = el("span", "view-dot");
    if (view.id === viewId) dot.style.background = "var(--accent)";
    const name = el("span", "view-name", view.name);
    const drop = button("view-drop", "×");
    drop.title = "Delete this view";
    drop.onclick = (event) => { event.stopPropagation(); confirmDeleteView(event.currentTarget, view); };
    row.append(dot, name, drop);
    node.append(row, el("span", "view-summary", view.summary));
    node.onclick = () => loadView(view);
    list.append(node);
  });
  if (!(data.views || []).length) {
    list.append(el("div", "rail-note", "No saved views yet. Filter the table, then Save as new view — a view is an ordinary mote, so it syncs and shows up in Monarch."));
  }
}

function loadView(view) {
  viewId = view.id;
  saved = JSON.parse(JSON.stringify(view.query));
  draft = JSON.parse(JSON.stringify(view.query));
  sel.clear();
  limit = ROW_PAGE;
  refresh();
}

function renderColumns() {
  const list = clear($("columns"));
  const on = new Set(draft.columns.map(fieldKey));
  const filter = columnFilter.trim().toLowerCase();
  const shown = schema.columns.filter((c) => !filter || c.label.toLowerCase().includes(filter)
                                          || c.key.toLowerCase().includes(filter));
  shown.forEach((column) => {
    const node = button("col-toggle" + (on.has(column.key) ? " on" : ""));
    node.append(el("span", "box"));
    node.append(el("span", "label", column.label));
    if (column.reserved) node.append(el("span", "reserved", "reserved"));
    node.append(el("span", "n", String(column.fill)));
    node.onclick = () => toggleColumn(column);
    list.append(node);
  });
  $("column-count").textContent = `${draft.columns.length} of ${schema.columns.length}`;
}

/** A column turned on goes to the right-hand end, and the columns
 *  already there do not move. Re-sorting them into the schema's own
 *  order was the first attempt and it was worse: turning on one
 *  relationship column reshuffled the whole table, because the schema
 *  lists every builtin before any property key. */
function toggleColumn(column) {
  const on = draft.columns.some((f) => fieldKey(f) === column.key);
  if (on && draft.columns.length === 1) {
    toast("A table needs at least one column.");
    return;
  }
  edit({
    columns: on
      ? draft.columns.filter((f) => fieldKey(f) !== column.key)
      : [...draft.columns, column.field],
  });
}

// ---- the table -----------------------------------------------------

function widthFor(column) {
  const dragged = layout.columnWidths[column.key];
  if (dragged) return `${dragged}px`;
  if (column.field.source === "builtin" && column.field.key === "name") return "minmax(220px, 1.7fr)";
  if (column.kind === "list") return "minmax(150px, 1fr)";
  if (column.kind === "number") return "104px";
  if (column.kind === "date") return "124px";
  return "minmax(120px, 1fr)";
}

function rowTemplate() {
  return `34px ${data.columns.map(widthFor).join(" ")}`;
}

function visibleIds() {
  return data.groups.flatMap((group) => group.rows.map((row) => row.id));
}

function renderTable() {
  const table = clear($("table"));
  const template = rowTemplate();
  table.append(headRow(template));

  let drawn = 0;
  let clipped = false;
  data.groups.forEach((group) => {
    if (drawn >= limit) { clipped = true; return; }
    if (group.labelled) table.append(groupRow(group));
    group.rows.forEach((row) => {
      if (drawn >= limit) { clipped = true; return; }
      table.append(rowNode(row, template));
      drawn++;
    });
  });

  $("table-empty").classList.toggle("hidden", data.matched > 0);
  const more = $("table-more");
  more.classList.toggle("hidden", !clipped);
  more.firstElementChild.textContent = `show ${Math.min(ROW_PAGE, data.matched - drawn)} more of ${data.matched - drawn} remaining`;
}

function headRow(template) {
  const head = el("div", "trow");
  head.id = "head-row";
  head.style.gridTemplateColumns = template;

  const ids = visibleIds();
  const all = ids.length > 0 && ids.every((id) => sel.has(id));
  const box = el("div", "checkcell");
  const check = button("check" + (all ? " on" : ""), all ? "✓" : "");
  check.title = "Select every row this query matches";
  check.onclick = () => {
    if (all) sel.clear(); else ids.forEach((id) => sel.add(id));
    render();
  };
  box.append(check);
  head.append(box);

  data.columns.forEach((column) => {
    const sorted = draft.sort && fieldKey(draft.sort.field) === column.key;
    const right = column.kind === "number";
    const cell = button("hcell" + (sorted ? " sorted" : "") + (right ? " right" : ""));
    cell.append(el("span", null, column.label));
    if (sorted) cell.append(el("span", "arrow", draft.sort.direction === "asc" ? "▲" : "▼"));
    cell.onclick = () => edit({
      sort: {
        field: column.field,
        direction: sorted && draft.sort.direction === "asc" ? "desc" : "asc",
      },
    });
    const resizer = el("div", "col-resizer");
    attachColumnResizer(resizer, column);
    cell.append(resizer);
    head.append(cell);
  });
  return head;
}

function groupRow(group) {
  const node = el("div", "group-row");
  const swatch = el("span", "swatch");
  swatch.style.background = group.label === "unset" ? "oklch(0.34 0 0)" : typeColor(group.label);
  node.append(swatch, el("span", "label", group.label || "unset"), el("span", "n", String(group.count)));
  node.append(el("div", "spacer"));
  const pick = button("pick", "select all");
  pick.onclick = () => { group.rows.forEach((row) => sel.add(row.id)); render(); };
  node.append(pick);
  return node;
}

function rowNode(row, template) {
  const node = el("div", "trow row" + (sel.has(row.id) ? " on" : ""));
  node.style.gridTemplateColumns = template;
  // Identity on the node, not position: the smoke test and any future
  // debugging address a row by the mote it draws, and rows reorder on
  // every sort.
  node.dataset.id = row.id;

  const box = el("div", "checkcell");
  const on = sel.has(row.id);
  const check = button("check" + (on ? " on" : ""), on ? "✓" : "");
  check.onclick = (event) => toggleRow(row.id, event.shiftKey);
  box.append(check);
  node.append(box);

  row.cells.forEach((cell, index) => node.append(cellNode(row, cell, data.columns[index])));

  // Right-click a row for the same things the "i" button and the bulk
  // bar offer, aimed at just this mote.
  node.oncontextmenu = (event) => {
    event.preventDefault();
    ContextMenu.open(event.clientX, event.clientY, [
      ...ContextMenu.inspectItems(row.id, node),
      "-",
      { label: sel.has(row.id) ? "Deselect" : "Select", run: () => toggleRow(row.id, false) },
      { label: "Delete mote", danger: true, run: () => deleteMotes([row.id]) },
    ]);
  };

  const inspectBtn = button("inspect-btn", "i");
  inspectBtn.onclick = (event) => {
    event.stopPropagation();
    Inspector.open(row.id, { anchor: inspectBtn, newTab: event.ctrlKey || event.metaKey });
  };
  node.append(inspectBtn);

  return node;
}

/** Shift-click selects the range between this row and the last one
 *  clicked, in the order the rows are *drawn* -- which is the order the
 *  query put them in, groups included, so a range never jumps a header. */
function toggleRow(id, extend) {
  const ids = visibleIds();
  if (extend && lastRow && ids.includes(lastRow)) {
    const a = ids.indexOf(lastRow);
    const b = ids.indexOf(id);
    ids.slice(Math.min(a, b), Math.max(a, b) + 1).forEach((each) => sel.add(each));
  } else if (sel.has(id)) {
    sel.delete(id);
  } else {
    sel.add(id);
  }
  lastRow = id;
  render();
}

function cellNode(row, cell, column) {
  const isName = column.field.source === "builtin" && column.field.key === "name";
  const node = button("cell"
    + (cell.empty ? " empty" : "")
    + (cell.editable ? "" : " readonly")
    + (isName ? " name-cell" : "")
    + (column.kind === "number" ? " right" : ""));
  node.dataset.col = column.key;

  if (isName || column.field.key === "species") {
    const dot = el("span", "dot");
    dot.style.background = colorOf(row);
    node.append(dot);
  }
  const chip = !cell.empty && column.values.length > 0 && column.values.length <= CHIP_VALUES
    && column.kind !== "number";
  node.append(el("span", chip ? "text chip" : "text", cell.empty ? "—" : cell.text));

  if (cell.editable) {
    node.onclick = (event) => {
      event.stopPropagation();
      editCell(event.currentTarget, row, cell, column);
    };
  }
  return node;
}

/** Which motes an edit lands on: the whole selection when this row is
 *  part of it, otherwise just this row. Clicking a cell outside the
 *  selection edits that one mote and leaves the selection alone -- the
 *  alternative silently writes 40 motes because a click missed. */
function targets(rowId) {
  if (sel.size > 1 && sel.has(rowId)) return [...sel];
  return [rowId];
}

// ---- editing a cell -------------------------------------------------

function editCell(anchor, row, cell, column) {
  const ids = targets(row.id);
  if (column.field.source === "rel") { editLinks(anchor, row, cell, column, ids); return; }

  const key = column.field.source === "builtin" ? "name" : column.field.key;
  const noun = ids.length > 1 ? `${ids.length} motes` : "1 mote";
  const options = column.values.map((value) => ({
    label: value,
    on: !cell.empty && cell.text === value,
    dot: column.values.length <= CHIP_VALUES ? typeColor(value) : null,
    run: () => setProperty(ids, key, value),
  }));
  options.push({ label: "clear value", hint: "×", run: () => clearProperty(ids, key) });

  openPop(anchor, {
    title: `${column.label} · ${noun}`,
    options,
    form: {
      value: cell.empty ? "" : cell.text,
      placeholder: column.kind === "date" ? "2026-09-04" : "value",
      commit: (text) => (text ? setProperty(ids, key, text) : clearProperty(ids, key)),
    },
  });
}

async function setProperty(ids, key, value) {
  closePop();
  const answer = await call(api.set_property(ids, key, value), "Could not write that property.");
  if (answer === null) return;
  toast(answer.written === 1 ? `${key} set on 1 mote.` : `${key} set on ${answer.written} motes.`);
  refresh();
}

async function clearProperty(ids, key) {
  closePop();
  const answer = await call(api.clear_property(ids, key), "Could not clear that property.");
  if (answer === null) return;
  toast(`${key} cleared on ${answer.cleared} mote${answer.cleared === 1 ? "" : "s"}.`);
  refresh();
}

/** A relationship cell. Its options are the links that are there (click
 *  one to erase it) and a search for one more -- the same two writes the
 *  matrix makes, reached from the row instead of the grid. */
function editLinks(anchor, row, cell, column, ids) {
  const type = column.field.key;
  const outgoing = column.field.direction === "out";
  const names = cell.text ? cell.text.split(", ") : [];
  const options = cell.ids.map((otherId, index) => ({
    label: names[index] || otherId,
    hint: "unlink",
    dot: typeColor(type),
    run: () => writeLinks("unrelate", pairsFor([row.id], otherId, outgoing), type),
  }));
  options.push({
    label: "link to a mote…",
    run: () => searchPop(anchor, `${ids.length > 1 ? ids.length + " motes" : row.name} ${type} …`,
                         (mote) => writeLinks("relate", pairsFor(ids, mote.id, outgoing), type)),
  });
  openPop(anchor, { title: `${column.label} · ${row.name}`, options });
}

function pairsFor(ids, otherId, outgoing) {
  return ids.map((id) => (outgoing ? [id, otherId] : [otherId, id]));
}

async function writeLinks(action, pairs, type) {
  closePop();
  const answer = await call(
    action === "relate" ? api.relate(pairs, type) : api.unrelate(pairs, type),
    "Could not write those relationships.");
  if (answer === null) return;
  const n = action === "relate" ? answer.written : answer.removed;
  toast(n === 0
    ? (action === "relate" ? "Those relationships already exist." : "Nothing to unlink there.")
    : `${n} relationship${n === 1 ? "" : "s"} ${action === "relate" ? "written" : "erased"} · ${type}`);
  refresh();
}

// ---- the footer and the bulk bar ------------------------------------

function renderFooter() {
  $("stat-rows").textContent = `${data.matched} of ${data.total} motes`;
  $("stat-sel").textContent = `${sel.size} selected`;
  $("stat-sort").textContent = draft.sort
    ? `sorted by ${labelFor(draft.sort.field)} ${draft.sort.direction}`
    : "unsorted";
  $("hint").textContent = sel.size > 1
    ? "editing any cell in the selection writes to every selected mote"
    : "shift-click a checkbox to select a range · hover a row for its inspector";
}

function renderBulk() {
  const bar = $("bulkbar");
  bar.classList.toggle("hidden", sel.size === 0);
  if (!sel.size) return;
  $("bulk-count").textContent = `${sel.size} selected`;

  const actions = clear($("bulk-actions"));
  const ids = [...sel];

  const set = button("bulk-btn", "Set property");
  set.onclick = (event) => pickColumn(event.currentTarget, "set which property",
    (column) => column.editable && column.field.source !== "rel",
    (field) => {
      const column = columnFor(field);
      const key = field.source === "builtin" ? "name" : field.key;
      openPop(set, {
        title: `set ${column.label.toLowerCase()} · ${ids.length} motes`,
        options: column.values.map((value) => ({
          label: value, dot: typeColor(value), run: () => setProperty(ids, key, value),
        })),
        form: { value: "", placeholder: "value", commit: (text) => setProperty(ids, key, text) },
      });
    });
  actions.append(set);

  const clearBtn = button("bulk-btn", "Clear property");
  clearBtn.onclick = (event) => pickColumn(event.currentTarget, "clear which property",
    (column) => column.editable && column.field.source !== "rel",
    (field) => clearProperty(ids, field.source === "builtin" ? "name" : field.key));
  actions.append(clearBtn);

  const link = button("bulk-btn link", "Link…");
  link.onclick = (event) => pickRelationshipType(event.currentTarget, ids);
  actions.append(link);

  const remove = button("bulk-btn danger", "Delete");
  remove.onclick = () => deleteMotes(ids);
  actions.append(remove);

  $("bulk-deselect").onclick = () => { sel.clear(); render(); };
}

/** The three ways a selection becomes relationships. Chain and fan-in
 *  are why bulk linking is worth having at all: 30 imported motes into a
 *  dependency chain is one gesture here and 29 in the graph window. */
function pickRelationshipType(anchor, ids) {
  const types = data.matrix.types.length ? data.matrix.types : ["depends_on"];
  openPop(anchor, {
    title: `relate ${ids.length} mote${ids.length === 1 ? "" : "s"}`,
    options: types.map((type) => ({
      label: type, dot: typeColor(type), on: type === paintType,
      run: () => pickLinkShape(anchor, ids, type),
    })),
    form: {
      value: "", placeholder: "new relationship type",
      label: "Use",
      commit: (text) => pickLinkShape(anchor, ids, text),
    },
  });
}

function pickLinkShape(anchor, ids, type) {
  const ordered = visibleIds().filter((id) => ids.includes(id));
  const options = [];
  if (ordered.length > 1) {
    options.push({
      label: "chain in row order", hint: `${ordered.length - 1} links`, dot: typeColor(type),
      run: () => writeLinks("relate", ordered.slice(0, -1).map((id, i) => [id, ordered[i + 1]]), type),
    });
    options.push({
      label: "all → the first row", hint: `${ordered.length - 1} links`, dot: typeColor(type),
      run: () => writeLinks("relate", ordered.slice(1).map((id) => [id, ordered[0]]), type),
    });
  }
  options.push({
    label: "all → one mote…",
    run: () => searchPop(anchor, `${ordered.length} motes ${type} …`,
                         (mote) => writeLinks("relate", ordered.map((id) => [id, mote.id]), type)),
  });
  openPop(anchor, { title: `${ordered.length} motes ${type} …`, options });
}

async function deleteMotes(ids) {
  const answer = await call(api.delete_motes(ids), "Could not delete those motes.");
  if (answer === null) return;
  sel.clear();
  refresh();
  toast(
    `${answer.deleted} mote${answer.deleted === 1 ? "" : "s"} deleted from the garden.`,
    false,
    { label: "Undo", run: () => undoDelete(answer.undo) },
  );
}

async function undoDelete(tokens) {
  const answer = await call(api.undo_delete(tokens), "Could not undo that.");
  if (answer === null) return;
  refresh();
  toast(`${answer.restored} mote${answer.restored === 1 ? "" : "s"} restored.`);
}

// ---- the relationship matrix ----------------------------------------

function renderMatrix() {
  $("matrix").classList.toggle("hidden", !matrixOpen);
  $("matrix-resizer").classList.toggle("hidden", !matrixOpen);
  if (!matrixOpen) return;
  const matrix = data.matrix;
  const types = matrix.types.length ? matrix.types : [paintType];

  const chips = clear($("matrix-types"));
  types.forEach((type) => {
    const chip = button("type-chip" + (type === paintType ? " on" : ""));
    const dot = el("span", "dot");
    dot.style.background = typeColor(type);
    chip.append(dot, el("span", null, type));
    chip.onclick = () => { paintType = type; persist(); renderMatrix(); };
    chips.append(chip);
  });

  const edges = new Map();
  matrix.edges.forEach((edge) => edges.set(`${edge.from}:${edge.to}`, edge));
  $("matrix-note").textContent =
    `${matrix.edges.length} between these rows · painting ${paintType} · click a cell to write or erase`
    + (matrix.shown < matrix.rows ? ` · first ${matrix.shown} of ${matrix.rows} rows` : "");

  const scroll = clear($("matrix-scroll"));
  scroll.append(matrixHead(matrix));
  matrix.motes.forEach((mote, i) => scroll.append(matrixRow(matrix, mote, i, edges)));
}

function matrixHead(matrix) {
  const head = el("div", "matrix-head-row");
  head.append(el("div", "matrix-corner"));
  matrix.motes.forEach((mote, j) => {
    const cell = el("div", "matrix-colhead" + (sel.has(mote.id) ? " on" : ""), String(j + 1));
    cell.title = mote.name;
    head.append(cell);
  });
  return head;
}

function matrixRow(matrix, mote, i, edges) {
  const row = el("div", "matrix-row" + (sel.has(mote.id) ? " on" : ""));
  const label = el("div", "matrix-label");
  const dot = el("span", "dot");
  dot.style.background = colorOf(mote);
  label.append(el("span", "idx", String(i + 1)), dot, el("span", "name", mote.name));
  row.append(label);

  matrix.motes.forEach((other, j) => {
    const edge = edges.get(`${i}:${j}`);
    const cell = button("matrix-cell" + (i === j ? " self" : "") + (edge ? " linked" : ""),
                        edge ? glyphFor(edge.type) : "");
    cell.dataset.from = String(i);
    cell.dataset.to = String(j);
    if (edge) {
      cell.style.color = typeColor(edge.type);
      cell.title = `${mote.name} ${edge.type} ${other.name} — click to erase`;
    } else if (i !== j) {
      cell.title = `${mote.name} ${paintType} ${other.name} — click to write`;
    }
    if (i !== j) {
      cell.onclick = () => (edge
        ? writeLinks("unrelate", [[mote.id, other.id]], edge.type)
        : writeLinks("relate", [[mote.id, other.id]], paintType));
    }
    row.append(cell);
  });
  return row;
}

/** A glyph per relationship type, so the matrix reads without a legend.
 *  Arbitrary but stable: the same type always draws the same mark. */
const GLYPHS = ["→", "⇥", "⊃", "·→", "◇", "∴", "⊕", "≡"];
function glyphFor(type) {
  return GLYPHS[hashString(type) % GLYPHS.length];
}

// ---- the one popover ------------------------------------------------
//
// Every picker in this window is this popover: cell editors, the
// condition chips' three menus, the column picker, the bulk actions and
// the view delete confirmation. One implementation because they are one
// interaction -- a list of options, sometimes with a text field under it
// -- and five copies of it would drift apart by the third.

function openPop(anchor, spec) {
  popState = spec;
  const pop = $("pop");
  $("pop-title").textContent = spec.title || "";

  const options = clear($("pop-options"));
  (spec.options || []).forEach((option) => {
    const node = button("pop-option" + (option.on ? " on" : "") + (option.danger ? " danger" : ""));
    if (option.dot) {
      const dot = el("span", "dot");
      dot.style.background = option.dot;
      node.append(dot);
    }
    node.append(el("span", "label", option.label));
    if (option.hint) node.append(el("span", "hint", option.hint));
    node.onclick = () => option.run();
    options.append(node);
  });

  const note = $("pop-note");
  note.textContent = spec.note || "";
  note.classList.toggle("hidden", !spec.note);
  note.style.whiteSpace = spec.note && spec.note.includes("\n") ? "pre-wrap" : "normal";
  note.style.fontFamily = spec.note && spec.note.includes("\n") ? "var(--mono)" : "var(--sans)";

  const form = $("pop-form");
  form.classList.toggle("hidden", !spec.form);
  if (spec.form) {
    const input = $("pop-input");
    input.value = spec.form.value || "";
    input.placeholder = spec.form.placeholder || "";
    input.oninput = spec.form.oninput || null;
    input.onkeydown = (event) => {
      if (event.key === "Enter") spec.form.commit(input.value);
      if (event.key === "Escape") closePop();
    };
    const commit = $("pop-commit");
    commit.textContent = spec.form.label || "Set";
    commit.classList.toggle("danger", !!spec.form.danger);
    commit.onclick = () => spec.form.commit(input.value);
  }

  pop.classList.toggle("wide", !!spec.centered);
  pop.classList.remove("hidden");
  $("pop-shade").classList.remove("hidden");
  place(pop, anchor, spec.centered);
  if (spec.form) $("pop-input").focus();
}

/** Anchored under whatever was clicked, flipped above it when there is
 *  no room below, and never off the edge -- the mockup's own rule. */
function place(pop, anchor, centered) {
  const rect = anchor.getBoundingClientRect();
  const height = pop.offsetHeight || 300;
  if (centered) {
    pop.style.left = `${Math.max(8, (window.innerWidth - pop.offsetWidth) / 2)}px`;
    pop.style.top = `${Math.max(8, (window.innerHeight - height) / 2)}px`;
    return;
  }
  const left = Math.min(Math.max(8, rect.left), window.innerWidth - pop.offsetWidth - 8);
  const below = rect.bottom + 6;
  const top = below + height > window.innerHeight ? Math.max(8, rect.top - height - 6) : below;
  pop.style.left = `${left}px`;
  pop.style.top = `${top}px`;
}

function closePop() {
  popState = null;
  $("pop").classList.add("hidden");
  $("pop-shade").classList.add("hidden");
}

function popIsOpen() { return popState !== null; }

/** The column picker, used by the condition chips, the group chips and
 *  both bulk property actions. `accept` narrows it -- a bulk edit cannot
 *  set a degree, so it never offers one. */
function pickColumn(anchor, title, accept, run) {
  openPop(anchor, {
    title,
    options: schema.columns.filter(accept).map((column) => ({
      label: column.label,
      hint: column.reserved ? "reserved" : String(column.fill),
      run: () => { closePop(); run(column.field); },
    })),
  });
}

function pickOp(anchor, condition, index) {
  openPop(anchor, {
    title: "operator",
    options: opsFor(condition.field).map((op) => ({
      label: op, on: op === condition.op,
      run: () => { closePop(); setCondition(index, { ...condition, op }); },
    })),
  });
}

function pickValue(anchor, condition, index) {
  const column = columnFor(condition.field);
  const commit = (value) => { closePop(); setCondition(index, { ...condition, value }); };
  openPop(anchor, {
    title: `${labelFor(condition.field)} ${condition.op}`,
    options: (column ? column.values : []).map((value) => ({
      label: value, on: value === condition.value, run: () => commit(value),
    })),
    form: { value: condition.value, placeholder: "value", commit },
  });
}

/** Search the whole garden for one mote. core/motes.py::search_motes is
 *  the same search Monarch's palette uses, so the same words find the
 *  same motes in both windows. */
function searchPop(anchor, title, pick) {
  let timer = null;
  const run = (results) => openPop(anchor, {
    title,
    options: results.map((mote) => ({
      label: mote.name, hint: mote.speciesName || "", dot: colorOf(mote),
      run: () => pick(mote),
    })),
    form: {
      value: $("pop-input") && popIsOpen() ? $("pop-input").value : "",
      placeholder: "search motes…",
      label: "Find",
      oninput: (event) => {
        clearTimeout(timer);
        const text = event.target.value;
        timer = setTimeout(async () => {
          const found = await call(api.search(text), "Could not search.");
          if (found) run(found);
        }, SEARCH_DEBOUNCE_MS);
      },
      commit: () => {},
    },
  });
  run([]);
}

// ---- saved views ----------------------------------------------------

function saveAsNewView(anchor) {
  openPop(anchor, {
    title: "name the view",
    note: "A view is an ordinary mote carrying this query, so it syncs with the garden and shows up in every other window.",
    form: {
      value: "", placeholder: "e.g. Blocked, high priority", label: "Save",
      commit: async (text) => {
        closePop();
        const answer = await call(api.save_view(text.trim() || "Untitled view", draft),
                                  "Could not save that view.");
        if (answer === null) return;
        viewId = answer.id;
        saved = JSON.parse(JSON.stringify(draft));
        toast(`Saved view “${text.trim() || "Untitled view"}”.`);
        refresh();
      },
    },
  });
}

async function updateView() {
  const view = (data.views || []).find((v) => v.id === viewId);
  if (!view) return;
  const answer = await call(api.save_view(view.name, draft, viewId), "Could not update that view.");
  if (answer === null) return;
  saved = JSON.parse(JSON.stringify(draft));
  toast(`View “${view.name}” updated.`);
  refresh();
}

function confirmDeleteView(anchor, view) {
  openPop(anchor, {
    title: `delete view “${view.name}”`,
    note: "The saved query is deleted; nothing it selected is touched.",
    options: [{
      label: "Delete this view", danger: true,
      run: async () => {
        closePop();
        const answer = await call(api.delete_view(view.id), "Could not delete that view.");
        if (answer === null) return;
        if (viewId === view.id) { viewId = null; saved = null; }
        toast(`View “${view.name}” deleted.`);
        refresh();
      },
    }],
  });
}

// ---- garden menu ----------------------------------------------------

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
  clear(menu);
  gardens.forEach((name) => {
    const item = button(name === data.garden ? "on" : null, name);
    item.onclick = async () => {
      closeGardenMenu();
      const answer = await call(api.switch_garden(name), "Could not switch garden.");
      if (answer === null) return;
      // Another garden is another set of property keys, so this one's
      // columns and conditions mean nothing there -- start from whatever
      // that garden was last showing, or from its own discovered default.
      sel.clear();
      draft = null;
      viewId = null;
      saved = null;
      restore(answer.garden);
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

// ---- wiring ---------------------------------------------------------

function wire() {
  $("garden-btn").onclick = openGardenMenu;
  document.addEventListener("click", (event) => {
    if (!event.target.closest(".topbar-menu")) closeGardenMenu();
  });

  $("add-cond").onclick = (event) => {
    const field = (schema.columns.find((c) => c.field.source === "property") || schema.columns[0]).field;
    const conditions = [...draft.conditions, resetCondition(field)];
    draft = { ...draft, conditions };
    render();
    pickColumn(event.currentTarget, "property", () => true,
               (picked) => setCondition(conditions.length - 1, resetCondition(picked)));
  };

  $("save-view").onclick = (event) => saveAsNewView(event.currentTarget);
  $("update-view").onclick = updateView;
  $("clear-view").onclick = () => {
    viewId = null;
    saved = null;
    edit({ conditions: [], group: null });
  };

  $("column-filter").oninput = (event) => { columnFilter = event.target.value; renderColumns(); };

  $("matrix-toggle").onclick = () => { matrixOpen = !matrixOpen; persist(); render(); };
  $("matrix-close").onclick = () => { matrixOpen = false; persist(); render(); };

  $("table-more").firstElementChild.onclick = () => { limit += ROW_PAGE; renderTable(); };

  $("new-mote").onclick = async () => {
    const answer = await call(api.create_mote({ name: "New mote" }), "Could not create a mote.");
    if (answer === null) return;
    sel.clear();
    sel.add(answer.id);
    toast("New mote created — it is selected, and the name cell edits it.");
    refresh();
  };

  $("pop-shade").onclick = closePop;

  $("refresh-btn").onclick = async () => {
    const icon = $("refresh-btn");
    icon.classList.add("spinning");
    const answer = await call(api.refresh(), "Could not refresh.");
    icon.classList.remove("spinning");
    if (answer && answer.synced) toast("Synced.");
    refresh();
  };

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

  $("table-scroll").addEventListener("scroll", (event) => {
    const node = event.target;
    if (node.scrollTop + node.clientHeight > node.scrollHeight - 400 && limit < data.matched) {
      limit += ROW_PAGE;
      renderTable();
    }
  });

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
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "a" && !typing) {
      event.preventDefault();
      visibleIds().forEach((id) => sel.add(id));
      render();
      return;
    }
    if (event.key === "Escape") {
      if (popIsOpen()) { closePop(); return; }
      if (launcherIsOpen()) { closeLauncher(); return; }
      if (sel.size) { sel.clear(); render(); }
      return;
    }
    if (typing) return;
    if (event.key.toLowerCase() === "r") { matrixOpen = !matrixOpen; persist(); render(); }
  });

  // Refetch when the window comes back: another MonOSS window may have
  // been writing to the same garden while this one sat idle.
  window.addEventListener("focus", () => refresh());
}

// ---- app switcher (⌘J) ----
//
// Every sibling app but this one, in a fixed order -- so "Monastery is
// ⌘3" holds regardless of what's typed into the filter box. See
// api_common.py::APPS for the id each maps to on the Python side.
const LAUNCHER_COLUMNS = 2;
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
    desc: "Every dated mote on a week or a month.",
    icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
      <rect x="7" y="9" width="26" height="24" rx="3" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></rect>
      <rect x="7" y="9" width="26" height="7" rx="3" fill="var(--fg)" opacity="0.25"></rect>
      <rect x="12" y="21" width="6" height="5" rx="1" fill="var(--accent)"></rect>
      <rect x="22" y="21" width="6" height="5" rx="1" fill="var(--fg)" opacity="0.35"></rect>
    </svg>`,
  },
  {
    id: "momentum", name: "Momentum", kind: "tasks",
    desc: "A board, a week and a focus timer over the garden's tasks.",
    icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
      <rect x="7" y="8" width="9" height="24" rx="2.5" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></rect>
      <rect x="20" y="8" width="9" height="16" rx="2.5" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></rect>
      <rect x="9.5" y="11" width="4" height="3" rx="1" fill="var(--accent)"></rect>
      <rect x="22.5" y="11" width="4" height="3" rx="1" fill="var(--fg)" opacity="0.4"></rect>
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
  // Whatever is selected is what "open this over there" means -- one row
  // opens that mote in the sibling window; several open none of them.
  const focus = sel.size === 1 ? [...sel][0] : null;
  await call(api.open_app(appId, focus), `Could not open ${app.name}.`);
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
  const garden = await api.current_garden();
  restore(garden);
  loadLayout();
  applyLayout();
  wire();
  wireResizers();
  await refresh();
}

window.addEventListener("pywebviewready", init);
