// ui/frontend/app.js
//
// Wiring between ui/api.py and the canvas graph in graph.js. State lives
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
let folderPickerTargetId = null;
let searchVisible = [];
let searchHighlight = -1;
const searchSpeciesFilter = new Set();
const searchFolderFilter = new Set();
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
function hiddenFoldersKey(garden) { return `monoss:hiddenFolders:${garden}`; }
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
 *  no longer has its own partial idea of what exists -- it used to know
 *  Species, Folder and Rule (that last one by sniffing for an `equation`
 *  property itself) and had no idea what a portal, template or property
 *  model was, even while graph.js drew portals with their own ring. */
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
  renderFolderLegend();
  renderRelFilters();
  // Species and relationship types are what the settings panel is a list
  // of, so it has to follow the same data everything else here does.
  if (viewSettingsOpen()) renderViewSettings();
  return next;
}

async function loadGarden(garden, { focusId } = {}) {
  currentGarden = garden;
  $("garden-label").textContent = garden;
  closeSimBar();
  closeDetailPanel();
  closeRunsPanel();
  // Attractors are per garden, so an arrangement in progress means
  // nothing once a different one is open.
  if (attractorMode) exitAttractorMode();
  graph.setHiddenSpecies(readJSON(hiddenSpeciesKey(garden), []));
  graph.setHiddenFolders(readJSON(hiddenFoldersKey(garden), []));
  graph.setHiddenRelTypes(readJSON(hiddenRelKey(garden), []));
  viewSettings = loadViewSettings(garden);
  graph.setViewSettings(viewSettings);
  const stored = focusId || localStorage.getItem(lastFocusKey(garden)) || null;
  await refreshMoteCache();
  // An explicit focusId (a portal jump) means go straight there and
  // select it, like clicking a search result would. Otherwise frame the
  // whole garden on open rather than panning to the remembered mote --
  // you want to see where you are before you go anywhere -- which is
  // also why fitWhenSettled() below is skipped for a portal jump: it
  // re-fits every tick for a couple of seconds and would fight the pan.
  await refocus(stored, { pan: !!focusId, select: !!focusId });
  if (!focusId) graph.fitWhenSettled();
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

function folderRows() {
  const counts = new Map();
  for (const mote of payload.motes) {
    if (isKind(mote.id, KIND.FOLDER)) continue;
    for (const id of folderIdsOf(mote.id)) counts.set(id, (counts.get(id) || 0) + 1);
  }
  const rows = idsWithKind(payload.kinds, KIND.FOLDER).map((id) => ({
    id,
    label: nodeLabel(moteById(id) || { id, properties: {} }),
    count: counts.get(id) || 0,
    color: "oklch(0.68 0.13 250)",
    square: true,
  }));
  rows.sort((a, b) => a.label.localeCompare(b.label));
  return rows;
}

function renderFolderLegend() {
  const container = $("legend-folders");
  const rows = folderRows();
  $("folder-heading").classList.toggle("hidden", rows.length === 0);
  container.innerHTML = "";
  for (const row of rows) {
    const el = document.createElement("div");
    const hidden = graph.hiddenFolders.has(row.id);
    el.className = "filter-row" + (hidden ? " off" : "");
    el.title = hidden ? `Show ${row.label}` : `Hide ${row.label}`;
    el.innerHTML =
      `<span class="dot square" style="background:${row.color}"></span>` +
      `<span class="name"></span><span class="count">${row.count}</span>`;
    el.querySelector(".name").textContent = row.label;
    el.addEventListener("click", () => {
      const hiddenIds = new Set(graph.hiddenFolders);
      hiddenIds.has(row.id) ? hiddenIds.delete(row.id) : hiddenIds.add(row.id);
      graph.setHiddenFolders(hiddenIds);
      localStorage.setItem(hiddenFoldersKey(currentGarden), JSON.stringify([...hiddenIds]));
      renderFolderLegend();
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
  graph.setHiddenFolders(new Set());
  graph.setHiddenRelTypes(new Set());
  localStorage.removeItem(hiddenSpeciesKey(currentGarden));
  localStorage.removeItem(hiddenFoldersKey(currentGarden));
  localStorage.removeItem(hiddenRelKey(currentGarden));
  renderLegend();
  renderFolderLegend();
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
  const [mote, rels, species, instances, folders, members, history] = await Promise.all([
    call(api.get_mote(moteId)),
    call(api.mote_relationships(moteId)),
    call(api.species_of(moteId)),
    call(api.instances_of(moteId)),
    call(api.folders_of(moteId)),
    call(api.folder_members(moteId)),
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
  renderFolderSection(mote, folders, members);
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

/** `date`, `recurrence` and `provider` get a small picker instead of
 *  hand-typed syntax; every other property keeps the plain text/textarea
 *  field. Falls back to plainField for a `date` value that doesn't parse
 *  -- a picker that can't represent an existing value shouldn't clobber
 *  it. */
function valueField(key, rawValue, onCommit) {
  const text = typeof rawValue === "string" ? rawValue : JSON.stringify(rawValue);
  if (key === "date") {
    const parsed = parseDateValue(text);
    if (parsed) return dateField(parsed, onCommit);
  }
  if (key === "modoc") return noteField(text);
  if (key === "recurrence") return recurrenceField(text, onCommit);
  if (key === "provider") return providerField(text, onCommit);
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

/** Parses MonOSS's two `date` shapes (docs/google-calendar.md): a bare
 *  "YYYY-MM-DD" (all-day) or a full "YYYY-MM-DDTHH:MM[:SS]" (timed,
 *  local time, no offset). Returns null for anything else so callers can
 *  fall back to plain text rather than force a picker on an unparseable
 *  value. */
function parseDateValue(text) {
  if (text === "") return { date: "", time: "", allDay: true };
  const timed = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?$/.exec(text);
  if (timed) return { date: timed[1], time: timed[2], allDay: false };
  if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return { date: text, time: "", allDay: true };
  return null;
}

/** Native date/time inputs plus an "All day" toggle -- mirrors
 *  event_payload()'s own "T" in raw_date branch
 *  (core/google_calendar.py) so the two stay in sync. Defines its own
 *  `value` getter/setter so the revert-on-block call site in
 *  renderProperties() (`field.value = displayedText`) works unmodified,
 *  same as it does for a plain <input>. */
function dateField(parsed, onCommit) {
  const wrap = document.createElement("div");
  wrap.className = "picker-field date-field";

  const dateInput = document.createElement("input");
  dateInput.type = "date";
  dateInput.value = parsed.date;

  const allDayLabel = document.createElement("label");
  allDayLabel.className = "picker-checkbox";
  const allDayCheckbox = document.createElement("input");
  allDayCheckbox.type = "checkbox";
  allDayCheckbox.checked = parsed.allDay;
  allDayLabel.append(allDayCheckbox, document.createTextNode("All day"));

  const timeInput = document.createElement("input");
  timeInput.type = "time";
  timeInput.value = parsed.time;
  timeInput.classList.toggle("hidden", allDayCheckbox.checked);

  // Seconds appended explicitly so the stored value always matches
  // MonOSS's existing "YYYY-MM-DDTHH:MM:SS" shape, regardless of what
  // precision the native time input happens to report.
  const compose = () => (
    allDayCheckbox.checked ? dateInput.value : `${dateInput.value}T${timeInput.value || "00:00"}:00`
  );
  const commit = () => onCommit(compose());

  dateInput.addEventListener("change", commit);
  timeInput.addEventListener("change", commit);
  allDayCheckbox.addEventListener("change", () => {
    timeInput.classList.toggle("hidden", allDayCheckbox.checked);
    commit();
  });

  wrap.append(dateInput, allDayLabel, timeInput);

  Object.defineProperty(wrap, "value", {
    get: compose,
    set(v) {
      const next = parseDateValue(v) || { date: "", time: "", allDay: true };
      dateInput.value = next.date;
      timeInput.value = next.time;
      allDayCheckbox.checked = next.allDay;
      timeInput.classList.toggle("hidden", next.allDay);
    },
  });
  return wrap;
}

const SIMPLE_RECURRENCE_FREQUENCIES = ["DAILY", "WEEKLY", "MONTHLY", "YEARLY"];

/** Recognizes only "FREQ=<X>" or "FREQ=<X>;UNTIL=<YYYYMMDD>" (a leading
 *  "RRULE:" stripped the same way core/google_calendar.py's
 *  event_payload() does server-side) -- anything else (BYDAY, COUNT,
 *  INTERVAL, ...) returns null so the picker can tell "simple enough to
 *  edit structurally" from "must stay a raw rule". */
function parseSimpleRecurrence(text) {
  if (!text) return { freq: "", until: "" };
  const rule = text.trim().replace(/^RRULE:/i, "");
  const parts = {};
  for (const piece of rule.split(";")) {
    const [k, v] = piece.split("=");
    if (!k || v === undefined) return null;
    parts[k] = v;
  }
  const keys = Object.keys(parts);
  if (keys.some((k) => k !== "FREQ" && k !== "UNTIL") || !SIMPLE_RECURRENCE_FREQUENCIES.includes(parts.FREQ)) {
    return null;
  }
  let until = "";
  if (parts.UNTIL) {
    const m = /^(\d{4})(\d{2})(\d{2})/.exec(parts.UNTIL);
    if (!m) return null;
    until = `${m[1]}-${m[2]}-${m[3]}`;
  }
  return { freq: parts.FREQ, until };
}

function buildSimpleRecurrence(freq, until) {
  if (!freq) return "";
  return until ? `FREQ=${freq};UNTIL=${until.replace(/-/g, "")}` : `FREQ=${freq}`;
}

/** Structured Frequency+Until picker for the common case, with a raw-text
 *  fallback (today's plain field, via plainField) for anything
 *  parseSimpleRecurrence can't express -- so an existing BYDAY/COUNT/...
 *  rule is never silently offered a lossy downgrade. Same `value`
 *  getter/setter contract as dateField. */
function recurrenceField(text, onCommit) {
  let currentText = text;
  let mode = parseSimpleRecurrence(currentText) !== null ? "simple" : "raw";
  const wrap = document.createElement("div");
  wrap.className = "picker-field recurrence-field";

  function commit(next) {
    currentText = next;
    onCommit(next);
  }

  function renderSimple() {
    wrap.innerHTML = "";
    const parsed = parseSimpleRecurrence(currentText) || { freq: "", until: "" };

    const select = document.createElement("select");
    for (const [value, label] of [
      ["", "Does not repeat"], ["DAILY", "Daily"], ["WEEKLY", "Weekly"],
      ["MONTHLY", "Monthly"], ["YEARLY", "Yearly"],
    ]) {
      const opt = document.createElement("option");
      opt.value = value;
      opt.textContent = label;
      if (value === parsed.freq) opt.selected = true;
      select.appendChild(opt);
    }

    const until = document.createElement("input");
    until.type = "date";
    until.title = "Repeat until (optional)";
    until.value = parsed.until;
    until.classList.toggle("hidden", !parsed.freq);

    const compose = () => buildSimpleRecurrence(select.value, until.value);
    select.addEventListener("change", () => {
      until.classList.toggle("hidden", !select.value);
      commit(compose());
    });
    until.addEventListener("change", () => commit(compose()));

    const customBtn = document.createElement("button");
    customBtn.type = "button";
    customBtn.className = "linkish picker-mode-toggle";
    customBtn.textContent = "Custom rule…";
    customBtn.addEventListener("click", () => { mode = "raw"; renderRaw(); });

    wrap.append(select, until, customBtn);
  }

  function renderRaw() {
    wrap.innerHTML = "";
    wrap.appendChild(plainField(currentText, commit));
    if (parseSimpleRecurrence(currentText) !== null) {
      const simpleBtn = document.createElement("button");
      simpleBtn.type = "button";
      simpleBtn.className = "linkish picker-mode-toggle";
      simpleBtn.textContent = "Use simple picker";
      simpleBtn.addEventListener("click", () => { mode = "simple"; renderSimple(); });
      wrap.appendChild(simpleBtn);
    }
  }

  mode === "simple" ? renderSimple() : renderRaw();

  Object.defineProperty(wrap, "value", {
    get: () => currentText,
    set(v) {
      currentText = v;
      mode = parseSimpleRecurrence(v) !== null ? "simple" : "raw";
      mode === "simple" ? renderSimple() : renderRaw();
    },
  });
  return wrap;
}

function openLink(url) {
  call(api.open_link(url));
}

/** Follows a portal mote: resolves+switches gardens in one backend call
 *  (Api.jump_to_portal), then rebuilds the graph on the target garden,
 *  landing panned/selected on the target mote itself rather than the
 *  target garden's own last-focused one. */
async function jumpToPortal(moteId) {
  const result = await call(api.jump_to_portal(moteId));
  closeDetailPanel();
  await loadGarden(result.garden, { focusId: result.mote_id });
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
    const field = valueField(key, value, async (next) => {
      if (editBlocked()) { field.value = displayedText; return; }
      await call(api.update_mote(mote.id, { [key]: next }));
      await refocus(focusId, { pan: false });
      await refreshMoteCache();
    });

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
    // treatment url and portal_mote get below, since the row itself is
    // deliberately only a summary.
    if (key === "modoc" && typeof value === "string") {
      const open = document.createElement("button");
      open.className = "open-link";
      open.textContent = "↗";
      open.title = "Open in Mora";
      open.addEventListener("click", () => openMora(mote.id));
      trailing.push(open);
    }
    if (key === "url" && typeof value === "string" && value.trim()) {
      const open = document.createElement("button");
      open.className = "open-link";
      open.textContent = "↗";
      open.title = "Open";
      open.addEventListener("click", () => openLink(value));
      trailing.push(open);
    }
    // "portal_garden"/"portal_mote" together mark this mote as a portal
    // into another garden -- same one-click-to-follow treatment as url
    // above, shown on the portal_mote row specifically since that's the
    // half that actually identifies a destination.
    if (
      key === "portal_mote" && typeof value === "string" && value.trim() &&
      typeof mote.properties.portal_garden === "string" && mote.properties.portal_garden.trim()
    ) {
      const jump = document.createElement("button");
      jump.className = "open-link portal-jump";
      jump.textContent = "↗";
      jump.title = `Jump to ${mote.properties.portal_garden}`;
      jump.addEventListener("click", () => jumpToPortal(mote.id));
      trailing.push(jump);
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

function renderFolderSection(mote, folders, members) {
  const container = $("detail-folders");
  container.innerHTML = "";
  if (folders.length === 0) {
    container.innerHTML = '<div class="empty-note">Not grouped into any folder</div>';
  }
  for (const folderMote of folders) {
    const rel = payload.relationships.find(
      (r) => r.type === "in_folder" && r.from_id === mote.id && r.to_id === folderMote.id
    );
    const row = document.createElement("div");
    row.className = "rel-row";

    const label = document.createElement("span");
    label.className = "rel-other";
    label.textContent = nodeLabel(folderMote);
    label.addEventListener("click", () => selectMote(folderMote.id));

    const del = document.createElement("button");
    del.textContent = "×";
    del.title = "Remove from this folder";
    del.addEventListener("click", async () => {
      if (editBlocked()) return;
      if (rel) await call(api.delete_relationship(rel.id));
      await refocus(focusId, { pan: false });
      await openDetailPanel(mote.id);
    });

    row.append(label, del);
    container.appendChild(row);
  }

  // The folder counterpart of the species section's instance count: a
  // folder is a mote others point at, so what's worth stating on it is
  // how many that is.
  $("detail-member-count").textContent =
    members.length > 0 ? `${members.length} member${members.length === 1 ? "" : "s"}` : "";
}

function renderRelationships(moteId, rels) {
  const container = $("detail-relationships");
  container.innerHTML = "";
  const shown = rels.filter((r) => r.type !== "instance_of" && r.type !== "in_folder");
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
  for (const mote of candidates) {
    const row = document.createElement("div");
    row.className = "search-result-row";
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
  }
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

  $("new-instance-btn").addEventListener("click", async () => {
    if (editBlocked()) return;
    if (!focusId) return;
    const mote = await call(api.create_instance(focusId, {}));
    await refreshMoteCache();
    await selectMote(mote.id);
    showToast("Instance created.");
  });
}

// ---- folder picker ----
//
// Mirrors the species picker above closely -- a folder is just a mote
// other motes point at via `in_folder`, the same way a species is just a
// mote pointed at via `instance_of`, so this needs no gate on "is this
// mote already a folder" any more than the species picker gates on
// "is this mote already a species."

function hideFolderPicker() {
  $("folder-picker-wrap").classList.add("hidden");
  $("folder-picker-input").value = "";
  folderPickerTargetId = null;
}

function renderFolderPickerResults(query) {
  const container = $("folder-picker-results");
  container.innerHTML = "";
  const q = (query || "").toLowerCase();
  const candidates = allMotes
    .filter((m) => m.id !== folderPickerTargetId)
    .filter((m) => !q || nodeLabel(m).toLowerCase().includes(q))
    .sort((a, b) => nodeLabel(a).localeCompare(nodeLabel(b)))
    .slice(0, 40);

  if (candidates.length === 0) {
    container.innerHTML = '<div class="search-empty">No matches</div>';
    return;
  }
  for (const mote of candidates) {
    const row = document.createElement("div");
    row.className = "search-result-row";
    row.innerHTML = '<span class="label"></span>';
    row.querySelector(".label").textContent = nodeLabel(mote);
    row.addEventListener("mousedown", async (e) => {
      e.preventDefault();
      if (editBlocked()) return;
      const targetId = folderPickerTargetId;
      await call(api.set_folder(targetId, mote.id));
      hideFolderPicker();
      await refocus(targetId, { pan: false });
      await openDetailPanel(targetId);
    });
    container.appendChild(row);
  }
}

function setupFolderControls() {
  $("set-folder-btn").addEventListener("click", () => {
    if (editBlocked()) return;
    if (!focusId) return;
    folderPickerTargetId = focusId;
    $("folder-picker-wrap").classList.remove("hidden");
    renderFolderPickerResults("");
    $("folder-picker-input").focus();
  });
  $("folder-picker-input").addEventListener("input", (e) =>
    renderFolderPickerResults(e.target.value)
  );
  $("folder-picker-input").addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideFolderPicker();
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

/** motes/relationships-parametrized so both the main search palette
 *  (against the live payload/allMotes) and the library picker (against a
 *  one-off library_snapshot() read) share the exact same filtering
 *  instead of the picker reinventing a weaker version of it. */
function relatedTargetIds(relationships, moteId, type) {
  return relationships.filter((r) => r.type === type && r.from_id === moteId).map((r) => r.to_id);
}

function speciesIdsOf(moteId) {
  return relatedTargetIds(payload.relationships, moteId, "instance_of");
}

function folderIdsOf(moteId) {
  return relatedTargetIds(payload.relationships, moteId, "in_folder");
}

function computeFilteredMotes(motes, relationships, query, { speciesFilter, folderFilter, relFilter } = {}) {
  return motes
    .filter((mote) => {
      if (speciesFilter && speciesFilter.size) {
        const ids = relatedTargetIds(relationships, mote.id, "instance_of");
        const matches = ids.some((id) => speciesFilter.has(id)) ||
          (speciesFilter.has("__none__") && ids.length === 0);
        if (!matches) return false;
      }
      if (folderFilter && folderFilter.size) {
        const ids = relatedTargetIds(relationships, mote.id, "in_folder");
        if (!ids.some((id) => folderFilter.has(id))) return false;
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
    speciesFilter: searchSpeciesFilter, folderFilter: searchFolderFilter, relFilter: searchRelFilter,
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
  renderChipRow($("search-folder-chips"), folderRows(), searchFolderFilter, (id) => {
    searchFolderFilter.has(id) ? searchFolderFilter.delete(id) : searchFolderFilter.add(id);
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
    // Single-mote only -- a library entry is one mote's properties
    // (core/library.py), not yet a group/subgraph.
    if (ids.length === 1) {
      addMenuItem(menu, "Save to library…", async () => {
        await call(api.save_to_library(targetId));
        showToast("Saved to library.");
      });
    }
    const sep = document.createElement("div");
    sep.className = "menu-sep";
    menu.appendChild(sep);
    addMenuItem(menu, "Delete", () => deleteSelection(), { danger: true });
  } else {
    addMenuItem(menu, "New mote here", () => openQuickAddAt(worldX, worldY, clientX, clientY));
    // With only one garden there's nowhere to portal to, so the item is
    // omitted entirely rather than shown disabled.
    if ((await call(api.list_gardens())).length > 1) {
      addMenuItem(menu, "New portal…", () => openPortalModalAt(worldX, worldY));
    }
    addMenuItem(menu, "Reuse from library…", () => openLibraryModalAt(worldX, worldY));
    const pasteLabel = clipboard && clipboard.length > 1 ? `Paste ${clipboard.length} motes` : "Paste";
    addMenuItem(menu, pasteLabel, () => pasteAt(worldX, worldY), { disabled: !clipboard });
  }

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
// (#run-sim-btn, #export-calendar-btn, ...) so the setup functions that
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

  $("garden-menu-open-library").addEventListener("click", async () => {
    closeTopMenus();
    const result = await call(api.open_library());
    await loadGarden(result.garden);
  });
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

// ---- portal picker ----
//
// Reached from "New portal…" on the empty-canvas context menu. Modeled on
// the search palette (#search-overlay) for its result-list markup, but
// each keystroke round-trips to Python and opens a real sqlite connection
// against the *target* garden (Api.find_portal_target) rather than
// filtering an already-cached array the way the in-garden search palette
// does -- hence the debounce below, which that palette doesn't need.

let portalPlaceAt = null;      // world coords from the context-menu click, for graph.placeAt
let portalSearchTimer = null;

function openPortalModalAt(worldX, worldY) {
  portalPlaceAt = { x: worldX, y: worldY };
  $("portal-overlay").classList.remove("hidden");
  $("portal-search-input").value = "";
  renderPortalResults([]);
  renderPortalGardenOptions().then(refreshPortalResults);
  $("portal-search-input").focus();
}

function closePortalModal() {
  $("portal-overlay").classList.add("hidden");
  portalPlaceAt = null;
}

async function renderPortalGardenOptions() {
  const gardens = (await call(api.list_gardens())).filter((g) => g !== currentGarden);
  const select = $("portal-garden-select");
  select.innerHTML = "";
  for (const g of gardens) {
    const opt = document.createElement("option");
    opt.value = g;
    opt.textContent = g;
    select.appendChild(opt);
  }
}

async function refreshPortalResults() {
  const garden = $("portal-garden-select").value;
  if (!garden) { renderPortalResults([]); return; }
  const results = await call(api.find_portal_target(garden, $("portal-search-input").value));
  renderPortalResults(results);
}

function renderPortalResults(results) {
  const container = $("portal-search-results");
  container.innerHTML = "";
  if (results.length === 0) {
    container.innerHTML = '<div class="search-empty">No matches</div>';
    return;
  }
  for (const mote of results) {
    const row = document.createElement("div");
    row.className = "search-result-row";
    row.innerHTML = '<span class="label"></span>';
    row.querySelector(".label").textContent = nodeLabel(mote);
    row.addEventListener("mousedown", async (e) => {
      e.preventDefault();
      const garden = $("portal-garden-select").value;
      const placeAt = portalPlaceAt;
      closePortalModal();
      // Snapshotted once here, not kept in sync afterward -- a portal
      // has no live coupling to its target beyond the stored pointer.
      // Ordering (select, then placeAt/savePositions) matches "New mote
      // here"'s own create-mote-btn handler above.
      const portal = await call(api.create_portal(garden, mote.id, { name: nodeLabel(mote) }));
      await refreshMoteCache();
      await selectMote(portal.id, { pan: !placeAt });
      if (placeAt) {
        graph.placeAt(portal.id, placeAt.x, placeAt.y);
        savePositions();
      }
      showToast(`Portal to '${garden}' created.`);
    });
    container.appendChild(row);
  }
}

function setupPortalModal() {
  $("portal-overlay").addEventListener("mousedown", (e) => {
    if (e.target === $("portal-overlay")) closePortalModal();
  });
  $("portal-garden-select").addEventListener("change", refreshPortalResults);
  $("portal-search-input").addEventListener("input", () => {
    clearTimeout(portalSearchTimer);
    portalSearchTimer = setTimeout(refreshPortalResults, 150);
  });
}

// ---- library picker ----
//
// Reached from "Reuse from library…" on the empty-canvas context menu,
// and "Save to library…" on a single mote's. Modeled on the portal
// picker above for the modal shell, but filters like the main search
// palette: librarySnapshot is a one-off whole-graph read of _library
// (Api.library_snapshot()) fetched once per modal-open, then every
// keystroke/chip-toggle filters it client-side via the same
// computeFilteredMotes()/renderChipRow() the search palette uses --
// no per-keystroke round trip, unlike the portal picker.

let libraryPlaceAt = null;     // world coords from the context-menu click, for graph.placeAt
let librarySnapshot = null;    // {motes, relationships, kinds}
const librarySpeciesFilter = new Set();
const libraryFolderFilter = new Set();
const libraryRelFilter = new Set();

async function openLibraryModalAt(worldX, worldY) {
  libraryPlaceAt = { x: worldX, y: worldY };
  librarySnapshot = await call(api.library_snapshot());
  librarySpeciesFilter.clear();
  libraryFolderFilter.clear();
  libraryRelFilter.clear();
  $("library-overlay").classList.remove("hidden");
  $("library-search-input").value = "";
  renderLibraryChips();
  renderLibraryResults("");
  $("library-search-input").focus();
}

function closeLibraryModal() {
  $("library-overlay").classList.add("hidden");
  libraryPlaceAt = null;
  librarySnapshot = null;
}

/** Rows shaped like speciesRows()/folderRows(), but derived from
 *  librarySnapshot instead of the live payload -- the library is a
 *  different garden from whichever one is currently open. */
function librarySpeciesRows() {
  return idsWithKind(librarySnapshot.kinds, KIND.SPECIES).map((id) => ({
    id,
    label: nodeLabel(librarySnapshot.motes.find((m) => m.id === id) || { id, properties: {} }),
    color: "oklch(0.68 0.16 320)",
  }));
}

function libraryFolderRows() {
  return idsWithKind(librarySnapshot.kinds, KIND.FOLDER).map((id) => ({
    id,
    label: nodeLabel(librarySnapshot.motes.find((m) => m.id === id) || { id, properties: {} }),
    color: "oklch(0.68 0.13 250)",
    square: true,
  }));
}

function renderLibraryChips() {
  renderChipRow($("library-species-chips"), librarySpeciesRows(), librarySpeciesFilter, (id) => {
    librarySpeciesFilter.has(id) ? librarySpeciesFilter.delete(id) : librarySpeciesFilter.add(id);
    renderLibraryChips();
    renderLibraryResults($("library-search-input").value);
  });
  renderChipRow($("library-folder-chips"), libraryFolderRows(), libraryFolderFilter, (id) => {
    libraryFolderFilter.has(id) ? libraryFolderFilter.delete(id) : libraryFolderFilter.add(id);
    renderLibraryChips();
    renderLibraryResults($("library-search-input").value);
  });
  renderChipRow($("library-rel-chips"), relTypeRows(librarySnapshot.relationships), libraryRelFilter, (type) => {
    libraryRelFilter.has(type) ? libraryRelFilter.delete(type) : libraryRelFilter.add(type);
    renderLibraryChips();
    renderLibraryResults($("library-search-input").value);
  });
}

function renderLibraryResults(query) {
  const container = $("library-search-results");
  container.innerHTML = "";
  const results = computeFilteredMotes(librarySnapshot.motes, librarySnapshot.relationships, query, {
    speciesFilter: librarySpeciesFilter, folderFilter: libraryFolderFilter, relFilter: libraryRelFilter,
  });
  if (results.length === 0) {
    container.innerHTML = '<div class="search-empty">No matches</div>';
    return;
  }
  for (const entry of results) {
    const row = document.createElement("div");
    row.className = "search-result-row";
    row.innerHTML =
      '<span class="label"></span><button type="button" class="danger delete-entry">Delete</button>';
    row.querySelector(".label").textContent = nodeLabel(entry);
    row.querySelector(".label").addEventListener("mousedown", async (e) => {
      e.preventDefault();
      const placeAt = libraryPlaceAt;
      closeLibraryModal();
      // Fresh independent copy every time -- reuse_from_library() never
      // links the new mote back to the library entry, and never copies
      // its relationships (organizational or otherwise) either --
      // docs/architecture.md.
      const mote = await call(api.reuse_from_library(entry.id, { name: nodeLabel(entry) }));
      await refreshMoteCache();
      await selectMote(mote.id, { pan: !placeAt });
      if (placeAt) {
        graph.placeAt(mote.id, placeAt.x, placeAt.y);
        savePositions();
      }
      showToast("Added from library.");
    });
    row.querySelector(".delete-entry").addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!window.confirm(`Remove '${nodeLabel(entry)}' from the library?`)) return;
      await call(api.delete_library_entry(entry.id));
      librarySnapshot = await call(api.library_snapshot());
      renderLibraryChips();
      renderLibraryResults($("library-search-input").value);
    });
    container.appendChild(row);
  }
}

function setupLibraryModal() {
  $("library-overlay").addEventListener("mousedown", (e) => {
    if (e.target === $("library-overlay")) closeLibraryModal();
  });
  $("library-search-input").addEventListener("input", (e) => renderLibraryResults(e.target.value));
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

  $("detail-add-property-btn").addEventListener("click", async () => {
    if (editBlocked()) return;
    const key = $("detail-new-key").value.trim();
    if (!key || !focusId) return;
    await call(api.update_mote(focusId, { [key]: $("detail-new-value").value }));
    $("detail-new-key").value = "";
    $("detail-new-value").value = "";
    await refocus(focusId, { pan: false });
    await openDetailPanel(focusId);
  });

  $("delete-mote-btn").addEventListener("click", deleteSelection);
  $("delete-selection-btn").addEventListener("click", deleteSelection);
  $("delete-relationship-selection-btn").addEventListener("click", deleteRelationshipSelection);
}

function setupSimulation() {
  const button = $("run-sim-btn");
  const stepsInput = $("sim-steps-input");

  const storedSteps = localStorage.getItem(SIM_STEPS_KEY);
  if (storedSteps) stepsInput.value = storedSteps;
  stepsInput.addEventListener("change", () => {
    localStorage.setItem(SIM_STEPS_KEY, stepsInput.value);
  });

  button.addEventListener("click", async () => {
    closeTopMenus();
    const steps = Math.max(1, parseInt(stepsInput.value, 10) || 1);
    button.disabled = true;
    button.textContent = "Running…";
    try {
      const result = await call(api.preview_simulation(steps));
      if (result.steps.length === 0) {
        showToast(result.error ? `Simulation failed: ${result.error}` : "Nothing to simulate.", SIM_ERROR_TOAST_MS);
        return;
      }
      openSimBar({ ...result, mode: "preview" });
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
      const result = await call(api.commit_simulation(simStep));
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
      const result = await call(api.save_simulation(simPreview.steps.length));
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

  $("sim-step-count").textContent = `${simStep}/${simPreview.steps.length}`;
  $("sim-prev-btn").disabled = simStep <= 0;
  $("sim-next-btn").disabled = simStep >= simPreview.steps.length;
  $("sim-commit-btn").classList.toggle("hidden", isHistory);
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


function setupRunsPanel() {
  $("toggle-runs-panel-btn").addEventListener("click", () => {
    closeTopMenus();
    if ($("runs-panel").classList.contains("hidden")) openRunsPanel();
    else closeRunsPanel();
  });
  $("close-runs-panel").addEventListener("click", closeRunsPanel);
}

function setupCalendarExport() {
  const button = $("export-calendar-btn");
  button.addEventListener("click", async () => {
    closeTopMenus();
    button.disabled = true;
    button.textContent = "Exporting…";
    // Shown immediately, not just on completion: the very first export
    // can block for a while on the user finishing sign-in in their
    // browser, and without this the button just looks stuck.
    showToast("Exporting to Google Calendar… complete sign-in in your browser if prompted.", 8000);
    try {
      const result = await call(api.export_to_google_calendar());
      const parts = [`${result.created} new`, `${result.updated} updated`];
      if (result.skipped) parts.push(`${result.skipped} skipped (invalid date)`);
      if (result.deleted) parts.push(`${result.deleted} removed`);
      showToast(`Exported ${result.total} mote${result.total === 1 ? "" : "s"} to Google Calendar: ${parts.join(", ")}.`, 6000);
    } finally {
      button.disabled = false;
      button.textContent = "Export to Calendar";
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
    if (!$("gardens-overlay").classList.contains("hidden")) { closeGardensModal(); return; }
    if (!$("portal-overlay").classList.contains("hidden")) { closePortalModal(); return; }
    if (!$("library-overlay").classList.contains("hidden")) { closeLibraryModal(); return; }
    if (!$("species-picker-wrap").classList.contains("hidden")) { hideSpeciesPicker(); return; }
    if (!$("folder-picker-wrap").classList.contains("hidden")) { hideFolderPicker(); return; }
    if (!$("context-menu").classList.contains("hidden")) { hideContextMenu(); return; }
    if (openMenuName) { closeTopMenus(); return; }
    if (!$("quick-add").classList.contains("hidden")) { closeQuickAdd(); return; }
    if (!$("detail-panel").classList.contains("hidden")) { closeDetailPanel(); return; }
    // Leaving attractor mode is a smaller step than closing the panel it
    // lives in, so it comes first -- one Esc puts the graph back to
    // editable, a second puts the panel away.
    if (attractorMode) { exitAttractorMode(); return; }
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
    onRelationshipsSelected: (ids) =>
      ids.length === 0 ? closeDetailPanel()
      : ids.length === 1 ? openRelationshipPanel(ids[0])
      : renderMultiRelationshipPanel(ids),
  });
  window.graph = graph;

  setupGardensModal();
  setupPortalModal();
  setupLibraryModal();
  setupTopMenus();
  setupDetailResize();
  setupQuickAdd();
  setupSpeciesControls();
  setupFolderControls();
  setupSearch();
  setupViewControls();
  setupViewSettings();
  setupSimulation();
  setupRunsPanel();
  setupMora();
  setupCalendarExport();
  setupContextMenu();
  setupKeyboard();
  applyControlPanelVisibility();

  currentGarden = await call(api.current_garden());
  await loadGarden(currentGarden);
}

window.addEventListener("pywebviewready", init);
