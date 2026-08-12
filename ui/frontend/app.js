// ui/frontend/app.js
//
// Wiring between ui/api.py and the canvas graph in graph.js. State lives
// here; graph.js owns only rendering, layout and pointer gestures.

let api;
let graph;
let currentGarden = null;
let focusId = null;
let payload = { motes: [], relationships: [], species_ids: [] };
let allMotes = [];
let quickAddSpeciesId = null;
let speciesPickerTargetId = null;
let searchVisible = [];
let searchHighlight = -1;
const searchSpeciesFilter = new Set();
const searchRelFilter = new Set();
let multiSelectIds = null;             // non-null while the aggregate panel is open
let clipboard = null;                  // array of {properties, speciesIds, offset} from Copy
let pasteRepeatCount = 0;              // steps repeated Ctrl+V pastes apart so they don't stack
let contextMenuWorldPos = null;        // world coords the context menu was opened at

const DEPTH_KEY = "monoss:depth";
const SIM_ERROR_TOAST_MS = 12000;

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

async function runUndo() {
  if (!lastUndo) return;
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

/** Label for whatever a mote *is* -- its species names, or a structural
 *  role if it plays one. Shown above the title in the detail panel and in
 *  search results. */
function kindLabel(mote) {
  if (!mote) return "";
  if (payload.species_ids.includes(mote.id)) return "Species";
  const species = (payload.relationships || [])
    .filter((r) => r.type === "instance_of" && r.from_id === mote.id)
    .map((r) => moteById(r.to_id))
    .filter(Boolean)
    .map(nodeLabel);
  if (species.length) return species.join(", ");
  if (mote.properties && mote.properties.equation) return "Rule";
  return "Mote";
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
  return next;
}

async function loadGarden(garden) {
  currentGarden = garden;
  $("garden-label").textContent = garden;
  closeDetailPanel();
  graph.setHiddenSpecies(readJSON(hiddenSpeciesKey(garden), []));
  graph.setHiddenRelTypes(readJSON(hiddenRelKey(garden), []));
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
    if (payload.species_ids.includes(mote.id)) continue;
    const species = (payload.relationships || [])
      .filter((r) => r.type === "instance_of" && r.from_id === mote.id)
      .map((r) => r.to_id);
    if (species.length === 0) { untyped += 1; continue; }
    for (const id of species) counts.set(id, (counts.get(id) || 0) + 1);
  }
  const rows = payload.species_ids.map((id) => ({
    id,
    label: nodeLabel(moteById(id) || { id, properties: {} }),
    count: counts.get(id) || 0,
    color: graph.colorFor(id),
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
  for (const row of rows) {
    const el = document.createElement("div");
    const hidden = graph.hiddenSpecies.has(row.id);
    el.className = "filter-row" + (hidden ? " off" : "");
    el.title = hidden ? `Show ${row.label}` : `Hide ${row.label}`;
    el.innerHTML =
      `<span class="dot${row.diamond ? " diamond" : ""}" style="background:${row.color}"></span>` +
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

// ---- detail panel ----

async function openDetailPanel(moteId) {
  const [mote, rels, species, instances] = await Promise.all([
    call(api.get_mote(moteId)),
    call(api.mote_relationships(moteId)),
    call(api.species_of(moteId)),
    call(api.instances_of(moteId)),
  ]);
  if (!mote) return;

  multiSelectIds = null;
  $("detail-multi").classList.add("hidden");
  $("detail-single").classList.remove("hidden");

  $("detail-dot").style.background = graph.colorFor(moteId);
  $("detail-type-label").textContent = kindLabel(mote);
  $("detail-title").textContent = nodeLabel(mote);
  $("detail-id").textContent = mote.id;

  renderProperties(mote);
  renderSpeciesSection(mote, species, instances);
  renderRelationships(moteId, rels);
  $("detail-panel").classList.remove("hidden");
  fitAllTextareas();
}

/** Shown instead of the single-mote panel once a box-select (or shift-add)
 *  produces more than one selected mote. Its own small render function
 *  rather than a generalization of openDetailPanel -- an aggregate view
 *  and a per-mote editor genuinely show different things, and trying to
 *  make one a special case of the other would tangle both. */
function renderMultiSelectPanel(ids) {
  multiSelectIds = ids;
  $("detail-single").classList.add("hidden");
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
    .map(([sid, count]) => ({ label: nodeLabel(moteById(sid) || { id: sid, properties: {} }), count, color: graph.colorFor(sid) }))
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
  hideSpeciesPicker();
  multiSelectIds = null;
  if (graph) graph.select(null);
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
 *  one-line input. */
function valueField(rawValue, onCommit) {
  const text = typeof rawValue === "string" ? rawValue : JSON.stringify(rawValue);
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

function openLink(url) {
  call(api.open_link(url));
}

function renderProperties(mote) {
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

    const field = valueField(value, async (next) => {
      await call(api.update_mote(mote.id, { [key]: next }));
      await refocus(focusId, { pan: false });
      await refreshMoteCache();
    });

    const del = document.createElement("button");
    del.className = "del";
    del.textContent = "×";
    del.title = `Delete ${key}`;
    del.addEventListener("click", async () => {
      await call(api.delete_property(mote.id, key));
      await refocus(focusId, { pan: false });
      await openDetailPanel(mote.id);
    });

    // "url" is a reserved property name -- a mote holding one, whether a
    // web link or a local path, gets a one-click way to launch it via the
    // OS's default handler instead of only being able to read/edit it.
    if (key === "url" && typeof value === "string" && value.trim()) {
      const open = document.createElement("button");
      open.className = "open-link";
      open.textContent = "↗";
      open.title = "Open";
      open.addEventListener("click", () => openLink(value));
      row.append(keyEl, field, open, del);
    } else {
      row.append(keyEl, field, del);
    }
    container.appendChild(row);
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
      if (rel) await call(api.delete_relationship(rel.id));
      await refocus(focusId, { pan: false });
      await openDetailPanel(mote.id);
    });

    row.append(label, del);
    container.appendChild(row);
  }

  // Instantiating from a species is offered on the species itself, which
  // is where you are when you think "I want another one of these".
  const isSpecies = instances.length > 0 || payload.species_ids.includes(mote.id);
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
    type.addEventListener("click", async () => {
      const next = window.prompt("Relationship type:", rel.type);
      if (!next || next === rel.type) return;
      await call(api.update_relationship(rel.id, null, null, next));
      await refocus(focusId, { pan: false });
      await openDetailPanel(moteId);
    });

    const label = document.createElement("span");
    label.className = "rel-other";
    label.textContent = other ? nodeLabel(other) : otherId.slice(0, 8) + "…";
    label.title = otherId;
    label.addEventListener("click", () => selectMote(otherId));

    const del = document.createElement("button");
    del.textContent = "×";
    del.addEventListener("click", async () => {
      await call(api.delete_relationship(rel.id));
      await refocus(focusId, { pan: false });
      await openDetailPanel(moteId);
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

function setupQuickAdd() {
  resetQuickAddRows();
  $("quick-add-btn").addEventListener("click", () => {
    const panel = $("quick-add");
    const opening = panel.classList.contains("hidden");
    panel.classList.toggle("hidden", !opening);
    $("quick-add-btn").textContent = opening ? "×" : "+";
    if (opening) renderSpeciesChips();
    else contextMenuWorldPos = null; // closing without creating cancels any pending placement
  });

  $("add-property-row").addEventListener("click", () => addQuickAddRow());

  $("create-mote-btn").addEventListener("click", async () => {
    const props = collectQuickAddProperties();
    const mote = quickAddSpeciesId
      ? await call(api.create_instance(quickAddSpeciesId, props))
      : await call(api.create_mote(props));
    $("quick-add").classList.add("hidden");
    $("quick-add-btn").textContent = "+";
    resetQuickAddRows();
    await refreshMoteCache();
    // "New mote here" (right-click on empty canvas) remembers the click's
    // world position so the mote lands under the cursor instead of at its
    // usual hash-seeded spot; the ordinary + button leaves this unset.
    const placeAt = contextMenuWorldPos;
    contextMenuWorldPos = null;
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
 *  produces lands there instead of at its usual hash-seeded spot. */
function openQuickAddAt(worldX, worldY) {
  contextMenuWorldPos = { x: worldX, y: worldY };
  $("quick-add").classList.remove("hidden");
  $("quick-add-btn").textContent = "×";
  renderSpeciesChips();
}

// ---- species picker + new instance ----

function hideSpeciesPicker() {
  $("species-picker-wrap").classList.add("hidden");
  $("species-picker-input").value = "";
  speciesPickerTargetId = null;
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
      const targetId = speciesPickerTargetId;
      await call(api.set_species(targetId, mote.id));
      hideSpeciesPicker();
      await refocus(targetId, { pan: false });
      await openDetailPanel(targetId);
    });
    container.appendChild(row);
  }
}

function setupSpeciesControls() {
  $("set-species-btn").addEventListener("click", () => {
    if (!focusId) return;
    speciesPickerTargetId = focusId;
    $("species-picker-wrap").classList.remove("hidden");
    renderSpeciesPickerResults("");
    $("species-picker-input").focus();
  });
  $("species-picker-input").addEventListener("input", (e) =>
    renderSpeciesPickerResults(e.target.value)
  );
  $("species-picker-input").addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideSpeciesPicker();
  });

  $("new-instance-btn").addEventListener("click", async () => {
    if (!focusId) return;
    const mote = await call(api.create_instance(focusId, {}));
    await refreshMoteCache();
    await selectMote(mote.id);
    showToast("Instance created.");
  });
}

// ---- search palette ----

function matchesQuery(mote, query) {
  if (!query) return true;
  const q = query.toLowerCase();
  if (nodeLabel(mote).toLowerCase().includes(q)) return true;
  for (const [k, v] of Object.entries(mote.properties || {})) {
    if (k.toLowerCase().includes(q)) return true;
    const vs = typeof v === "string" ? v : JSON.stringify(v);
    if (vs.toLowerCase().includes(q)) return true;
  }
  return false;
}

function speciesIdsOf(moteId) {
  return payload.relationships
    .filter((r) => r.type === "instance_of" && r.from_id === moteId)
    .map((r) => r.to_id);
}

function computeSearchResults(query) {
  return allMotes
    .filter((mote) => {
      if (searchSpeciesFilter.size) {
        const ids = speciesIdsOf(mote.id);
        const matches = ids.some((id) => searchSpeciesFilter.has(id)) ||
          (searchSpeciesFilter.has("__none__") && ids.length === 0);
        if (!matches) return false;
      }
      if (searchRelFilter.size) {
        const touches = payload.relationships.some(
          (r) => (r.from_id === mote.id || r.to_id === mote.id) && searchRelFilter.has(r.type)
        );
        if (!touches) return false;
      }
      return matchesQuery(mote, query);
    })
    .sort((a, b) => nodeLabel(a).localeCompare(nodeLabel(b)))
    .slice(0, 60);
}

function renderSearchChips() {
  const speciesBox = $("search-species-chips");
  speciesBox.innerHTML = "";
  for (const row of speciesRows()) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip" + (searchSpeciesFilter.has(row.id) ? " on" : "");
    chip.style.borderColor = searchSpeciesFilter.has(row.id) ? row.color : "var(--border)";
    chip.innerHTML = `<span class="dot" style="background:${row.color}"></span><span></span>`;
    chip.querySelector("span:last-child").textContent = row.label;
    chip.addEventListener("click", () => {
      searchSpeciesFilter.has(row.id)
        ? searchSpeciesFilter.delete(row.id)
        : searchSpeciesFilter.add(row.id);
      renderSearchChips();
      renderSearchResults($("search-input").value);
    });
    speciesBox.appendChild(chip);
  }

  const relBox = $("search-rel-chips");
  relBox.innerHTML = "";
  for (const type of [...new Set(payload.relationships.map((r) => r.type))].sort()) {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip" + (searchRelFilter.has(type) ? " on" : "");
    chip.style.borderColor = searchRelFilter.has(type) ? "var(--accent)" : "var(--border)";
    chip.textContent = type;
    chip.addEventListener("click", () => {
      searchRelFilter.has(type) ? searchRelFilter.delete(type) : searchRelFilter.add(type);
      renderSearchChips();
      renderSearchResults($("search-input").value);
    });
    relBox.appendChild(chip);
  }
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

function updateSelectBox(rect) {
  const box = $("select-box");
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
    addMenuItem(menu, "New mote here", () => openQuickAddAt(worldX, worldY));
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
 *  on the same point. pasteRepeatCount tracks how many times the current
 *  clipboard has been pasted via Ctrl+V, so repeated pastes step apart
 *  instead of landing exactly on top of each other. */
function copySelection(ids) {
  const nodes = ids.map((id) => graph.nodes.get(id)).filter(Boolean);
  if (nodes.length === 0) return;
  const cx = nodes.reduce((s, n) => s + n.x, 0) / nodes.length;
  const cy = nodes.reduce((s, n) => s + n.y, 0) / nodes.length;
  clipboard = ids.map((id, i) => ({
    properties: { ...moteById(id).properties },
    speciesIds: speciesIdsOf(id),
    offset: { dx: nodes[i].x - cx, dy: nodes[i].y - cy },
  }));
  pasteRepeatCount = 0;
  showToast(clipboard.length > 1 ? `Copied ${clipboard.length} motes.` : "Copied.");
}

async function pasteAt(worldX, worldY) {
  if (!clipboard) return;
  const placed = [];
  for (const entry of clipboard) {
    const mote = await call(api.create_mote(entry.properties));
    for (const speciesId of entry.speciesIds) await call(api.set_species(mote.id, speciesId));
    placed.push({ id: mote.id, x: worldX + entry.offset.dx, y: worldY + entry.offset.dy });
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

// ---- gardens modal ----
//
// A centered modal like the search palette, not an anchored dropdown --
// consistent with how every other panel in this app works, and gives
// Export/Delete room to sit as real per-row buttons instead of being
// crammed into a narrow menu item.

async function renderGardensList() {
  const gardens = await call(api.list_gardens());
  const container = $("gardens-list");
  container.innerHTML = "";

  for (const name of gardens) {
    const row = document.createElement("div");
    row.className = "garden-row";
    row.innerHTML =
      '<span class="check"></span><span class="name"></span>' +
      '<button type="button" class="ghost">Export</button>' +
      '<button type="button" class="danger">Delete</button>';
    row.querySelector(".check").textContent = name === currentGarden ? "✓" : "";
    row.querySelector(".name").textContent = name;

    row.addEventListener("click", async () => {
      if (name === currentGarden) return;
      closeGardensModal();
      await call(api.switch_garden(name));
      await loadGarden(name);
    });

    row.querySelector(".ghost").addEventListener("click", async (e) => {
      e.stopPropagation();
      const result = await call(api.export_garden(name));
      if (!result.cancelled) showToast(`Exported to ${result.path}`);
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
  $("garden-btn").addEventListener("click", openGardensModal);
  $("gardens-overlay").addEventListener("mousedown", (e) => {
    if (e.target === $("gardens-overlay")) closeGardensModal();
  });

  $("new-garden-btn").addEventListener("click", async () => {
    const name = window.prompt("New garden name:");
    if (!name) return;
    await call(api.create_garden(name));
    await call(api.switch_garden(name));
    closeGardensModal();
    await loadGarden(name);
  });

  $("import-garden-btn").addEventListener("click", async () => {
    const name = window.prompt("Import into a new garden named:");
    if (!name) return;
    const result = await call(api.import_garden_into_new(name));
    if (result.cancelled) return;
    await call(api.switch_garden(result.garden));
    closeGardensModal();
    await loadGarden(result.garden);
    showToast(`Imported ${result.motes} motes, ${result.relationships} relationships.`);
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

  $("reset-view-btn").addEventListener("click", () => graph.resetView());
  $("close-panel").addEventListener("click", closeDetailPanel);
  $("connect-btn").addEventListener("click", () => {
    showToast("Ctrl+drag from one mote to another to connect them.");
  });

  $("detail-add-property-btn").addEventListener("click", async () => {
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
}

function setupSimulation() {
  const button = $("run-sim-btn");
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Running…";
    try {
      const result = await call(api.run_simulation());
      showToast(result.message, result.ok ? 3500 : SIM_ERROR_TOAST_MS);
      if (!result.ok || result.written === 0) return;
      await refreshMoteCache();
      await refocus(focusId, { pan: false });
      if (!$("detail-panel").classList.contains("hidden") && focusId) {
        await openDetailPanel(focusId);
      }
    } finally {
      button.disabled = false;
      button.textContent = "Run simulation";
    }
  });
}

function setupCalendarExport() {
  const button = $("export-calendar-btn");
  button.addEventListener("click", async () => {
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
    if (!$("species-picker-wrap").classList.contains("hidden")) { hideSpeciesPicker(); return; }
    if (!$("context-menu").classList.contains("hidden")) { hideContextMenu(); return; }
    if (!$("quick-add").classList.contains("hidden")) {
      $("quick-add").classList.add("hidden");
      $("quick-add-btn").textContent = "+";
      contextMenuWorldPos = null;
      return;
    }
    if (!$("detail-panel").classList.contains("hidden")) { closeDetailPanel(); return; }
    // Nothing else was open -- Esc's last job is to deselect.
    graph.select(null);
  });
}

// ---- init ----

async function init() {
  api = window.pywebview.api;

  graph = new MonossGraph($("graph"), {
    labelFor: nodeLabel,
    onSelect: (id) => selectMote(id),
    onConnect: (sourceId, targetId) => connectNodes(sourceId, targetId),
    onPositionsChanged: savePositions,
    onBoxSelect: updateSelectBox,
    onSelectionChanged: syncSelectionPanel,
    onContextMenu: openContextMenu,
  });
  window.graph = graph;

  setupGardensModal();
  setupQuickAdd();
  setupSpeciesControls();
  setupSearch();
  setupViewControls();
  setupSimulation();
  setupCalendarExport();
  setupContextMenu();
  setupKeyboard();

  currentGarden = await call(api.current_garden());
  await loadGarden(currentGarden);
}

window.addEventListener("pywebviewready", init);
