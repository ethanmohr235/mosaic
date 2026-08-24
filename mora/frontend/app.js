// mora/frontend/app.js
//
// Wiring between mora/api.py and the three panes. Everything about what
// a note *means* -- what a macro resolves to, what is wrong with it --
// comes back from Python already decided (core/modoc.py parses,
// core/modoc_bind.py resolves, mora/payload.py shapes). This file
// renders that answer and takes edits.
//
// The one exception is the source pane's syntax highlighting, which runs
// here: colour is presentation rather than meaning, and doing it locally
// is what keeps typing feel immediate.

let api;
let noteId = null;
let noteName = "";
let savedSource = "";
let payload = null;          // the last render() answer
let notes = [];              // every note mote in the garden
let mode = "split";
let dataOpen = true;
let step = 0;
let focusId = null;          // panel target while browsing away from the anchor
let renderTimer = null;
let toastTimer = null;
let picker = null;           // {start, query, rows, index} while the @ picker is open
let renderSeq = 0;           // guards against two renders landing out of order

const RENDER_DEBOUNCE_MS = 180;
const MAX_STEP = 60;
const UNKNOWN_COLOR = "oklch(0.62 0 0)";

const $ = (id) => document.getElementById(id);

// ---- colour -------------------------------------------------------
//
// Byte-for-byte the hash ui/frontend/graph.js uses, so a mote is the
// same colour in Mora as it is on the canvas. Two windows disagreeing
// about what colour "Task" is would be worse than either of them being
// arbitrary.

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
  if ((entry.roles || []).includes("species")) return `oklch(0.80 0.13 ${hueFor(entry.id || entry.moteId || "")})`;
  if (!entry.speciesId) return UNKNOWN_COLOR;
  return `oklch(0.72 0.11 ${hueFor(entry.speciesId)})`;
}

/** What a mote *is*: the roles core says it plays, else its species,
 *  else plain "mote". The same order ui/frontend/app.js::kindLabel uses,
 *  so the two windows describe a mote the same way. */
function kindOf(entry) {
  if (!entry) return "mote";
  const roles = entry.roles || [];
  if (roles.length) return roles.map((r) => r.replace(/_/g, " ")).join(" · ");
  return entry.speciesName || "mote";
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

function toast(message, isError) {
  const node = $("toast");
  node.textContent = message;
  node.classList.toggle("error", !!isError);
  node.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.classList.add("hidden"), 4200);
}

// ---- source pane ---------------------------------------------------

const HIGHLIGHT_RE = /(%[^\n]*)|(\\[a-zA-Z]+)|([{}[\]])|(\$[^$\n]*\$)/g;

function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** A <pre> behind the transparent textarea, scrolled in lockstep with
 *  it. The trailing newline matters: without it the last line's
 *  highlight is a pixel short of the caret. */
function paintHighlight() {
  const html = escapeHtml($("editor").value).replace(
    HIGHLIGHT_RE,
    (match, comment, command, brace, math) => {
      if (comment) return `<span class="hl-comment">${comment}</span>`;
      if (command) return `<span class="hl-cmd">${command}</span>`;
      if (brace) return `<span class="hl-brace">${brace}</span>`;
      return `<span class="hl-math">${math}</span>`;
    },
  );
  $("highlight").innerHTML = `${html}\n`;
}

function onEditorInput() {
  paintHighlight();
  markDirty();
  updatePicker();
  scheduleRender();
}

function markDirty() {
  const dirty = $("editor").value !== savedSource;
  const pill = $("dirty");
  pill.textContent = dirty ? "edited" : "synced";
  pill.classList.toggle("edited", dirty);
}

function scheduleRender() {
  clearTimeout(renderTimer);
  renderTimer = setTimeout(renderNow, RENDER_DEBOUNCE_MS);
}

async function renderNow() {
  if (!noteId) return;
  // The stepper and the debounce can both fire a render, and a heavier
  // one (a simulation preview eight steps out) can land after a lighter
  // one it started before. Only the newest answer is allowed to draw.
  const seq = ++renderSeq;
  let answer;
  try {
    answer = await api.render(noteId, $("editor").value, step);
  } catch (e) {
    toast(String(e), true);
    return;
  }
  if (seq !== renderSeq) return;
  payload = answer;
  drawRendered();
  drawStatus();
  if (focusId === null) drawPanel(payload.anchor.id);
  else drawNoteSections();
}

// ---- rendered pane -------------------------------------------------

function drawRendered() {
  const root = clear($("rendered"));
  if (!payload.blocks.length) {
    root.append(el("p", "empty-note", "Nothing yet. Start with \\section{…}."));
    return;
  }
  payload.blocks.forEach((block) => root.append(buildBlock(block)));
}

function buildBlock(block) {
  if (block.type === "heading") {
    return inlineInto(el(block.level === 1 ? "h1" : "h2"), block.content);
  }
  if (block.type === "paragraph") {
    return inlineInto(el("p"), block.content);
  }
  if (block.type === "list") {
    const list = el("ul");
    block.items.forEach((item) => list.append(inlineInto(el("li"), item)));
    return list;
  }
  if (block.type === "equation") {
    const node = el("div", "math display-math");
    mathInto(node, block.spans);
    return node;
  }
  return buildFigure(block);
}

function inlineInto(parent, nodes) {
  nodes.forEach((node) => parent.append(buildInline(node)));
  return parent;
}

function buildInline(node) {
  if (node.type === "text") return document.createTextNode(node.text);
  if (node.type === "styled") {
    const tag = { bold: "strong", emph: "em", code: "code" }[node.style];
    return el(tag, null, node.text);
  }
  if (node.type === "math") return mathInto(el("span", "math"), node.spans);
  if (node.type === "link") return buildLink(node);
  return buildValue(node);
}

function mathInto(parent, spans) {
  spans.forEach((span) => {
    if (span.kind === "text") parent.append(document.createTextNode(span.text));
    else parent.append(el(span.kind, null, span.text));
  });
  return parent;
}

function buildLink(node) {
  const label = node.label || node.name;
  const button = el("button", "mote-link", label);
  button.type = "button";
  if (node.moteId) {
    button.style.color = colorOf(node);
    button.title = `${kindOf(node)} · ${node.moteId}`;
    button.onclick = () => selectMote(node.moteId);
  } else {
    button.classList.add("broken");
    button.title = node.error || "unresolved";
    button.onclick = () => toast(node.error || "unresolved reference", true);
  }
  return button;
}

function buildValue(node) {
  const chip = el("span", "val", node.text);
  const where = node.ref ? `\\val[${node.ref}]{${node.expression}}` : `\\val{${node.expression}}`;
  chip.title = node.error ? `${where} — ${node.error}` : where;
  if (node.error) chip.classList.add("broken");
  return chip;
}

function buildFigure(block) {
  const figure = el("div", "figure");
  const head = el("div", "figure-head");
  head.append(el("span", null, figureTitle(block)));
  head.append(el("span", "right", figureNote(block)));
  figure.append(head);

  if (block.error && !block.values.length) {
    figure.append(el("div", "figure-error", block.error));
  } else if (block.kind === "over_t") {
    figure.append(buildLineChart(block));
    const axis = el("div", "figure-axis");
    axis.append(el("span", null, block.labels[0] || "t+0"));
    axis.append(el("span", null, block.labels[block.labels.length - 1] || ""));
    figure.append(axis);
  } else {
    block.labels.forEach((label, index) => figure.append(buildBar(block, label, index)));
  }
  return figure;
}

function figureTitle(block) {
  if (block.kind === "by") return `${block.expression} by ${block.key}`;
  if (block.kind === "over_t") return `${block.expression} over t`;
  return "graph";
}

function figureNote(block) {
  if (block.error && !block.values.length) return "unresolved";
  if (block.kind === "over_t") {
    const last = block.values[block.values.length - 1];
    return `${format(last)} at ${block.labels[block.labels.length - 1]}`;
  }
  const total = block.values.reduce((a, b) => a + b, 0);
  return `${format(total)} total`;
}

function format(value) {
  if (value === undefined || value === null) return "–";
  return Number.isInteger(value) ? String(value) : value.toFixed(2);
}

function buildBar(block, label, index) {
  const peak = Math.max(1, ...block.values);
  const row = el("div", "bar-row");
  row.append(el("span", "bar-label", label));
  const track = el("div", "bar-track");
  const fill = el("div", "bar-fill");
  fill.style.width = `${(block.values[index] / peak) * 100}%`;
  fill.style.background = `oklch(0.72 0.12 ${hueFor(label)})`;
  track.append(fill);
  row.append(track);
  row.append(el("span", "bar-value", format(block.values[index])));
  return row;
}

/** An SVG path scaled to the series' own range, so a flat projection
 *  reads as flat rather than as noise magnified to fill the box. */
function buildLineChart(block) {
  const W = 100;
  const H = 40;
  const low = Math.min(...block.values);
  const high = Math.max(...block.values);
  const span = high - low || 1;
  const points = block.values.map((value, index) => {
    const x = (index / Math.max(1, block.values.length - 1)) * W;
    const y = H - ((value - low) / span) * (H - 4) - 2;
    return `${index === 0 ? "M" : "L"}${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(" ");

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.append(svgPath(`${points} L${W},${H} L0,${H} Z`, { fill: "oklch(0.74 0.1 68 / 0.1)" }));
  svg.append(svgPath(points, { stroke: "oklch(0.74 0.1 68)" }));
  return svg;
}

function svgPath(d, { fill = "none", stroke = "none" }) {
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", d);
  path.setAttribute("fill", fill);
  path.setAttribute("stroke", stroke);
  path.setAttribute("stroke-width", "1");
  path.setAttribute("vector-effect", "non-scaling-stroke");
  return path;
}

// ---- data panel ----------------------------------------------------

async function drawPanel(moteId) {
  if (!moteId) return;
  const mote = await api.focus(moteId);
  if (!mote) return;
  $("panel-dot").style.background = colorOf(mote);
  $("panel-kind-text").style.color = colorOf(mote);
  $("panel-kind-text").textContent = kindOf(mote);
  $("panel-name").textContent = mote.name;
  $("panel-id").textContent = mote.id;
  $("panel-back").classList.toggle("hidden", focusId === null);

  drawProperties(mote.properties);
  drawRelationships(mote.relationships);
  drawNoteSections();
}

function drawProperties(properties) {
  const root = clear($("panel-props"));
  const keys = Object.keys(properties).filter((key) => key !== "modoc");
  if (!keys.length) {
    root.append(el("div", "panel-empty", "No properties."));
    return;
  }
  keys.forEach((key) => {
    const row = el("div", "prop-row");
    row.append(el("span", "prop-key", key.replace(/_/g, " ")));
    const value = properties[key];
    row.append(el("span", "prop-value", typeof value === "string" ? value : JSON.stringify(value)));
    root.append(row);
  });
}

function drawRelationships(relationships) {
  const root = clear($("panel-rels"));
  if (!relationships.length) {
    root.append(el("div", "panel-empty", "Nothing connected."));
    return;
  }
  relationships.forEach((relationship) => {
    const row = el("button", "entry-row");
    row.type = "button";
    row.append(el("span", "arrow", relationship.outgoing ? "→" : "←"));
    const dot = el("span", "dot");
    dot.style.background = colorOf(relationship.other);
    row.append(dot);
    row.append(el("span", "label", relationship.other.name));
    row.append(el("span", "sub", relationship.type));
    row.onclick = () => selectMote(relationship.other.id);
    root.append(row);
  });
}

/** The two parts of the panel that depend on the note rather than on the
 *  mote in focus, so they refresh on every render whichever mote the
 *  panel is showing. */
function drawNoteSections() {
  drawLinked();
  drawProblems();
}

function drawLinked() {
  const root = clear($("panel-linked"));
  const linked = (payload && payload.linked) || [];
  if (!linked.length) {
    root.append(el("div", "panel-empty", "Nothing linked from the text."));
    return;
  }
  linked.forEach((entry) => {
    const row = el("button", "entry-row");
    row.type = "button";
    if (!entry.moteId) row.classList.add("broken");
    const dot = el("span", "dot");
    dot.style.background = colorOf({ ...entry, id: entry.moteId });
    row.append(dot);
    row.append(el("span", "label", entry.name));
    row.append(el("span", "sub", `×${entry.count}`));
    row.onclick = () => (entry.moteId ? selectMote(entry.moteId) : toast(entry.error, true));
    root.append(row);
  });
}

function drawProblems() {
  const problems = (payload && payload.diagnostics) || [];
  $("panel-problems-section").classList.toggle("hidden", !problems.length);
  const root = clear($("panel-problems"));
  problems.forEach((problem) => {
    const row = el("div", "problem");
    row.append(el("div", "where", problem.line ? `line ${problem.line} · ${problem.kind}` : problem.kind));
    row.append(el("div", "what", problem.message));
    root.append(row);
  });
}

function selectMote(moteId) {
  focusId = moteId === payload.anchor.id ? null : moteId;
  dataOpen = true;
  applyLayout();
  drawPanel(moteId);
}

// ---- status bar ----------------------------------------------------

function drawStatus() {
  const stats = payload.stats;
  $("stat-words").textContent = `${stats.words} words`;
  $("stat-links").textContent = `${stats.links} mote links`;
  $("stat-values").textContent = `${stats.values} values`;
  $("line-count").textContent = `${stats.lines} lines`;
  $("binding-count").textContent = `${stats.values} live bindings`;

  const problems = payload.diagnostics.length;
  const button = $("problems-btn");
  button.textContent = `${problems} problem${problems === 1 ? "" : "s"}`;
  button.classList.toggle("has-problems", problems > 0);

  $("tick-value").textContent = `t+${step}`;
  $("tick-label").textContent = payload.projected ? "projected" : "real";
  $("tick-label").classList.toggle("projected", payload.projected);
  $("apply").textContent = `Apply t+${step}…`;
  $("apply").disabled = step === 0;
}

function setStep(next) {
  step = Math.max(0, Math.min(MAX_STEP, next));
  renderNow();
}

async function applyStep() {
  const message =
    `Apply t+${step} to the graph?\n\n` +
    "This writes the projected values onto the motes and logs a simulation run. " +
    "It does not save the note.";
  if (!window.confirm(message)) return;
  try {
    const result = await api.apply_step(step);
    if (result.error) {
      toast(`Stopped at step ${result.failed_at}: ${result.error}`, true);
    } else {
      toast(`Applied t+${step} as run #${result.run_number}.`);
      step = 0;
    }
  } catch (e) {
    toast(String(e), true);
  }
  renderNow();
}

// ---- notes and saving ----------------------------------------------

async function loadNote(id) {
  const opened = await api.open_note(id ?? null);
  noteId = opened.noteId;
  noteName = opened.name;
  savedSource = opened.source;
  $("editor").value = opened.source;
  $("note-title").textContent = noteName || "no motes yet";
  focusId = null;
  step = 0;
  paintHighlight();
  markDirty();
  await renderNow();
}

async function save() {
  if (!noteId) return;
  const source = $("editor").value;
  try {
    await api.save_note(noteId, source);
  } catch (e) {
    toast(String(e), true);
    return;
  }
  savedSource = source;
  markDirty();
  toast("Note saved.");
  notes = await api.list_notes();
}

async function openNoteMenu() {
  const menu = $("note-menu");
  if (!menu.classList.contains("hidden")) return closeNoteMenu();
  notes = await api.list_notes();
  clear(menu);
  if (!notes.length) {
    menu.append(el("div", "menu-empty", "No notes in this garden yet."));
  }
  notes.forEach((note) => {
    const row = el("button", "menu-item");
    row.type = "button";
    const dot = el("span", "dot");
    dot.style.cssText = `width:6px;height:6px;border-radius:50%;background:${colorOf(note)}`;
    row.append(dot, el("span", null, note.name));
    row.onclick = () => { closeNoteMenu(); loadNote(note.id); };
    menu.append(row);
  });
  menu.classList.remove("hidden");
  $("note-btn").setAttribute("aria-expanded", "true");
}

function closeNoteMenu() {
  $("note-menu").classList.add("hidden");
  $("note-btn").setAttribute("aria-expanded", "false");
}

// ---- the @ picker --------------------------------------------------
//
// Typing `@` opens a mote search and inserts `\mote{<id>}{<name>}`. Ids
// rather than names on purpose: a picked link survives a rename, which
// is the whole reason core/modoc_bind.py accepts both forms.

function updatePicker() {
  const editor = $("editor");
  const upto = editor.value.slice(0, editor.selectionStart);
  const match = /@([^@\s{}]*)$/.exec(upto);
  if (!match) return closePicker();
  showPicker(editor.selectionStart - match[0].length, match[1]);
}

async function showPicker(start, query) {
  const rows = await api.search(query, 8);
  // The user kept typing while that was in flight: this answer is about
  // a prefix they have already moved past, so it is not an answer.
  if (currentPickerQuery() !== query) return;
  if (!rows.length) return closePicker();
  picker = { start, query, rows, index: 0 };
  drawPicker();
}

function currentPickerQuery() {
  const editor = $("editor");
  const match = /@([^@\s{}]*)$/.exec(editor.value.slice(0, editor.selectionStart));
  return match ? match[1] : null;
}

function drawPicker() {
  const node = clear($("picker"));
  picker.rows.forEach((row, index) => {
    const button = el("button", `picker-row${index === picker.index ? " on" : ""}`);
    button.type = "button";
    const dot = el("span", "dot");
    dot.style.cssText = `width:6px;height:6px;border-radius:50%;background:${colorOf(row)}`;
    button.append(dot, el("span", null, row.name), el("span", "sub", kindOf(row)));
    button.onmousedown = (event) => { event.preventDefault(); choosePicker(index); };
    node.append(button);
  });
  node.style.left = "18px";
  node.style.top = `${Math.min(360, 24 + caretLine() * 22)}px`;
  node.classList.remove("hidden");
}

function caretLine() {
  const editor = $("editor");
  return editor.value.slice(0, editor.selectionStart).split("\n").length;
}

function choosePicker(index) {
  const row = picker.rows[index];
  const editor = $("editor");
  const before = editor.value.slice(0, picker.start);
  const after = editor.value.slice(editor.selectionStart);
  const insertion = `\\mote{${row.id}}{${row.name}}`;
  editor.value = before + insertion + after;
  editor.selectionStart = editor.selectionEnd = before.length + insertion.length;
  closePicker();
  editor.focus();
  onEditorInput();
}

function closePicker() {
  picker = null;
  $("picker").classList.add("hidden");
}

function onPickerKey(event) {
  if (!picker) return false;
  if (event.key === "Escape") { closePicker(); return true; }
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    const delta = event.key === "ArrowDown" ? 1 : -1;
    picker.index = (picker.index + delta + picker.rows.length) % picker.rows.length;
    drawPicker();
    return true;
  }
  if (event.key === "Enter" || event.key === "Tab") { choosePicker(picker.index); return true; }
  return false;
}

// ---- layout --------------------------------------------------------

function applyLayout() {
  $("source-pane").classList.toggle("hidden", mode === "preview");
  $("preview-pane").classList.toggle("hidden", mode === "source");
  $("data-pane").classList.toggle("hidden", !dataOpen);
  $("data-toggle").classList.toggle("on", dataOpen);
  document.querySelectorAll("#modes button").forEach((button) => {
    button.classList.toggle("on", button.dataset.mode === mode);
  });
}

function setMode(next) {
  mode = next;
  applyLayout();
}

// ---- wiring --------------------------------------------------------

function wire() {
  const editor = $("editor");
  editor.addEventListener("input", onEditorInput);
  editor.addEventListener("scroll", () => { $("highlight").scrollTop = editor.scrollTop; });
  editor.addEventListener("blur", closePicker);
  editor.addEventListener("keydown", (event) => {
    if (onPickerKey(event)) { event.preventDefault(); return; }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      save();
    }
  });
  editor.addEventListener("click", updatePicker);

  $("save").onclick = save;
  $("data-toggle").onclick = () => { dataOpen = !dataOpen; applyLayout(); };
  document.querySelectorAll("#modes button").forEach((button) => {
    button.onclick = () => setMode(button.dataset.mode);
  });

  $("note-btn").onclick = openNoteMenu;
  $("panel-back").onclick = () => { focusId = null; drawPanel(payload.anchor.id); };
  $("problems-btn").onclick = () => {
    dataOpen = true;
    applyLayout();
    $("panel-problems-section").scrollIntoView({ behavior: "smooth" });
  };

  $("tick-up").onclick = () => setStep(step + 1);
  $("tick-down").onclick = () => setStep(step - 1);
  $("apply").onclick = applyStep;

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".topbar-menu")) closeNoteMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { closeNoteMenu(); closePicker(); }
  });
}

async function init() {
  api = window.pywebview.api;
  wire();
  applyLayout();
  $("brand-sub").textContent = await api.current_garden();
  await loadNote(null);
}

window.addEventListener("pywebviewready", init);
