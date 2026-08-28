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
let speciesPickerTargetId = null;
let speciesPickerMode = "instance";   // 'instance' (set species) | 'template' (be a template of)
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
let simPreview = null;                 // the last preview_simulation() payload, while the sim bar is showing
let simStep = 0;                       // 0 = real/unprojected; k>0 = cumulative state through steps[k-1]
let simPlaying = false;
let simPlayTimer = null;
let openMenuName = null;               // 'garden' | 'file' | 'view' | 'tools' | null -- which topbar dropdown is open
let allRuns = [];                      // cached list_runs() result, while the runs panel is open
let selectedRunId = null;              // the run currently entered in history-scrub mode (simPreview.mode === 'history'), if any
let viewSettings = null;               // this garden's advanced view settings (forces/colors/attractors)
let attractorMode = false;             // arranging species attractors -- read-only, like a sim preview

const DEPTH_KEY = "monoss:depth";
const DETAIL_WIDTH_KEY = "monoss:detailWidth";
const DETAIL_WIDTH_MIN = 280;
const DETAIL_WIDTH_MAX = 640;
const SIM_STEPS_KEY = "monoss:simSteps";
const SIM_SOLVER_KEY = "monoss:simSolver";
const SIM_ERROR_TOAST_MS = 12000;
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

/** True (after showing a toast) whenever a simulation preview is open --
 *  gates every mutating action while previewing. Not just while scrubbed
 *  to a projected step: committing always re-solves from the *current* DB
 *  state, so an edit made mid-preview (even at the real/step-0 view) would
 *  silently diverge from what the preview actually showed. */
function simBlocked() {
  if (!simPreview) return false;
  showToast(
    simPreview.mode === "history"
      ? "Viewing a past run — exit to edit."
      : "Previewing a simulation — commit or discard to edit.",
    2600,
  );
  return true;
}

/** The read-only modes, as a single gate in front of every mutation.
 *  simBlocked() covers a simulation preview or history scrub; arranging
 *  attractors is read-only for the same reason -- what's on screen is a
 *  layout tool, not the editable graph. */
function editBlocked() {
  if (attractorMode) {
    showToast("Arranging attractors — click Done arranging to edit.", 2600);
    return true;
  }
  return simBlocked();
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
  closeSimBar();
  closeDetailPanel();
  closeRunsPanel();
  // Attractors are per garden, so an arrangement in progress means
  // nothing once a different one is open.
  if (attractorMode) exitAttractorMode();
  graph.setHiddenSpecies(readJSON(hiddenSpeciesKey(garden), []));
  graph.setHiddenRelTypes(readJSON(hiddenRelKey(garden), []));
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
    el.title = hidden ? `Show ${row.label}` : `Hide ${row.label}`;
    el.innerHTML =
      `<span class="dot${row.diamond ? " diamond" : ""}${hollow ? " hollow" : ""}" style="background:${row.color}"></span>` +
      `<span class="name"></span><span class="count">${row.count}</span>`;
    el.querySelector(".name").textContent = row.label;
    el.addEventListener("click", () => {
      const hiddenIds = new Set(graph.hiddenSpecies);
      hiddenIds.has(row.id) ? hiddenIds.delete(row.id) : hiddenIds.add(row.id);
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
    el.title = hidden ? `Show ${type}` : `Hide ${type}`;
    el.innerHTML = '<span class="name"></span><span class="count"></span>';
    el.querySelector(".name").textContent = type;
    el.querySelector(".count").textContent = counts.get(type);
    el.addEventListener("click", () => {
      const hiddenTypes = new Set(graph.hiddenRelTypes);
      hiddenTypes.has(type) ? hiddenTypes.delete(type) : hiddenTypes.add(type);
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
// The panel is docked rather than modal for the same reason the sim bar
// is: every control here is judged by what it does to the graph, so the
// graph has to stay visible while you drag a slider.

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
  closeRunsPanel();   // same corner; only one of them can have it
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

/** A read-only mode, gated the same way a simulation preview is (see
 *  editBlocked): the graph is there to be rearranged, not edited, so
 *  every canvas gesture except panning and zooming is off while it's on
 *  and every mutation elsewhere toasts instead. */
function enterAttractorMode() {
  if (simPreview) {
    showToast("Exit the simulation view first.", 2600);
    return;
  }
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

async function openDetailPanel(moteId) {
  const [mote, rels, species, instances, history] = await Promise.all([
    call(api.get_mote(moteId)),
    call(api.mote_relationships(moteId)),
    call(api.species_of(moteId)),
    call(api.instances_of(moteId)),
    call(api.mote_history(moteId)),
  ]);
  if (!mote) return;

  multiSelectIds = null;
  $("detail-multi").classList.add("hidden");
  $("detail-relationship").classList.add("hidden");
  $("detail-relationship-multi").classList.add("hidden");
  $("detail-single").classList.remove("hidden");
  $("detail-sim-banner").classList.toggle("hidden", !simPreview);

  // While scrubbed past the real/step-0 view, show the mote as the preview
  // projects it rather than its actual DB properties -- this is the
  // inspector's own read of "inspect the new values directly," alongside
  // the canvas highlight ring. Species/relationships aren't touched by
  // simulation, so only properties need the overlay.
  const displayMote = simStep > 0 ? withSimOverlay(mote) : mote;

  $("detail-dot").style.background = graph.colorFor(moteId);
  $("detail-type-label").textContent = kindLabel(displayMote);
  $("detail-title").textContent = nodeLabel(displayMote);
  $("detail-id").textContent = mote.id;

  renderProperties(displayMote, history, mote);
  renderNoteSection(mote);
  renderSpeciesSection(mote, species, instances);
  renderRelationships(moteId, rels);
  $("detail-panel").classList.remove("hidden");
  fitAllTextareas();
}

/** A mote's note is edited in Mora, not here -- .modoc is a document
 *  format with live bindings in it (docs/modoc.md), and a one-line text
 *  box in a side panel is the wrong instrument for one. So the panel
 *  offers the door rather than the room, and says which side of it the
 *  mote is on. */
function renderNoteSection(mote) {
  const button = $("open-note-btn");
  const has = typeof mote.properties.modoc === "string";
  button.textContent = has ? "Open note…" : "Write a note…";
  button.title = has
    ? "Open this mote's note in Mora"
    : "Open Mora on this mote and start a note";
  button.onclick = () => openMora(mote.id);
}

function openMora(moteId) {
  call(api.open_mora(moteId || null));
  showToast(
    moteId
      ? "Opening Mora… notes saved there appear here after a refocus."
      : "Opening Mora…",
  );
}

function openMonastery(moteId) {
  call(api.open_monastery(moteId || null));
  showToast(
    moteId
      ? "Opening Monastery… dates changed there appear here after a refocus."
      : "Opening Monastery…",
  );
}

function openMomentum(moteId) {
  call(api.open_momentum(moteId || null));
  showToast(
    moteId
      ? "Opening Momentum… statuses changed there appear here after a refocus."
      : "Opening Momentum…",
  );
}

function withSimOverlay(mote) {
  const overlay = {};
  for (const c of cumulativeSimChanges()) {
    if (c.mote_id === mote.id) overlay[c.key] = c.new_value;
  }
  return { ...mote, properties: { ...mote.properties, ...overlay } };
}

/** Shown instead of the single-mote panel once a box-select (or shift-add)
 *  produces more than one selected mote. Its own small render function
 *  rather than a generalization of openDetailPanel -- an aggregate view
 *  and a per-mote editor genuinely show different things, and trying to
 *  make one a special case of the other would tangle both. */
function renderMultiSelectPanel(ids) {
  multiSelectIds = ids;
  $("detail-single").classList.add("hidden");
  $("detail-relationship").classList.add("hidden");
  $("detail-relationship-multi").classList.add("hidden");
  $("detail-multi").classList.remove("hidden");
  hideSpeciesPicker();

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
  hideSpeciesPicker();
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
  $("detail-single").classList.add("hidden");
  $("detail-multi").classList.add("hidden");
  $("detail-relationship-multi").classList.add("hidden");
  $("detail-relationship").classList.remove("hidden");
  hideSpeciesPicker();

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
  $("detail-single").classList.add("hidden");
  $("detail-multi").classList.add("hidden");
  $("detail-relationship").classList.add("hidden");
  $("detail-relationship-multi").classList.remove("hidden");
  hideSpeciesPicker();

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

function fitAllTextareas() {
  $("detail-properties").querySelectorAll("textarea").forEach(fitTextarea);
}

/** Values long enough to wrap get a textarea sized to its content, so a
 *  rule's equation is readable in full instead of scrolling sideways in a
 *  one-line input. Shared by the plain fallback below and the recurrence
 *  picker's raw-rule mode. */
function plainField(text, onCommit) {
  const multiline = text.length > 38 || text.includes("\n");
  const field = document.createElement(multiline ? "textarea" : "input");
  if (!multiline) field.type = "text";
  field.value = text;

  if (multiline) {
    field.rows = 1;
    field.addEventListener("input", () => fitTextarea(field));
  }
  field.addEventListener("change", () => onCommit(field.value));
  return field;
}

/** `date` and `provider` get a small picker instead of hand-typed syntax;
 *  every other property keeps the plain text/textarea field. Falls back
 *  to plainField for a value that doesn't parse -- a picker that can't
 *  represent an existing value shouldn't clobber it. */
function valueField(key, rawValue, onCommit, moteId) {
  const text = typeof rawValue === "string" ? rawValue : JSON.stringify(rawValue);
  if (key === "modoc") return noteField(text);
  if (key === "provider") return providerField(text, onCommit);
  // By shape, not by name: `date` stopped being a key the engine reads
  // when a start became an ISO 8601 interval (core/interval.py), so any
  // property holding one gets the picker -- `when`, `due`, `starts`, or
  // whatever this garden calls it, repeating or not. A value the picker
  // can only partly represent -- an UNTIL= rule, a multi-day span --
  // still gets the picker, just with a raw-text fallback for the one
  // piece it can't structure-edit (see parseIntervalValue's `simple:
  // false` branches) rather than losing the picker entirely.
  if (text !== "") {
    if (parseIntervalValue(text)) return intervalField(text, onCommit, moteId);
  }
  return plainField(text, onCommit);
}

/** A note's source, summarized and read-only. Every other property is
 *  editable in place; this one is a whole document, and rendering it
 *  into an inline field would both bury the rest of the panel and put a
 *  note one stray keystroke from being overwritten. The row still
 *  deletes like any other -- removing a note is an ordinary property
 *  delete, because that is all it is. */
function noteField(text) {
  const lines = text.split("\n").length;
  const field = plainField(`${lines} line${lines === 1 ? "" : "s"} — edit in Mora`, () => {});
  field.readOnly = true;
  field.classList.add("note-summary");
  field.title = text.slice(0, 400);
  return field;
}

/** The "add a property" row's value box (#detail-new-value) has no key
 *  yet, so valueField's shape/key dispatch can't run on it -- there's
 *  nothing typed to sniff. Right-click offers the same picker choice up
 *  front instead (openValueTypeMenu, near the context menu). Swapping the
 *  node rather than the value keeps its id, so the existing add/Enter
 *  handlers keep reading $("detail-new-value").value unchanged -- an
 *  interval field defines that getter too (intervalField, above). */
function useIntervalForNewValue() {
  const old = $("detail-new-value");
  if (old.classList.contains("interval-field")) return;
  const picker = intervalField(isoOf(new Date()), () => {}, focusId);
  picker.id = "detail-new-value";
  old.replaceWith(picker);
  picker.querySelector(".interval-summary-btn").click();
}

/** Back to a plain text box, called once a property is actually added --
 *  the picker was only ever for shaping this one value, not a standing
 *  choice for the row. */
function resetNewValueField() {
  const old = $("detail-new-value");
  if (old.tagName === "INPUT") { old.value = ""; return; }
  const input = document.createElement("input");
  input.type = "text";
  input.id = "detail-new-value";
  input.placeholder = "value";
  wireNewValueRightClick(input);
  old.replaceWith(input);
}

function wireNewValueRightClick(el) {
  el.addEventListener("contextmenu", (e) => {
    e.preventDefault();
    openValueTypeMenu(e.clientX, e.clientY, (typeId) => {
      if (typeId === "interval") useIntervalForNewValue();
    });
  });
}

/** A plain text box with the registered providers as completions. Text,
 *  not a <select>: `provider` is an ordinary property like any other and
 *  naming one MonOSS doesn't have yet has to stay possible -- the list
 *  is help, not a constraint. */
function providerField(text, onCommit) {
  const field = plainField(text, onCommit);
  const options = providerOptions();
  if (options.length === 0) return field;

  const listId = "provider-options";
  let list = $(listId);
  if (!list) {
    list = document.createElement("datalist");
    list.id = listId;
    document.body.appendChild(list);
  }
  list.innerHTML = "";
  for (const name of options) {
    const option = document.createElement("option");
    option.value = name;
    list.appendChild(option);
  }
  field.setAttribute("list", listId);
  return field;
}

// ---- interval picker: constants + pure parse/compose helpers ----
//
// core/interval.py owns the real grammar; these mirror just enough of it
// in JS to drive a picker -- decomposing a value into an editable draft
// and recomposing one back. Never expanding a recurring value into
// occurrences, though (core/calendar.py's job, via dateutil -- "no
// JavaScript reads an RRULE" stays true for *that*). A piece of a value
// this can't safely decompose (a multi-day span, an UNTIL= rule, two
// RRULE lines) is marked `simple: false` and kept as raw, editable text
// instead of being silently rewritten or dropped.

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

/** A collapsed summary button that expands into a full calendar grid,
 *  time/end-time, and repeat controls -- Monarch's Observatory design
 *  (docs/ui-setup.md). Defines its own `value` getter/setter so the
 *  revert-on-block call site in renderProperties() (`field.value =
 *  displayedText`) works unmodified, same as a plain <input>.
 *
 *  Writes on every control change rather than only on Done, and always
 *  in the duration form (`start/PT_H_M`) -- this editor doesn't need
 *  core/interval.py's format_like style-preservation, which exists for
 *  Monastery's drag/resize paths where exactly one dimension may change
 *  at a time. moteId is used only by Clear, to rebuild the row the same
 *  way the per-property delete (×) button already does once the value
 *  stops being interval-shaped. */
function intervalField(rawValue, onCommit, moteId) {
  let draft = parseIntervalValue(rawValue);
  let month = draft.date.slice(0, 7);
  let open = false;

  const wrap = document.createElement("div");
  wrap.className = "interval-field";

  const summaryBtn = document.createElement("button");
  summaryBtn.type = "button";
  summaryBtn.className = "interval-summary-btn";

  const panel = document.createElement("div");
  panel.className = "interval-panel hidden";

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
        draft.rule = { simple: false, raw: "RRULE:FREQ=WEEKLY" };
        renderRepeatDetail();
        commit();
      });
      repeatDetail.appendChild(addBtn);
      return;
    }

    const note = document.createElement("div");
    note.className = "interval-fallback-note";
    note.textContent = "Structured repeat editing lives in Monastery now — edit the rule as text here:";
    const seed = draft.rule.simple ? composeRuleLines(draft.rule).join("\n") : draft.rule.raw;
    const field = plainField(seed, (v) => {
      draft.rule = { simple: false, raw: v };
      commit();
    });
    const reset = document.createElement("button");
    reset.type = "button"; reset.className = "interval-fallback-reset"; reset.textContent = "Remove repeat";
    reset.addEventListener("click", () => {
      draft.rule = { simple: true, freq: "NONE", interval: 1, byday: [], count: null, until: null, otherLines: draft.rule.otherLines || [] };
      renderRepeatDetail();
      commit();
    });
    repeatDetail.append(note, field, reset);
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

  summaryBtn.addEventListener("click", () => {
    open = !open;
    panel.classList.toggle("hidden", !open);
    summaryBtn.classList.toggle("open", open);
  });
  doneBtn.addEventListener("click", () => {
    open = false;
    panel.classList.add("hidden");
    summaryBtn.classList.remove("open");
  });
  clearBtn.addEventListener("click", async () => {
    if (editBlocked()) return;
    open = false;
    panel.classList.add("hidden");
    await onCommit("");
    await openDetailPanel(moteId);
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

function openLink(url) {
  call(api.open_link(url));
}

/** A tiny inline trend line for one property's history -- hand-rolled
 *  canvas draw, no charting library, same spirit as graph.js's own
 *  from-scratch renderer. min/max-scaled so a flat run of identical
 *  values still draws as a flat (not division-by-zero) line. */
function renderSparkline(values, unitLabel = "runs", color = "oklch(0.74 0.10 68)") {
  const canvas = document.createElement("canvas");
  canvas.className = "sparkline";
  canvas.width = 52;
  canvas.height = 18;
  canvas.title = `${values[0]} → ${values[values.length - 1]} (${values.length} ${unitLabel})`;

  const ctx = canvas.getContext("2d");
  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = max - min || 1;
  const w = canvas.width, h = canvas.height, pad = 2;

  ctx.beginPath();
  values.forEach((v, i) => {
    const x = pad + (i / (values.length - 1)) * (w - pad * 2);
    const y = h - pad - ((v - min) / range) * (h - pad * 2);
    i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
  });
  ctx.strokeStyle = color;
  ctx.lineWidth = 1.3;
  ctx.stroke();

  return canvas;
}

/** While a simulation (preview or entered-run history) is open, a
 *  property's sparkline should trace that property's own trajectory
 *  across this simulation's ticks rather than the garden's committed
 *  run history -- the two are read from different places entirely, so
 *  this walks simPreview.steps directly instead of going through
 *  cumulativeSimChanges (which only folds up to the current scrub
 *  position). Returns null if the property never moves, matching the
 *  "nothing to show" gate used for garden history. */
function simTrajectoryFor(moteId, key, baseValue) {
  if (typeof baseValue !== "number") return null;
  const values = [baseValue];
  let current = baseValue;
  for (const step of simPreview.steps) {
    for (const c of step.changes) {
      if (c.mote_id === moteId && c.key === key) current = c.new_value;
    }
    values.push(current);
  }
  return values.some((v) => v !== values[0]) ? values : null;
}

function renderProperties(mote, history, baseMote) {
  const container = $("detail-properties");
  container.innerHTML = "";
  const entries = Object.entries(mote.properties);
  if (entries.length === 0) {
    container.innerHTML = '<div class="empty-note">None</div>';
    return;
  }
  for (const [key, value] of entries) {
    const row = document.createElement("div");
    row.className = "detail-row";

    const keyEl = document.createElement("span");
    keyEl.className = "key";
    keyEl.textContent = key;
    keyEl.title = key;
    maybeAddReservedBadge(keyEl, key, reservedProperties);

    const displayedText = typeof value === "string" ? value : JSON.stringify(value);
    let currentField;
    const commitProperty = async (next) => {
      if (editBlocked()) { currentField.value = displayedText; return; }
      await call(api.update_mote(mote.id, { [key]: next }));
      await refocus(focusId, { pan: false });
      await refreshMoteCache();
    };
    const field = valueField(key, value, commitProperty, mote.id);
    currentField = field;

    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "×";
    del.title = `Delete ${key}`;
    del.addEventListener("click", async () => {
      if (editBlocked()) return;
      await call(api.delete_property(mote.id, key));
      await refocus(focusId, { pan: false });
      await openDetailPanel(mote.id);
    });

    // A property gets a picker by shape once it already holds a value
    // that fits one (valueField, above); this is how to reach the same
    // picker before that's true -- an empty box, or one holding plain
    // text you mean to replace. modoc/provider keep their own dedicated
    // editors and are left alone; an interval field already has the
    // picker, so it doesn't need a second way in.
    if (key !== "modoc" && key !== "provider" && !field.classList.contains("interval-field")) {
      field.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        openValueTypeMenu(e.clientX, e.clientY, (typeId) => {
          if (typeId !== "interval") return;
          const seed = parseIntervalValue(displayedText) ? displayedText : isoOf(new Date());
          const picker = intervalField(seed, commitProperty, mote.id);
          currentField.replaceWith(picker);
          currentField = picker;
          picker.querySelector(".interval-summary-btn").click();
        });
      });
    }

    // "url" is a reserved property name -- a mote holding one, whether a
    // web link or a local path, gets a one-click way to launch it via the
    // OS's default handler instead of only being able to read/edit it.
    const trailing = [];
    const values = simPreview
      ? simTrajectoryFor(mote.id, key, (baseMote || mote).properties[key])
      : history && history[key];
    if (values && values.length > 1) {
      const color = simPreview && simPreview.mode === "history" ? "oklch(0.62 0.19 300)" : undefined;
      trailing.push(renderSparkline(values, simPreview ? "ticks" : "runs", color));
    }
    // "modoc" is a note (docs/modoc.md) -- same one-click-to-follow
    // treatment "url" gets below, since the row itself is deliberately
    // only a summary.
    if (key === "modoc" && typeof value === "string") {
      const open = document.createElement("button");
      open.className = "open-link";
      open.textContent = "↗";
      open.title = "Open in Mora";
      open.addEventListener("click", () => openMora(mote.id));
      trailing.push(open);
    }
    // Any numeric property may be something the solver determined, or
    // something other values depend on. Which of the two it is is
    // exactly what the panel answers, so the button is offered for both
    // rather than guessing from whether a value looks solved.
    if (typeof value === "number") {
      const explain = document.createElement("button");
      explain.className = "explain";
      explain.textContent = "?";
      explain.title = `Why ${key}, and what depends on it`;
      explain.addEventListener("click", () => toggleExplain(row, explain, mote.id, key));
      trailing.push(explain);
    }
    if (key === "url" && typeof value === "string" && value.trim()) {
      const open = document.createElement("button");
      open.className = "open-link";
      open.textContent = "↗";
      open.title = "Open";
      open.addEventListener("click", () => openLink(value));
      trailing.push(open);
    }
    row.append(keyEl, field, ...trailing, del);
    container.appendChild(row);

    // On a property-model mote, say what the named provider can actually
    // compute, right under the box naming it -- the CLI has had `model
    // capabilities` for this and the app had nothing.
    if (key === "provider" && typeof value === "string") {
      const summary = providerSummary(value.trim());
      if (summary) {
        const note = document.createElement("div");
        note.className = "detail-note";
        note.textContent = summary;
        container.appendChild(note);
      }
    }
  }
}

/* The scenario picker lists every mote that varies something, plus the
 * garden itself. Refreshed whenever the graph changes rather than only
 * at startup, because a scenario is built out of ordinary motes and
 * relationships -- the user can create one without leaving the app. */
async function refreshScenarioPicker() {
  const select = $("sim-scenario-select");
  if (!select) return;
  const previous = select.value;
  // Nothing awaits this -- it is fired from the menu-open handler -- so a
  // failure has to end here rather than surface as an unhandled rejection.
  let scenarios;
  try {
    scenarios = await api.list_scenarios();
  } catch {
    return;
  }

  // Built as DOM rather than interpolated into innerHTML: a scenario's
  // name is an ordinary mote property, so it is user data, and
  // textContent cannot be made to mean markup.
  select.replaceChildren();
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "Garden as it stands";
  select.appendChild(none);
  for (const scenario of scenarios) {
    const option = document.createElement("option");
    option.value = scenario.id;
    const motes = `${scenario.target_count} mote${scenario.target_count === 1 ? "" : "s"}`;
    option.textContent = `${scenario.name} (${motes})`;
    select.appendChild(option);
  }
  if (scenarios.some((s) => s.id === previous)) select.value = previous;
}

/* Explanation and dependencies for one property, opened inline under its
 * row. Two questions with two different sources: `why` reads the run log
 * (so it needs a run to have happened), while `dependencies` reads the
 * rules themselves and answers before anything is solved. Showing them
 * together is the point -- "this is how it was found" next to "this is
 * what it rests on and what rests on it". */
async function toggleExplain(row, button, moteId, key) {
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains("explain-block")) {
    existing.remove();
    button.classList.remove("open");
    return;
  }
  button.classList.add("open");

  const block = document.createElement("div");
  block.className = "explain-block";
  block.textContent = "Loading…";
  row.after(block);

  try {
    const [explanation, deps] = await Promise.all([
      call(api.why(moteId, key)),
      call(api.dependencies(moteId, key)),
    ]);
    block.replaceChildren(...explanationNodes(explanation), ...dependencyNodes(deps));
  } catch {
    // call() has already toasted the reason; leaving "Loading…" sitting
    // there forever would be the only thing worse than saying so.
    block.replaceChildren(explainLine("Could not load.", "explain-note"));
  }
}

function explainHeading(text) {
  const el = document.createElement("div");
  el.className = "explain-heading";
  el.textContent = text;
  return el;
}

function explainLine(text, className) {
  const el = document.createElement("div");
  if (className) el.className = className;
  el.textContent = text;
  return el;
}

function moteLabel(moteId) {
  const mote = moteById(moteId);
  return mote ? nodeLabel(mote) : moteId.slice(0, 8);
}

function variableLabel(moteId, key) {
  return `${moteLabel(moteId)}.${key}`;
}

/* One derivation and, recursively, the derived values it read. Stops
 * wherever the run didn't determine something -- a stored property, a
 * property-model call -- which is exactly the set of things needing no
 * explanation. */
function explanationNodes(explanation, depth = 0) {
  const nodes = [];
  if (depth === 0) nodes.push(explainHeading("How it was found"));

  if (!explanation || !explanation.method) {
    nodes.push(explainLine(
      (explanation && explanation.note) || "Not determined by any run.", "explain-note"
    ));
    return nodes;
  }

  for (const equation of explanation.equations) {
    nodes.push(explainLine(`${equation.rule_name} on ${moteLabel(equation.anchor_mote_id)}`));
    nodes.push(explainLine(equation.equation, "explain-equation"));
  }
  if (explanation.also_determined.length > 0) {
    nodes.push(explainLine(
      "Found together with " +
      explanation.also_determined.map((v) => variableLabel(v.mote_id, v.key)).join(", "),
      "explain-note"
    ));
  }
  for (const input of explanation.inputs) {
    const line = explainLine(
      `${input.kind} · ${variableLabel(input.mote_id, input.key)} = ${formatNumber(input.value)}`
    );
    nodes.push(line);
    if (input.derivation) {
      const nested = document.createElement("div");
      nested.className = "explain-nested";
      nested.append(...explanationNodes(input.derivation, depth + 1));
      nodes.push(nested);
    }
  }
  if (depth === 0) {
    nodes.push(explainLine("Values in base SI units — what the solver worked in.", "explain-note"));
  }
  return nodes;
}

function dependencyNodes(deps) {
  const nodes = [];

  nodes.push(explainHeading("What it needs"));
  if (!deps.free) {
    nodes.push(explainLine(
      "Not among this mote's freedoms, so nothing can determine it.", "explain-note"
    ));
  } else if (!deps.determinable) {
    nodes.push(explainLine("Free, but no equation constrains it.", "explain-note"));
  } else {
    nodes.push(explainLine(`Determined by ${deps.producers.join(", ")}`));
    const chips = document.createElement("div");
    for (const need of deps.needs) {
      const chip = document.createElement("span");
      chip.className = `explain-chip${need.status === "missing" ? " missing" : ""}`;
      chip.textContent = `${need.status} ${variableLabel(need.mote_id, need.key)}`;
      chips.appendChild(chip);
    }
    if (deps.needs.length === 0) chips.appendChild(explainLine("Nothing.", "explain-note"));
    nodes.push(chips);
  }

  nodes.push(explainHeading("What it could move"));
  if (deps.impact.length === 0) {
    nodes.push(explainLine("Nothing depends on it.", "explain-note"));
  } else {
    const chips = document.createElement("div");
    for (const effect of deps.impact) {
      const chip = document.createElement("span");
      chip.className = "explain-chip explain-link";
      chip.textContent = variableLabel(effect.mote_id, effect.key);
      chip.title = `${effect.distance} step(s) away, via ${effect.through.join(", ")}`;
      chip.addEventListener("click", () => selectMote(effect.mote_id));
      chips.appendChild(chip);
    }
    nodes.push(chips);
  }
  return nodes;
}

/* Six significant figures, with the padding zeros toPrecision leaves
 * behind trimmed off -- and the bare decimal point that trimming can
 * expose (2.0000001 -> "2.00000" -> "2.") trimmed with them. */
function formatNumber(value) {
  if (typeof value !== "number") return String(value);
  if (Number.isInteger(value)) return String(value);
  const text = value.toPrecision(6);
  return text.includes("e") ? text : text.replace(/0+$/, "").replace(/\.$/, "");
}

function renderSpeciesSection(mote, species, instances) {
  const container = $("detail-species");
  container.innerHTML = "";
  if (species.length === 0) {
    container.innerHTML = '<div class="empty-note">Not an instance of anything</div>';
  }
  for (const speciesMote of species) {
    const rel = payload.relationships.find(
      (r) => r.type === "instance_of" && r.from_id === mote.id && r.to_id === speciesMote.id
    );
    const row = document.createElement("div");
    row.className = "rel-row";

    const label = document.createElement("span");
    label.className = "rel-other";
    label.textContent = nodeLabel(speciesMote);
    label.addEventListener("click", () => selectMote(speciesMote.id));

    const del = document.createElement("button");
    del.textContent = "×";
    del.title = "Remove from this species";
    del.addEventListener("click", async () => {
      if (editBlocked()) return;
      if (rel) await call(api.delete_relationship(rel.id));
      await refocus(focusId, { pan: false });
      await openDetailPanel(mote.id);
    });

    row.append(label, del);
    container.appendChild(row);
  }

  // Instantiating from a species is offered on the species itself, which
  // is where you are when you think "I want another one of these".
  const isSpecies = instances.length > 0 || isKind(mote.id, KIND.SPECIES);
  $("new-instance-btn").classList.toggle("hidden", !isSpecies);
  $("detail-instance-count").textContent =
    instances.length > 0 ? `${instances.length} instance${instances.length === 1 ? "" : "s"}` : "";
}

function renderRelationships(moteId, rels) {
  const container = $("detail-relationships");
  container.innerHTML = "";
  const shown = rels.filter((r) => r.type !== "instance_of");
  if (shown.length === 0) {
    container.innerHTML = '<div class="empty-note">None</div>';
  }
  for (const rel of shown) {
    const otherId = rel.from_id === moteId ? rel.to_id : rel.from_id;
    const other = moteById(otherId);
    const row = document.createElement("div");
    row.className = "rel-row";

    const arrow = document.createElement("span");
    arrow.className = "arrow";
    arrow.textContent = rel.from_id === moteId ? "→" : "←";

    const type = document.createElement("span");
    type.className = "rel-type";
    type.textContent = rel.type;
    type.title = "Click to rename";
    maybeAddReservedBadge(type, rel.type, reservedRelTypes);
    type.addEventListener("click", async () => {
      if (await renameRelationshipType(rel)) await openDetailPanel(moteId);
    });

    const label = document.createElement("span");
    label.className = "rel-other";
    label.textContent = other ? nodeLabel(other) : otherId.slice(0, 8) + "…";
    label.title = otherId;
    label.addEventListener("click", () => selectMote(otherId));

    const del = document.createElement("button");
    del.textContent = "×";
    del.addEventListener("click", async () => {
      if (await deleteRelationshipAndRefresh(rel)) await openDetailPanel(moteId);
    });

    row.append(arrow, type, label, del);
    container.appendChild(row);
  }
}

async function selectMote(moteId, opts = {}) {
  await refocus(moteId, opts);
  await openDetailPanel(moteId);
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

// ---- species picker + new instance ----

function hideSpeciesPicker() {
  $("species-picker-wrap").classList.add("hidden");
  $("species-picker-input").value = "";
  speciesPickerTargetId = null;
  speciesPickerMode = "instance";
}

/** One picker, two questions. "Set species…" declares this mote an
 *  instance of the one you pick; "Make template of…" declares it that
 *  species' template instead. Both are "point at a species", so they
 *  share the picker rather than the second growing its own copy of it --
 *  templates previously had no UI at all, reachable only over MCP or by
 *  hand-typing a template_of relationship. */
function openSpeciesPicker(mode) {
  if (editBlocked()) return;
  if (!focusId) return;
  speciesPickerMode = mode;
  speciesPickerTargetId = focusId;
  $("species-picker-input").placeholder =
    mode === "template" ? "Template of which species?…" : "Search for a species…";
  $("species-picker-wrap").classList.remove("hidden");
  renderSpeciesPickerResults("");
  $("species-picker-input").focus();
}

function renderSpeciesPickerResults(query) {
  const container = $("species-picker-results");
  container.innerHTML = "";
  const q = (query || "").toLowerCase();
  const candidates = allMotes
    .filter((m) => m.id !== speciesPickerTargetId)
    .filter((m) => !q || nodeLabel(m).toLowerCase().includes(q))
    .sort((a, b) => nodeLabel(a).localeCompare(nodeLabel(b)))
    .slice(0, 40);

  if (candidates.length === 0) {
    container.innerHTML = '<div class="search-empty">No matches</div>';
    return;
  }
  candidates.forEach((mote, i) => {
    const row = document.createElement("div");
    row.className = "search-result-row" + (i === 0 ? " highlighted" : "");
    row.innerHTML = '<span class="label"></span>';
    row.querySelector(".label").textContent = nodeLabel(mote);
    row.addEventListener("mousedown", async (e) => {
      e.preventDefault();
      if (editBlocked()) return;
      const targetId = speciesPickerTargetId;
      const mode = speciesPickerMode;
      await call(
        mode === "template"
          ? api.set_template(targetId, mote.id)
          : api.set_species(targetId, mote.id)
      );
      hideSpeciesPicker();
      await refocus(targetId, { pan: false });
      await openDetailPanel(targetId);
      showToast(mode === "template" ? "Template set." : "Species set.");
    });
    container.appendChild(row);
  });
}

function setupSpeciesControls() {
  $("set-species-btn").addEventListener("click", () => openSpeciesPicker("instance"));
  $("set-template-btn").addEventListener("click", () => openSpeciesPicker("template"));
  $("species-picker-input").addEventListener("input", (e) =>
    renderSpeciesPickerResults(e.target.value)
  );
  $("species-picker-input").addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideSpeciesPicker();
  });
  setupPickerKeyNav($("species-picker-input"), $("species-picker-results"));

  $("new-instance-btn").addEventListener("click", async () => {
    if (editBlocked()) return;
    if (!focusId) return;
    const mote = await call(api.create_instance(focusId, {}));
    await refreshMoteCache();
    await selectMote(mote.id);
    showToast("Instance created.");
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

/** Shared by openContextMenu and openValueTypeMenu: drops the menu at the
 *  click point, then nudges it back onscreen if it would overflow. */
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

/** A property's value has no stored type -- valueField (above) picks an
 *  editor by shape, which only works once a value already exists to sniff.
 *  This is the other direction: right-click an empty or plain-text value
 *  box to say up front what it should hold. One entry today (the interval
 *  picker); a future property type -- a mote reference, a color -- is
 *  another line here, not a redesign. */
const VALUE_TYPE_PICKERS = [
  { id: "interval", label: "Date & time…" },
];

function openValueTypeMenu(clientX, clientY, onChoose) {
  closeTopMenus();
  const menu = $("context-menu");
  menu.innerHTML = "";
  for (const t of VALUE_TYPE_PICKERS) {
    addMenuItem(menu, t.label, () => onChoose(t.id));
  }
  positionMenu(menu, clientX, clientY);
}

async function openContextMenu(clientX, clientY, targetId, worldX, worldY) {
  closeTopMenus(); // mutual exclusion with the topbar dropdowns, both ways
  // graph.js may have just replaced the selection with the right-clicked
  // mote (or left an existing multi-selection that includes it) -- the
  // panel needs to catch up before the menu's actions read graph.selectedIds.
  if (targetId) await syncSelectionPanel([...graph.selectedIds]);

  const menu = $("context-menu");
  menu.innerHTML = "";

  if (targetId) {
    const node = graph.nodes.get(targetId);
    // Right-clicking a mote that's part of the current multi-selection
    // (syncSelectionPanel above already reconciled it) acts on the whole
    // selection, matching the precedent Delete already set.
    const ids = graph.selectedIds.has(targetId) ? [...graph.selectedIds] : [targetId];
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
// (#run-sim-btn, #check-roles-btn, ...) so the setup functions that
// already own those elements' behavior need no changes, only a leading
// closeTopMenus() so choosing an action closes the menu it came from.

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
  else if (name === "tools") refreshToolsMenu();
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

/** Computed at open time rather than kept in sync continuously -- simpler
 *  than threading a menu refresh into every place simPreview changes,
 *  and the menu is closed the rest of the time anyway. */
function refreshToolsMenu() {
  $("run-sim-btn").classList.toggle("hidden", !!simPreview);
  $("exit-sim-btn").classList.toggle("hidden", !simPreview);
  // A scenario is built from ordinary motes and relationships, so one
  // can appear without leaving the app. Reloading the list when the menu
  // opens is exactly when it matters and costs nothing the rest of the
  // time.
  refreshScenarioPicker();
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

/** Shared by the garden dropdown's "+ New garden…" and the File menu's
 *  "New garden…" -- two entry points to the same action, so they can't
 *  drift. closeGardensModal() is a harmless no-op if the modal isn't the
 *  one open (it's the gardens modal's own #new-garden-btn that also calls
 *  this, and that one *is* inside the modal). */
async function createGardenFlow() {
  const name = window.prompt("New garden name:");
  if (!name) return;
  await call(api.create_garden(name));
  await call(api.switch_garden(name));
  closeGardensModal();
  await loadGarden(name);
}

/** Shared by the gardens modal's per-row Export button and the File
 *  menu's "Export garden as JSON…" -- the same action on the same
 *  format, so the File menu points at the real thing rather than a
 *  "soon" stub sitting next to a feature that already works. */
async function exportGardenFlow(name) {
  const result = await call(api.export_garden_to_file(name));
  if (!result.cancelled) showToast(`Exported to ${result.path}`);
}

/** Shared by the gardens modal's Import button and the File menu's
 *  "Import garden…". Imports always land in a new garden: garden_io
 *  refuses to merge into one that already holds any of the incoming
 *  mote ids (core/garden_io.py), so asking for a name up front is the
 *  honest flow rather than discovering the conflict afterward. */
async function importGardenFlow() {
  const name = window.prompt("Import into a new garden named:");
  if (!name) return;
  const result = await call(api.import_garden_into_new(name));
  if (result.cancelled) return;
  await call(api.switch_garden(result.garden));
  closeGardensModal();
  await loadGarden(result.garden);
  showToast(`Imported ${result.motes} motes, ${result.relationships} relationships.`);
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

  $("garden-menu-new").addEventListener("click", () => { closeTopMenus(); createGardenFlow(); });
  $("garden-menu-manage").addEventListener("click", () => { closeTopMenus(); openGardensModal(); });
  $("file-new-garden").addEventListener("click", () => { closeTopMenus(); createGardenFlow(); });
  $("file-import-garden").addEventListener("click", () => { closeTopMenus(); importGardenFlow(); });
  $("file-export-garden").addEventListener("click", () => {
    closeTopMenus();
    exportGardenFlow(currentGarden);
  });
  $("view-reset-view").addEventListener("click", () => { closeTopMenus(); resetViewAndUnhideAll(); });
  $("view-toggle-panel").addEventListener("click", () => { closeTopMenus(); toggleControlPanel(); });
  $("exit-sim-btn").addEventListener("click", () => { closeTopMenus(); discardSimPreview(); });
}

// ---- gardens modal ----
//
// Reached from the garden dropdown's "Manage gardens…" (and, for New
// garden specifically, duplicated as a quick entry point in both that
// dropdown and the File menu) -- a centered modal, not an anchored
// dropdown, gives Export/Delete room to sit as real per-row buttons
// instead of being crammed into a narrow menu item.

async function renderGardensList() {
  const [gardens, defaultGarden] = await Promise.all([
    call(api.list_gardens()),
    call(api.get_default_garden()),
  ]);
  const container = $("gardens-list");
  container.innerHTML = "";

  for (const name of gardens) {
    const isDefault = name === defaultGarden;
    const row = document.createElement("div");
    row.className = "garden-row";
    row.innerHTML =
      '<span class="check"></span><span class="name"></span>' +
      '<button type="button" class="linkish star"></button>' +
      '<button type="button" class="ghost">Export</button>' +
      '<button type="button" class="danger">Delete</button>';
    row.querySelector(".check").textContent = name === currentGarden ? "✓" : "";
    row.querySelector(".name").textContent = name;

    // Whichever garden this points at is what a fresh launch (no
    // --garden flag, and any MCP/CLI call that omits garden) opens on,
    // in place of silently auto-creating an empty "default" -- see
    // core/gardens.py::resolve_garden. Clicking the star on the current
    // default clears the preference back to that old behavior.
    const starBtn = row.querySelector(".star");
    starBtn.textContent = isDefault ? "★" : "☆";
    starBtn.title = isDefault
      ? "Default garden -- click to unset"
      : "Set as default garden";
    starBtn.classList.toggle("on", isDefault);
    starBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (isDefault) {
        await call(api.clear_default_garden());
      } else {
        await call(api.set_default_garden(name));
      }
      await renderGardensList();
    });

    row.addEventListener("click", async () => {
      if (name === currentGarden) return;
      closeGardensModal();
      await call(api.switch_garden(name));
      await loadGarden(name);
    });

    row.querySelector(".ghost").addEventListener("click", (e) => {
      e.stopPropagation();
      exportGardenFlow(name);
    });

    // Disabled client-side for the same two cases the backend itself
    // blocks, so the common case never even reaches a rejected-promise
    // toast -- switching away first (or there being nothing else) is
    // obvious just from looking at the list.
    const deleteBtn = row.querySelector(".danger");
    if (name === currentGarden || gardens.length <= 1) {
      deleteBtn.disabled = true;
      deleteBtn.title = name === currentGarden
        ? "Switch to another garden before deleting this one"
        : "Can't delete the last garden";
    } else {
      deleteBtn.addEventListener("click", async (e) => {
        e.stopPropagation();
        if (!window.confirm(`Delete garden '${name}'?`)) return;
        await call(api.delete_garden(name));
        await renderGardensList();
        showUndoToast(`Deleted garden '${name}'.`, async () => {
          await call(api.restore_garden(name));
          await renderGardensList();
          showToast(`Restored garden '${name}'.`);
        });
      });
    }

    container.appendChild(row);
  }
}

function openGardensModal() {
  $("gardens-overlay").classList.remove("hidden");
  renderGardensList();
}

function closeGardensModal() {
  $("gardens-overlay").classList.add("hidden");
}

function setupGardensModal() {
  $("gardens-overlay").addEventListener("mousedown", (e) => {
    if (e.target === $("gardens-overlay")) closeGardensModal();
  });

  $("new-garden-btn").addEventListener("click", createGardenFlow);
  $("import-garden-btn").addEventListener("click", importGardenFlow);
}

// ---- sync modal ----
//
// Reached from Tools -> Sync…. core/sync.py already does the real work
// (push/pull over an append-only oplog, safe for a folder-sync tool like
// Syncthing/Dropbox/iCloud Drive to carry); this modal just exposes
// enable/disable, folder choice, manual sync, and the auto-sync flag
// that gates monarch/api.py's existing lifecycle sync points (open, garden
// switch, close) -- there's no new timer here, "automatic" means those
// three call sites, not a background poll.

async function renderSyncModal() {
  const status = await call(api.sync_status());
  const enabled = !!status.sync_dir;

  $("sync-disabled-body").classList.toggle("hidden", enabled);
  $("sync-enabled-body").classList.toggle("hidden", !enabled);
  if (!enabled) return;

  $("sync-folder-path").textContent = status.sync_dir;
  $("sync-folder-path").title = status.sync_dir;
  $("sync-auto-checkbox").checked = status.auto_sync;

  const lines = [`${status.local_ops_pending} of ${status.local_ops} local change(s) not yet pushed.`];
  lines.push(status.remotes.length
    ? `${status.remotes.length} other device(s) seen.`
    : "No other devices seen yet.");
  $("sync-status-lines").innerHTML = lines.map((l) => `<p>${l}</p>`).join("");
}

function openSyncModal() {
  $("sync-overlay").classList.remove("hidden");
  renderSyncModal();
}

function closeSyncModal() {
  $("sync-overlay").classList.add("hidden");
}

function setupSyncModal() {
  $("sync-overlay").addEventListener("mousedown", (e) => {
    if (e.target === $("sync-overlay")) closeSyncModal();
  });

  const enableFlow = async () => {
    const picked = await call(api.choose_sync_folder());
    if (picked.cancelled) return;
    await call(api.enable_sync(picked.path));
    await renderSyncModal();
    showToast("Sync enabled.");
  };
  $("sync-enable-btn").addEventListener("click", enableFlow);
  $("sync-change-folder-btn").addEventListener("click", enableFlow);

  $("sync-auto-checkbox").addEventListener("change", (e) => {
    call(api.set_auto_sync(e.target.checked));
  });

  $("sync-now-btn").addEventListener("click", async () => {
    const btn = $("sync-now-btn");
    btn.disabled = true;
    btn.textContent = "Syncing…";
    try {
      await call(api.sync_now());
      showToast("Synced.");
    } finally {
      btn.disabled = false;
      btn.textContent = "Sync now";
    }
    await renderSyncModal();
  });

  $("sync-disable-btn").addEventListener("click", async () => {
    if (!window.confirm("Stop syncing this garden? Your local data is unaffected.")) return;
    await call(api.disable_sync());
    await renderSyncModal();
    showToast("Sync disabled.");
  });
}

function setupSync() {
  $("open-sync-btn").addEventListener("click", () => {
    closeTopMenus();
    openSyncModal();
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
  $("close-panel").addEventListener("click", closeDetailPanel);

  wireNewValueRightClick($("detail-new-value"));

  $("detail-add-property-btn").addEventListener("click", async () => {
    if (editBlocked()) return;
    const key = $("detail-new-key").value.trim();
    if (!key || !focusId) return;
    await call(api.update_mote(focusId, { [key]: $("detail-new-value").value }));
    $("detail-new-key").value = "";
    resetNewValueField();
    await refocus(focusId, { pan: false });
    await openDetailPanel(focusId);
  });

  // Mirrors quick-add's own Enter-to-submit (setupQuickAdd) -- this is the
  // same "type key/value, press Enter" gesture, and only one of the two
  // required a click on its button to actually do anything. Matched by id
  // rather than tagName so an Enter inside the interval picker's own time
  // or repeat-count inputs (swapped in via useIntervalForNewValue) edits
  // that control instead of submitting the row early.
  $("detail-add-property").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.target.id === "detail-new-key" || e.target === $("detail-new-value"))) {
      e.preventDefault();
      $("detail-add-property-btn").click();
    }
  });

  $("delete-mote-btn").addEventListener("click", deleteSelection);
  $("delete-selection-btn").addEventListener("click", deleteSelection);
  $("delete-relationship-selection-btn").addEventListener("click", deleteRelationshipSelection);
}

function setupSimulation() {
  const button = $("run-sim-btn");
  const stepsInput = $("sim-steps-input");
  const solverSelect = $("sim-solver-select");

  const storedSteps = localStorage.getItem(SIM_STEPS_KEY);
  if (storedSteps) stepsInput.value = storedSteps;
  stepsInput.addEventListener("change", () => {
    localStorage.setItem(SIM_STEPS_KEY, stepsInput.value);
  });

  (async () => {
    const solvers = await call(api.list_solvers());
    solverSelect.innerHTML = solvers.map((name) => `<option value="${name}">${name}</option>`).join("");
    const storedSolver = localStorage.getItem(SIM_SOLVER_KEY);
    if (storedSolver && solvers.includes(storedSolver)) solverSelect.value = storedSolver;
  })();
  solverSelect.addEventListener("change", () => {
    localStorage.setItem(SIM_SOLVER_KEY, solverSelect.value);
  });

  refreshScenarioPicker();

  $("check-roles-btn").addEventListener("click", async () => {
    closeTopMenus();
    const report = await call(api.check_roles());
    const lines = [...report.problems, ...report.once_scoped];
    if (report.motes_checked === 0 && lines.length === 0) {
      showToast("No motes declare roles.");
    } else if (report.problems.length === 0) {
      showToast(
        `Roles OK — ${report.motes_checked} assembly(s) wired correctly.` +
        (report.once_scoped.length ? ` ${report.once_scoped.length} rule(s) asserted about one mote.` : "")
      );
    } else {
      showToast(`${report.problems.length} role problem(s): ${report.problems[0]}`, SIM_ERROR_TOAST_MS);
    }
  });

  button.addEventListener("click", async () => {
    closeTopMenus();
    const steps = Math.max(1, parseInt(stepsInput.value, 10) || 1);
    const solver = solverSelect.value || "linear";
    // "" means the garden as it stands. A scenario is never committed,
    // so it travels with the preview the same way the solver does and
    // decides whether Commit is offered at all.
    const scenario = $("sim-scenario-select").value || null;
    button.disabled = true;
    button.textContent = "Running…";
    try {
      const result = await call(api.preview_simulation(steps, solver, null, scenario));
      if (result.steps.length === 0) {
        showToast(result.error ? `Simulation failed: ${result.error}` : "Nothing to simulate.", SIM_ERROR_TOAST_MS);
        return;
      }
      // The solver that produced this preview travels with it, so
      // Commit/Save (below) re-solve with the same backend even if the
      // dropdown is changed while the sim bar is still open.
      openSimBar({ ...result, mode: "preview", solver, scenario });
    } finally {
      // openSimBar() already disabled it (and keeps it disabled) on success;
      // this only matters for the "nothing to simulate" early return above.
      // The ellipsis is part of the label (it opens the sim bar rather
      // than running to completion), so restoring it has to keep it.
      button.disabled = !!simPreview;
      button.textContent = "Simulate…";
    }
  });

  $("sim-step-slider").addEventListener("input", () => {
    stopSimPlay();
    setSimStep(parseInt($("sim-step-slider").value, 10));
  });
  $("sim-prev-btn").addEventListener("click", () => { stopSimPlay(); setSimStep(simStep - 1); });
  $("sim-next-btn").addEventListener("click", () => { stopSimPlay(); setSimStep(simStep + 1); });
  $("sim-play-btn").addEventListener("click", () => {
    if (simPlaying) stopSimPlay(); else startSimPlay();
  });

  $("sim-discard-btn").addEventListener("click", discardSimPreview);

  $("sim-commit-btn").addEventListener("click", async () => {
    if (!simPreview || simPreview.mode !== "preview" || simStep === 0) return;
    const btn = $("sim-commit-btn");
    btn.disabled = true;
    try {
      const result = await call(api.commit_simulation(simStep, simPreview.solver));
      const committed = result.steps[result.steps.length - 1];
      const committedStep = simStep;
      closeSimBar();
      showToast(committed ? committed.summary : `Committed tick ${committedStep}.`, 3500);
      await refreshMoteCache();
      if (!$("runs-panel").classList.contains("hidden")) await refreshRuns();
      await refocus(focusId, { pan: false });
      if (!$("detail-panel").classList.contains("hidden") && focusId) {
        await openDetailPanel(focusId);
      }
    } finally {
      btn.disabled = false;
    }
  });

  $("sim-save-btn").addEventListener("click", async () => {
    if (!simPreview || simPreview.mode !== "preview") return;
    const btn = $("sim-save-btn");
    btn.disabled = true;
    try {
      const result = await call(api.save_simulation(
        simPreview.steps.length, simPreview.solver, null, simPreview.scenario || null));
      closeSimBar();
      showToast(
        result.run_id ? "Simulation saved for later review." : "Nothing to save.",
        3000,
      );
      if (!$("runs-panel").classList.contains("hidden")) await refreshRuns();
    } finally {
      btn.disabled = false;
    }
  });
}

/** Shared by both scrub modes -- `result.mode` is 'preview' (a fresh,
 *  nondestructive preview_simulation() call) or 'history' (an already-
 *  logged run's own ticks, from enterRunScrub()). Either way nothing is
 *  written here, so opening this is purely a display action. Starts at
 *  step 0 (the real state before anything in `result.steps` happened)
 *  rather than jumping to where the run ended up -- the whole graph
 *  stays visible and interactive, so the natural thing is to let the
 *  user step or play forward through it. */
function openSimBar(result) {
  simPreview = result;
  simStep = 0;
  const slider = $("sim-step-slider");
  slider.max = String(result.steps.length);
  slider.value = "0";
  $("run-sim-btn").disabled = true;
  graph.setSimActive(true);
  $("sim-bar").classList.remove("hidden");
  renderSimBar();
  if (focusId && !$("detail-panel").classList.contains("hidden") && multiSelectIds === null) {
    openDetailPanel(focusId);
  }
}

function closeSimBar() {
  stopSimPlay();
  $("sim-bar").classList.add("hidden");
  $("run-sim-btn").disabled = false;
  simPreview = null;
  simStep = 0;
  graph.setSimActive(false);
  graph.setSimHighlight([]);
  if (selectedRunId !== null) {
    selectedRunId = null;
    renderRunsPanel();
  }
}

/** Shared by the sim bar's own Discard button and the Tools menu's "Exit
 *  simulation" item -- two entry points to the same action. */
async function discardSimPreview() {
  closeSimBar();
  if (focusId && !$("detail-panel").classList.contains("hidden") && multiSelectIds === null) {
    await openDetailPanel(focusId);
  }
}

function setSimStep(idx) {
  if (!simPreview) return;
  simStep = Math.max(0, Math.min(idx, simPreview.steps.length));
  $("sim-step-slider").value = String(simStep);
  renderSimBar();
  if (focusId && !$("detail-panel").classList.contains("hidden") && multiSelectIds === null) {
    openDetailPanel(focusId);
  }
}

function startSimPlay() {
  if (!simPreview) return;
  simPlaying = true;
  $("sim-play-btn").textContent = "Pause";
  simPlayTimer = setInterval(() => {
    const next = simStep + 1;
    if (!simPreview || next > simPreview.steps.length) { stopSimPlay(); return; }
    setSimStep(next);
  }, 900);
}

function stopSimPlay() {
  clearInterval(simPlayTimer);
  simPlaying = false;
  $("sim-play-btn").textContent = "Play";
}

/** Every property touched from step 0 through simStep (exclusive of
 *  simStep 0 itself, which is the real/unprojected view), with the value
 *  it had entering the whole run and its value as of the currently
 *  scrubbed step -- not just that one step's own delta, so scrubbing
 *  shows the state of the garden at that point in time, not a single
 *  frame's diff. Feeds both the inspector's projected values and the
 *  graph's changed-mote ring. */
function cumulativeSimChanges() {
  if (!simPreview || simStep === 0) return [];
  const byKey = new Map();
  for (let i = 0; i < simStep; i++) {
    for (const c of simPreview.steps[i].changes) {
      const key = `${c.mote_id}|${c.key}`;
      const existing = byKey.get(key);
      if (existing) existing.new_value = c.new_value;
      else byKey.set(key, { ...c });
    }
  }
  return [...byKey.values()].sort((a, b) =>
    a.mote_id.localeCompare(b.mote_id) || a.key.localeCompare(b.key));
}

/* Dimensional warnings and unsatisfied constraints, shown together but
 * labelled apart: one is a statement about the model, the other about the
 * answer. Neither stops a run, so neither belongs in the error area. */
function renderSimAdvisories() {
  const el = $("sim-bar-advisory");
  el.replaceChildren();

  const lines = [];
  if (simPreview.scenario) {
    const mote = moteById(simPreview.scenario);
    const name = mote ? nodeLabel(mote) : "a scenario";
    lines.push(`Scenario · ${name} — nothing here will be written to the graph.`);
  }
  for (const warning of simPreview.dimension_warnings || []) {
    lines.push(`Units · ${warning}`);
  }
  for (const violation of simPreview.constraint_violations || []) {
    lines.push(`Constraint · ${violation}`);
  }

  el.classList.toggle("hidden", lines.length === 0);
  for (const line of lines) {
    const row = document.createElement("div");
    row.className = "advisory-line";
    row.textContent = line;
    el.appendChild(row);
  }
}

function renderSimBar() {
  const isHistory = simPreview.mode === "history";
  const projected = simStep > 0;
  $("sim-bar").classList.toggle("projected", projected && !isHistory);
  $("sim-bar").classList.toggle("history", isHistory);
  $("sim-badge").classList.toggle("projected", projected && !isHistory);
  $("sim-badge").classList.toggle("history", isHistory);

  if (isHistory) {
    $("sim-badge-label").textContent = projected
      ? `Tick ${simStep} of ${simPreview.steps.length}`
      : "Before this run";
  } else {
    $("sim-badge-label").textContent = projected
      ? `Projected · tick ${simStep} of ${simPreview.steps.length}`
      : "Real";
  }

  const step = projected ? simPreview.steps[simStep - 1] : null;
  $("sim-caption").textContent = step
    ? step.summary
    : isHistory ? "State entering this run" : "Current state of the garden";

  const errorEl = $("sim-bar-error");
  if (simPreview.failed_at !== null) {
    errorEl.textContent = `Run stopped at step ${simPreview.failed_at + 1}: ${simPreview.error}`;
    errorEl.classList.remove("hidden");
  } else {
    errorEl.classList.add("hidden");
  }

  renderSimAdvisories();

  $("sim-step-count").textContent = `${simStep}/${simPreview.steps.length}`;
  $("sim-prev-btn").disabled = simStep <= 0;
  $("sim-next-btn").disabled = simStep >= simPreview.steps.length;
  // A scenario run answers what *would* happen, so there is nothing to
  // commit: the engine refuses it outright. Save still applies -- that
  // is how a hypothetical is kept for comparison.
  const scenarioRun = !!simPreview.scenario;
  $("sim-commit-btn").classList.toggle("hidden", isHistory || scenarioRun);
  $("sim-commit-btn").disabled = simStep === 0;
  $("sim-save-btn").classList.toggle("hidden", isHistory);
  $("sim-discard-btn").textContent = isHistory ? "Exit" : "Discard";

  graph.setSimHighlight(new Set(cumulativeSimChanges().map((c) => c.mote_id)));
}

// ---- run history ----

async function refreshRuns() {
  allRuns = await call(api.list_runs());
  renderRunsPanel();
}

function renderRunsPanel() {
  const container = $("runs-list");
  container.innerHTML = "";
  $("runs-empty").classList.toggle("hidden", allRuns.length > 0);

  for (const run of allRuns) {
    const row = document.createElement("div");
    row.className = "run-row" + (run.run_id === selectedRunId ? " highlighted" : "");
    row.innerHTML =
      `<span class="dot ${run.status}"></span>` +
      '<span class="label"><span class="number"></span><span class="applied"></span><span class="time"></span></span>' +
      '<span class="meta"></span>';
    row.querySelector(".number").textContent = `Run #${run.run_number}`;
    row.querySelector(".applied").textContent = run.committed ? "applied" : "saved";
    row.querySelector(".time").textContent = new Date(run.ran_at).toLocaleString();
    const meta = row.querySelector(".meta");
    if (run.status === "ok") {
      meta.textContent = `${run.change_count} change${run.change_count === 1 ? "" : "s"}`;
    } else {
      meta.textContent = "failed";
      meta.classList.add("error");
      meta.title = run.detail || "";
    }
    row.addEventListener("mousedown", (e) => { e.preventDefault(); enterRunScrub(run.run_id); });
    container.appendChild(row);
  }
}

function openRunsPanel() {
  closeViewSettings();   // same corner as the view settings panel
  $("runs-panel").classList.remove("hidden");
  refreshRuns();
}

function closeRunsPanel() {
  $("runs-panel").classList.add("hidden");
}

/** "Entering" a run drops into the same scrub bar a live preview uses,
 *  but walking only *this run's own ticks* -- unrelated to any other
 *  run in the garden's history. A steady-state run has exactly one tick
 *  (before/after); a run committed or saved from an N-step time-series
 *  has N, each logged individually (core/simulation.py's run_ticks())
 *  specifically so this is possible. */
async function enterRunScrub(runId) {
  if (simPreview && simPreview.mode === "preview") {
    showToast("Commit or discard the current preview first.", 2600);
    return;
  }
  const result = await call(api.run_ticks(runId));
  if (result.steps.length === 0) {
    showToast("Nothing to show for this run.", 2600);
    return;
  }
  openSimBar({ ...result, mode: "history", run_id: runId, failed_at: null, error: null });
  selectedRunId = runId;
  renderRunsPanel();
}

function setupMora() {
  $("open-mora-btn").addEventListener("click", () => {
    closeTopMenus();
    openMora(null);
  });
}

function setupMonastery() {
  $("open-monastery-btn").addEventListener("click", () => {
    closeTopMenus();
    openMonastery(null);
  });
}

function setupMomentum() {
  $("open-momentum-btn").addEventListener("click", () => {
    closeTopMenus();
    openMomentum(null);
  });
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
];
const THIS_APP = "monarch";

let launcherHighlight = 0;

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
      if (launcherHighlight < list.length - 1) launcherHighlight++;
      updateLauncherHighlight();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      if (launcherHighlight > 0) launcherHighlight--;
      updateLauncherHighlight();
    } else if (e.key === "Enter" && list[launcherHighlight]) {
      launchApp(list[launcherHighlight].id);
    }
  });
}


function setupRunsPanel() {
  $("toggle-runs-panel-btn").addEventListener("click", () => {
    closeTopMenus();
    if ($("runs-panel").classList.contains("hidden")) openRunsPanel();
    else closeRunsPanel();
  });
  $("close-runs-panel").addEventListener("click", closeRunsPanel);
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
    if ((e.metaKey || e.ctrlKey) && /^[1-3]$/.test(e.key)) {
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
    if (!$("gardens-overlay").classList.contains("hidden")) { closeGardensModal(); return; }
    if (!$("species-picker-wrap").classList.contains("hidden")) { hideSpeciesPicker(); return; }
    if (!$("context-menu").classList.contains("hidden")) { hideContextMenu(); return; }
    if (openMenuName) { closeTopMenus(); return; }
    if (!$("quick-add").classList.contains("hidden")) { closeQuickAdd(); return; }
    if (!$("detail-panel").classList.contains("hidden")) { closeDetailPanel(); return; }
    // Leaving attractor mode is a smaller step than closing the panel it
    // lives in, so it comes first -- one Esc puts the graph back to
    // editable, a second puts the panel away.
    if (attractorMode) { exitAttractorMode(); return; }
    // Runs history and advanced view settings share a corner and are
    // mutually exclusive (see openRunsPanel/openViewSettings), so at most
    // one of these two ever fires.
    if (!$("runs-panel").classList.contains("hidden")) { closeRunsPanel(); return; }
    if (viewSettingsOpen()) { closeViewSettings(); return; }
    // The sim bar is non-blocking (the rest of the UI stays usable while
    // it's open), so it sits behind every other panel in the Esc chain --
    // dismissed only once nothing more local is open.
    if (simPreview) { closeSimBar(); return; }
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

  const reserved = await call(api.reserved_names());
  reservedProperties = new Set(reserved.properties);
  reservedRelTypes = new Set(reserved.relationshipTypes);
  reservedMeanings = { ...reserved.propertyMeanings, ...reserved.relationshipTypeMeanings };
  propertyModelProviders = reserved.propertyModelProviders || {};

  graph = new MonossGraph($("graph"), {
    labelFor: nodeLabel,
    onSelect: (id) => selectMote(id),
    onConnect: (sourceId, targetId) => connectNodes(sourceId, targetId),
    onPositionsChanged: savePositions,
    onBoxSelect: updateSelectBox,
    onSelectionChanged: syncSelectionPanel,
    onContextMenu: openContextMenu,
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

  setupGardensModal();
  setupSyncModal();
  setupTopMenus();
  setupDetailResize();
  setupQuickAdd();
  setupSpeciesControls();
  setupSearch();
  setupViewControls();
  setupViewSettings();
  setupSimulation();
  setupRunsPanel();
  setupMora();
  setupMonastery();
  setupMomentum();
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
