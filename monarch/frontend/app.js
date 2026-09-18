// monarch/frontend/app.js
//
// Wiring between monarch/api.py and the canvas graph in graph.js. State lives
// here; graph.js owns only rendering, layout and pointer gestures.

let api;
let graph;
let currentGarden = null;
let focusId = null;
let payload = { motes: [], relationships: [], kinds: {} };
let allMotes = [];
let quickAddSpeciesId = null;
let searchVisible = [];
let searchHighlight = -1;
const searchSpeciesFilter = new Set();
const searchRelFilter = new Set();
let multiSelectIds = null;             // non-null while the aggregate panel is open
let clipboard = null;                  // array of {sourceId, properties, offset} from Copy
let clipboardRelationships = null;     // every relationship touching a copied mote, original ids
let pasteRepeatCount = 0;              // steps repeated Ctrl+V pastes apart so they don't stack
let contextMenuWorldPos = null;        // world coords the context menu was opened at
let reservedProperties = new Set();    // property keys recognized by convention (core/reserved.py)
let reservedRelTypes = new Set();      // relationship types recognized by convention
let reservedMeanings = {};             // reserved name -> what it means, for tooltips
let propertyModelProviders = {};       // provider name -> description + capabilities
let openMenuName = null;               // 'garden' | 'file' | 'view' | 'tools' | null -- which topbar dropdown is open
let viewSettings = null;               // this garden's advanced view settings (forces/colors/attractors)
let activeViewFilterId = null;         // id of the Monolith saved view currently filtering the graph, or null
let attractorMode = false;             // arranging species attractors -- read-only

const DEPTH_KEY = "monoss:depth";
const DETAIL_WIDTH_KEY = "monoss:detailWidth";
const DETAIL_WIDTH_MIN = 280;
const DETAIL_WIDTH_MAX = 640;
const CONTROL_PANEL_KEY = "monoss:controlPanel";

window.addEventListener("error", (e) => {
  console.error(e.error || e.message);
  showToast(String((e.error && e.error.message) || e.message));
});
window.addEventListener("unhandledrejection", (e) => {
  console.error(e.reason);
  showToast(String((e.reason && e.reason.message) || e.reason));
});

// ---- small helpers ----

const $ = (id) => document.getElementById(id);

function positionsKey(garden) { return `monoss:positions:${garden}`; }
function lastFocusKey(garden) { return `monoss:lastFocus:${garden}`; }
function hiddenSpeciesKey(garden) { return `monoss:hiddenSpecies:${garden}`; }
function hiddenRelKey(garden) { return `monoss:hiddenRels:${garden}`; }
function viewFilterKey(garden) { return `monoss:viewFilter:${garden}`; }
function viewSettingsKey(garden) { return `monoss:viewSettings:${garden}`; }

function readJSON(key, fallback) {
  try { return JSON.parse(localStorage.getItem(key)) ?? fallback; }
  catch { return fallback; }
}

function loadDepth() {
  const stored = localStorage.getItem(DEPTH_KEY);
  if (stored === null) return null;          // whole garden is the default
  if (stored === "all") return null;
  const hops = parseInt(stored, 10);
  return Number.isFinite(hops) && hops > 0 ? hops : null;
}

function saveDepth(hops) {
  localStorage.setItem(DEPTH_KEY, hops === null ? "all" : String(hops));
}

function showToast(message, durationMs = 3500) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.remove("hidden");
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => toast.classList.add("hidden"), durationMs);
}

let lastUndo = null; // { perform: async () => {...} } while an undo is available

function showUndoToast(message, perform, durationMs = 6000) {
  const toast = $("toast");
  toast.textContent = "";
  toast.append(document.createTextNode(message + "  "));
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "linkish";
  btn.textContent = "Undo";
  btn.addEventListener("click", runUndo);
  toast.append(btn);
  toast.classList.remove("hidden");
  lastUndo = { perform };
  clearTimeout(showToast._timer);
  showToast._timer = setTimeout(() => { toast.classList.add("hidden"); lastUndo = null; }, durationMs);
}

/** The one read-only mode, as a single gate in front of every mutation.
 *  Arranging attractors is read-only because what's on screen is a
 *  layout tool, not the editable graph.
 *
 *  This used to also gate a simulation preview, back when previewing
 *  happened in this window; simulation now lives in Motion, which holds
 *  its own connection and never leaves this graph in a projected state.
 *  Kept as a gate rather than inlined into its one caller: it is the
 *  place a second read-only mode goes. */
function editBlocked() {
  if (attractorMode) {
    showToast("Arranging attractors — click Done arranging to edit.", 2600);
    return true;
  }
  return false;
}

async function runUndo() {
  if (!lastUndo) return;
  if (editBlocked()) return;
  const { perform } = lastUndo;
  lastUndo = null;
  $("toast").classList.add("hidden");
  clearTimeout(showToast._timer);
  await perform();
}

async function call(promise) {
  try { return await promise; }
  catch (err) { showToast(String(err.message || err)); throw err; }
}

function nodeLabel(mote) {
  const props = mote.properties || {};
  if (typeof props.name === "string" && props.name.trim()) return props.name;
  const keys = Object.keys(props).sort();
  if (keys.length > 0) {
    const key = keys[0];
    const value = typeof props[key] === "string" ? props[key] : JSON.stringify(props[key]);
    return `${key}: ${value}`;
  }
  return mote.id.slice(0, 8) + "…";
}

function moteById(id) {
  return payload.motes.find((m) => m.id === id) || allMotes.find((m) => m.id === id) || null;
}

/** Label for whatever a mote *is*: the kinds core says it is, else its
 *  species names, else plain "Mote". Shown above the title in the detail
 *  panel and in search results.
 *
 *  Every kind comes from core/kinds.py's table via the payload, so this
 *  has no partial idea of what exists of its own -- it used to know only
 *  Species and Rule (that last one by sniffing for an `equation` property
 *  itself) and had no idea what a template or property model was. */
function kindLabel(mote) {
  if (!mote) return "";
  const kinds = kindsOf(mote.id);
  if (kinds.length) return kinds.map((k) => KIND_LABEL[k] || k).join(" · ");
  const species = speciesIdsOf(mote.id).map(moteById).filter(Boolean).map(nodeLabel);
  if (species.length) return species.join(", ");
  return "Mote";
}

/** The kinds core assigned this mote, or [] for an ordinary one --
 *  motes of no particular kind are left out of the payload entirely. */
function kindsOf(moteId) {
  return (payload.kinds && payload.kinds[moteId]) || [];
}

function isKind(moteId, kind) {
  return kindsOf(moteId).includes(kind);
}

// ---- status bar ----

/** The bottom line: which garden, how much of it the current depth put on
 *  screen, and what is selected. Read straight off the payload and
 *  graph.js's own selection sets rather than kept in a parallel variable,
 *  so it cannot drift from what is drawn -- every caller only has to say
 *  "something changed."
 *
 *  `payload` is the depth-limited neighbourhood and `allMotes` is the
 *  whole garden, which is why the mote count says "of N" only when the
 *  two disagree: at whole-garden depth a second number would be the same
 *  number twice. */
function updateStatusbar() {
  if (!graph) return;
  const inView = payload.motes.length;
  const total = allMotes.length;
  $("status-garden").textContent = currentGarden || "\u2026";
  $("status-motes").textContent =
    total > inView ? `${inView} of ${total} motes` : plural(inView, "mote");
  $("status-rels").textContent = plural(payload.relationships.length, "relationship");
  const depth = loadDepth();
  $("status-depth").textContent = depth === null ? "whole garden" : `depth ${depth}`;

  const motes = graph.selectedIds.size;
  const rels = graph.selectedRelIds.size;
  $("status-selection").textContent =
    motes > 0 ? `${plural(motes, "mote")} selected`
    : rels > 0 ? `${plural(rels, "relationship")} selected`
    : "";
}

function plural(count, noun) {
  return `${count} ${noun}${count === 1 ? "" : "s"}`;
}

// ---- graph data ----

async function refocus(moteId, { pan = true, select = true } = {}) {
  const next = await call(api.focus(moteId, loadDepth()));
  focusId = next.focus_id;
  // Before setData, so the first paint of this answer already has the
  // right palette: species colours are claimed from the garden's whole
  // species list (which the answer carries), not from the subgraph a hop
  // limit left on the canvas.
  graph.setSpeciesUniverse(next.speciesIds);
  payload = next;
  localStorage.setItem(lastFocusKey(currentGarden), focusId || "");

  graph.setData(next, readJSON(positionsKey(currentGarden), {}));
  // Selection dims everything unrelated, which is what you want after
  // clicking a mote and not what you want when a garden first opens.
  graph.select(select ? focusId : null, { pan });
  $("empty-state").classList.toggle("hidden", !!focusId);
  renderLegend();
  renderRelFilters();
  updateStatusbar();
  // Species and relationship types are what the settings panel is a list
  // of, so it has to follow the same data everything else here does.
  if (viewSettingsOpen()) renderViewSettings();
  return next;
}

async function loadGarden(garden) {
  currentGarden = garden;
  $("garden-label").textContent = garden;
  closeDetailPanel();
  // Attractors are per garden, so an arrangement in progress means
  // nothing once a different one is open.
  if (attractorMode) exitAttractorMode();
  graph.setHiddenSpecies(readJSON(hiddenSpeciesKey(garden), []));
  graph.setHiddenRelTypes(readJSON(hiddenRelKey(garden), []));
  activeViewFilterId = localStorage.getItem(viewFilterKey(garden)) || null;
  viewSettings = loadViewSettings(garden);
  graph.setViewSettings(viewSettings);
  const stored = localStorage.getItem(lastFocusKey(garden)) || null;
  await refreshMoteCache();
  // Frame the whole garden on open rather than panning to the remembered
  // mote -- you want to see where you are before you go anywhere.
  await refocus(stored, { pan: false, select: false });
  graph.fitWhenSettled();
}

async function refreshMoteCache() {
  allMotes = await call(api.list_motes());
  await renderViewFilterRow();
}

function savePositions() {
  localStorage.setItem(positionsKey(currentGarden), JSON.stringify(graph.pinnedPositions()));
}

// ---- legend + filters (view decluttering) ----

function speciesRows() {
  const counts = new Map();
  let untyped = 0;
  for (const mote of payload.motes) {
    if (isKind(mote.id, KIND.SPECIES)) continue;
    const species = speciesIdsOf(mote.id);
    if (species.length === 0) { untyped += 1; continue; }
    for (const id of species) counts.set(id, (counts.get(id) || 0) + 1);
  }
  // speciesColorFor, not colorFor: these rows are about species membership
  // (they're the species filter, and the chips that pick one), which is
  // still true when the canvas is colored by a property or a count.
  const rows = idsWithKind(payload.kinds, KIND.SPECIES).map((id) => ({
    id,
    label: nodeLabel(moteById(id) || { id, properties: {} }),
    count: counts.get(id) || 0,
    color: graph.speciesColorFor(id, { emphatic: true }),
    diamond: true,
  }));
  rows.sort((a, b) => a.label.localeCompare(b.label));
  if (untyped > 0) {
    rows.push({
      id: "__none__", label: "No species", count: untyped,
      color: "oklch(0.62 0.02 250)", diamond: false,
    });
  }
  return rows;
}

function renderLegend() {
  const container = $("legend");
  container.innerHTML = "";
  const rows = speciesRows();
  if (rows.length === 0) {
    container.innerHTML = '<div class="empty-note">No species yet</div>';
    return;
  }
  // A non-species color mode means node color no longer encodes species,
  // so the legend stops presenting itself as the color key -- see the
  // .hollow rule in style.css.
  const hollow = graph.colors.mode !== "species";
  for (const row of rows) {
    const el = document.createElement("div");
    const hidden = graph.hiddenSpecies.has(row.id);
    el.className = "filter-row" + (hidden ? " off" : "");
    el.title = (hidden ? `Show ${row.label}` : `Hide ${row.label}`) + " (shift-click to isolate)";
    el.innerHTML =
      `<span class="dot${row.diamond ? " diamond" : ""}${hollow ? " hollow" : ""}" style="background:${row.color}"></span>` +
      `<span class="name"></span><span class="count">${row.count}</span>`;
    el.querySelector(".name").textContent = row.label;
    el.addEventListener("click", (ev) => {
      const hiddenIds = ev.shiftKey
        ? new Set(rows.filter((r) => r.id !== row.id).map((r) => r.id))
        : new Set(graph.hiddenSpecies);
      if (!ev.shiftKey) hiddenIds.has(row.id) ? hiddenIds.delete(row.id) : hiddenIds.add(row.id);
      graph.setHiddenSpecies(hiddenIds);
      localStorage.setItem(hiddenSpeciesKey(currentGarden), JSON.stringify([...hiddenIds]));
      renderLegend();
    });
    container.appendChild(el);
  }
}

function renderRelFilters() {
  const container = $("rel-filters");
  container.innerHTML = "";
  const counts = new Map();
  for (const rel of payload.relationships) {
    counts.set(rel.type, (counts.get(rel.type) || 0) + 1);
  }
  const types = [...counts.keys()].sort();
  $("rel-heading").classList.toggle("hidden", types.length === 0);
  for (const type of types) {
    const el = document.createElement("div");
    const hidden = graph.hiddenRelTypes.has(type);
    el.className = "filter-row" + (hidden ? " off" : "");
    el.title = (hidden ? `Show ${type}` : `Hide ${type}`) + " (shift-click to isolate)";
    el.innerHTML = '<span class="name"></span><span class="count"></span>';
    el.querySelector(".name").textContent = type;
    el.querySelector(".count").textContent = counts.get(type);
    el.addEventListener("click", (ev) => {
      const hiddenTypes = ev.shiftKey
        ? new Set(types.filter((t) => t !== type))
        : new Set(graph.hiddenRelTypes);
      if (!ev.shiftKey) hiddenTypes.has(type) ? hiddenTypes.delete(type) : hiddenTypes.add(type);
      graph.setHiddenRelTypes(hiddenTypes);
      localStorage.setItem(hiddenRelKey(currentGarden), JSON.stringify([...hiddenTypes]));
      renderRelFilters();
    });
    container.appendChild(el);
  }
}

/** Shared by both "Reset view" entry points (the View menu and the
 *  control panel's own linkish button). Recentering the camera doesn't
 *  put back anything species/relationship filters took off the graph, so
 *  a reset that leaves half the garden hidden doesn't feel like a reset --
 *  clear those filters too, the same way it clears pinned/attractor
 *  layout via graph.resetView(). */
function resetViewAndUnhideAll() {
  graph.setHiddenSpecies(new Set());
  graph.setHiddenRelTypes(new Set());
  localStorage.removeItem(hiddenSpeciesKey(currentGarden));
  localStorage.removeItem(hiddenRelKey(currentGarden));
  renderLegend();
  renderRelFilters();
  graph.resetView();
  $("view-filter-select").value = "";
  applyViewFilter(null);
}

// ---- saved view filter ----
//
// A saved view (monolith/api.py::save_view) is an ordinary mote carrying
// a `query` property -- core/query.py's language, not this app's. Rather
// than teach the frontend that language, evaluating one is a single
// round trip to Api.view_filter_ids(), which asks core/query.py to name
// the matching motes; this side only has to know a query mote when it
// sees one (hasQuery, mirroring core/query.py::has_query) and turn the
// answer into graph.js's allow-list.

function hasQuery(properties) {
  const q = properties && properties.query;
  return typeof q === "string" || (typeof q === "object" && q !== null && !Array.isArray(q));
}

function savedViewRows() {
  return allMotes
    .filter((m) => hasQuery(m.properties))
    .map((m) => ({ id: m.id, name: (m.properties && m.properties.name) || "Untitled view" }))
    .sort((a, b) => a.name.localeCompare(b.name));
}

/** Rebuilds the picker off the current whole-garden mote cache, then
 *  re-applies whichever view is selected -- called after every
 *  refreshMoteCache() so a view that stopped existing (or whose query
 *  now matches different motes) self-corrects instead of drifting. */
async function renderViewFilterRow() {
  const rows = savedViewRows();
  $("view-filter-row").classList.toggle("hidden", rows.length === 0);
  const select = $("view-filter-select");
  select.innerHTML = '<option value="">No filter</option>';
  for (const row of rows) {
    const opt = document.createElement("option");
    opt.value = row.id;
    opt.textContent = row.name;
    select.appendChild(opt);
  }
  if (activeViewFilterId && !rows.some((r) => r.id === activeViewFilterId)) {
    activeViewFilterId = null;
    if (currentGarden) localStorage.removeItem(viewFilterKey(currentGarden));
  }
  select.value = activeViewFilterId || "";
  await applyViewFilter(activeViewFilterId, { persist: false });
}

/** Fetches the chosen saved view's matches and pushes them into the
 *  graph as an allow-list, or lifts the filter for a null/empty id.
 *  `persist: false` is renderViewFilterRow's own re-apply, which must
 *  not overwrite the stored choice with itself mid-reconciliation. */
async function applyViewFilter(viewId, { persist = true } = {}) {
  activeViewFilterId = viewId || null;
  if (persist && currentGarden) {
    if (activeViewFilterId) localStorage.setItem(viewFilterKey(currentGarden), activeViewFilterId);
    else localStorage.removeItem(viewFilterKey(currentGarden));
  }
  if (!activeViewFilterId) {
    graph.setViewFilter(null);
    updateStatusbar();
    return;
  }
  const ids = await call(api.view_filter_ids(activeViewFilterId));
  if (ids === null) {
    activeViewFilterId = null;
    if (currentGarden) localStorage.removeItem(viewFilterKey(currentGarden));
    $("view-filter-select").value = "";
    graph.setViewFilter(null);
    showToast("That saved view no longer exists.");
  } else {
    graph.setViewFilter(ids);
  }
  updateStatusbar();
}

// ---- advanced view settings ----
//
// Per-garden control over how the graph lays itself out and colors
// itself. Stored in localStorage beside the hidden-species and pinned-
// position keys, because this is view state, not garden data -- it stays
// on this machine and doesn't travel with an export. Only what's actually
// been changed is written: a garden nobody has adjusted has no key at all
// and renders on graph.js's own defaults.
//
// The panel is docked rather than modal because every control here is
// judged by what it does to the graph, so the graph has to stay visible
// while you drag a slider.

const FORCE_SLIDERS = [
  { key: "repulsion", label: "Repulsion", min: 200, max: 6000, step: 50, decimals: 0,
    hint: "How hard every mote pushes every other mote away" },
  { key: "anchorPull", label: "Species pull", min: 0, max: 0.08, step: 0.002, decimals: 3,
    hint: "How strongly a mote is drawn to its species' attractor" },
  { key: "edgeLength", label: "Edge length", min: 20, max: 260, step: 5, decimals: 0,
    hint: "The distance a relationship's spring is happy at" },
];

const REL_STRENGTH_MAX = 0.12;
const REL_STRENGTH_STEP = 0.002;

const COLOR_MODE_NOTES = {
  property: "Each distinct value gets its own hue. Motes without the property stay grey.",
  degree: "Dim = few relationships, bright = many.",
  numeric: "Dim = lowest value on screen, bright = highest. Non-numeric motes stay grey.",
};

function defaultViewSettings() {
  return MonossGraph.defaults();
}

function loadViewSettings(garden) {
  const stored = readJSON(viewSettingsKey(garden), {}) || {};
  const base = defaultViewSettings();
  const forces = stored.forces || {};
  const colors = stored.colors || {};
  return {
    forces: { ...base.forces, ...forces, relStrength: { ...(forces.relStrength || {}) } },
    colors: { ...base.colors, ...colors, species: { ...(colors.species || {}) } },
    attractors: { ...(stored.attractors || {}) },
  };
}

function saveViewSettings() {
  localStorage.setItem(viewSettingsKey(currentGarden), JSON.stringify(viewSettings));
}

/** Push settings into the graph, persist them, and rebuild whatever
 *  displays them. Only for discrete changes (a mode switch, a reset, an
 *  attractor drop): a slider or color picker mid-gesture updates the
 *  graph directly instead, since rebuilding the panel's DOM under the
 *  pointer would end the gesture halfway through. */
function applyViewSettings() {
  graph.setViewSettings(viewSettings);
  saveViewSettings();
  renderLegend();
  renderViewSettings();
}

function viewSettingsOpen() {
  return !$("view-settings-panel").classList.contains("hidden");
}

function openViewSettings() {
  $("view-settings-panel").classList.remove("hidden");
  renderViewSettings();
}

function closeViewSettings() {
  // "Done arranging" lives in this panel, so closing it has to end the
  // mode too rather than stranding the graph read-only with no way out.
  if (attractorMode) exitAttractorMode();
  $("view-settings-panel").classList.add("hidden");
}

function toggleViewSettings() {
  viewSettingsOpen() ? closeViewSettings() : openViewSettings();
}

function renderViewSettings() {
  renderForceSettings();
  renderColorSettings();
  renderLayoutSettings();
}

// ---- forces ----

/** Shared skeleton for every slider in the panel. Returns the element
 *  plus its parts, so each caller wires up its own meaning of "changed"
 *  and "reset" without duplicating the markup. */
function buildSliderRow({ label, hint, min, max, step, value, valueText, isDefault }) {
  const el = document.createElement("div");
  el.className = "slider-row";
  el.innerHTML =
    '<div class="slider-label"><span class="name"></span><span class="value"></span>' +
    '<button type="button" class="reset-btn" title="Back to the default">↺</button></div>' +
    '<input type="range">';
  el.title = hint;
  el.querySelector(".name").textContent = label;

  const valueEl = el.querySelector(".value");
  valueEl.textContent = valueText;
  valueEl.classList.toggle("auto", isDefault);

  const slider = el.querySelector("input");
  slider.min = String(min);
  slider.max = String(max);
  slider.step = String(step);
  slider.value = String(value);

  const reset = el.querySelector(".reset-btn");
  reset.disabled = isDefault;
  return { el, slider, value: valueEl, reset };
}

function forceSliderRow(spec) {
  const fallback = defaultViewSettings().forces[spec.key];
  const current = viewSettings.forces[spec.key];
  const row = buildSliderRow({
    label: spec.label, hint: spec.hint,
    min: spec.min, max: spec.max, step: spec.step,
    value: current, valueText: current.toFixed(spec.decimals), isDefault: current === fallback,
  });

  row.slider.addEventListener("input", () => {
    const next = Number(row.slider.value);
    viewSettings.forces[spec.key] = next;
    row.value.textContent = next.toFixed(spec.decimals);
    row.value.classList.toggle("auto", next === fallback);
    row.reset.disabled = next === fallback;
    graph.setViewSettings(viewSettings);
  });
  // Persist on release rather than on every intermediate frame of a drag,
  // and deliberately without a re-render: a slider being driven by arrow
  // keys fires change on each press, and rebuilding the row would drop
  // focus after the first one.
  row.slider.addEventListener("change", saveViewSettings);
  row.reset.addEventListener("click", () => {
    viewSettings.forces[spec.key] = fallback;
    applyViewSettings();
  });
  return row.el;
}

function relStrengthRow(type) {
  const override = viewSettings.forces.relStrength[type];
  const overridden = typeof override === "number";
  // An untouched type keeps graph.js's species-aware default, which is
  // per-edge and so has no single number -- the slider rests at whichever
  // case this type mostly is, labelled "auto" until it's actually moved.
  const auto = graph.defaultRelStrength(type);
  const row = buildSliderRow({
    label: type, hint: `How strongly ${type} pulls its two motes together`,
    min: 0, max: REL_STRENGTH_MAX, step: REL_STRENGTH_STEP,
    value: overridden ? override : auto,
    valueText: overridden ? override.toFixed(3) : "auto",
    isDefault: !overridden,
  });

  row.slider.addEventListener("input", () => {
    const next = Number(row.slider.value);
    viewSettings.forces.relStrength[type] = next;
    row.value.textContent = next.toFixed(3);
    row.value.classList.remove("auto");
    row.reset.disabled = false;
    graph.setViewSettings(viewSettings);
  });
  row.slider.addEventListener("change", saveViewSettings);
  row.reset.addEventListener("click", () => {
    delete viewSettings.forces.relStrength[type];
    applyViewSettings();
  });
  return row.el;
}

function renderForceSettings() {
  const globals = $("force-globals");
  globals.innerHTML = "";
  for (const spec of FORCE_SLIDERS) globals.appendChild(forceSliderRow(spec));

  const container = $("force-rel-rows");
  container.innerHTML = "";
  const types = graph.relationshipTypes();
  $("force-rel-heading").classList.toggle("hidden", types.length === 0);
  for (const type of types) container.appendChild(relStrengthRow(type));
}

// ---- colors ----

/** Any CSS color -> #rrggbb, via canvas's own color serialization.
 *  <input type="color"> only speaks hex, while the automatic species
 *  colors are generated as oklch. */
function toHexColor(color) {
  const ctx = toHexColor._ctx ||
    (toHexColor._ctx = document.createElement("canvas").getContext("2d"));
  ctx.fillStyle = "#888888";     // survives an unparseable color, which canvas ignores
  ctx.fillStyle = color;
  const value = ctx.fillStyle;
  return typeof value === "string" && value.startsWith("#") ? value : "#888888";
}

function speciesColorRow(row) {
  const el = document.createElement("div");
  el.className = "color-row";
  el.innerHTML =
    '<input type="color"><span class="name"></span>' +
    '<button type="button" class="reset-btn" title="Back to the automatic color">↺</button>';
  el.querySelector(".name").textContent = row.label;

  const picker = el.querySelector("input");
  const reset = el.querySelector(".reset-btn");
  const custom = viewSettings.colors.species[row.id];
  picker.value = toHexColor(custom || row.color);
  picker.dataset.speciesId = row.id;
  reset.disabled = !custom;

  picker.addEventListener("input", () => {
    viewSettings.colors.species[row.id] = picker.value;
    reset.disabled = false;
    graph.setViewSettings(viewSettings);
    renderLegend();
  });
  picker.addEventListener("change", saveViewSettings);
  reset.addEventListener("click", () => {
    delete viewSettings.colors.species[row.id];
    applyViewSettings();
  });
  return el;
}

function renderSpeciesColors() {
  const container = $("species-colors");
  container.innerHTML = "";
  // Only meaningful in species mode -- in the others the canvas isn't
  // showing species colors at all, so offering to pick one would lie.
  if (viewSettings.colors.mode !== "species") return;
  const rows = speciesRows().filter((row) => row.id !== "__none__");
  if (rows.length === 0) {
    container.innerHTML = '<div class="empty-note">No species yet</div>';
    return;
  }
  for (const row of rows) container.appendChild(speciesColorRow(row));
}

function renderColorSettings() {
  const mode = viewSettings.colors.mode;
  $("color-mode-select").value = mode;
  $("color-key-row").classList.toggle("hidden", mode !== "property" && mode !== "numeric");
  $("color-key-input").value = viewSettings.colors.propertyKey || "";

  const options = $("color-key-options");
  options.innerHTML = "";
  for (const key of graph.propertyKeys()) {
    const option = document.createElement("option");
    option.value = key;
    options.appendChild(option);
  }

  const note = $("color-mode-note");
  note.textContent = COLOR_MODE_NOTES[mode] || "";
  note.classList.toggle("hidden", !note.textContent);

  renderSpeciesColors();
}

// ---- layout / attractors ----

function renderLayoutSettings() {
  $("arrange-attractors-btn").classList.toggle("hidden", attractorMode);
  $("attractor-done-btn").classList.toggle("hidden", !attractorMode);
  $("attractor-mode-note").classList.toggle("hidden", !attractorMode);
  $("reset-attractors-btn").disabled = Object.keys(viewSettings.attractors).length === 0;
}

/** A read-only mode (see editBlocked): the graph is there to be
 *  rearranged, not edited, so every canvas gesture except panning and
 *  zooming is off while it's on and every mutation elsewhere toasts
 *  instead. */
function enterAttractorMode() {
  attractorMode = true;
  graph.setAttractorMode(true);
  closeDetailPanel();
  closeQuickAdd();
  hideContextMenu();
  if (viewSettingsOpen()) renderViewSettings();
  else openViewSettings();
  showToast("Arranging attractors — drag one to move that species' cluster.", 4500);
}

function exitAttractorMode() {
  attractorMode = false;
  graph.setAttractorMode(false);
  renderViewSettings();
}

function setupViewSettings() {
  $("open-view-settings-btn").addEventListener("click", toggleViewSettings);
  $("view-advanced-settings").addEventListener("click", () => {
    closeTopMenus();
    openViewSettings();
  });
  $("close-view-settings").addEventListener("click", closeViewSettings);

  $("color-mode-select").addEventListener("change", () => {
    viewSettings.colors.mode = $("color-mode-select").value;
    applyViewSettings();
  });

  const keyInput = $("color-key-input");
  keyInput.addEventListener("input", () => {
    viewSettings.colors.propertyKey = keyInput.value.trim();
    graph.setViewSettings(viewSettings);
    saveViewSettings();
  });

  $("arrange-attractors-btn").addEventListener("click", enterAttractorMode);
  $("attractor-done-btn").addEventListener("click", exitAttractorMode);

  $("reset-attractors-btn").addEventListener("click", () => {
    viewSettings.attractors = {};
    applyViewSettings();
  });

  // Pinned motes ignore their anchor entirely, so a hand-arranged garden
  // can make a dragged attractor look broken. This is the way out.
  $("unpin-all-btn").addEventListener("click", () => {
    graph.unpinAll();
    savePositions();
    showToast("Unpinned every mote — the layout will re-settle.");
  });

  $("reset-view-settings-btn").addEventListener("click", () => {
    viewSettings = defaultViewSettings();
    applyViewSettings();
    showToast("View settings reset to defaults.");
  });
}

// ---- detail panel ----

/** One mote goes to the floating inspector (frontend_shared/inspector.js)
 *  rather than to the docked panel beside the canvas.
 *
 *  The docked panel is not gone -- it still holds the two things that
 *  are about a *selection* rather than a mote: the aggregate editor for
 *  a box-select, and the relationship views. Those stay here because
 *  they are Monarch's own instruments. What one mote is, and what may be
 *  done to it, is the same question in all six windows, and now has the
 *  same answer in all six. */
/** Keep the inspector's leader line on the mote it points at as the
 *  graph's camera pans, zooms, or eases a selection into view -- all of
 *  which move a node's screen position without any of them being the
 *  click that opened the panel. Only meaningful when the open mote is
 *  still an anchor's worth of "on the graph": openDetailPanel's default
 *  anchor is always graph.screenPositionOf(moteId), so if the inspector
 *  is showing at all, this is what it's anchored to. */
function reanchorInspector() {
  if (!Inspector.isOpen()) return;
  const id = Inspector.currentId();
  if (!id || !graph.nodes.has(id)) return;
  Inspector.updateAnchor(graph.screenPositionOf(id));
}

async function openDetailPanel(moteId, options = {}) {
  multiSelectIds = null;
  // The docked panel's own modes are put away here directly rather than
  // through closeDetailPanel(), which also clears the graph's selection
  // -- and this is being called precisely *because* something was just
  // selected. Routing through it deselected the mote on the way to
  // showing it, and closed a relationship panel a moment after opening
  // one.
  $("detail-panel").classList.add("hidden");
  $("detail-relationship").classList.add("hidden");
  $("detail-relationship-multi").classList.add("hidden");
  await Inspector.open(moteId, {
    anchor: options.anchor || graph.screenPositionOf(moteId),
    newTab: !!options.newTab,
  });
}

/** A mote's note is edited in Mora, not here -- .modoc is a document
 *  format with live bindings in it (docs/modoc.md), and a one-line text
 *  box in a side panel is the wrong instrument for one. So the panel
 *  offers the door rather than the room, and says which side of it the
 *  mote is on. */
function openMora(moteId) {
  call(api.open_mora(moteId || null));
  showToast(
    moteId
      ? "Opening Mora… notes saved there appear here after a refocus."
      : "Opening Mora…",
  );
}

/** Shown instead of the single-mote panel once a box-select (or shift-add)
 *  produces more than one selected mote. Its own small render function
 *  rather than a generalization of openDetailPanel -- an aggregate view
 *  and a per-mote editor genuinely show different things, and trying to
 *  make one a special case of the other would tangle both. */
function renderMultiSelectPanel(ids) {
  multiSelectIds = ids;
  $("detail-relationship").classList.add("hidden");
  $("detail-relationship-multi").classList.add("hidden");
  $("detail-multi").classList.remove("hidden");

  $("detail-multi-count").textContent = `${ids.length} motes selected`;

  const counts = new Map(); // species id -> count
  let untyped = 0;
  for (const id of ids) {
    const species = speciesIdsOf(id);
    if (species.length === 0) { untyped += 1; continue; }
    for (const sid of species) counts.set(sid, (counts.get(sid) || 0) + 1);
  }

  const container = $("detail-multi-species");
  container.innerHTML = "";
  const rows = [...counts.entries()]
    .map(([sid, count]) => ({
      label: nodeLabel(moteById(sid) || { id: sid, properties: {} }),
      count,
      color: graph.speciesColorFor(sid, { emphatic: true }),
    }))
    .sort((a, b) => a.label.localeCompare(b.label));
  if (untyped > 0) rows.push({ label: "No species", count: untyped, color: "oklch(0.62 0.02 250)" });

  if (rows.length === 0) {
    container.innerHTML = '<div class="empty-note">None</div>';
  }
  for (const row of rows) {
    const el = document.createElement("div");
    el.className = "multi-species-row";
    el.innerHTML = `<span class="dot" style="background:${row.color}"></span><span class="name"></span><span class="count"></span>`;
    el.querySelector(".name").textContent = row.label;
    el.querySelector(".count").textContent = row.count;
    container.appendChild(el);
  }

  $("delete-selection-btn").textContent = `Delete ${ids.length} motes`;
  $("detail-panel").classList.remove("hidden");
}

function closeDetailPanel() {
  $("detail-panel").classList.add("hidden");
  $("detail-relationship").classList.add("hidden");
  $("detail-relationship-multi").classList.add("hidden");
  multiSelectIds = null;
  if (graph) graph.select(null);
  updateStatusbar();
}

/** Shared by both places a relationship's type can be renamed (its own
 *  panel, and a mote's relationship list): prompts, applies via the API,
 *  and refreshes the shared graph data. Returns false without doing
 *  anything if the prompt was cancelled or unchanged, so callers know
 *  whether to bother re-rendering. */
async function renameRelationshipType(rel) {
  if (editBlocked()) return false;
  const next = window.prompt("Relationship type:", rel.type);
  if (!next || next === rel.type) return false;
  await call(api.update_relationship(rel.id, null, null, next));
  await refocus(focusId, { pan: false });
  return true;
}

/** Shared by both places a relationship can be deleted -- see
 *  renameRelationshipType above. Returns whether it actually deleted
 *  anything, so callers know whether to bother refreshing/closing. */
async function deleteRelationshipAndRefresh(rel) {
  if (editBlocked()) return false;
  await call(api.delete_relationship(rel.id));
  await refocus(focusId, { pan: false });
  return true;
}

/** Mirrors openDetailPanel, but for a relationship rather than a mote --
 *  its own render function for the same reason renderMultiSelectPanel is
 *  its own: a relationship's panel shows genuinely different things
 *  (endpoints instead of properties) and forcing it through the mote
 *  code path would tangle both. Looks the relationship up in
 *  graph.relationships (already the full in-memory list from the last
 *  refocus -- no separate fetch needed). */
async function openRelationshipPanel(relId) {
  const rel = graph.relationships.find((r) => r.id === relId);
  if (!rel) { closeDetailPanel(); return; }

  graph.selectRelationship(relId);
  multiSelectIds = null;
  $("detail-multi").classList.add("hidden");
  $("detail-relationship-multi").classList.add("hidden");
  $("detail-relationship").classList.remove("hidden");

  const from = moteById(rel.from_id);
  const to = moteById(rel.to_id);

  const fromEl = $("detail-rel-from");
  fromEl.textContent = from ? nodeLabel(from) : rel.from_id.slice(0, 8) + "…";
  fromEl.title = rel.from_id;
  fromEl.onclick = () => selectMote(rel.from_id);

  const toEl = $("detail-rel-to");
  toEl.textContent = to ? nodeLabel(to) : rel.to_id.slice(0, 8) + "…";
  toEl.title = rel.to_id;
  toEl.onclick = () => selectMote(rel.to_id);

  const typeRow = $("detail-rel-type-row");
  typeRow.innerHTML = "";
  const arrow = document.createElement("span");
  arrow.className = "arrow";
  arrow.textContent = "↓";
  const type = document.createElement("span");
  type.className = "rel-type";
  type.textContent = rel.type;
  type.title = "Click to rename";
  maybeAddReservedBadge(type, rel.type, reservedRelTypes);
  type.addEventListener("click", async () => {
    if (await renameRelationshipType(rel)) await openRelationshipPanel(rel.id);
  });
  typeRow.append(arrow, type);

  $("delete-relationship-btn").onclick = async () => {
    if (await deleteRelationshipAndRefresh(rel)) closeDetailPanel();
  };

  $("detail-panel").classList.remove("hidden");
}

/** Shown instead of the single-relationship panel once shift+click has
 *  built up more than one selected relationship -- mirrors
 *  renderMultiSelectPanel's relationship to openDetailPanel. */
function renderMultiRelationshipPanel(ids) {
  multiSelectIds = null;
  $("detail-multi").classList.add("hidden");
  $("detail-relationship").classList.add("hidden");
  $("detail-relationship-multi").classList.remove("hidden");

  $("detail-relationship-multi-count").textContent = `${ids.length} relationships selected`;

  const counts = new Map(); // type -> count
  for (const id of ids) {
    const rel = graph.relationships.find((r) => r.id === id);
    if (!rel) continue;
    counts.set(rel.type, (counts.get(rel.type) || 0) + 1);
  }

  const container = $("detail-relationship-multi-types");
  container.innerHTML = "";
  const rows = [...counts.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  if (rows.length === 0) container.innerHTML = '<div class="empty-note">None</div>';
  for (const [type, count] of rows) {
    const el = document.createElement("div");
    el.className = "multi-species-row";
    el.innerHTML = `<span class="dot" style="background:var(--accent)"></span><span class="name"></span><span class="count"></span>`;
    el.querySelector(".name").textContent = type;
    el.querySelector(".count").textContent = count;
    container.appendChild(el);
  }

  $("delete-relationship-selection-btn").textContent = `Delete ${ids.length} relationships`;
  $("detail-panel").classList.remove("hidden");
}

/** Bulk counterpart to deleteRelationshipAndRefresh. No confirm dialog,
 *  matching the single-relationship delete it's built from -- deleting a
 *  relationship has no cascade, unlike a mote, so it never needed one. */
async function deleteRelationshipSelection() {
  if (editBlocked()) return;
  const ids = [...graph.selectedRelIds];
  if (ids.length === 0) return;
  for (const id of ids) await call(api.delete_relationship(id));
  closeDetailPanel();
  await refocus(focusId, { pan: false });
}

/** Drag-to-resize on the panel's left edge. A CSS custom property rather
 *  than a plain inline width so #detail-panel's own `width: var(...)`
 *  rule stays the single source of truth. Not per-garden like positions
 *  are -- it's a UI preference about the panel itself, not the data. */
function setupDetailResize() {
  const panel = $("detail-panel");
  const handle = $("detail-resize-handle");

  const stored = parseInt(localStorage.getItem(DETAIL_WIDTH_KEY), 10);
  if (Number.isFinite(stored)) {
    panel.style.setProperty("--detail-width", `${Math.min(DETAIL_WIDTH_MAX, Math.max(DETAIL_WIDTH_MIN, stored))}px`);
  }

  let dragState = null;
  handle.addEventListener("mousedown", (e) => {
    e.preventDefault();
    dragState = { startX: e.clientX, startWidth: panel.getBoundingClientRect().width };
    handle.classList.add("dragging");
  });
  window.addEventListener("mousemove", (e) => {
    if (!dragState) return;
    // The panel is right-anchored, so dragging the left edge leftward
    // (a shrinking clientX) is what grows it.
    const width = Math.min(DETAIL_WIDTH_MAX, Math.max(DETAIL_WIDTH_MIN,
      dragState.startWidth - (e.clientX - dragState.startX)));
    panel.style.setProperty("--detail-width", `${width}px`);
  });
  window.addEventListener("mouseup", () => {
    if (!dragState) return;
    dragState = null;
    handle.classList.remove("dragging");
    localStorage.setItem(DETAIL_WIDTH_KEY, parseInt(getComputedStyle(panel).width, 10));
  });
}

/** A textarea only reports a usable scrollHeight once it is attached and
 *  visible, so sizing is done in an explicit pass after the panel is shown
 *  rather than on a requestAnimationFrame -- that fired before the panel
 *  was laid out and silently left every equation one line tall. */
function fitTextarea(field) {
  field.style.height = "auto";
  field.style.height = field.scrollHeight + "px";
}


function openLink(url) {
  call(api.open_link(url));
}

async function selectMote(moteId, opts = {}) {
  await refocus(moteId, opts);
  await openDetailPanel(moteId, { newTab: !!opts.newTab });
}

async function connectNodes(sourceId, targetId) {
  if (editBlocked()) return;
  const type = window.prompt("Relationship type:");
  if (!type) return;
  await call(api.create_relationship(sourceId, targetId, type));
  await refocus(sourceId, { pan: false });
  await openDetailPanel(sourceId);
}

/** The one delete path -- the single-mote panel's own button, the
 *  multi-select panel's bulk button, the context menu's Delete, and the
 *  Del/Backspace key all call this, rather than each having its own
 *  inline delete-and-refresh logic. */
async function deleteSelection() {
  if (editBlocked()) return;
  const ids = [...graph.selectedIds];
  if (ids.length === 0) return;
  const prompt = ids.length === 1
    ? "Delete this mote and all its relationships?"
    : `Delete ${ids.length} motes and all their relationships?`;
  if (!window.confirm(prompt)) return;

  // Snapshotted before deleting -- cascading deletes wipe properties and
  // relationships along with the mote, so there's nothing left to read
  // afterward if the user hits Undo.
  const motesSnapshot = ids.map((id) => ({ id, properties: { ...moteById(id).properties } }));
  const relsById = new Map();
  for (const id of ids) {
    for (const rel of await call(api.mote_relationships(id))) relsById.set(rel.id, rel);
  }
  const relsSnapshot = [...relsById.values()];

  for (const id of ids) {
    await call(api.delete_mote(id));
    if (id === focusId) localStorage.removeItem(lastFocusKey(currentGarden));
  }
  closeDetailPanel();
  await refreshMoteCache();
  await refocus(null);

  showUndoToast(
    ids.length === 1 ? "Deleted." : `Deleted ${ids.length} motes.`,
    async () => {
      for (const m of motesSnapshot) await call(api.create_mote(m.properties, m.id));
      for (const r of relsSnapshot) await call(api.create_relationship(r.from_id, r.to_id, r.type, r.id));
      await refreshMoteCache();
      const restoredIds = motesSnapshot.map((m) => m.id);
      await refocus(restoredIds[0], { pan: false });
      graph.select(restoredIds, { pan: false });
      await syncSelectionPanel(restoredIds);
      showToast(restoredIds.length === 1 ? "Restored." : `Restored ${restoredIds.length} motes.`);
    }
  );
}

// ---- quick add (with species templates) ----

function renderSpeciesChips() {
  const container = $("species-chips");
  container.innerHTML = "";
  const rows = speciesRows().filter((r) => r.id !== "__none__");

  const blank = document.createElement("button");
  blank.type = "button";
  blank.className = "chip" + (quickAddSpeciesId === null ? " on" : "");
  blank.style.borderColor = quickAddSpeciesId === null ? "var(--accent)" : "var(--border)";
  blank.textContent = "Blank";
  blank.addEventListener("click", () => { quickAddSpeciesId = null; renderSpeciesChips(); resetQuickAddRows(); });
  container.appendChild(blank);

  for (const row of rows) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.dataset.speciesId = row.id;
    chip.style.borderColor = quickAddSpeciesId === row.id ? row.color : "var(--border)";
    chip.innerHTML = `<span class="dot" style="background:${row.color}"></span><span></span>`;
    chip.querySelector("span:last-child").textContent = row.label;
    chip.addEventListener("click", () => selectQuickAddSpecies(row.id));
    container.appendChild(chip);
  }
}

/** Picking a species pre-fills the form from its template, so making the
 *  tenth resistor is picking a chip and typing one number. */
async function selectQuickAddSpecies(speciesId) {
  quickAddSpeciesId = speciesId;
  renderSpeciesChips();
  const template = await call(api.template_for(speciesId));
  const rowsEl = $("quick-add-rows");
  rowsEl.innerHTML = "";
  const entries = template ? Object.entries(template.properties) : [["name", ""]];
  for (const [key, value] of entries) {
    addQuickAddRow(key, typeof value === "string" ? value : JSON.stringify(value));
  }
  if (entries.length === 0) addQuickAddRow("name", "");
}

function addQuickAddRow(key = "", value = "") {
  const row = document.createElement("div");
  row.className = "prop-row";
  const keyInput = document.createElement("input");
  keyInput.placeholder = "key";
  keyInput.value = key;
  const valueInput = document.createElement("input");
  valueInput.placeholder = "value";
  valueInput.value = value;
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "×";
  remove.addEventListener("click", () => row.remove());
  row.append(keyInput, valueInput, remove);
  $("quick-add-rows").appendChild(row);
}

function resetQuickAddRows() {
  $("quick-add-rows").innerHTML = "";
  addQuickAddRow("name", "");
}

function collectQuickAddProperties() {
  const props = {};
  for (const row of $("quick-add-rows").querySelectorAll(".prop-row")) {
    const [keyInput, valueInput] = row.querySelectorAll("input");
    if (keyInput.value.trim()) props[keyInput.value.trim()] = valueInput.value;
  }
  return props;
}

/** Shared close path for the quick-add panel -- the toggle button, Esc, a
 *  click outside it, and a successful create all funnel through here. */
function closeQuickAdd() {
  $("quick-add").classList.add("hidden");
  $("quick-add-btn").textContent = "+";
  contextMenuWorldPos = null; // closing without creating cancels any pending placement
}

function setupQuickAdd() {
  resetQuickAddRows();
  $("quick-add-btn").addEventListener("click", () => {
    if (editBlocked()) return;
    const panel = $("quick-add");
    if (!panel.classList.contains("hidden")) { closeQuickAdd(); return; }
    panel.classList.remove("hidden");
    $("quick-add-btn").textContent = "×";
    // The ordinary + button always opens at its usual bottom-left spot,
    // even if a right-click left the panel floating near the cursor.
    panel.classList.remove("floating");
    panel.style.left = "";
    panel.style.top = "";
    renderSpeciesChips();
  });

  $("add-property-row").addEventListener("click", () => addQuickAddRow());

  // Enter creates the mote instead of requiring a click on the Create
  // button, as long as focus is on one of the panel's own text inputs
  // (so it doesn't fire from, say, a button that happens to have focus).
  $("quick-add").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && e.target.tagName === "INPUT") {
      e.preventDefault();
      $("create-mote-btn").click();
    }
  });

  // Matches the context menu's own dismiss-on-outside-click pattern.
  // Clicks on the toggle button are excluded since its own handler above
  // already opens/closes the panel; clicks inside the context menu are
  // excluded too, since "New mote here" is itself the click that opens
  // this panel and would otherwise immediately close it again on the
  // same bubble.
  //
  // Uses composedPath() rather than a live .contains(e.target) check:
  // a property row's × button removes its own row, and a species chip
  // click rebuilds the whole chip strip (renderSpeciesChips()'s
  // innerHTML = "") -- both detach e.target from the tree before this
  // listener runs, which would make a live .contains() check wrongly
  // see the click as "outside". composedPath() is captured at dispatch
  // time and isn't affected by DOM mutations that happen while bubbling.
  document.addEventListener("click", (e) => {
    if ($("quick-add").classList.contains("hidden")) return;
    const path = e.composedPath();
    if (
      path.includes($("quick-add")) ||
      path.includes($("quick-add-btn")) ||
      path.includes($("context-menu"))
    ) return;
    closeQuickAdd();
  });

  $("create-mote-btn").addEventListener("click", async () => {
    if (editBlocked()) return;
    const props = collectQuickAddProperties();
    const mote = quickAddSpeciesId
      ? await call(api.create_instance(quickAddSpeciesId, props))
      : await call(api.create_mote(props));
    // "New mote here" (right-click on empty canvas) remembers the click's
    // world position so the mote lands under the cursor instead of at its
    // usual hash-seeded spot; the ordinary + button leaves this unset.
    const placeAt = contextMenuWorldPos;
    closeQuickAdd();
    resetQuickAddRows();
    await refreshMoteCache();
    await selectMote(mote.id, { pan: !placeAt });
    if (placeAt) {
      graph.placeAt(mote.id, placeAt.x, placeAt.y);
      savePositions();
    }
    showToast("Mote created.");
  });
}

/** Opens the existing quick-add panel (so species/template selection stays
 *  in one place) but remembers where the click happened, so the mote it
 *  produces lands there instead of at its usual hash-seeded spot -- and
 *  floats the form itself near the click too, rather than leaving it
 *  pinned at the bottom-left corner where it's awkward to reach after
 *  right-clicking somewhere else on a large graph. */
function openQuickAddAt(worldX, worldY, clientX, clientY) {
  if (editBlocked()) return;
  contextMenuWorldPos = { x: worldX, y: worldY };
  const panel = $("quick-add");
  panel.classList.remove("hidden");
  panel.classList.add("floating");
  panel.style.left = clientX + "px";
  panel.style.top = clientY + "px";
  $("quick-add-btn").textContent = "×";
  renderSpeciesChips();

  // Clamp into the viewport the same way the context menu does, once the
  // panel has real dimensions to measure.
  const rect = panel.getBoundingClientRect();
  if (rect.right > window.innerWidth) {
    panel.style.left = Math.max(8, window.innerWidth - rect.width - 8) + "px";
  }
  if (rect.bottom > window.innerHeight) {
    panel.style.top = Math.max(8, window.innerHeight - rect.height - 8) + "px";
  }
}

/** Arrow-key highlight + Enter-to-choose for a type-to-filter result list
 *  built from .search-result-row markup -- the species picker below shows
 *  exactly the kind of list the main search palette does
 *  (openSearch/renderSearchResults), but unlike it never grew arrow keys
 *  or Enter, leaving the mouse as the only way to pick a result.
 *  Reads/writes the highlighted row straight from the DOM instead of
 *  keeping its own index, so each picker's render function needs nothing
 *  more than marking row 0 highlighted the way renderSearchResults
 *  already does. Re-dispatches the row's own mousedown on Enter rather
 *  than duplicating what each row's listener does. */
function setupPickerKeyNav(input, resultsContainer) {
  input.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Enter") return;
    const rows = [...resultsContainer.querySelectorAll(".search-result-row")];
    if (rows.length === 0) return;
    e.preventDefault();
    const current = Math.max(0, rows.findIndex((r) => r.classList.contains("highlighted")));
    if (e.key === "Enter") {
      const target = rows[current].querySelector(".label") || rows[current];
      target.dispatchEvent(new MouseEvent("mousedown", { bubbles: true, cancelable: true }));
      return;
    }
    const next = Math.max(0, Math.min(current + (e.key === "ArrowDown" ? 1 : -1), rows.length - 1));
    rows[current].classList.remove("highlighted");
    rows[next].classList.add("highlighted");
    rows[next].scrollIntoView({ block: "nearest" });
  });
}

// ---- search palette ----

// Matches "key = value" (or "key=value"), e.g. "priority = high" --
// structured equality on a specific property, rather than the substring
// search below being asked to guess that "priority" is a key and "high"
// is its value out of a single blob of text.
const KEY_VALUE_QUERY_RE = /^\s*([^=\s][^=]*?)\s*=\s*(.*)$/;

/** The exact counterpart of core/motes.py::search_motes -- same two query
 *  forms, matched against the same three things (mote id, property keys,
 *  property values), so a query returns the same motes whichever surface
 *  asked. The id is matched in full rather than via nodeLabel(), which
 *  only ever shows its first 8 characters: pasting a whole mote id into
 *  the palette used to find nothing here while finding it over MCP. */
function matchesQuery(mote, query) {
  if (!query) return true;

  const kv = query.match(KEY_VALUE_QUERY_RE);
  if (kv) {
    const key = kv[1].trim().toLowerCase();
    const value = kv[2].trim().toLowerCase();
    for (const [k, v] of Object.entries(mote.properties || {})) {
      if (k.toLowerCase() !== key) continue;
      if (!value) return true; // "key =" with nothing after it: any value matches
      const vs = typeof v === "string" ? v : JSON.stringify(v);
      if (vs.toLowerCase() === value) return true;
    }
    return false;
  }

  const q = query.toLowerCase();
  if (mote.id.toLowerCase().includes(q)) return true;
  for (const [k, v] of Object.entries(mote.properties || {})) {
    if (k.toLowerCase().includes(q)) return true;
    const vs = typeof v === "string" ? v : JSON.stringify(v);
    if (vs.toLowerCase().includes(q)) return true;
  }
  return false;
}

function relatedTargetIds(relationships, moteId, type) {
  return relationships.filter((r) => r.type === type && r.from_id === moteId).map((r) => r.to_id);
}

function speciesIdsOf(moteId) {
  return relatedTargetIds(payload.relationships, moteId, "instance_of");
}

function computeFilteredMotes(motes, relationships, query, { speciesFilter, relFilter } = {}) {
  return motes
    .filter((mote) => {
      if (speciesFilter && speciesFilter.size) {
        const ids = relatedTargetIds(relationships, mote.id, "instance_of");
        const matches = ids.some((id) => speciesFilter.has(id)) ||
          (speciesFilter.has("__none__") && ids.length === 0);
        if (!matches) return false;
      }
      if (relFilter && relFilter.size) {
        const touches = relationships.some(
          (r) => (r.from_id === mote.id || r.to_id === mote.id) && relFilter.has(r.type)
        );
        if (!touches) return false;
      }
      return matchesQuery(mote, query);
    })
    .sort((a, b) => nodeLabel(a).localeCompare(nodeLabel(b)))
    .slice(0, 60);
}

function computeSearchResults(query) {
  return computeFilteredMotes(allMotes, payload.relationships, query, {
    speciesFilter: searchSpeciesFilter, relFilter: searchRelFilter,
  });
}

/** Generic chip row: rows with a `color` get a dot (square if `square`),
 *  rows without one (plain relationship types) render as text-only. */
function renderChipRow(container, rows, activeSet, onToggle) {
  container.innerHTML = "";
  for (const row of rows) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip" + (activeSet.has(row.id) ? " on" : "");
    chip.style.borderColor = activeSet.has(row.id) ? (row.color || "var(--accent)") : "var(--border)";
    if (row.color) {
      chip.innerHTML = `<span class="dot${row.square ? " square" : ""}" style="background:${row.color}"></span><span></span>`;
      chip.querySelector("span:last-child").textContent = row.label;
    } else {
      chip.textContent = row.label;
    }
    chip.addEventListener("click", () => onToggle(row.id));
    container.appendChild(chip);
  }
}

function relTypeRows(relationships) {
  return [...new Set(relationships.map((r) => r.type))].sort().map((type) => ({ id: type, label: type }));
}

function renderSearchChips() {
  renderChipRow($("search-species-chips"), speciesRows(), searchSpeciesFilter, (id) => {
    searchSpeciesFilter.has(id) ? searchSpeciesFilter.delete(id) : searchSpeciesFilter.add(id);
    renderSearchChips();
    renderSearchResults($("search-input").value);
  });
  renderChipRow($("search-rel-chips"), relTypeRows(payload.relationships), searchRelFilter, (type) => {
    searchRelFilter.has(type) ? searchRelFilter.delete(type) : searchRelFilter.add(type);
    renderSearchChips();
    renderSearchResults($("search-input").value);
  });
}

function renderSearchResults(query) {
  const container = $("search-results");
  container.innerHTML = "";
  searchVisible = computeSearchResults(query);
  searchHighlight = searchVisible.length ? 0 : -1;

  if (searchVisible.length === 0) {
    container.innerHTML = '<div class="search-empty">No matches</div>';
    return;
  }
  searchVisible.forEach((mote, i) => {
    const row = document.createElement("div");
    row.className = "search-result-row" + (i === searchHighlight ? " highlighted" : "");
    row.innerHTML =
      `<span class="dot" style="background:${graph.colorFor(mote.id)}"></span>` +
      '<span class="label"></span><span class="kind"></span>';
    row.querySelector(".label").textContent = nodeLabel(mote);
    row.querySelector(".kind").textContent = kindLabel(mote);
    row.addEventListener("mousedown", (e) => { e.preventDefault(); selectSearchResult(mote.id); });
    container.appendChild(row);
  });
}

function updateSearchHighlight() {
  const rows = document.querySelectorAll("#search-results .search-result-row");
  rows.forEach((row, i) => row.classList.toggle("highlighted", i === searchHighlight));
  const active = rows[searchHighlight];
  if (active) active.scrollIntoView({ block: "nearest" });
}

function openSearch() {
  $("search-overlay").classList.remove("hidden");
  $("search-input").value = "";
  renderSearchChips();
  renderSearchResults("");
  $("search-input").focus();
}

function closeSearch() { $("search-overlay").classList.add("hidden"); }

async function selectSearchResult(moteId) {
  closeSearch();
  await selectMote(moteId);
}

function setupSearch() {
  $("search-btn").addEventListener("click", openSearch);
  $("search-overlay").addEventListener("mousedown", (e) => {
    if (e.target === $("search-overlay")) closeSearch();
  });
  $("search-input").addEventListener("input", (e) => renderSearchResults(e.target.value));
  $("search-input").addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (searchHighlight < searchVisible.length - 1) searchHighlight++;
      updateSearchHighlight();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (searchHighlight > 0) searchHighlight--;
      updateSearchHighlight();
    } else if (e.key === "Enter") {
      e.preventDefault();
      const chosen = searchVisible[searchHighlight];
      if (chosen) selectSearchResult(chosen.id);
    } else if (e.key === "Escape") {
      closeSearch();
    }
  });
}

// ---- selection + box-select overlay ----

/** Reconciles the detail panel with graph.js's own selection state.
 *  Used both as graph's onSelectionChanged callback (fired after a
 *  box-select completes or an empty click deselects) and called directly
 *  after a right-click, since graph.js may replace the selection itself
 *  before the context menu callback fires. */
async function syncSelectionPanel(ids) {
  updateStatusbar();
  if (ids.length === 0) { closeDetailPanel(); return; }
  if (ids.length === 1) { await selectMote(ids[0], { pan: false }); return; }
  renderMultiSelectPanel(ids);
}

function updateSelectBox(rect, mode) {
  const box = $("select-box");
  box.classList.toggle("rel", mode === "rel");
  if (!rect) { box.classList.add("hidden"); return; }
  const wrapRect = $("graph-wrap").getBoundingClientRect();
  box.style.left = (rect.left - wrapRect.left) + "px";
  box.style.top = (rect.top - wrapRect.top) + "px";
  box.style.width = (rect.right - rect.left) + "px";
  box.style.height = (rect.bottom - rect.top) + "px";
  box.classList.remove("hidden");
}

// ---- context menu ----

function hideContextMenu() {
  $("context-menu").classList.add("hidden");
}

function addMenuItem(menu, label, onClick, { danger = false, disabled = false } = {}) {
  const item = document.createElement("div");
  item.className = "menu-item" + (danger ? " danger" : "") + (disabled ? " disabled" : "");
  item.textContent = label;
  if (!disabled) {
    item.addEventListener("click", async () => { hideContextMenu(); await onClick(); });
  }
  menu.appendChild(item);
}

/** Drops a menu at the click point, then nudges it back onscreen if it
 *  would overflow. */
function positionMenu(menu, clientX, clientY) {
  menu.style.left = clientX + "px";
  menu.style.top = clientY + "px";
  menu.classList.remove("hidden");
  const rect = menu.getBoundingClientRect();
  if (rect.right > window.innerWidth) {
    menu.style.left = Math.max(8, window.innerWidth - rect.width - 8) + "px";
  }
  if (rect.bottom > window.innerHeight) {
    menu.style.top = Math.max(8, window.innerHeight - rect.height - 8) + "px";
  }
}

async function openContextMenu(clientX, clientY, targetId, worldX, worldY) {
  closeTopMenus(); // mutual exclusion with the topbar dropdowns, both ways
  // graph.js may have just replaced the selection with the right-clicked
  // mote (or left an existing multi-selection that includes it) -- the
  // statusbar and multi-select panel need to catch up before the menu's
  // actions read graph.selectedIds. Deliberately *not* the full
  // syncSelectionPanel: that would also swap the inspector to focus this
  // mote, clobbering whatever tab is already open there before "Inspect
  // in new tab" ever gets a chance to add one.
  if (targetId) {
    updateStatusbar();
    const ids = [...graph.selectedIds];
    if (ids.length > 1) {
      renderMultiSelectPanel(ids);
    } else if (multiSelectIds) {
      // Right-click collapsed a previous multi-selection down to just this
      // one mote -- hide the now-stale aggregate panel without touching
      // the inspector, which would clobber whatever tab is open there.
      $("detail-panel").classList.add("hidden");
      $("detail-relationship").classList.add("hidden");
      $("detail-relationship-multi").classList.add("hidden");
      multiSelectIds = null;
    }
  }

  const menu = $("context-menu");
  menu.innerHTML = "";

  if (targetId) {
    const node = graph.nodes.get(targetId);
    // Right-clicking a mote that's part of the current multi-selection
    // (syncSelectionPanel above already reconciled it) acts on the whole
    // selection, matching the precedent Delete already set.
    const ids = graph.selectedIds.has(targetId) ? [...graph.selectedIds] : [targetId];
    // First, and about this one mote even inside a multi-selection: the
    // inspector is a single-mote surface, and "in a new tab" is the
    // gesture ctrl-click used to be before ctrl+drag claimed it here.
    addMenuItem(menu, "Inspect in new tab",
                () => Inspector.open(targetId, { newTab: true }));
    addMenuItem(menu, ids.length > 1 ? `Copy ${ids.length} motes` : "Copy", () => copySelection(ids));
    // Mixed pinned/unpinned within a selection reads as "Pin" and pins
    // everything; only once every member is already pinned does it flip
    // to "Unpin".
    const selectedNodes = ids.map((i) => graph.nodes.get(i)).filter(Boolean);
    const allPinned = selectedNodes.length > 0 && selectedNodes.every((n) => n.pinned);
    const pinLabel = ids.length > 1
      ? (allPinned ? `Unpin ${ids.length} motes` : `Pin ${ids.length} motes`)
      : (node && node.pinned ? "Unpin" : "Pin");
    addMenuItem(menu, pinLabel, () => togglePin(ids));
    const sep = document.createElement("div");
    sep.className = "menu-sep";
    menu.appendChild(sep);
    addMenuItem(menu, "Delete", () => deleteSelection(), { danger: true });
  } else {
    addMenuItem(menu, "New mote here", () => openQuickAddAt(worldX, worldY, clientX, clientY));
    const pasteLabel = clipboard && clipboard.length > 1 ? `Paste ${clipboard.length} motes` : "Paste";
    addMenuItem(menu, pasteLabel, () => pasteAt(worldX, worldY), { disabled: !clipboard });
  }

  positionMenu(menu, clientX, clientY);
}

/** clipboard is an array, one entry per copied mote, each carrying an
 *  offset from the copied group's centroid (in world units) -- so pasting
 *  a group reproduces its relative layout instead of stacking every mote
 *  on the same point. clipboardRelationships holds every relationship
 *  touching any copied mote (species included -- instance_of is just an
 *  ordinary relationship type, nothing special-cased about it), keyed by
 *  the *original* mote ids; pasteAt() remaps the ones that point at
 *  another copied mote to its new counterpart, and leaves the rest (e.g.
 *  a species outside the selection) pointing at the original.
 *  pasteRepeatCount tracks how many times the current clipboard has been
 *  pasted via Ctrl+V, so repeated pastes step apart instead of landing
 *  exactly on top of each other. */
async function copySelection(ids) {
  const nodes = ids.map((id) => graph.nodes.get(id)).filter(Boolean);
  if (nodes.length === 0) return;
  const cx = nodes.reduce((s, n) => s + n.x, 0) / nodes.length;
  const cy = nodes.reduce((s, n) => s + n.y, 0) / nodes.length;

  const relsById = new Map();
  for (const id of ids) {
    for (const rel of await call(api.mote_relationships(id))) relsById.set(rel.id, rel);
  }

  clipboard = ids.map((id, i) => ({
    sourceId: id,
    properties: { ...moteById(id).properties },
    offset: { dx: nodes[i].x - cx, dy: nodes[i].y - cy },
  }));
  clipboardRelationships = [...relsById.values()];
  pasteRepeatCount = 0;
  showToast(clipboard.length > 1 ? `Copied ${clipboard.length} motes.` : "Copied.");
}

async function pasteAt(worldX, worldY) {
  if (!clipboard) return;
  if (editBlocked()) return;
  const placed = [];
  const idMap = new Map(); // original mote id -> newly pasted mote id
  for (const entry of clipboard) {
    const mote = await call(api.create_mote(entry.properties));
    idMap.set(entry.sourceId, mote.id);
    placed.push({ id: mote.id, x: worldX + entry.offset.dx, y: worldY + entry.offset.dy });
  }
  for (const rel of clipboardRelationships) {
    const fromId = idMap.get(rel.from_id) ?? rel.from_id;
    const toId = idMap.get(rel.to_id) ?? rel.to_id;
    await call(api.create_relationship(fromId, toId, rel.type));
  }
  await refreshMoteCache();
  // select:false -- the freshly-pasted group is about to become the
  // selection below, not whatever single mote focus() would otherwise land
  // on when centering the neighborhood view on the first pasted mote.
  await refocus(placed[0].id, { pan: false, select: false });
  for (const p of placed) graph.placeAt(p.id, p.x, p.y);
  savePositions();
  const newIds = placed.map((p) => p.id);
  graph.select(newIds, { pan: false });
  await syncSelectionPanel(newIds);
  showToast(placed.length > 1 ? `Pasted ${placed.length} motes.` : "Pasted.");
}

function togglePin(ids) {
  const nodes = ids.map((id) => graph.nodes.get(id)).filter(Boolean);
  const allPinned = nodes.length > 0 && nodes.every((n) => n.pinned);
  for (const id of ids) {
    if (allPinned) graph.unpin(id); else graph.pin(id);
  }
  savePositions();
}

function setupContextMenu() {
  // Matches the garden menu's own dismiss-on-outside-click pattern.
  document.addEventListener("click", (e) => {
    if (!$("context-menu").contains(e.target)) hideContextMenu();
  });
}

// ---- topbar menus ----
//
// The garden switcher and the File/View/Tools menus are anchored dropdowns
// (reusing the .menu/.menu-item classes #context-menu already established)
// with only one open at a time -- opening any one closes the rest, mutually
// exclusive with the right-click context menu too. Most of their real
// items are static markup pointing at ids that predate this section
// so the setup functions that already own those elements' behavior need
// no changes, only a leading closeTopMenus() so choosing an action closes
// the menu it came from.

function closeTopMenus() {
  for (const name of ["garden", "file", "view", "tools"]) {
    $(`${name}-menu`).classList.add("hidden");
    const btn = $(`${name}-btn`);
    btn.classList.remove("open");
    btn.setAttribute("aria-expanded", "false");
  }
  openMenuName = null;
}

function openTopMenu(name) {
  closeTopMenus();
  hideContextMenu();
  if (name === "garden") renderGardenMenu();
  else if (name === "view") refreshViewMenu();
  $(`${name}-menu`).classList.remove("hidden");
  const btn = $(`${name}-btn`);
  btn.classList.add("open");
  btn.setAttribute("aria-expanded", "true");
  openMenuName = name;
}

function toggleTopMenu(name) {
  if (openMenuName === name) closeTopMenus();
  else openTopMenu(name);
}

/** Switch/create only -- Export/Delete stay in the gardens modal (reached
 *  via "Manage gardens…"), which has room for them as real per-row
 *  buttons instead of crammed into a narrow menu item. */
async function renderGardenMenu() {
  const gardens = await call(api.list_gardens());
  const container = $("garden-menu-list");
  container.innerHTML = "";
  for (const name of gardens) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "menu-item";
    row.innerHTML = '<span class="check"></span><span class="name"></span>';
    row.querySelector(".check").textContent = name === currentGarden ? "✓" : "";
    row.querySelector(".name").textContent = name;
    row.addEventListener("click", async () => {
      closeTopMenus();
      if (name === currentGarden) return;
      await call(api.switch_garden(name));
      await loadGarden(name);
    });
    container.appendChild(row);
  }
}

function refreshViewMenu() {
  const hidden = $("control-panel").classList.contains("hidden");
  $("view-toggle-panel").textContent = hidden ? "Show controls" : "Hide controls";
}

/** #control-panel (species legend, relationship filters, depth, reset
 *  view) has always been permanently visible; this is the first way to
 *  reclaim the screen space it docks over (top-right, the same corner
 *  #detail-panel covers at a higher z-index). Toggles the whole panel,
 *  not just the legend -- it's one visual box, and hiding only part of it
 *  would leave a half-empty box floating in the corner. */
function applyControlPanelVisibility() {
  const hidden = localStorage.getItem(CONTROL_PANEL_KEY) === "hidden";
  $("control-panel").classList.toggle("hidden", hidden);
}

// Set while the control panel is hidden and it took the (still logically
// open) advanced view settings panel down with it, so showing controls
// again knows to bring the settings panel back too -- rather than losing
// track of it and leaving it stranded closed.
let viewSettingsHiddenWithControls = false;

function toggleControlPanel() {
  const panel = $("control-panel");
  const hidden = !panel.classList.contains("hidden");
  panel.classList.toggle("hidden", hidden);
  localStorage.setItem(CONTROL_PANEL_KEY, hidden ? "hidden" : "shown");

  // "Advanced view settings…" is opened from inside #control-panel, so
  // hiding controls strands it with no visible way to close -- hide it
  // alongside, and only bring it back if that's the reason it went away.
  const settingsPanel = $("view-settings-panel");
  if (hidden) {
    viewSettingsHiddenWithControls = viewSettingsOpen();
    if (viewSettingsHiddenWithControls) settingsPanel.classList.add("hidden");
  } else if (viewSettingsHiddenWithControls) {
    viewSettingsHiddenWithControls = false;
    settingsPanel.classList.remove("hidden");
  }
}

/** Shared by every "soon" stub item (Export as CSV, Print graph,
 *  Timeline view, Group by type, Automations, Webhooks, API access) --
 *  these have no real implementation yet, so a toast explains that
 *  rather than leaving the click silently do nothing. A stub only ever
 *  stands for something that genuinely doesn't exist: garden
 *  import/export used to sit here as "soon" while already working in
 *  the gardens modal, which read as the app not knowing its own
 *  features. */
function comingSoon(label) {
  showToast(`${label} isn't built yet.`);
}

function setupTopMenus() {
  for (const name of ["garden", "file", "view", "tools"]) {
    $(`${name}-btn`).addEventListener("click", () => toggleTopMenu(name));
  }

  // Matches the context menu's own dismiss-on-outside-click pattern. A
  // click anywhere inside the open menu's own .topbar-menu wrapper --
  // including its trigger button -- is excluded so opening/using a menu
  // never immediately closes itself on the same bubble.
  document.addEventListener("click", (e) => {
    if (!openMenuName) return;
    if (e.target.closest(".topbar-menu")) return;
    closeTopMenus();
  });

  for (const el of document.querySelectorAll("[data-soon]")) {
    el.addEventListener("click", () => {
      closeTopMenus();
      comingSoon(el.dataset.soon);
    });
  }

  // Both entry points open the same window -- the one all six apps
  // open, rather than the modal this window used to keep to itself.
  for (const id of ["garden-menu-manage", "file-manage-gardens"]) {
    $(id).addEventListener("click", () => { closeTopMenus(); call(api.open_gardens()); });
  }
  $("view-reset-view").addEventListener("click", () => { closeTopMenus(); resetViewAndUnhideAll(); });
  $("view-toggle-panel").addEventListener("click", () => { closeTopMenus(); toggleControlPanel(); });
}

// Sync moved out of this window with the rest of garden management:
// it is a property of a garden, not of the window looking at one, and
// five of the six windows never had it at all. The Gardens window opens
// straight at this garden's sync settings (api_common.open_gardens's
// `sync` flag) so the menu item still lands where it used to.
function setupSync() {
  $("open-sync-btn").addEventListener("click", () => {
    closeTopMenus();
    call(api.open_gardens(true));
  });
}

function setupRefresh() {
  $("refresh-btn").addEventListener("click", async () => {
    const btn = $("refresh-btn");
    btn.disabled = true;
    btn.classList.add("spinning");
    try {
      const result = await call(api.refresh());
      await refocus(focusId, { pan: false });
      showToast(result.synced ? "Synced and refreshed." : "Refreshed.");
    } finally {
      btn.disabled = false;
      btn.classList.remove("spinning");
    }
  });
}


// ---- view controls ----

function setupViewControls() {
  const select = $("depth-select");
  const stored = loadDepth();
  select.value = stored === null ? "all" : String(stored);
  select.addEventListener("change", async () => {
    saveDepth(select.value === "all" ? null : parseInt(select.value, 10));
    await refocus(focusId, { pan: false });
    // Changing depth is an explicit "show me more/less" command, so
    // re-framing is what you asked for -- unlike selection, which isn't.
    graph.fitWhenSettled();
  });

  $("reset-view-btn").addEventListener("click", resetViewAndUnhideAll);
  $("view-filter-select").addEventListener("change", async () => {
    await applyViewFilter($("view-filter-select").value || null);
  });
  $("close-panel").addEventListener("click", closeDetailPanel);

  $("delete-selection-btn").addEventListener("click", deleteSelection);
  $("delete-relationship-selection-btn").addEventListener("click", deleteRelationshipSelection);
}

// ---- app switcher (⌘J) ----
//
// Every sibling app but this one, in a fixed order -- so "Monastery is
// ⌘2" holds regardless of what's typed into the filter box. See
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
  {
    id: "moxie", name: "Moxie", kind: "graphs",
    desc: "Sweep one property and graph another against it.",
    icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
      <path d="M7 30 C14.5 30 13.5 11 20 11 C26.5 11 25.5 30 33 30" fill="none"
            stroke="var(--fg)" stroke-width="2.2" stroke-linecap="round" opacity="0.6"></path>
      <circle cx="20" cy="11" r="3" fill="var(--accent)"></circle>
    </svg>`,
  },
];
const THIS_APP = "monarch";

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
  $("launcher-garden").textContent = "garden · " + currentGarden;
  launcherHighlight = 0;
  renderLauncherResults("");
  $("launcher-input").focus();
}

function closeLauncher() { $("launcher-overlay").classList.add("hidden"); }

function launcherIsOpen() { return !$("launcher-overlay").classList.contains("hidden"); }

function launchApp(appId) {
  closeLauncher();
  const app = LAUNCHER_APPS.find((a) => a.id === appId);
  call(api.open_app(appId));
  showToast(`Opening ${app.name}…`);
}

function setupLauncher() {
  $("switch-app-btn").addEventListener("click", openLauncher);
  $("launcher-overlay").addEventListener("mousedown", (e) => {
    if (e.target === $("launcher-overlay")) closeLauncher();
  });
  $("launcher-input").addEventListener("input", (e) => renderLauncherResults(e.target.value));
  $("launcher-input").addEventListener("keydown", (e) => {
    const list = launcherFiltered($("launcher-input").value);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      if (launcherHighlight + LAUNCHER_COLUMNS < list.length) launcherHighlight += LAUNCHER_COLUMNS;
      updateLauncherHighlight();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (launcherHighlight - LAUNCHER_COLUMNS >= 0) launcherHighlight -= LAUNCHER_COLUMNS;
      updateLauncherHighlight();
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      if (launcherHighlight % LAUNCHER_COLUMNS < LAUNCHER_COLUMNS - 1 && launcherHighlight + 1 < list.length) launcherHighlight++;
      updateLauncherHighlight();
    } else if (e.key === "ArrowLeft") {
      e.preventDefault();
      if (launcherHighlight % LAUNCHER_COLUMNS > 0) launcherHighlight--;
      updateLauncherHighlight();
    } else if (e.key === "Enter" && list[launcherHighlight]) {
      launchApp(list[launcherHighlight].id);
    }
  });
}


/** True while a text input/textarea has focus -- Del/Backspace and arrow
 *  keys must not hijack normal text editing inside the detail panel or
 *  quick-add's property fields. */
// ---- arrow-key navigation ----
//
// Each arrow key selects whichever visible neighbor sits closest to that
// screen direction from the currently selected mote -- not a fixed cycle
// order, so the key you press corresponds to where the neighbor actually
// is on screen.

const ARROW_ANGLES = { ArrowRight: 0, ArrowDown: 90, ArrowLeft: 180, ArrowUp: 270 };

function angleBetween(fromPos, toPos) {
  // Screen space has y increasing downward, so a plain atan2 already lines
  // up with "ArrowDown = 90deg, ArrowRight = 0deg" going clockwise.
  const degrees = (Math.atan2(toPos.y - fromPos.y, toPos.x - fromPos.x) * 180) / Math.PI;
  return degrees < 0 ? degrees + 360 : degrees;
}

function angleDelta(a, b) {
  const diff = Math.abs(a - b) % 360;
  return diff > 180 ? 360 - diff : diff;
}

function visibleNeighborsOf(moteId) {
  const ids = new Set();
  for (const rel of payload.relationships) {
    if (rel.from_id === moteId) ids.add(rel.to_id);
    else if (rel.to_id === moteId) ids.add(rel.from_id);
  }
  return [...ids].filter((id) => graph.isMoteVisible(id));
}

function navigateArrow(key) {
  const currentId = graph.selectedId;
  const fromPos = graph.screenPositionOf(currentId);
  if (!fromPos) return;

  const targetAngle = ARROW_ANGLES[key];
  let best = null, bestDelta = Infinity;
  for (const id of visibleNeighborsOf(currentId)) {
    const pos = graph.screenPositionOf(id);
    if (!pos) continue;
    const delta = angleDelta(angleBetween(fromPos, pos), targetAngle);
    if (delta < bestDelta) { bestDelta = delta; best = id; }
  }
  // No neighbors at all (a dead end) is a silent no-op, matching how most
  // graph/tree UIs handle running out of places to go.
  if (best) selectMote(best);
}

function isTypingInField() {
  const tag = document.activeElement && document.activeElement.tagName;
  return tag === "INPUT" || tag === "TEXTAREA";
}

function setupKeyboard() {
  window.addEventListener("keydown", (e) => {
    if ((e.key === "k" || e.key === "K") && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      openSearch();
      return;
    }

    if ((e.key === "j" || e.key === "J") && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      openLauncher();
      return;
    }
    if ((e.metaKey || e.ctrlKey) && /^[1-5]$/.test(e.key)) {
      const app = launcherChoices()[Number(e.key) - 1];
      if (app) {
        e.preventDefault();
        launchApp(app.id);
      }
      return;
    }

    if ((e.key === "Delete" || e.key === "Backspace") && !isTypingInField()) {
      if (graph.selectedIds.size > 0) {
        e.preventDefault();
        deleteSelection();
      } else if (graph.selectedRelIds.size > 0) {
        e.preventDefault();
        deleteRelationshipSelection();
      }
      return;
    }

    // Guarded by isTypingInField() so ordinary text copy/paste inside the
    // detail panel's own fields keeps working -- these only ever act on
    // the graph selection.
    if ((e.key === "c" || e.key === "C") && (e.metaKey || e.ctrlKey) && !isTypingInField()) {
      if (graph.selectedIds.size > 0) {
        e.preventDefault();
        copySelection([...graph.selectedIds]);
      }
      return;
    }
    if ((e.key === "v" || e.key === "V") && (e.metaKey || e.ctrlKey) && !isTypingInField()) {
      if (clipboard) {
        e.preventDefault();
        const center = graph.viewCenterWorld();
        const step = pasteRepeatCount * 24;
        pasteRepeatCount += 1;
        pasteAt(center.x + step, center.y + step);
      }
      return;
    }
    if ((e.key === "z" || e.key === "Z") && (e.metaKey || e.ctrlKey) && !isTypingInField()) {
      if (lastUndo) {
        e.preventDefault();
        runUndo();
      }
      return;
    }

    if (Object.prototype.hasOwnProperty.call(ARROW_ANGLES, e.key) && !isTypingInField()) {
      if (graph.selectedIds.size === 1) {
        e.preventDefault();
        navigateArrow(e.key);
      }
      return;
    }

    if (e.key !== "Escape") return;
    if (!$("search-overlay").classList.contains("hidden")) { closeSearch(); return; }
    if (launcherIsOpen()) { closeLauncher(); return; }
    if (!$("context-menu").classList.contains("hidden")) { hideContextMenu(); return; }
    if (openMenuName) { closeTopMenus(); return; }
    if (!$("quick-add").classList.contains("hidden")) { closeQuickAdd(); return; }
    if (!$("detail-panel").classList.contains("hidden")) { closeDetailPanel(); return; }
    // Leaving attractor mode is a smaller step than closing the panel it
    // lives in, so it comes first -- one Esc puts the graph back to
    // editable, a second puts the panel away.
    if (attractorMode) { exitAttractorMode(); return; }
    if (viewSettingsOpen()) { closeViewSettings(); return; }
    // Nothing else was open -- Esc's last job is to deselect.
    graph.select(null);
  });
}

// ---- init ----

/** Flags a property key or relationship type recognized by convention
 *  (core/reserved.py) with a small badge, so it doesn't look like
 *  ordinary user data. A dot rather than a text color: .rel-type is
 *  already accent-colored (it's clickable-to-rename), so recoloring a
 *  reserved one wouldn't read as different from an ordinary one.
 *
 *  The tooltip is the reserved name's actual meaning, straight from
 *  core/reserved.py, rather than a generic "this is reserved" -- the
 *  same text an MCP client gets from reserved_names(). `provider` and
 *  `configuration` on a property-model mote used to be raw text boxes
 *  here with nothing saying what they were for. */
function maybeAddReservedBadge(el, name, reservedSet) {
  if (!reservedSet.has(name)) return;
  const badge = document.createElement("span");
  badge.className = "reserved-badge";
  badge.title = reservedMeanings[name] ||
    "Reserved name -- recognized by MonOSS for special behavior.";
  el.appendChild(badge);
}

/** The provider names core/reserved.py knows about, offered as
 *  completions on a property-model mote's `provider` box. Not a
 *  restriction -- the property stays free text like any other, and a
 *  provider MonOSS doesn't have is a real thing to be able to type
 *  while setting one up. */
function providerOptions() {
  return Object.keys(propertyModelProviders).sort();
}

/** A one-line summary of what a provider can do, shown under the
 *  `provider` row of a property-model mote so its capabilities are
 *  visible where the model is edited rather than only over MCP. */
function providerSummary(name) {
  const provider = propertyModelProviders[name];
  if (!provider) return "";
  const operations = (provider.capabilities || [])
    .map((c) => `${c.operation}(${(c.inputs || []).join(", ")})`)
    .join(", ");
  return operations ? `${provider.description} Operations: ${operations}` : provider.description;
}

async function init() {
  api = window.pywebview.api;
  Inspector.install({
    api,
    // A write in the panel changes motes the canvas is drawing, and the
    // panel has no idea how to repaint a force-directed graph.
    onChanged: () => refocus(focusId, { pan: false }).then(refreshMoteCache),
    valueEditor: monarchValueEditor,
    advancedSections: [monarchSpeciesExtras],
    onOpenMora: () => showToast("Opening Mora…"),
  });
  CommandBar.install({
    api,
    onChanged: () => refocus(focusId, { pan: false }).then(refreshMoteCache),
    // Landing on a mote here means "recenter the graph on it", not just
    // opening the panel -- a command bar that finds R1 and leaves it off
    // in an unfocused corner of the canvas didn't really take you there.
    onNavigate: (moteId) => refocus(moteId).then(() => Inspector.open(moteId)),
  });

  const reserved = await call(api.reserved_names());
  reservedProperties = new Set(reserved.properties);
  reservedRelTypes = new Set(reserved.relationshipTypes);
  reservedMeanings = { ...reserved.propertyMeanings, ...reserved.relationshipTypeMeanings };
  propertyModelProviders = reserved.propertyModelProviders || {};

  graph = new MonossGraph($("graph"), {
    labelFor: nodeLabel,
    // No ctrl-modifier here: ctrl+drag from a mote draws a relationship,
    // and a ctrl-click that misses the drag threshold would then land as
    // "open in a new tab". Right-click -> Inspect in new tab instead.
    onSelect: (id) => selectMote(id),
    onConnect: (sourceId, targetId) => connectNodes(sourceId, targetId),
    onPositionsChanged: savePositions,
    onBoxSelect: updateSelectBox,
    onSelectionChanged: syncSelectionPanel,
    onContextMenu: openContextMenu,
    onCameraMove: reanchorInspector,
    // graph.js keeps its own copy of the attractor map, so the settings
    // this side persists have to be told where a dragged one landed.
    onAttractorMoved: (id, x, y) => {
      viewSettings.attractors[id] = { x, y };
      saveViewSettings();
      renderLayoutSettings();
    },
    onRelationshipsSelected: (ids) => {
      updateStatusbar();
      return ids.length === 0 ? closeDetailPanel()
        : ids.length === 1 ? openRelationshipPanel(ids[0])
        : renderMultiRelationshipPanel(ids);
    },
  });
  window.graph = graph;

  setupTopMenus();
  setupDetailResize();
  setupQuickAdd();
  setupSearch();
  setupViewControls();
  setupViewSettings();
  setupLauncher();
  setupSync();
  setupRefresh();
  setupContextMenu();
  setupKeyboard();
  applyControlPanelVisibility();

  currentGarden = await call(api.current_garden());
  await loadGarden(currentGarden);
}

window.addEventListener("pywebviewready", init);


// ---- Monarch's contributions to the shared inspector ------------------

/** Monarch's own value editors, offered to the shared panel before it
 *  falls back to its generic ones (frontend_shared/inspector.js). Both
 *  cases here are about a value the generic editors would flatten into a
 *  text box, and the list is deliberately short: anything only Monarch
 *  can edit is a thing the other five windows cannot, which is the
 *  situation the shared panel exists to end. */
function monarchValueEditor(data, key, meta, value, commit) {
  const text = typeof value === "string" ? value : "";

  // A property-model mote's `provider` names something with declared
  // capabilities, and saying what they are belongs under the box naming
  // it rather than only over MCP.
  if (key === "provider" && text.trim()) {
    const summary = providerSummary(text.trim());
    if (!summary) return null;
    const box = document.createElement("div");
    const input = document.createElement("input");
    input.className = "ins-input";
    input.spellcheck = false;
    input.value = text;
    input.setAttribute("list", "provider-options");
    ensureProviderOptions();
    input.addEventListener("change", () => commit(input.value));
    const note = document.createElement("div");
    note.className = "ins-empty";
    note.style.marginTop = "4px";
    note.textContent = summary;
    box.append(input, note);
    return box;
  }

  return null;
}

/** The registered providers, as completions on the `provider` box. A
 *  datalist rather than a <select>: `provider` is an ordinary property
 *  and naming one MonOSS doesn't have yet has to stay possible -- the
 *  list is help, not a constraint. */
function ensureProviderOptions() {
  let list = $("provider-options");
  if (!list) {
    list = document.createElement("datalist");
    list.id = "provider-options";
    document.body.appendChild(list);
  }
  list.innerHTML = "";
  for (const name of providerOptions()) {
    const option = document.createElement("option");
    option.value = name;
    list.appendChild(option);
  }
}

/** The species affordance the shared panel has no general form of:
 *  making another instance of this species. Offered on the species
 *  itself, which is where you are standing when you think "I want
 *  another one of these". */
function monarchSpeciesExtras(data) {
  if (!data.species.isSpecies) return null;
  const box = document.createElement("div");
  const make = document.createElement("button");
  make.type = "button";
  make.className = "ins-ghost";
  make.style.marginTop = "8px";
  make.textContent = "+ New instance";
  make.title = "Create a mote of this species, seeded from its template";
  make.addEventListener("click", async () => {
    if (editBlocked()) return;
    const created = await call(api.create_instance(data.id, {}));
    if (!created) return;
    await refreshMoteCache();
    // Focus what you just made, not what you made it from: selectMote
    // recentres the canvas on it and opens it in the panel, which is
    // what the old species section's button did too.
    await selectMote(created.id);
    showToast("Instance created.");
  });
  box.appendChild(make);
  return box;
}
