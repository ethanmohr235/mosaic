// motion/frontend/app.js
//
// Wiring between motion/api.py and the simulation window. Everything
// about *what* the garden contains comes back from Python already
// decided: which motes are nodes and what kind each one is
// (motion/payload.py derives them), which values a run moved, what a
// rule binds. This file draws that answer, scrubs it, and takes edits.
//
// Four things Python has no opinion about, and this file owns:
//
//   layout      where a node sits. A mote carries no coordinates, and
//               writing one back would make every drag a graph edit --
//               so positions live in localStorage per garden, seeded by
//               a deterministic layered placement (see layoutNodes).
//   the tick    which step of a run is being looked at. The whole run
//               comes back in one payload and the scrub is local, so
//               dragging the playhead costs nothing and never re-solves.
//   the overlay a node's value at that tick, folded from the run's
//               changes over the values the board shipped (valuesAt).
//   the mode    build / run / compare, which is a question about this
//               window and not about the garden.
//
// # Ticks, not seconds
//
// The axis is ticks throughout, because core/simulation.py has no
// timestep: a tick is one re-solve of the equation set. `axis` comes
// back from Python (motion/payload.py::tick_axis) rather than being
// assumed here, so when the engine grows a real time axis this file
// reads a second unit off the same field instead of learning to divide.
//
// # What is not here
//
// No hardware rail and no integrator menu. Neither exists in core, and
// the design's versions of both were cut rather than stubbed -- a live
// green readout that is really a sine wave is worse than an absent one.

const THIS_APP = "motion";
const LAUNCHER_COLUMNS = 2;     // matches #launcher-results grid-template-columns
const PLAY_MS = 90;             // one tick per this, while playing
const NODE_W = 168;             // node card width, in board coordinates
const NODE_H = 62;              // assumed node height, for edge clipping
const COL_GAP = 54;
const ROW_GAP = 48;
const MIN_ZOOM = 0.4;
const MAX_ZOOM = 2.4;
const DEFAULT_STEPS = 10;

const KIND_COLOR = {
  rule: "var(--node-rule)",
  free: "var(--node-free)",
  varied: "var(--node-varied)",
  fixed: "var(--node-fixed)",
};

let api;
let board = null;          // the last board() answer
let runA = null;           // { steps, axis, events, ... } -- the primary run
let runB = null;           // the comparison run, in compare mode
let lanesA = [];
let lanesB = [];
let tick = 0;              // 0 = the garden before the run; k = through steps[k-1]
let playing = false;
let playTimer = null;
let mode = "run";          // build | run | compare
let scenario = null;       // active scenario mote id, or null for the garden as it stands
let compareWith = null;
let compareRunA = null;    // a saved run's id standing in for scenario A, in compare mode
let compareRunB = null;    // a saved run's id standing in for scenario B, in compare mode
let panelTab = "rules";
let activeRule = null;
// Rule mote id -> [{name, candidates: [{id, label}]}] from the last
// check_equation_names() call on that rule's equation -- names it
// references that exist in the garden but aren't reachable from where
// it runs. Cleared on connect, dismiss, or the next successful equation
// write (which re-checks). Not part of `board`: it's a client-side
// hint about the *previous* blur's text, not a fact core answers.
let equationSuggestions = {};
let selectedId = null;
let selectedRelId = null;   // a clicked edge, mutually exclusive with selectedId
// The Set every multi-select gesture (shift+click, box-drag) actually
// mutates; selectedId mirrors it when exactly one mote is selected and
// goes null otherwise -- see setSelection().
let selectedIds = new Set();
let boxState = null;        // { startX, startY, moved, shiftBase } -- a box-select drag in progress
let connectArmed = false;
let solver = "linear";
let steps = DEFAULT_STEPS;
let replayRunId = null;    // set while the dock is showing a logged run rather than a preview
let toastTimer = null;
let launcherHighlight = 0;
let drag = null;
let pan = null;
let resize = null;
// The id of the board node the open inspector's leader line points at,
// if any -- re-resolved by data-id on every pan/zoom tick rather than
// held as an element reference, because render() rebuilds the board's
// DOM (renderBoards' innerHTML = "") on plenty of things that aren't a
// pan, including the click that opens the inspector in the first place.
// Only board A's nodes are ever clickable (buildNode only wires
// onmousedown for !isB), so a plain data-id match is unambiguous even
// in compare mode. Null when the inspector is anchored to something
// that doesn't pan with the board (a dock lane, a relationship
// endpoint) or not anchored at all.
let inspectedNodeId = null;

function resolveInspectedNode() {
  if (!inspectedNodeId) return null;
  return document.querySelector(`.node[data-id="${inspectedNodeId}"]`);
}
let scrubbing = false;
let linkDrag = null;       // { sourceId, x1, y1, line } -- a ctrl+drag in progress

let layout = {             // sizes and view the user drags -- window chrome, not garden data
  railW: 236, panelW: 328, dockH: 280, zoom: 1, panX: 0, panY: 0,
};
let positions = {};        // mote id -> {x, y}, per garden
// mote id -> [key, ...], per garden -- which of a node's numeric
// properties its card previews. A mote with no entry falls back to
// primaryKey's auto-pick (see previewKeysFor).
let previewKeys = {};

const $ = (id) => document.getElementById(id);

// ---- bridge ---------------------------------------------------------

async function call(promise, failure) {
  try {
    return await promise;
  } catch (error) {
    toast(failure || String(error && error.message ? error.message : error), true);
    return null;
  }
}

function toast(message, isError) {
  const el = $("toast");
  el.textContent = message;
  el.classList.toggle("error", !!isError);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), isError ? 5000 : 2600);
}

// ---- persisted window state -----------------------------------------
//
// Layout is per window and holds across gardens; positions are per
// garden, since a mote id means nothing in another one.

function layoutKey() { return "motion.layout"; }
function positionsKey() { return `motion.positions.${board ? board.garden : "?"}`; }
function previewKeysKey() { return `motion.previewKeys.${board ? board.garden : "?"}`; }

function loadLayout() {
  try {
    const raw = window.localStorage.getItem(layoutKey());
    if (raw) layout = { ...layout, ...JSON.parse(raw) };
  } catch (error) {
    // A window that cannot remember its sizes just uses the defaults.
  }
}

function saveLayout() {
  try {
    window.localStorage.setItem(layoutKey(), JSON.stringify(layout));
  } catch (error) {
    // Not fatal -- see loadLayout.
  }
}

function loadPositions() {
  positions = {};
  try {
    const raw = window.localStorage.getItem(positionsKey());
    if (raw) positions = JSON.parse(raw) || {};
  } catch (error) {
    positions = {};
  }
}

function savePositions() {
  try {
    window.localStorage.setItem(positionsKey(), JSON.stringify(positions));
  } catch (error) {
    // Not fatal.
  }
}

function loadPreviewKeys() {
  previewKeys = {};
  try {
    const raw = window.localStorage.getItem(previewKeysKey());
    if (raw) previewKeys = JSON.parse(raw) || {};
  } catch (error) {
    previewKeys = {};
  }
}

function savePreviewKeys() {
  try {
    window.localStorage.setItem(previewKeysKey(), JSON.stringify(previewKeys));
  } catch (error) {
    // Not fatal.
  }
}

/** Which of a node's numeric properties its card shows, in the order the
 *  user picked them. Empty/unset falls back to primaryKey's single
 *  auto-pick, so a mote nobody has customized still shows its most
 *  relevant value the way it always has. */
function previewKeysFor(node, moved) {
  const chosen = (previewKeys[node.id] || []).filter((k) =>
    Object.prototype.hasOwnProperty.call(node.values, k)
  );
  if (chosen.length) return chosen;
  const auto = primaryKey(node, moved);
  return auto ? [auto] : [];
}

function togglePreviewKey(nodeId, key) {
  const node = board.nodes.find((n) => n.id === nodeId);
  const current = previewKeys[nodeId] && previewKeys[nodeId].length
    ? previewKeys[nodeId].slice()
    : [primaryKey(node, movedKeys(runA))].filter(Boolean);
  const at = current.indexOf(key);
  if (at >= 0) current.splice(at, 1);
  else current.push(key);
  previewKeys[nodeId] = current;
  savePreviewKeys();
  render();
}

function applyLayout() {
  $("rail").style.width = `${layout.railW}px`;
  $("panel").style.width = `${layout.panelW}px`;
  $("dock").style.height = `${layout.dockH}px`;
  $("zoom-label").textContent = `${Math.round(layout.zoom * 100)}%`;
}

// ---- node layout ----------------------------------------------------
//
// A deterministic layered placement, not a force simulation: Monarch
// already owns the force field, and a board that settles somewhere
// different every open is a board you cannot point at. Rules go on the
// bottom row because they are what acts on everything above them, which
// is also how the arrows then read -- downward into a rule, and back up
// out of it.
//
// Only used to seed a position. Once a node has been dragged its
// position is the user's, and layoutNodes leaves it alone unless they
// ask for a re-layout.

function layoutNodes(nodes) {
  const rules = nodes.filter((n) => n.kind === "rule");
  const rest = nodes.filter((n) => n.kind !== "rule");
  const perRow = Math.max(3, Math.ceil(Math.sqrt(rest.length || 1)));
  const seeded = {};

  rest.forEach((node, i) => {
    seeded[node.id] = {
      x: (i % perRow) * (NODE_W + COL_GAP),
      y: Math.floor(i / perRow) * (NODE_H + ROW_GAP),
    };
  });
  const ruleRow = Math.ceil((rest.length || 1) / perRow) * (NODE_H + ROW_GAP) + ROW_GAP;
  rules.forEach((node, i) => {
    seeded[node.id] = {
      x: (i % perRow) * (NODE_W + COL_GAP),
      y: ruleRow + Math.floor(i / perRow) * (NODE_H + ROW_GAP),
    };
  });
  return seeded;
}

function nodePosition(id) {
  return positions[id] || { x: 0, y: 0 };
}

/** Seeds any node that has never been placed, leaving dragged ones
 *  alone -- so a mote added in Monarch while this window was open
 *  appears in a sensible spot instead of at the origin. */
function seedPositions(nodes) {
  const seeded = layoutNodes(nodes);
  let added = false;
  for (const node of nodes) {
    if (!positions[node.id]) {
      positions[node.id] = seeded[node.id];
      added = true;
    }
  }
  if (added) savePositions();
}

// ---- the run --------------------------------------------------------

function stepCount() { return runA ? runA.steps.length : 0; }

/** Every value a run moved, folded forward to `upTo` ticks. Point 0 is
 *  the garden as it stands, which is why the fold starts from the
 *  board's own values rather than from the first step. */
function valuesAt(run, upTo) {
  const overlay = {};
  if (!run) return overlay;
  for (const step of run.steps.slice(0, upTo)) {
    for (const change of step.changes) {
      overlay[`${change.mote_id}|${change.key}`] = change.new_value;
    }
  }
  return overlay;
}

function nodeValue(node, overlay, key) {
  const hit = overlay[`${node.id}|${key}`];
  return hit === undefined ? node.values[key] : hit;
}

/** Which of a node's numeric properties the card shows. Prefers one the
 *  run actually moves -- a card showing a constant while the value
 *  beside it is the one being solved for is the wrong card. */
function primaryKey(node, moved) {
  const keys = Object.keys(node.values);
  if (!keys.length) return null;
  const active = keys.find((k) => moved.has(`${node.id}|${k}`));
  return active || keys[0];
}

function movedKeys(run) {
  const set = new Set();
  if (!run) return set;
  for (const step of run.steps) {
    for (const change of step.changes) set.add(`${change.mote_id}|${change.key}`);
  }
  return set;
}

function fmt(value) {
  if (value === null || value === undefined) return "—";
  if (!isFinite(value)) return String(value);
  const abs = Math.abs(value);
  if (abs !== 0 && (abs < 0.001 || abs >= 1e6)) return value.toExponential(2);
  return String(Math.round(value * 1000) / 1000);
}

// ---- board rendering -------------------------------------------------

function render() {
  if (!board) return;
  renderModes();
  renderRail();
  renderBoards();
  renderDock();
  renderPanel();
  renderTopbar();
}

function renderTopbar() {
  $("garden-name").textContent = board.garden;
  $("solver-short").textContent = solver;
  $("solver-caption").textContent = runA
    ? `${solver} · ${stepCount()} ${runA.axis.label}`
    : "";

  // Fast mode solves and commits on its own -- the manual Run/Save/
  // Commit trio would otherwise still kick off the old preview flow
  // into a dock that Fast mode has hidden.
  const fast = mode === "fast";
  $("run-btn").classList.toggle("hidden", fast);
  $("save-btn").classList.toggle("hidden", fast);
  $("commit-btn").classList.toggle("hidden", fast);
  if (fast) { setRunState(); return; }

  // Saving is the only way to keep a scenario run, so it is offered
  // wherever there is a run to keep -- including the cases Commit
  // refuses.
  const save = $("save-btn");
  save.disabled = !runA || !!replayRunId;
  save.title = replayRunId
    ? "This run is already logged."
    : "Log the whole run for later review, without writing it to the graph";

  const canCommit = !!runA && tick > 0 && !scenario && !replayRunId && mode !== "compare";
  const commit = $("commit-btn");
  commit.disabled = !canCommit;
  commit.textContent = tick > 0 ? `Commit tick ${tick}` : "Commit tick";
  commit.title = scenario
    ? "A scenario run is hypothetical and is never written to the graph. Save it instead."
    : replayRunId
      ? "This is a logged run being replayed. Run the garden again to commit."
      : "Re-run to this tick and write the result to the graph";
  setRunState();
}

/** The chip that says which question the dock is answering. Three
 *  states worth telling apart, and all three mean "what you are looking
 *  at is not necessarily the garden": a scenario never will be, a
 *  replay already happened, and a preview has not been written. */
function setRunState() {
  const chip = $("run-state");
  chip.className = "state-chip";
  if (mode === "fast") {
    chip.classList.remove("hidden");
    chip.textContent = fastStatus === "solving" ? "solving…" : fastStatus === "error" ? "error" : "solved";
    chip.title = fastStatus === "error" && fastError ? fastError : "";
    if (fastStatus === "error") chip.classList.add("replay");
    return;
  }
  // A selected scenario is worth saying before anything has been run:
  // it is the question the next Run will answer, and picking one with
  // no visible acknowledgement is how you end up reading a scenario's
  // numbers as the garden's.
  if (!runA && !scenario) { chip.classList.add("hidden"); return; }
  if (replayRunId) {
    chip.textContent = "replay";
    chip.classList.add("replay");
  } else if (scenario) {
    const sc = board.scenarios.find((s) => s.id === scenario);
    chip.textContent = `scenario · ${sc ? sc.name : "?"}`;
  } else {
    chip.textContent = "preview";
  }
}

function renderModes() {
  const host = $("mode-chips");
  host.innerHTML = "";
  for (const [id, label] of [["build", "Build"], ["run", "Run"], ["compare", "Compare"], ["fast", "Fast"]]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    btn.className = mode === id ? "on" : "";
    btn.onclick = () => setMode(id);
    host.appendChild(btn);
  }
}

function setMode(next) {
  mode = next;
  if (mode === "compare" && !compareWith) {
    const other = board.scenarios.find((s) => s.id !== scenario);
    compareWith = other ? other.id : null;
  }
  if (mode !== "compare") { runB = null; lanesB = []; compareRunA = null; compareRunB = null; }
  if (mode === "fast") {
    runA = null;
    tick = 0;
    replayRunId = null;
    panelTab = "rules";
    scheduleFastRun();
  }
  if (mode === "build") panelTab = "rules";
  render();
}

function renderRail() {
  // Fast mode's edits commit straight away, so none of the rail earns
  // its space: a scenario can never be committed (core/simulation.py
  // refuses one), the saved-run history is exactly what Fast mode is
  // trying not to accumulate, and pre-flight role checking is a
  // build-time concern.
  const fast = mode === "fast";
  $("rail").classList.toggle("hidden", fast);
  $("rail-resize").classList.toggle("hidden", fast);
  if (fast) return;

  const scenarios = $("scenario-list");
  scenarios.innerHTML = "";
  $("scenario-count").textContent = board.scenarios.length
    ? `${board.scenarios.length}`
    : "";

  scenarios.appendChild(scenarioCard(
    { id: null, name: "Garden as it stands", overrideCount: 0 },
    scenario === null,
  ));
  for (const sc of board.scenarios) {
    scenarios.appendChild(scenarioCard(sc, scenario === sc.id));
  }
  if (!board.scenarios.length) {
    const note = document.createElement("div");
    note.className = "rail-empty";
    note.textContent =
      "No scenarios yet. A scenario is a mote with `varies` arrows to override motes — see core/scenarios.py.";
    scenarios.appendChild(note);
  }

  const runs = $("run-list");
  runs.innerHTML = "";
  // Fast mode auto-commits on every debounced edit and tags each one
  // "fast" (see runFastCommit) precisely so they don't pile up here --
  // they're still logged for the audit trail, just not worth a card.
  const keptRuns = board.runs.filter((r) => r.detail !== "fast");
  $("run-count").textContent = keptRuns.length ? `${keptRuns.length} kept` : "";
  for (const run of keptRuns) {
    const card = document.createElement("button");
    card.type = "button";
    const isRunA = mode === "compare" && compareRunA === run.run_id;
    const isRunB = mode === "compare" && compareRunB === run.run_id;
    card.className = "card" + (replayRunId === run.run_id || isRunA || isRunB ? " on" : "");
    card.innerHTML =
      `<div class="card-top"><span class="card-label"></span>` +
      (isRunA ? `<span class="chip a">A</span>` : "") +
      (isRunB ? `<span class="chip b">B</span>` : "") +
      `</div>` +
      `<div class="card-meta"></div>`;
    card.querySelector(".card-label").textContent = runLabel(run);
    card.querySelector(".card-meta").textContent =
      [run.status, run.committed ? "committed" : "saved",
       `${run.change_count} value${run.change_count === 1 ? "" : "s"}`,
       (run.ran_at || "").slice(0, 16).replace("T", " ")].filter(Boolean).join(" · ");
    if (run.scenario_id) {
      const chips = document.createElement("div");
      chips.className = "card-chips";
      const chip = document.createElement("span");
      chip.className = "chip";
      const sc = board.scenarios.find((s) => s.id === run.scenario_id);
      chip.textContent = sc ? sc.name : "scenario";
      chips.appendChild(chip);
      card.appendChild(chips);
    }
    if (run.detail) card.title = run.detail;
    if (mode === "compare") {
      // In compare mode a saved run can stand in for a scenario on
      // either side -- left click for A, right click for B, mirroring
      // how scenario cards themselves are picked.
      card.title = (card.title ? card.title + " -- " : "") +
        "Click to compare as scenario A, right-click for B.";
      card.onclick = () => {
        compareRunA = run.run_id;
        render();
        if (runA) runSimulation();
      };
      card.oncontextmenu = (e) => {
        e.preventDefault();
        compareRunB = run.run_id;
        render();
        if (runA) runSimulation();
      };
    } else {
      card.onclick = () => enterRun(run.run_id);
    }
    runs.appendChild(card);
  }
  if (!keptRuns.length) {
    const note = document.createElement("div");
    note.className = "rail-empty";
    note.textContent = "No runs logged yet. Commit or save a run to keep it here.";
    runs.appendChild(note);
  }
}

function scenarioCard(sc, on) {
  const card = document.createElement("button");
  card.type = "button";
  card.className = "card" + (on ? " on" : "");
  const isA = mode === "compare" && !compareRunA && scenario === sc.id;
  const isB = mode === "compare" && !compareRunB && compareWith === sc.id;
  card.innerHTML =
    `<div class="card-top">` +
    `<span class="card-dot"></span><span class="card-label"></span>` +
    (isA ? `<span class="chip a">A</span>` : "") +
    (isB ? `<span class="chip b">B</span>` : "") +
    `</div>`;
  card.querySelector(".card-dot").style.background =
    sc.id === null ? "var(--node-fixed)" : "var(--node-varied)";
  card.querySelector(".card-label").textContent = sc.name;
  if (sc.overrideCount) {
    const chips = document.createElement("div");
    chips.className = "card-chips";
    const chip = document.createElement("span");
    chip.className = "chip";
    chip.textContent = `${sc.overrideCount} override${sc.overrideCount === 1 ? "" : "s"}`;
    chips.appendChild(chip);
    card.appendChild(chips);
  }
  card.onclick = () => {
    // In compare mode, left click always picks scenario A.
    scenario = sc.id;
    compareRunA = null;
    replayRunId = null;
    render();
    if (runA) runSimulation();
  };
  card.oncontextmenu = (e) => {
    if (mode !== "compare") return;
    e.preventDefault();
    compareWith = sc.id;
    compareRunB = null;
    render();
    if (runA) runSimulation();
  };
  return card;
}

/** A run's label the way the rail lists it -- "run-0004" -- shared by
 *  the run card and, once a run stands in for a scenario, the compare
 *  board head. */
function runLabel(run) {
  return `run-${String(run.run_number).padStart(4, "0")}`;
}

function renderBoards() {
  const host = $("boards");
  host.innerHTML = "";
  seedPositions(board.nodes);
  if (!board.nodes.length) {
    const empty = document.createElement("div");
    empty.className = "stage-empty";
    empty.textContent =
      "This garden has no motes to simulate yet. Add one with ＋, give it an equation with ƒ, " +
      "and point the rule at what it governs.";
    host.appendChild(empty);
    return;
  }
  const boardA = buildBoard(runA, scenario, false, compareRunA);
  host.appendChild(boardA);
  finishEdges(boardA, false);
  if (mode === "compare") {
    const boardB = buildBoard(runB, compareWith, true, compareRunB);
    host.appendChild(boardB);
    finishEdges(boardB, true);
  }
}

function buildBoard(run, scenarioId, isB, runId) {
  const el = document.createElement("div");
  el.className = "board";

  const sc = runId ? null : board.scenarios.find((s) => s.id === scenarioId);
  const savedRun = runId ? board.runs.find((r) => r.run_id === runId) : null;
  const head = document.createElement("div");
  head.className = "board-head";
  head.innerHTML =
    `<span class="card-dot"></span><span class="board-label"></span>` +
    `<span class="board-badge${isB ? " b" : ""}"></span>` +
    `<span class="board-readout"></span>`;
  head.querySelector(".card-dot").style.background =
    runId || scenarioId !== null ? "var(--node-varied)" : "var(--node-fixed)";
  head.querySelector(".board-label").textContent =
    savedRun ? runLabel(savedRun) : sc ? sc.name : "Garden as it stands";
  head.querySelector(".board-badge").textContent =
    mode === "build" ? "editing" : runId ? (isB ? "run B" : "run A")
      : isB ? "scenario B" : mode === "compare" ? "scenario A" : "running";
  head.querySelector(".board-readout").textContent = run
    ? `${run.steps.length} ${run.axis.label} · tick ${tick}`
    : "not run";
  el.appendChild(head);

  const surface = document.createElement("div");
  surface.className = "board-surface";
  surface.onmousedown = (event) => onPanDown(event, isB);
  surface.onwheel = onZoomWheel;
  surface.oncontextmenu = (e) => { if (pan) e.preventDefault(); };

  const world = document.createElement("div");
  world.className = "board-world";
  world.style.transform = `translate(${layout.panX}px, ${layout.panY}px) scale(${layout.zoom})`;

  const extent = worldExtent();
  world.style.width = `${extent.w}px`;
  world.style.height = `${extent.h}px`;

  const overlay = valuesAt(run, tick);
  const moved = movedKeys(run);
  for (const node of board.nodes) {
    world.appendChild(buildNode(node, overlay, moved, run, isB));
  }
  // Cards now size to their content, so a long mote name can reach past
  // the width worldExtent() assumed (it only knows NODE_W). Grow the
  // world to the actual rendered right edge or dragged-off cards get
  // clipped by the surface's overflow:hidden.
  let maxRight = extent.w;
  for (const nodeEl of world.querySelectorAll(".node")) {
    const p = nodePosition(nodeEl.dataset.id);
    maxRight = Math.max(maxRight, p.x + nodeEl.offsetWidth + 60);
  }
  if (maxRight > extent.w) world.style.width = `${maxRight}px`;
  surface.appendChild(world);
  el.appendChild(surface);
  return el;
}

/** Draws edges once a board's nodes are attached to the document, so
 *  clipping can read each card's real rendered height instead of the
 *  assumed NODE_H -- a rule's equation text wraps to whatever height it
 *  needs, and clipping against a guessed height is how an arrowhead ends
 *  up short of, or past, the actual card border. */
function finishEdges(boardEl, isB) {
  const world = boardEl.querySelector(".board-world");
  if (!world) return;
  const sizes = {};
  for (const nodeEl of world.querySelectorAll(".node")) {
    sizes[nodeEl.dataset.id] = { w: nodeEl.offsetWidth, h: nodeEl.offsetHeight };
  }
  const extent = { w: parseFloat(world.style.width) || 0, h: parseFloat(world.style.height) || 0 };
  world.insertBefore(buildEdges(extent, sizes, isB), world.firstChild);
}

function worldExtent() {
  let w = 400;
  let h = 300;
  for (const node of board.nodes) {
    const p = nodePosition(node.id);
    w = Math.max(w, p.x + NODE_W + 60);
    h = Math.max(h, p.y + NODE_H + 60);
  }
  return { w, h };
}

function buildNode(node, overlay, moved, run, isB) {
  const el = document.createElement("div");
  const p = nodePosition(node.id);
  el.className = `node ${node.kind}` + (selectedIds.has(node.id) ? " on" : "");
  el.dataset.id = node.id;
  el.style.left = `${p.x}px`;
  el.style.top = `${p.y}px`;

  const top = document.createElement("div");
  top.className = "node-top";
  const dot = document.createElement("span");
  dot.className = "node-dot";
  dot.style.background = KIND_COLOR[node.kind] || KIND_COLOR.fixed;
  const label = document.createElement("span");
  label.className = "node-label";
  label.textContent = node.label;
  label.title = `${node.label} · ${node.kind}`;
  top.append(dot, label);
  el.appendChild(top);

  if (node.kind === "rule" && node.equation) {
    const eq = document.createElement("div");
    eq.className = "node-eq";
    eq.textContent = node.equation;
    el.appendChild(eq);
  } else {
    for (const key of previewKeysFor(node, moved)) {
      const now = nodeValue(node, overlay, key);
      const before = nodeValue(node, valuesAt(run, Math.max(0, tick - 1)), key);
      const delta = typeof now === "number" && typeof before === "number" ? now - before : 0;
      const unit = (node.units && node.units[key]) || "";
      const row = document.createElement("div");
      row.className = "node-value";
      row.dataset.key = key;
      row.innerHTML =
        `<span class="node-num"></span><span class="node-unit"></span>` +
        `<span class="node-delta"></span><span class="node-key"></span>`;
      row.querySelector(".node-num").textContent = fmt(now);
      row.querySelector(".node-unit").textContent = unit;
      row.querySelector(".node-key").textContent = key;
      const deltaEl = row.querySelector(".node-delta");
      deltaEl.textContent = delta === 0 ? "—" : `${delta > 0 ? "+" : ""}${fmt(delta)}`;
      deltaEl.className = `node-delta ${delta === 0 ? "flat" : delta > 0 ? "up" : "down"}`;
      el.appendChild(row);
    }
  }

  // Only board A takes edits: two boards showing the same motes under
  // two scenarios would otherwise offer two places to drag the same node
  // to two different positions.
  if (!isB) {
    el.onmousedown = onNodeDown(node.id);
    el.oncontextmenu = onNodeContext(node.id);
  }
  return el;
}

function buildEdges(extent, sizes, isB) {
  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("class", "board-edges");
  svg.setAttribute("width", extent.w);
  svg.setAttribute("height", extent.h);

  const defs = document.createElementNS(NS, "defs");
  for (const [id, color] of [
    ["mt-arrow", "oklch(0.55 0 0)"],
    ["mt-arrow-rule", "oklch(0.66 0.09 75)"],
    ["mt-arrow-selected", "var(--select-rel-edge)"],
  ]) {
    const marker = document.createElementNS(NS, "marker");
    marker.setAttribute("id", id);
    marker.setAttribute("viewBox", "0 0 8 8");
    marker.setAttribute("refX", "7");
    marker.setAttribute("refY", "4");
    marker.setAttribute("markerWidth", "7");
    marker.setAttribute("markerHeight", "7");
    marker.setAttribute("orient", "auto");
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", "M0,1 L7,4 L0,7 z");
    path.setAttribute("fill", color);
    marker.appendChild(path);
    defs.appendChild(marker);
  }
  svg.appendChild(defs);

  // Two motes can carry several relationships between them -- stacking
  // them on the same straight line leaves every edge but the topmost
  // unclickable. Group edges by their unordered node pair and fan the
  // group out into parallel, evenly spaced lines.
  const PARALLEL_SPACING = 7;
  const pairGroups = new Map();
  for (const edge of board.edges) {
    const key = [edge.from, edge.to].sort().join("::");
    if (!pairGroups.has(key)) pairGroups.set(key, []);
    pairGroups.get(key).push(edge);
  }

  for (const edge of board.edges) {
    const a = positions[edge.from];
    const b = positions[edge.to];
    if (!a || !b) continue;
    const sizeA = sizes[edge.from] || { w: NODE_W, h: NODE_H };
    const sizeB = sizes[edge.to] || { w: NODE_W, h: NODE_H };
    const ax = a.x + sizeA.w / 2;
    const ay = a.y + sizeA.h / 2;
    const bx = b.x + sizeB.w / 2;
    const by = b.y + sizeB.h / 2;
    let [sx, sy] = clip(ax, ay, bx - ax, by - ay, sizeA.w / 2, sizeA.h / 2, 3);
    let [ex, ey] = clip(bx, by, ax - bx, ay - by, sizeB.w / 2, sizeB.h / 2, 6);

    const group = pairGroups.get([edge.from, edge.to].sort().join("::"));
    if (group.length > 1) {
      const idx = group.indexOf(edge);
      const offset = (idx - (group.length - 1) / 2) * PARALLEL_SPACING;
      // Derive the perpendicular from the pair's node order, not this
      // edge's own from/to -- two relationships drawn in opposite
      // directions between the same motes would otherwise negate each
      // other's offset and land back on the same line.
      const flip = edge.from > edge.to ? -1 : 1;
      const len = Math.hypot(bx - ax, by - ay) || 1;
      const px = (-(by - ay) / len) * flip;
      const py = ((bx - ax) / len) * flip;
      sx += px * offset; sy += py * offset;
      ex += px * offset; ey += py * offset;
    }
    // `reads` edges are synthetic -- see payload.build_edges -- and
    // carry no relationship row to select, rename or delete.
    const selectable = edge.kind !== "reads" && !isB;
    const selected = edge.id === selectedRelId;
    const line = document.createElementNS(NS, "line");
    line.setAttribute("x1", sx);
    line.setAttribute("y1", sy);
    line.setAttribute("x2", ex);
    line.setAttribute("y2", ey);
    line.setAttribute("stroke", selected
      ? "var(--select-rel-edge)"
      : edge.kind === "governs"
        ? "oklch(0.66 0.09 75 / 0.65)"
        : edge.kind === "reads"
          ? "oklch(0.60 0.05 235 / 0.4)"
          : "oklch(0.55 0 0 / 0.8)");
    line.setAttribute("stroke-width", selected ? 2.2 : edge.kind === "relation" ? 1.4 : 1.1);
    if (edge.kind !== "relation") line.setAttribute("stroke-dasharray", "5 4");
    line.setAttribute("marker-end", selected
      ? "url(#mt-arrow-selected)"
      : edge.kind === "governs" ? "url(#mt-arrow-rule)" : "url(#mt-arrow)");
    const title = document.createElementNS(NS, "title");
    title.textContent = edge.type;
    line.appendChild(title);
    if (selectable) {
      // A transparent, wider twin under the visible stroke -- the drawn
      // line is 1-2px, too thin a target to reliably click.
      const hit = document.createElementNS(NS, "line");
      hit.setAttribute("x1", sx);
      hit.setAttribute("y1", sy);
      hit.setAttribute("x2", ex);
      hit.setAttribute("y2", ey);
      hit.setAttribute("stroke", "transparent");
      hit.setAttribute("stroke-width", group.length > 1 ? String(Math.min(12, PARALLEL_SPACING)) : "12");
      hit.style.pointerEvents = "stroke";
      hit.style.cursor = "pointer";
      hit.onmousedown = (event) => { event.stopPropagation(); selectRelationship(edge.id); };
      svg.appendChild(hit);
    }
    svg.appendChild(line);
  }
  return svg;
}

/** Clip an edge to a node's box so the arrowhead lands on the border
 *  rather than under the card. halfW/halfH are that node's actual
 *  rendered half-extents, not the assumed NODE_W/NODE_H -- a card's real
 *  box is what the arrow has to land on. */
function clip(cx, cy, dx, dy, halfW, halfH, pad) {
  const len = Math.hypot(dx, dy) || 1;
  const ux = dx / len;
  const uy = dy / len;
  const tx = Math.abs(ux) > 1e-6 ? halfW / Math.abs(ux) : Infinity;
  const ty = Math.abs(uy) > 1e-6 ? halfH / Math.abs(uy) : Infinity;
  const t = Math.min(tx, ty) + pad;
  return [cx + ux * t, cy + uy * t];
}

// ---- dock ------------------------------------------------------------

function renderDock() {
  const fast = mode === "fast";
  $("dock").classList.toggle("hidden", fast);
  $("dock-resize").classList.toggle("hidden", fast);
  if (fast) return;

  const total = stepCount();
  const hasRun = total > 0;
  $("dock-empty").classList.toggle("hidden", hasRun);
  $("track").classList.toggle("empty", !hasRun);
  $("playhead").classList.toggle("hidden", !hasRun);
  $("ruler").innerHTML = "";
  $("lanes").innerHTML = "";
  const strip = $("event-strip");
  strip.innerHTML = `<span class="strip-label">constraints</span>`;

  $("tick-label").textContent = hasRun ? `tick ${tick} / ${total}` : "—";
  $("tick-axis").textContent = hasRun ? runA.axis.label : "";
  $("play-btn").textContent = playing ? "Pause" : "Play";
  $("play-btn").classList.toggle("on", playing);
  $("step-back").disabled = !hasRun;
  $("step-fwd").disabled = !hasRun;
  $("play-btn").disabled = !hasRun;

  const advisory = $("dock-advisory");
  const problems = runA ? (runA.constraint_violations || []).concat(runA.dimension_warnings || []) : [];
  if (runA && runA.error) problems.unshift(runA.error);
  advisory.classList.toggle("hidden", !problems.length);
  if (problems.length) {
    advisory.textContent = problems.length === 1 ? problems[0] : `${problems[0]} (+${problems.length - 1} more)`;
    advisory.title = problems.join("\n");
  }

  if (!hasRun) return;

  renderRuler(total);
  renderLanes();
  renderEvents(total);
  $("playhead").style.left = `${(tick / total) * 100}%`;
}

function renderRuler(total) {
  const host = $("ruler");
  // One mark per tick while they are far enough apart to read, thinning
  // to every 5th and every 10th as the run gets long -- a 400-tick run
  // drawn tick by tick is a grey block, not a ruler.
  const stride = total <= 40 ? 1 : total <= 120 ? 5 : 10;
  const majorEvery = total <= 40 ? 5 : total <= 120 ? 20 : 50;
  for (let i = 0; i <= total; i += stride) {
    const major = i % majorEvery === 0;
    const mark = document.createElement("div");
    mark.className = "ruler-tick" + (major ? " major" : "");
    mark.style.left = `${(i / total) * 100}%`;
    mark.style.height = major ? "11px" : "6px";
    host.appendChild(mark);
    if (major) {
      const label = document.createElement("div");
      label.className = "ruler-label";
      label.style.left = `${(i / total) * 100}%`;
      label.textContent = `t${i}`;
      host.appendChild(label);
    }
  }
}

function renderLanes() {
  const host = $("lanes");
  if (!lanesA.length) {
    const note = document.createElement("div");
    note.className = "rail-empty";
    note.style.padding = "14px";
    note.textContent = "This run moved no values — nothing to trace.";
    host.appendChild(note);
    return;
  }
  const paired = pairLanes();
  for (const { a, b } of paired) {
    host.appendChild(buildLane(a, b));
  }
}

/** Match compare-mode lanes by the value they trace, not by position:
 *  two scenarios can move different sets of values, and lining them up
 *  by index would draw scenario B's mass flow against scenario A's
 *  temperature. A lane with no partner draws single-sided. */
function pairLanes() {
  const byKey = new Map(lanesB.map((l) => [`${l.moteId}|${l.key}`, l]));
  return lanesA.map((a) => ({ a, b: byKey.get(`${a.moteId}|${a.key}`) || null }));
}

function buildLane(a, b) {
  const el = document.createElement("div");
  el.className = "lane" + (selectedIds.has(a.moteId) ? " on" : "");
  el.dataset.id = a.moteId;
  el.dataset.key = a.key;
  el.onclick = (event) => {
    setSelection([a.moteId]);
    inspectedNodeId = null;
    Inspector.open(a.moteId, { anchor: el, newTab: event.ctrlKey || event.metaKey });
    render();
  };

  const NS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(NS, "svg");
  svg.setAttribute("viewBox", "0 0 1000 40");
  svg.setAttribute("preserveAspectRatio", "none");
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", "100%");

  // Both series share one scale so the compare band means something --
  // two lines drawn against their own extents always look identical.
  const bounds = laneBounds(a, b);
  if (b) {
    const band = document.createElementNS(NS, "polygon");
    band.setAttribute("points", `${polyline(a, bounds)} ${polyline(b, bounds, true)}`);
    band.setAttribute("fill", "oklch(0.74 0.10 68 / 0.14)");
    svg.appendChild(band);
    const lineB = document.createElementNS(NS, "polyline");
    lineB.setAttribute("points", polyline(b, bounds));
    lineB.setAttribute("fill", "none");
    lineB.setAttribute("stroke", "var(--accent)");
    lineB.setAttribute("stroke-width", "1.3");
    lineB.setAttribute("stroke-dasharray", "4 3");
    lineB.setAttribute("vector-effect", "non-scaling-stroke");
    svg.appendChild(lineB);
  }
  const lineA = document.createElementNS(NS, "polyline");
  lineA.setAttribute("points", polyline(a, bounds));
  lineA.setAttribute("fill", "none");
  lineA.setAttribute("stroke", "var(--node-free)");
  lineA.setAttribute("stroke-width", "1.6");
  lineA.setAttribute("vector-effect", "non-scaling-stroke");
  svg.appendChild(lineA);
  el.appendChild(svg);

  const name = document.createElement("div");
  name.className = "lane-name";
  name.innerHTML = `<span></span><span class="lane-unit"></span>`;
  name.querySelector("span").textContent = a.label;
  name.querySelector(".lane-unit").textContent = a.unit || "";
  el.appendChild(name);

  const value = document.createElement("div");
  value.className = "lane-value";
  value.innerHTML = `<span class="lane-a"></span>` + (b ? `<span class="lane-b"></span>` : "");
  value.querySelector(".lane-a").textContent = fmt(at(a, tick));
  if (b) value.querySelector(".lane-b").textContent = fmt(at(b, tick));
  el.appendChild(value);

  return el;
}

function at(lane, index) {
  const point = lane.points[Math.min(index, lane.points.length - 1)];
  return point === null || point === undefined ? null : point;
}

function laneBounds(a, b) {
  const values = [];
  for (const lane of [a, b]) {
    if (!lane) continue;
    for (const p of lane.points) if (p !== null && p !== undefined) values.push(p);
  }
  if (!values.length) return { lo: 0, hi: 1 };
  let lo = Math.min(...values);
  let hi = Math.max(...values);
  // A flat line still needs a band to sit in, or it divides by zero and
  // lands off the top of the lane.
  if (hi - lo < 1e-9) { lo -= 1; hi += 1; }
  const pad = (hi - lo) * 0.08;
  return { lo: lo - pad, hi: hi + pad };
}

function polyline(lane, bounds, reverse) {
  const n = lane.points.length - 1 || 1;
  const pts = [];
  lane.points.forEach((value, i) => {
    if (value === null || value === undefined) return;
    const x = (i / n) * 1000;
    const y = 38 - ((value - bounds.lo) / (bounds.hi - bounds.lo)) * 36;
    pts.push(`${x.toFixed(1)},${Math.max(2, Math.min(38, y)).toFixed(1)}`);
  });
  return (reverse ? pts.reverse() : pts).join(" ");
}

function renderEvents(total) {
  const strip = $("event-strip");
  for (const event of runA.events || []) {
    const mark = document.createElement("div");
    mark.className = "event-mark" + (Math.abs(event.tick - tick) < 1 ? " near" : "");
    mark.style.left = `${(event.tick / total) * 100}%`;
    mark.title = `tick ${event.tick} — ${event.text}`;
    mark.onmousedown = (e) => { e.stopPropagation(); stopPlay(); setTick(event.tick); };
    strip.appendChild(mark);
  }
}

// ---- scrubbing -------------------------------------------------------

function setTick(next) {
  const total = stepCount();
  tick = Math.max(0, Math.min(total, Math.round(next)));
  updateBoardsForTick();
  renderDock();
  updatePanelForTick();
  renderTopbar();
}

/** Scrubbing moves the tick, never the graph's shape -- rebuilding every
 *  node and edge from scratch (renderBoards' innerHTML = "") on each
 *  step is what made dragging the playhead flicker. This only patches
 *  the numbers a tick change actually affects, leaving the DOM the
 *  structural render already built untouched. */
function updateBoardsForTick() {
  const host = $("boards");
  const boards = [{ el: host.children[0], run: runA }];
  if (mode === "compare") boards.push({ el: host.children[1], run: runB });
  const byId = new Map(board.nodes.map((n) => [n.id, n]));
  for (const { el, run } of boards) {
    if (!el) continue;
    const readout = el.querySelector(".board-readout");
    if (readout) readout.textContent = run ? `${run.steps.length} ${run.axis.label} · tick ${tick}` : "not run";
    const overlay = valuesAt(run, tick);
    const before = valuesAt(run, Math.max(0, tick - 1));
    for (const nodeEl of el.querySelectorAll(".node")) {
      const node = byId.get(nodeEl.dataset.id);
      if (!node || node.kind === "rule") continue;
      for (const row of nodeEl.querySelectorAll(".node-value")) {
        const key = row.dataset.key;
        if (!key) continue;
        const now = nodeValue(node, overlay, key);
        const prev = nodeValue(node, before, key);
        const delta = typeof now === "number" && typeof prev === "number" ? now - prev : 0;
        const numEl = row.querySelector(".node-num");
        const deltaEl = row.querySelector(".node-delta");
        if (numEl) numEl.textContent = fmt(now);
        if (deltaEl) {
          deltaEl.textContent = delta === 0 ? "—" : `${delta > 0 ? "+" : ""}${fmt(delta)}`;
          deltaEl.className = `node-delta ${delta === 0 ? "flat" : delta > 0 ? "up" : "down"}`;
        }
      }
    }
  }
}

function startPlay() {
  if (!stepCount()) return;
  playing = true;
  playTimer = setInterval(() => {
    if (tick >= stepCount()) { stopPlay(); return; }
    setTick(tick + 1);
  }, PLAY_MS);
  renderDock();
}

function stopPlay() {
  clearInterval(playTimer);
  playing = false;
  renderDock();
}

function scrubTo(clientX) {
  const rect = $("track").getBoundingClientRect();
  setTick(((clientX - rect.left) / rect.width) * stepCount());
}

// ---- running ---------------------------------------------------------

async function runSimulation() {
  const btn = $("run-btn");
  btn.disabled = true;
  btn.textContent = "Running…";
  replayRunId = null;
  try {
    if (mode === "compare") {
      // Either side of a compare can be a saved run instead of a live
      // scenario preview -- those are fetched with run_ticks (the same
      // call enterRun uses) rather than folded into compare(), since a
      // logged run has already happened and re-previewing it would be
      // asking the engine a question it was never asked.
      if (compareRunA || compareRunB) {
        const [a, b] = await Promise.all([
          compareRunA ? call(api.run_ticks(compareRunA)) : call(api.preview(steps, solver, null, scenario)),
          compareRunB ? call(api.run_ticks(compareRunB)) : call(api.preview(steps, solver, null, compareWith)),
        ]);
        if (!a || !b) return;
        runA = a;
        runB = b;
      } else {
        const result = await call(api.compare(steps, solver, scenario, compareWith));
        if (!result) return;
        runA = result.a;
        runB = result.b;
      }
      lanesA = (await call(api.lanes(runA.steps))) || [];
      lanesB = (await call(api.lanes(runB.steps))) || [];
    } else {
      const result = await call(api.preview(steps, solver, null, scenario));
      if (!result) return;
      runA = result;
      runB = null;
      lanesA = (await call(api.lanes(runA.steps))) || [];
      lanesB = [];
    }
    tick = Math.min(tick, stepCount());
    if (runA.error && !runA.steps.length) {
      toast(`Simulation failed: ${runA.error}`, true);
    }
    render();
  } finally {
    btn.disabled = false;
    btn.textContent = "Run";
  }
}

// Fast mode's own run loop: no preview/dock, just a one-step commit
// debounced after every edit. `fastStatus` drives the run-state chip
// the same way scenario/replay do in the other three modes.
let fastRunTimer = null;
const FAST_RUN_DEBOUNCE_MS = 400;
let fastStatus = "solved"; // "solving" | "solved" | "error"
let fastError = null; // last commit failure, shown on hover over the chip

function scheduleFastRun() {
  if (mode !== "fast") return;
  clearTimeout(fastRunTimer);
  fastRunTimer = setTimeout(runFastCommit, FAST_RUN_DEBOUNCE_MS);
}

async function runFastCommit() {
  fastStatus = "solving";
  renderTopbar();
  let result = null;
  try {
    result = await api.commit(1, solver, null, "fast");
  } catch (error) {
    fastError = String(error && error.message ? error.message : error);
  }
  if (result && result.error) fastError = result.error;
  fastStatus = result && !result.error ? "solved" : "error";
  if (result) await refresh();
  else renderTopbar();
}

/** Load a logged run into the same dock a preview uses. Nothing is
 *  written, and the run-state chip says `replay` so a scrub through
 *  history is never mistaken for a pending change. */
async function enterRun(runId) {
  const result = await call(api.run_ticks(runId));
  if (!result) return;
  stopPlay();
  runA = { ...result, constraint_violations: [], dimension_warnings: [] };
  runB = null;
  lanesA = (await call(api.lanes(result.steps))) || [];
  lanesB = [];
  replayRunId = runId;
  tick = 0;
  render();
}

async function commitRun() {
  if (!runA || tick === 0) return;
  const btn = $("commit-btn");
  btn.disabled = true;
  const result = await call(api.commit(tick, solver));
  if (result) {
    const last = result.steps[result.steps.length - 1];
    toast(last ? last.summary : `Committed tick ${tick}.`);
    runA = null;
    runB = null;
    lanesA = [];
    lanesB = [];
    tick = 0;
    await refresh();
  } else {
    btn.disabled = false;
  }
}

async function saveRun() {
  if (!runA) return;
  const btn = $("save-btn");
  btn.disabled = true;
  const result = await call(api.save(stepCount(), solver, null, scenario));
  if (!result) { btn.disabled = false; return; }
  toast(result.run_id ? "Run saved for later review." : "Nothing to save.");
  await refresh();
}

// ---- right panel -----------------------------------------------------

function renderPanel() {
  const tabs = $("panel-tabs");
  tabs.innerHTML = "";
  const available = selectedRelId
    ? [["rules", "Rules"], ["relationship", "Relationship"]]
    : [["rules", "Rules"]];
  for (const [id, label] of available) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    btn.className = panelTab === id ? "on" : "";
    btn.onclick = () => { panelTab = id; renderPanel(); };
    tabs.appendChild(btn);
  }
  if (panelTab === "relationship" && selectedRelId) renderRelationshipInspector();
  else renderRulesPanel();
}

/** setTick's counterpart to updateBoardsForTick: the panel's tick-dependent
 *  numbers (binding values, a mote's live properties) without the teardown
 *  renderPanel does -- and, for the inspector, without re-fetching the mote
 *  over the bridge on every scrub step. */
function updatePanelForTick() {
  if (panelTab === "rules") updateRulesBindingsForTick();
}

function updateRulesBindingsForTick() {
  if (!board.rules.length) return;
  const rule = board.rules.find((r) => r.moteId === activeRule) || board.rules[0];
  const rows = $("panel-body").querySelectorAll(".binding");
  if (rows.length !== rule.bindings.length) return;
  const overlay = valuesAt(runA, tick);
  rule.bindings.forEach((binding, i) => {
    const live = overlay[`${binding.moteId}|${binding.key}`];
    const valueEl = rows[i].querySelector(".binding-value");
    if (valueEl) valueEl.textContent = fmt(live === undefined ? binding.value : live);
  });
}

function renderRulesPanel() {
  const host = $("panel-body");
  host.innerHTML = "";
  if (!board.rules.length) {
    host.innerHTML =
      `<div class="panel-empty">No rules yet. A rule is a mote with an <code>equation</code> ` +
      `property and an <code>applies_to</code> arrow at what it governs — both halves, or it ` +
      `isn't one (core/rules.py).</div>`;
    const add = document.createElement("button");
    add.className = "wide-btn";
    add.type = "button";
    add.textContent = "+ New rule mote";
    add.onclick = addRule;
    host.appendChild(add);
    return;
  }

  for (const rule of board.rules) {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "rule-row" + (activeRule === rule.moteId ? " on" : "");
    row.innerHTML =
      `<span class="card-dot" style="background:var(--node-rule)"></span>` +
      `<span class="card-label"></span><span class="rule-scope"></span>`;
    row.querySelector(".card-label").textContent = rule.name;
    row.querySelector(".rule-scope").textContent =
      rule.scope === "each" ? `each · ${rule.anchors.length}` : "once";
    row.onclick = () => { activeRule = rule.moteId; setSelection([rule.moteId]); render(); };
    host.appendChild(row);
  }

  const rule = board.rules.find((r) => r.moteId === activeRule) || board.rules[0];
  activeRule = rule.moteId;

  const heading = document.createElement("div");
  heading.className = "panel-section";
  heading.textContent = "Equation";
  host.appendChild(heading);

  const box = document.createElement("div");
  box.className = "eq-box";
  const head = document.createElement("div");
  head.className = "eq-head";
  head.innerHTML = `<span class="mono-tag">${rule.scope}</span><span class="mono-tag"></span>`;
  head.querySelectorAll(".mono-tag")[1].textContent = `${rule.bindings.length} bound`;
  box.appendChild(head);

  const editor = document.createElement("textarea");
  editor.className = "eq-edit";
  editor.value = rule.equation;
  editor.spellcheck = false;
  if (mode === "build" || mode === "fast") {
    // Committed on blur rather than per keystroke: an equation is only
    // valid in whole, and writing it letter by letter would put a dozen
    // broken rules through the oplog for every edit.
    editor.onblur = async () => {
      if (editor.value === rule.equation) return;
      const written = await call(api.update_mote(rule.moteId, { equation: editor.value }));
      if (!written) return;
      toast(`${rule.name} updated.`);
      // Fired without awaiting: the name check is advisory, not part of
      // what "the equation was written" means, and letting it hold up
      // refresh() here used to delay it long enough to land after
      // whatever the user clicked next, undoing that click's state when
      // it finally resolved.
      checkEquationNames(rule, editor.value);
      scheduleFastRun();
      await refresh();
    };
  } else {
    // Same rule as the inspector's stored properties: editable in Build
    // mode only, and said so rather than left to look editable and
    // silently discard what was typed.
    editor.readOnly = true;
    editor.classList.add("eq-edit-locked");
    editor.title = "Switch to Build mode to edit";
  }
  box.appendChild(editor);
  host.appendChild(box);

  renderNameSuggestions(host, rule);

  const bindHeading = document.createElement("div");
  bindHeading.className = "panel-section";
  bindHeading.textContent = "Bindings";
  host.appendChild(bindHeading);

  if (!rule.bindings.length) {
    const note = document.createElement("div");
    note.className = "panel-empty";
    note.textContent =
      "This equation resolves against nothing the index recognises — check the names against " +
      "the mote it applies to.";
    host.appendChild(note);
  }
  const overlay = valuesAt(runA, tick);
  for (const binding of rule.bindings) {
    const row = document.createElement("div");
    row.className = "binding";
    row.innerHTML =
      `<span class="binding-role ${binding.role}"></span>` +
      `<span class="binding-target"></span><span class="binding-value"></span>`;
    row.querySelector(".binding-role").textContent = binding.role === "determines" ? "solves" : "reads";
    row.querySelector(".binding-target").textContent = `${binding.moteLabel} · ${binding.key}`;
    const live = overlay[`${binding.moteId}|${binding.key}`];
    row.querySelector(".binding-value").textContent =
      fmt(live === undefined ? binding.value : live);
    host.appendChild(row);
  }

  const actions = document.createElement("div");
  actions.className = "panel-actions";
  const rerun = document.createElement("button");
  rerun.className = "wide-btn";
  rerun.type = "button";
  rerun.textContent = "Re-run";
  rerun.onclick = runSimulation;
  const add = document.createElement("button");
  add.className = "wide-btn";
  add.type = "button";
  add.textContent = "+ Rule";
  add.onclick = addRule;
  actions.append(rerun, add);
  host.appendChild(actions);
}

/** Runs after an equation write, independently of the write's own
 *  refresh() -- see the editor's onblur handler. Checked against the
 *  rule's first anchor: the mote it already applies_to, which the
 *  equation edit didn't touch. A brand-new rule with no applies_to yet
 *  has no anchor to check against, so this simply clears any stale
 *  suggestion and waits until one exists. */
async function checkEquationNames(rule, equationText) {
  const anchorId = rule.anchors[0];
  if (!anchorId) { delete equationSuggestions[rule.moteId]; return; }
  const result = await call(api.check_equation_names(anchorId, equationText, scenario));
  equationSuggestions[rule.moteId] = result || [];
  // Only worth a re-render if this rule's equation panel is still the
  // one on screen -- a suggestion for a rule the user has since clicked
  // away from is still there next time they open it, not forced back now.
  if (panelTab === "rules" && activeRule === rule.moteId) render();
}

/** One row per name the last equation check found unreachable but
 *  recognisable elsewhere in the garden -- offering to draw the
 *  relationship that would make it resolve (docs/simulation.md's
 *  "Connected-name references"). Reads equationSuggestions rather than
 *  taking data as an argument, the same as renderRulesPanel reads board:
 *  it's this rule's own pending state, keyed by its mote id so a
 *  refresh() in between doesn't lose it. */
function renderNameSuggestions(host, rule) {
  const pending = equationSuggestions[rule.moteId];
  if (!pending || !pending.length) return;

  for (const item of pending) {
    const row = document.createElement("div");
    row.className = "name-hint";
    const text = document.createElement("span");
    text.className = "name-hint-text";
    text.textContent = `'${item.name}' isn't connected yet — `;
    row.appendChild(text);
    for (const candidate of item.candidates) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "name-hint-btn";
      btn.textContent = item.candidates.length > 1
        ? `connect to ${candidate.label}`
        : "connect";
      btn.onclick = async () => {
        const anchorId = rule.anchors[0];
        const made = await call(api.create_relationship(anchorId, candidate.id, "relates_to"));
        if (!made) return;
        toast(`Connected to ${candidate.label} — ${item.name} now resolves.`);
        clearNameSuggestion(rule.moteId, item);
        await refresh();
      };
      row.appendChild(btn);
    }
    const dismiss = document.createElement("button");
    dismiss.type = "button";
    dismiss.className = "name-hint-btn ghost";
    dismiss.textContent = "dismiss";
    dismiss.onclick = () => { clearNameSuggestion(rule.moteId, item); render(); };
    row.appendChild(dismiss);
    host.appendChild(row);
  }
}

function clearNameSuggestion(ruleMoteId, item) {
  const remaining = (equationSuggestions[ruleMoteId] || []).filter((x) => x !== item);
  if (remaining.length) equationSuggestions[ruleMoteId] = remaining;
  else delete equationSuggestions[ruleMoteId];
}

/** The inspector's other shape, for a clicked edge instead of a clicked
 *  node -- endpoints and a type instead of properties and a run state,
 *  since a relationship carries neither. Reads straight off board.edges
 *  rather than a fetch: the board answer already has everything a
 *  relationship shows here. */
function renderRelationshipInspector() {
  const host = $("panel-body");
  const edge = board.edges.find((e) => e.id === selectedRelId);
  if (!edge) {
    selectedRelId = null;
    host.innerHTML = `<div class="panel-empty">That relationship is gone.</div>`;
    return;
  }
  const from = board.nodes.find((n) => n.id === edge.from);
  const to = board.nodes.find((n) => n.id === edge.to);

  const title = document.createElement("div");
  title.style.cssText = "font-family:var(--serif);font-size:19px;color:var(--fg-bright);margin:6px 0 2px";
  title.textContent = "Relationship";
  host.appendChild(title);

  const heading = document.createElement("div");
  heading.className = "panel-section";
  heading.textContent = "Type";
  host.appendChild(heading);

  const typeRow = document.createElement("div");
  typeRow.className = "rel-row";
  typeRow.innerHTML = `<span class="rel-arrow">→</span><span class="rel-type"></span>`;
  typeRow.querySelector(".rel-type").textContent = edge.type;
  typeRow.querySelector(".rel-type").title = "Click to rename";
  typeRow.style.cursor = "pointer";
  typeRow.onclick = () => renameSelectedRelationship(edge);
  host.appendChild(typeRow);

  const endHeading = document.createElement("div");
  endHeading.className = "panel-section";
  endHeading.textContent = "Endpoints";
  host.appendChild(endHeading);

  for (const [role, node, id] of [["from", from, edge.from], ["to", to, edge.to]]) {
    const row = document.createElement("div");
    row.className = "binding";
    row.innerHTML = `<span class="binding-role"></span><span class="binding-target"></span>`;
    row.querySelector(".binding-role").textContent = role;
    const target = row.querySelector(".binding-target");
    target.textContent = node ? node.label : id;
    target.style.cursor = "pointer";
    target.onclick = (event) => {
      setSelection([id]);
      inspectedNodeId = null;
      Inspector.open(id, { anchor: target, newTab: event.ctrlKey || event.metaKey });
      render();
    };
    host.appendChild(row);
  }

  const actions = document.createElement("div");
  actions.className = "panel-actions";
  const rename = document.createElement("button");
  rename.className = "wide-btn";
  rename.type = "button";
  rename.textContent = "Change type";
  rename.onclick = () => renameSelectedRelationship(edge);
  const del = document.createElement("button");
  del.className = "wide-btn";
  del.type = "button";
  del.textContent = "Delete";
  del.onclick = () => deleteSelectedRelationship(edge);
  actions.append(rename, del);
  host.appendChild(actions);
}

async function renameSelectedRelationship(edge) {
  const next = window.prompt("Relationship type:", edge.type);
  if (!next || next === edge.type) return;
  const written = await call(api.update_relationship(edge.id, null, null, next));
  if (written) { toast(`Relationship renamed to ${next}.`); await refresh(); }
}

async function deleteSelectedRelationship(edge) {
  const ok = await call(api.delete_relationship(edge.id));
  if (!ok) return;
  toast(`${edge.type} relationship deleted.`);
  selectedRelId = null;
  await refresh();
}

// ---- build mode ------------------------------------------------------

async function addMote() {
  const created = await call(api.create_mote({ name: "New mote" }));
  if (!created) return;
  await refresh();
  setSelection([created.id]);
  render();
  inspectedNodeId = null;
  Inspector.open(created.id);
}

async function addRule() {
  const created = await call(api.create_mote({ name: "New rule", equation: "" }));
  if (!created) return;
  toast("Rule mote created — give it an equation, then point it at what it governs with →.");
  await refresh();
  activeRule = created.id;
  setSelection([created.id]);
  panelTab = "rules";
  render();
}

/** Deletes every currently selected mote -- a single one for the
 *  ordinary case, or the whole multi-selection when a box-select or
 *  shift+click has built one up. */
async function deleteSelectedMote() {
  const ids = [...selectedIds];
  if (!ids.length) return;
  const label = ids.length === 1
    ? (board.nodes.find((n) => n.id === ids[0])?.label || "this mote")
    : `${ids.length} motes`;
  if (!window.confirm(`Delete ${label} and all its relationships?`)) return;
  for (const id of ids) {
    const ok = await call(api.delete_mote(id));
    if (!ok) continue;
    delete positions[id];
    if (activeRule === id) activeRule = null;
    delete equationSuggestions[id];
  }
  savePositions();
  toast(`${label} deleted.`);
  setSelection([]);
  await refresh();
}

function armConnect() {
  connectArmed = !connectArmed;
  $("tool-link").classList.toggle("armed", connectArmed);
  if (connectArmed) toast("Pick a source mote on the board, then a target.");
}

async function connectTo(targetId, sourceId = selectedId) {
  connectArmed = false;
  $("tool-link").classList.remove("armed");
  if (!sourceId || sourceId === targetId) return;
  // A rule connects with `applies_to`, which is the half core/rules.py
  // insists on beyond the equation; anything else is whatever the user
  // types, defaulting to `relates_to` for the common case.
  const source = board.nodes.find((n) => n.id === sourceId);
  let type;
  if (source && source.kind === "rule") {
    type = "applies_to";
  } else {
    type = window.prompt("Relationship type:", "relates_to");
    if (!type) return;
  }
  const made = await call(api.create_relationship(sourceId, targetId, type));
  if (made) { toast(`${type} drawn.`); await refresh(); }
}

// ---- ctrl+drag relationship drawing -----------------------------------
//
// A screen-space overlay rather than a board-world element: the dragged
// line is pure visual feedback between wherever the gesture started and
// the cursor, and neither end needs to survive a pan or zoom mid-drag.

function startLinkDrag(sourceId, x, y) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.id = "link-drag-overlay";
  svg.style.cssText = "position:fixed;inset:0;width:100vw;height:100vh;pointer-events:none;z-index:25;";
  const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
  line.setAttribute("stroke", "var(--accent-bright)");
  line.setAttribute("stroke-width", "2");
  line.setAttribute("stroke-dasharray", "6 4");
  svg.appendChild(line);
  document.body.appendChild(svg);
  linkDrag = { sourceId, x1: x, y1: y, line };
  document.body.style.cursor = "crosshair";
  updateLinkDrag(x, y);
}

function updateLinkDrag(x, y) {
  if (!linkDrag) return;
  linkDrag.line.setAttribute("x1", linkDrag.x1);
  linkDrag.line.setAttribute("y1", linkDrag.y1);
  linkDrag.line.setAttribute("x2", x);
  linkDrag.line.setAttribute("y2", y);
}

async function finishLinkDrag(clientX, clientY) {
  const { sourceId } = linkDrag;
  linkDrag = null;
  document.body.style.cursor = "";
  const overlay = $("link-drag-overlay");
  if (overlay) overlay.remove();
  const el = document.elementFromPoint(clientX, clientY);
  const nodeEl = el && el.closest && el.closest(".node");
  const targetId = nodeEl ? nodeEl.dataset.id : null;
  // Released on the node it started from, or on nothing: a link that
  // went nowhere, so nothing happens. This used to open a second
  // inspector tab, on the theory that a ctrl-press that never moved was
  // a ctrl-click -- but guessing which of two gestures ctrl meant, from
  // how far the mouse travelled, is how you get a relationship you did
  // not ask for. Right-click -> Inspect in new tab says it outright.
  if (!targetId || targetId === sourceId) return;
  await connectTo(targetId, sourceId);
}

// ---- pointer gestures -------------------------------------------------

/** Canonical setter for the mote selection -- keeps selectedId (the
 *  single-mote convenience the rest of the app reads: connect, delete,
 *  the inspector leader line) in sync with selectedIds, the Set that
 *  shift+click and box-select actually build up, and always clears the
 *  relationship selection, mutually exclusive with mote selection
 *  everywhere else in the suite. More than one id closes the Inspector
 *  -- it's a single-mote surface shared across all six windows. */
function setSelection(ids) {
  selectedIds = new Set(ids);
  selectedRelId = null;
  if (selectedIds.size === 1) {
    selectedId = [...selectedIds][0];
  } else {
    selectedId = null;
    inspectedNodeId = null;
    Inspector.close();
  }
}

/** The "which values does this card show" submenu, inlined as its own
 *  checklist rather than a real submenu -- ContextMenu only knows flat
 *  lists. Toggling a row reopens the menu at the same point instead of
 *  dismissing it, so picking several properties is one right-click, not
 *  one per property. */
function previewMenuItems(node, reopen) {
  const keys = Object.keys(node.values);
  if (node.kind === "rule" || keys.length < 2) return [];
  const shown = new Set(previewKeysFor(node, movedKeys(runA)));
  return [
    "-",
    { label: "Displayed values", disabled: true },
    ...keys.map((key) => ({
      label: `${shown.has(key) ? "\u2713" : "\u00a0\u00a0"} ${key}${node.units[key] ? ` (${node.units[key]})` : ""}`,
      run: () => { togglePreviewKey(node.id, key); reopen(); },
    })),
  ];
}

function onNodeContext(id) {
  return (event) => {
    event.preventDefault();
    event.stopPropagation();
    const anchorEl = event.currentTarget;
    const openMenu = () => {
      const node = board.nodes.find((n) => n.id === id);
      // Right-clicking a mote that's already part of the current
      // multi-selection acts on the whole group for Delete, same as
      // Monarch's box-select context menu -- but Connect always narrows
      // to just this one mote, since a relationship only ever has one
      // source.
      const groupIds = selectedIds.has(id) && selectedIds.size > 1 ? [...selectedIds] : [id];
      ContextMenu.open(event.clientX, event.clientY, [
        ...ContextMenu.inspectItems(id, anchorEl).map((item) => ({
          ...item,
          run: () => { inspectedNodeId = id; item.run(); },
        })),
        "-",
        node && node.kind === "rule"
          ? { label: "Edit this rule", run: () => { activeRule = id; panelTab = "rules"; render(); } }
          : null,
        { label: "Connect to\u2026", run: () => { setSelection([id]); armConnect(); render(); } },
        ...(node ? previewMenuItems(node, openMenu) : []),
        "-",
        {
          label: groupIds.length > 1 ? `Delete ${groupIds.length} motes` : "Delete mote",
          danger: true,
          run: () => { setSelection(groupIds); return deleteSelectedMote(); },
        },
      ]);
    };
    openMenu();
  };
}

function onNodeDown(id) {
  return (event) => {
    if (event.button === 1) return;         // middle-drag pans, handled below
    if (event.button === 2) return;         // right-click only opens the context menu, handled below
    event.preventDefault();
    event.stopPropagation();
    if (connectArmed && selectedId && id !== selectedId) { connectTo(id); return; }
    if (event.ctrlKey) { startLinkDrag(id, event.clientX, event.clientY); return; }
    // Shift+click toggles this mote into or out of the current
    // multi-selection instead of replacing it -- shift means "add to"
    // everywhere else in the suite (isolate/hide toggles, etc).
    if (event.shiftKey) {
      const next = new Set(selectedIds);
      next.has(id) ? next.delete(id) : next.add(id);
      setSelection([...next]);
      if (selectedId) Inspector.open(selectedId, { anchor: event.currentTarget });
      render();
      return;
    }
    // Dragging a mote that's part of the current multi-selection moves
    // the whole group together; dragging any other mote (including one
    // outside a smaller/different selection) only ever moves that one.
    // Which of those this click actually *selects* is resolved in onUp,
    // once it's known whether this was a click or a drag -- a plain
    // click on a mote inside a group collapses the selection down to
    // just that one, same as everywhere selection already works.
    const groupIds = selectedIds.has(id) && selectedIds.size > 1 ? [...selectedIds] : [id];
    const startPositions = new Map(groupIds.map((gid) => [gid, nodePosition(gid)]));
    drag = { id, sx: event.clientX, sy: event.clientY, moved: false, groupIds, startPositions, anchorEl: event.currentTarget };
  };
}

function onPanDown(event, isB) {
  if (event.button === 0) {
    // Only board A's nodes are ever selectable (see buildNode) -- a
    // click or box-select drag on board B still just deselects, same as
    // empty space on board A.
    if (isB) { deselect(); return; }
    boxState = {
      startX: event.clientX, startY: event.clientY, moved: false,
      shiftBase: event.shiftKey ? [...selectedIds] : null,
    };
    return;
  }
  if (event.button !== 1) return;
  event.preventDefault();
  pan = { sx: event.clientX, sy: event.clientY, ox: layout.panX, oy: layout.panY };
  event.currentTarget.classList.add("panning");
}

/** A node's own mousedown handler stops propagation, so anything that
 *  reaches here is a click on empty board -- off every mote, as
 *  deselecting expects everywhere else in the suite. */
function deselect() {
  if (!selectedIds.size && !selectedRelId) return;
  setSelection([]);
  render();
}

// ---- box-select --------------------------------------------------------
//
// A left-drag starting on empty board (not a node) rubber-bands a
// selection rectangle in screen space, same gesture as Monarch's canvas
// box-select. #select-box lives outside #boards so renderBoards()'s
// innerHTML rebuild (every drag-move of a dragged node, every pan
// settle) never wipes it mid-drag.

function normalizedRect(x1, y1, x2, y2) {
  return { left: Math.min(x1, x2), top: Math.min(y1, y2), right: Math.max(x1, x2), bottom: Math.max(y1, y2) };
}

/** Every board-A node whose screen position falls inside a screen-space
 *  rectangle. Board A's nodes are always the first `.board` in #boards
 *  (see renderBoards/buildBoard) -- board B, when present, only ever
 *  mirrors the same mote ids and isn't itself selectable. */
function hitsInRect(rect) {
  const hits = [];
  for (const nodeEl of document.querySelectorAll("#boards .board:first-child .node")) {
    const r = nodeEl.getBoundingClientRect();
    const cx = r.left + r.width / 2, cy = r.top + r.height / 2;
    if (cx >= rect.left && cx <= rect.right && cy >= rect.top && cy <= rect.bottom) {
      hits.push(nodeEl.dataset.id);
    }
  }
  return hits;
}

function updateSelectBox(rect) {
  const box = $("select-box");
  if (!rect) { box.classList.add("hidden"); return; }
  box.style.left = rect.left + "px";
  box.style.top = rect.top + "px";
  box.style.width = (rect.right - rect.left) + "px";
  box.style.height = (rect.bottom - rect.top) + "px";
  box.classList.remove("hidden");
}

/** A click on an edge's hit-line. Mutually exclusive with node selection
 *  -- the inspector shows one or the other -- and always lands on the
 *  Inspector tab, the same as clicking a node. */
function selectRelationship(relId) {
  setSelection([]);
  selectedRelId = relId;
  connectArmed = false;
  $("tool-link").classList.remove("armed");
  panelTab = "relationship";
  render();
}

/** Repaints every board's pan/zoom without touching a single node or
 *  edge: neither changes any world-space geometry, only which window
 *  onto it is showing, so a pan or zoom tick only ever needs to move
 *  the `.board-world` transform. Rebuilding the DOM on every wheel and
 *  mousemove event (renderBoards() does, via innerHTML = "") was the
 *  actual cause of both the compare-mode flicker and the soft text --
 *  a fresh raster every frame, doubled when a second board is on
 *  screen, never lets the compositor settle. */
let transformSettleTimer = null;

/** `.transforming` (and the will-change it carries -- see style.css)
 *  stays on only for the span of an actual gesture. Left on longer, the
 *  compositor never repaints at the resting scale and text stays soft
 *  after a zoom; dropped too eagerly, mid-gesture frames repaint from
 *  scratch and the flicker this function was written to kill comes
 *  back. A short debounce after the last transform call is the
 *  compromise: cheap GPU-scaled frames while the wheel or drag is still
 *  moving, one real repaint once it stops. */
function applyBoardTransforms() {
  const t = `translate(${layout.panX}px, ${layout.panY}px) scale(${layout.zoom})`;
  for (const world of document.querySelectorAll(".board-world")) {
    world.classList.add("transforming");
    world.style.transform = t;
  }
  clearTimeout(transformSettleTimer);
  transformSettleTimer = setTimeout(() => {
    // Mouse-drag panning gets its guaranteed crisp repaint from onUp's
    // renderBoards() call once the gesture ends. A wheel-driven zoom (or
    // trackpad pan) never fires an "up" event, so this timeout is the
    // only place such a gesture's end is ever detected -- without a
    // renderBoards() here too, wheel zooming was left relying solely on
    // dropping `.transforming`, which doesn't reliably force the
    // compositor to re-rasterize text at the new resting scale.
    for (const world of document.querySelectorAll(".board-world")) world.classList.remove("transforming");
    renderBoards();
  }, 150);
  // The inspected node just moved under this same transform; its own
  // getBoundingClientRect() is already up to date by the time the
  // browser hands back control here, so the leader can follow it on
  // this tick rather than waiting for the settle timeout above.
  const inspectedEl = resolveInspectedNode();
  if (inspectedEl) Inspector.updateAnchor(inspectedEl);
}

function onZoomWheel(event) {
  event.preventDefault();
  const rect = event.currentTarget.getBoundingClientRect();
  const cx = event.clientX - rect.left;
  const cy = event.clientY - rect.top;
  const prev = layout.zoom;
  const next = Math.max(MIN_ZOOM, Math.min(MAX_ZOOM, prev * Math.exp(-event.deltaY * 0.0015)));
  if (next === prev) return;
  const k = next / prev;
  layout.zoom = next;
  layout.panX = cx - (cx - layout.panX) * k;
  layout.panY = cy - (cy - layout.panY) * k;
  saveLayout();
  applyLayout();
  applyBoardTransforms();
}

// Below half the usable minimum, a resize collapses the bar shut rather
// than settling on some odd sliver width -- pulling back out from the
// edge (where the handle still sits) reopens it at the same minimum.
function snapSize(value, min, max) {
  if (value < min / 2) return 0;
  return Math.max(min, Math.min(max, value));
}

function onMove(event) {
  if (linkDrag) { updateLinkDrag(event.clientX, event.clientY); return; }
  if (pan) {
    layout.panX = pan.ox + (event.clientX - pan.sx);
    layout.panY = pan.oy + (event.clientY - pan.sy);
    applyBoardTransforms();
    return;
  }
  if (resize) {
    if (resize.kind === "rail") {
      layout.railW = snapSize(resize.orig + (event.clientX - resize.sx), 180, 420);
    } else if (resize.kind === "panel") {
      layout.panelW = snapSize(resize.orig - (event.clientX - resize.sx), 250, 540);
    } else {
      layout.dockH = snapSize(resize.orig - (event.clientY - resize.sy), 140, window.innerHeight - 260);
    }
    applyLayout();
    return;
  }
  if (scrubbing) { scrubTo(event.clientX); return; }
  if (boxState) {
    const dx = event.clientX - boxState.startX;
    const dy = event.clientY - boxState.startY;
    if (!boxState.moved && Math.hypot(dx, dy) > 3) boxState.moved = true;
    if (boxState.moved) {
      const rect = normalizedRect(boxState.startX, boxState.startY, event.clientX, event.clientY);
      updateSelectBox(rect);
      // Live preview only -- toggles classes on the actual node elements
      // rather than going through setSelection()+render(), which would
      // rebuild the whole board on every mousemove (see applyBoardTransforms
      // for why that's avoided for pan/zoom too). The authoritative call is
      // in onUp.
      const hits = new Set(hitsInRect(rect));
      if (boxState.shiftBase) for (const id of boxState.shiftBase) hits.add(id);
      for (const nodeEl of document.querySelectorAll("#boards .board:first-child .node")) {
        nodeEl.classList.toggle("on", hits.has(nodeEl.dataset.id));
      }
    }
    return;
  }
  if (!drag) return;
  const dx = event.clientX - drag.sx;
  const dy = event.clientY - drag.sy;
  if (!drag.moved && Math.hypot(dx, dy) < 3) return;
  drag.moved = true;
  // Delta from the primary (grabbed) node's own start position is
  // applied to every group member's own start position, rather than
  // snapping each one to the cursor directly -- that would collapse the
  // whole group onto a single point instead of moving it as one.
  const wdx = dx / layout.zoom, wdy = dy / layout.zoom;
  for (const gid of drag.groupIds) {
    const start = drag.startPositions.get(gid);
    positions[gid] = { x: start.x + wdx, y: start.y + wdy };
  }
  renderBoards();
}

function onUp(event) {
  if (linkDrag) finishLinkDrag(event.clientX, event.clientY);
  if (drag) {
    if (drag.moved) {
      savePositions();
    } else {
      // A plain click (no drag) on a mote inside a multi-selection
      // collapses the selection down to just that one -- dragging is
      // what moves the whole group; clicking always picks one.
      setSelection([drag.id]);
      const node = board.nodes.find((n) => n.id === drag.id);
      if (node && node.kind === "rule") { activeRule = drag.id; panelTab = "rules"; }
      inspectedNodeId = drag.id;
      Inspector.open(drag.id, { anchor: drag.anchorEl });
      render();
    }
  }
  if (boxState) {
    const { moved, startX, startY, shiftBase } = boxState;
    updateSelectBox(null);
    if (moved) {
      // The live preview in onMove has almost certainly already put the
      // right ids on screen; this is the authoritative final call in
      // case no mousemove landed exactly on the release point.
      const rect = normalizedRect(startX, startY, event.clientX, event.clientY);
      const hits = hitsInRect(rect);
      setSelection(shiftBase ? [...new Set([...shiftBase, ...hits])] : hits);
      render();
    } else {
      deselect();
    }
    boxState = null;
  }
  if (resize) { saveLayout(); document.querySelectorAll(".active").forEach((el) => el.classList.remove("active")); }
  if (pan) {
    document.querySelectorAll(".panning").forEach((el) => el.classList.remove("panning"));
    // The debounced settle timer in applyBoardTransforms clears
    // `.transforming` ~150ms after the last mousemove, but that alone
    // doesn't always coax the compositor into a fresh, crisp raster --
    // text can stay soft until some unrelated click forces a real
    // render(). Rebuilding the boards' DOM here, once, at the end of
    // the gesture (not on every mousemove -- see applyBoardTransforms)
    // guarantees the same clean repaint a click already gets.
    clearTimeout(transformSettleTimer);
    renderBoards();
  }
  drag = null;
  pan = null;
  resize = null;
  scrubbing = false;
}

// ---- settings --------------------------------------------------------

function openSettings() {
  const host = $("solver-options");
  host.innerHTML = "";
  for (const name of board.solvers) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "solver-option" + (solver === name ? " on" : "");
    btn.textContent = name;
    btn.onclick = () => { solver = name; openSettings(); };
    host.appendChild(btn);
  }
  $("steps-value").textContent = String(steps);
  $("settings-summary").textContent = `${steps} tick${steps === 1 ? "" : "s"} · ${solver}`;
  const sc = board.scenarios.find((s) => s.id === scenario);
  $("settings-scenario").textContent = sc ? sc.name : "garden as it stands";
  $("settings-overlay").classList.remove("hidden");
}

function closeSettings() { $("settings-overlay").classList.add("hidden"); }

// ---- garden menu ------------------------------------------------------

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
  if (!menu.classList.contains("hidden")) { menu.classList.add("hidden"); return; }
  const gardens = (await call(api.list_gardens())) || [];
  menu.innerHTML = "";
  for (const name of gardens) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = name;
    btn.className = name === board.garden ? "on" : "";
    btn.onclick = async () => {
      menu.classList.add("hidden");
      await call(api.switch_garden(name));
      runA = null; runB = null; lanesA = []; lanesB = []; tick = 0; replayRunId = null;
      scenario = null; equationSuggestions = {};
      setSelection([]);
      await refresh();
    };
    menu.appendChild(btn);
  }

  // Every window ends its garden menu with the same two lines: the
  // gardens you can switch to, then the one window where gardens
  // themselves are made, renamed, imported, exported and synced
  // (gardens_app/). Switching is per window; managing is not, which is
  // why this opens a window instead of a modal.
  menu.append(gardenMenuManageItem(() => menu.classList.add("hidden")));
  menu.classList.remove("hidden");
}

// ---- app switcher (⌘J) -------------------------------------------------

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
    id: "monolith", name: "Monolith", kind: "table",
    desc: "The whole garden as one editable table.",
    icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
      <rect x="14" y="7" width="12" height="26" rx="2" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></rect>
      <rect x="17" y="12" width="6" height="3" rx="1" fill="var(--accent)"></rect>
      <rect x="17" y="18" width="6" height="3" rx="1" fill="var(--fg)" opacity="0.4"></rect>
      <rect x="17" y="24" width="6" height="3" rx="1" fill="var(--fg)" opacity="0.4"></rect>
    </svg>`,
  },
];

function launcherChoices() { return LAUNCHER_APPS.filter((a) => a.id !== THIS_APP); }

function launcherFiltered(query) {
  const q = query.trim().toLowerCase();
  const choices = launcherChoices();
  if (!q) return choices;
  return choices.filter((a) => (a.name + " " + a.kind + " " + a.desc).toLowerCase().includes(q));
}

function renderLauncherResults(query) {
  const list = launcherFiltered(query);
  launcherHighlight = Math.min(launcherHighlight, Math.max(list.length - 1, 0));
  const host = $("launcher-results");
  host.innerHTML = "";
  list.forEach((app, i) => {
    const card = document.createElement("button");
    card.type = "button";
    card.className = "launcher-card" + (i === launcherHighlight ? " highlighted" : "");
    card.innerHTML =
      `<div class="launcher-icon">${app.icon}</div>` +
      `<div class="launcher-text">` +
      `<div class="launcher-name-row"><span class="launcher-name">${app.name}</span>` +
      `<span class="launcher-kind">${app.kind}</span></div>` +
      `<div class="launcher-desc">${app.desc}</div></div>` +
      `<span class="launcher-key">⌘${i + 1}</span>`;
    card.onclick = () => launchApp(app.id);
    host.appendChild(card);
  });
  $("launcher-empty").classList.toggle("hidden", list.length > 0);
  $("launcher-empty").textContent = list.length ? "" : "No app matches that.";
}

function launcherIsOpen() { return !$("launcher-overlay").classList.contains("hidden"); }

function openLauncher() {
  launcherHighlight = 0;
  $("launcher-garden").textContent = board ? board.garden : "";
  $("launcher-overlay").classList.remove("hidden");
  $("launcher-input").value = "";
  renderLauncherResults("");
  $("launcher-input").focus();
}

function closeLauncher() { $("launcher-overlay").classList.add("hidden"); }

async function launchApp(appId) {
  closeLauncher();
  await call(api.open_app(appId, selectedId));
}

// ---- wiring ------------------------------------------------------------

function wire() {
  $("garden-btn").onclick = openGardenMenu;
  document.addEventListener("mousedown", (event) => {
    const menu = $("garden-menu");
    if (!menu.classList.contains("hidden") && !menu.contains(event.target) && event.target !== $("garden-btn")) {
      menu.classList.add("hidden");
    }
  });

  $("run-btn").onclick = runSimulation;
  $("dock-run").onclick = runSimulation;
  $("commit-btn").onclick = commitRun;
  $("save-btn").onclick = saveRun;
  $("solver-btn").onclick = openSettings;
  $("settings-close").onclick = closeSettings;
  $("settings-cancel").onclick = closeSettings;
  $("settings-apply").onclick = () => { closeSettings(); runSimulation(); };
  $("settings-overlay").onmousedown = (e) => { if (e.target === $("settings-overlay")) closeSettings(); };
  $("steps-dec").onclick = () => { steps = Math.max(1, steps - 5); openSettings(); };
  $("steps-inc").onclick = () => { steps = Math.min(500, steps + 5); openSettings(); };

  $("refresh-btn").onclick = async () => {
    const icon = $("refresh-btn");
    icon.classList.add("spinning");
    await call(api.refresh());
    await refresh();
    icon.classList.remove("spinning");
  };
  $("switch-app-btn").onclick = openLauncher;

  $("play-btn").onclick = () => (playing ? stopPlay() : startPlay());
  $("step-back").onclick = () => { stopPlay(); setTick(tick - 1); };
  $("step-fwd").onclick = () => { stopPlay(); setTick(tick + 1); };
  $("track").onmousedown = (event) => {
    if (!stepCount()) return;
    stopPlay();
    scrubbing = true;
    scrubTo(event.clientX);
  };

  $("tool-mote").onclick = addMote;
  $("tool-rule").onclick = addRule;
  $("tool-link").onclick = armConnect;
  $("tool-layout").onclick = () => {
    positions = layoutNodes(board.nodes);
    layout.zoom = 1;
    layout.panX = 0;
    layout.panY = 0;
    savePositions();
    saveLayout();
    applyLayout();
    renderBoards();
    toast("Layout reset, view recentred.");
  };
  $("zoom-label").onclick = () => {
    layout.zoom = 1;
    layout.panX = 0;
    layout.panY = 0;
    saveLayout();
    applyLayout();
    renderBoards();
  };

  $("check-roles-btn").onclick = async () => {
    const report = await call(api.check_roles());
    if (!report) return;
    const box = $("roles-report");
    box.classList.remove("hidden");
    const lines = [...report.problems, ...report.once_scoped];
    box.classList.toggle("bad", report.problems.length > 0);
    box.textContent = report.motes_checked === 0 && !lines.length
      ? "No motes declare roles."
      : lines.length
        ? lines.join("\n")
        : `Roles OK — ${report.motes_checked} assembly(s) wired correctly.`;
  };

  for (const [id, kind] of [["rail-resize", "rail"], ["panel-resize", "panel"], ["dock-resize", "dock"]]) {
    $(id).onmousedown = (event) => {
      event.preventDefault();
      $(id).classList.add("active");
      resize = {
        kind, sx: event.clientX, sy: event.clientY,
        orig: kind === "rail" ? layout.railW : kind === "panel" ? layout.panelW : layout.dockH,
      };
    };
  }

  window.addEventListener("mousemove", onMove);
  window.addEventListener("mouseup", onUp);

  $("launcher-overlay").addEventListener("mousedown", (event) => {
    if (event.target === $("launcher-overlay")) closeLauncher();
  });
  $("launcher-input").addEventListener("input", (event) => renderLauncherResults(event.target.value));
  $("launcher-input").addEventListener("keydown", (event) => {
    const list = launcherFiltered($("launcher-input").value);
    if (event.key === "ArrowDown" && launcherHighlight + LAUNCHER_COLUMNS < list.length) {
      launcherHighlight += LAUNCHER_COLUMNS;
    } else if (event.key === "ArrowUp" && launcherHighlight - LAUNCHER_COLUMNS >= 0) {
      launcherHighlight -= LAUNCHER_COLUMNS;
    } else if (event.key === "ArrowRight" && launcherHighlight + 1 < list.length) {
      launcherHighlight++;
    } else if (event.key === "ArrowLeft" && launcherHighlight > 0) {
      launcherHighlight--;
    } else if (event.key === "Enter" && list[launcherHighlight]) {
      launchApp(list[launcherHighlight].id);
      return;
    } else {
      return;
    }
    event.preventDefault();
    renderLauncherResults($("launcher-input").value);
  });

  document.addEventListener("keydown", (event) => {
    const typing = /^(INPUT|TEXTAREA)$/.test(document.activeElement.tagName);
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "j") {
      event.preventDefault();
      launcherIsOpen() ? closeLauncher() : openLauncher();
      return;
    }
    if ((event.metaKey || event.ctrlKey) && /^[1-5]$/.test(event.key)) {
      const app = launcherChoices()[Number(event.key) - 1];
      if (app) { event.preventDefault(); launchApp(app.id); }
      return;
    }
    if (event.key === "Escape") {
      if (launcherIsOpen()) { closeLauncher(); return; }
      if (!$("settings-overlay").classList.contains("hidden")) { closeSettings(); return; }
      if (connectArmed) { armConnect(); return; }
      if (selectedIds.size || selectedRelId) { deselect(); return; }
    }
    if (typing) return;
    if ((event.key === "Delete" || event.key === "Backspace") && selectedRelId) {
      event.preventDefault();
      const edge = board.edges.find((e) => e.id === selectedRelId);
      if (edge) deleteSelectedRelationship(edge);
      return;
    }
    if ((event.key === "Delete" || event.key === "Backspace") && selectedIds.size) {
      event.preventDefault();
      deleteSelectedMote();
      return;
    }
    if (event.key === " ") { event.preventDefault(); playing ? stopPlay() : startPlay(); }
    else if (event.key === "ArrowLeft") { stopPlay(); setTick(tick - 1); }
    else if (event.key === "ArrowRight") { stopPlay(); setTick(tick + 1); }
    else if (event.key === "Home") { stopPlay(); setTick(0); }
    else if (event.key === "End") { stopPlay(); setTick(stepCount()); }
    else if (event.key.toLowerCase() === "r" && !event.metaKey && !event.ctrlKey) runSimulation();
    else if (event.key.toLowerCase() === "s" && (event.metaKey || event.ctrlKey)) {
      event.preventDefault();
      saveRun();
    }
  });
}

// ---- boot ---------------------------------------------------------------

async function refresh() {
  const next = await call(api.board(scenario));
  if (!next) return;
  const firstLoad = board === null;
  const gardenChanged = !firstLoad && board.garden !== next.garden;
  board = next;
  // Positions are keyed by garden, so a switch reloads them rather than
  // dragging the old garden's layout onto motes that do not exist here.
  if (firstLoad || gardenChanged) { loadPositions(); loadPreviewKeys(); }
  if (!solver || !board.solvers.includes(solver)) solver = board.solvers[0] || "linear";
  if (!activeRule && board.rules.length) activeRule = board.rules[0].moteId;
  if (firstLoad && board.focus) setSelection([board.focus]);
  render();
  // The scenario picker moves independently of the inspector, and the
  // bridge redirects property writes onto the selected scenario's
  // override -- so it has to be told which hypothetical is on screen.
  await call(api.set_inspector_scenario(scenario || null));
  if (firstLoad && board.focus) { inspectedNodeId = null; Inspector.open(board.focus); }
  else await Inspector.reload();
}

async function init() {
  api = window.pywebview.api;
  Inspector.install({ api, onChanged: () => { refresh(); scheduleFastRun(); } });
  CommandBar.install({
    api,
    onChanged: () => { refresh(); scheduleFastRun(); },
    onNavigate: (moteId) => { inspectedNodeId = null; Inspector.open(moteId); },
  });
  loadLayout();
  applyLayout();
  wire();
  await refresh();
}

window.addEventListener("pywebviewready", init);
