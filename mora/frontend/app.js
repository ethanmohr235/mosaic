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
let autosaveTimer = null;
let savePromise = null;      // the save_note call in flight, if any

const RENDER_DEBOUNCE_MS = 180;
// Longer than the render debounce on purpose: a render is cheap and
// local-feeling, a write touches the garden file that Monarch may have
// open in another process, so it waits for a real pause in typing.
const AUTOSAVE_DEBOUNCE_MS = 900;
const MAX_STEP = 60;
const UNKNOWN_COLOR = "oklch(0.62 0 0)";

const $ = (id) => document.getElementById(id);

// ---- colour -------------------------------------------------------
//
// Byte-for-byte the hash monarch/frontend/graph.js uses, so a mote is the
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
  if ((entry.kinds || []).includes("species")) return paletteColor(slotOf(entry.id || entry.moteId || ""), true);
  if (!entry.speciesId) return UNKNOWN_COLOR;
  return paletteColor(slotOf(entry.speciesId), false);
}

/** What a mote *is*: the kinds core says it is, else its species, else
 *  plain "mote". The same order monarch/frontend/app.js::kindLabel uses, so
 *  the two windows describe a mote the same way. */
function kindOf(entry) {
  if (!entry) return "mote";
  const kinds = entry.kinds || [];
  if (kinds.length) return kinds.map((k) => k.replace(/_/g, " ")).join(" · ");
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

// The comment alternative's lookbehind mirrors core/modoc.py's
// `_COMMENT_RE = re.compile(r"(?<!\\)%")` byte for byte: without it, a
// literal `\%` (the escape that keeps a percent sign out of comment
// territory) still read as a comment starting mid-line here even though
// the parser it's highlighting for never treated it as one.
const HIGHLIGHT_RE = /((?<!\\)%[^\n]*)|(\\[a-zA-Z]+)|([{}[\]])|(\$[^$\n]*\$)/g;
const MATH_TOKEN_RE = /(\\[a-zA-Z]+)|([{}])/g;

// core/modoc.py's INLINE_COMMANDS and BLOCK_COMMANDS -- the only
// backslash-words that mean something to the parser outside of a `$…$`
// span. Anything else is either a typo or a command that only exists
// inside math, so it earns hl-cmd-unknown rather than being coloured as
// if it were about to work.
const KNOWN_COMMANDS = new Set([
  "mote", "val", "textbf", "emph", "texttt",
  "section", "subsection", "graph", "item", "begin", "end",
]);

function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/** Ranges may nest: a `$…$` span is coloured as a whole, and the
 *  commands and braces inside it (`\frac`, `{`, `}`) get their own
 *  range layered on top, so an equation reads with the same structure
 *  a `\section{…}` does rather than as one flat token. */
function syntaxRanges(text) {
  const ranges = [];
  const re = new RegExp(HIGHLIGHT_RE.source, "g");
  let m;
  while ((m = re.exec(text))) {
    if (m[1]) {
      ranges.push({ start: m.index, end: m.index + m[0].length, cls: "hl-comment" });
    } else if (m[2]) {
      const known = KNOWN_COMMANDS.has(m[2].slice(1));
      ranges.push({ start: m.index, end: m.index + m[0].length, cls: known ? "hl-cmd" : "hl-cmd-unknown" });
    } else if (m[3]) {
      ranges.push({ start: m.index, end: m.index + m[0].length, cls: "hl-brace" });
    } else {
      const start = m.index;
      const end = start + m[0].length;
      ranges.push({ start, end, cls: "hl-math" });
      const inner = m[0].slice(1, -1);
      const tokenRe = new RegExp(MATH_TOKEN_RE.source, "g");
      let tm;
      while ((tm = tokenRe.exec(inner))) {
        const tokenStart = start + 1 + tm.index;
        ranges.push({ start: tokenStart, end: tokenStart + tm[0].length, cls: tm[1] ? "hl-math-cmd" : "hl-brace" });
      }
    }
  }
  return ranges;
}

/** A <pre> behind the transparent textarea, scrolled in lockstep with
 *  it. The trailing newline matters: without it the last line's
 *  highlight is a pixel short of the caret.
 *
 *  Command/comment/brace/math colouring and find-bar match highlighting
 *  are independent sets of ranges over the same text, and the syntax
 *  ranges can themselves nest (a command inside a `$…$` span sits inside
 *  the span's own range), so this cuts the text at every boundary any
 *  of them introduces and gives each resulting piece every class that
 *  covers it -- a segment inside both a \command and the current find
 *  match gets both, rather than one highlighter clobbering another's
 *  span. */
function paintHighlight() {
  const text = $("editor").value;
  const synRanges = syntaxRanges(text);
  const points = new Set([0, text.length]);
  synRanges.forEach((r) => { points.add(r.start); points.add(r.end); });
  findMatches.forEach(([s, e]) => { points.add(s); points.add(e); });
  const bounds = [...points].sort((a, b) => a - b);

  let html = "";
  for (let i = 0; i < bounds.length - 1; i++) {
    const start = bounds[i];
    const end = bounds[i + 1];
    if (start === end) continue;
    const classes = [];
    synRanges.filter((r) => r.start <= start && end <= r.end).forEach((r) => classes.push(r.cls));
    const hitIndex = findMatches.findIndex(([s, e]) => s <= start && end <= e);
    if (hitIndex !== -1) classes.push(hitIndex === findIndex ? "find-hit find-hit-current" : "find-hit");
    const segment = escapeHtml(text.slice(start, end));
    html += classes.length ? `<span class="${classes.join(" ")}">${segment}</span>` : segment;
  }
  $("highlight").innerHTML = `${html}\n`;
}

function isFindBarOpen() {
  return !$("find-bar").classList.contains("hidden");
}

function onEditorInput() {
  markDirty();
  updatePicker();
  scheduleRender();
  scheduleAutosave();
  // Editing shifts every position after the caret, so a find bar left
  // open needs its match ranges (and their highlight) recomputed rather
  // than painted stale; otherwise paint them directly.
  if (isFindBarOpen()) refreshFindMatches({ keepIndex: true });
  else paintHighlight();
}

function markDirty() {
  const dirty = $("editor").value !== savedSource;
  const pill = $("dirty");
  pill.textContent = savePromise ? "saving…" : dirty ? "edited" : "synced";
  pill.classList.toggle("edited", dirty && !savePromise);
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
  adoptPalette(answer.speciesIds);
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
    return mathInto(el("div", "math display-math"), block.tex, true);
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
  if (node.type === "math") return mathInto(el("span", "math"), node.tex, false);
  if (node.type === "link") return buildLink(node);
  return buildValue(node);
}

/** The second thing this frontend reads for itself, and only because
 *  there is no tree to read: core/modoc.py keeps a `$...$` body raw on
 *  purpose, so what arrives is the TeX the author typed and KaTeX lays
 *  it out here.
 *
 *  throwOnError:false is the same bargain the parser strikes -- a note
 *  with a bad formula in it still renders, with the broken formula
 *  visibly broken in red, rather than one typo blanking the page. The
 *  catch below is for the tier past that (KaTeX missing or throwing
 *  something that isn't a ParseError): fall back to showing the source,
 *  which is never as good as typesetting it and always better than
 *  showing nothing. `trust` is left at its default of false, which is
 *  what keeps \href and \includegraphics out of a note that may have
 *  arrived over a garden sync. */
function mathInto(parent, tex, display) {
  try {
    katex.render(tex, parent, {
      displayMode: display,
      throwOnError: false,
      errorColor: "var(--danger)",
      strict: false,
    });
  } catch (e) {
    parent.classList.add("math-raw");
    parent.textContent = tex;
  }
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

// ---- export ----------------------------------------------------------

/** Print-to-PDF via the browser's own dialog rather than a server-side
 *  renderer: the rendered pane only ever exists as live DOM (KaTeX
 *  included, mathInto() above), so nothing here re-typesets it -- the
 *  @media print rules in style.css just hide everything but #rendered
 *  and swap it to a light page. The title becomes the dialog's
 *  suggested filename. */
function exportPdf() {
  const previousTitle = document.title;
  document.title = noteName || "Untitled note";
  window.print();
  document.title = previousTitle;
}

// ---- notes and saving ----------------------------------------------


async function loadNote(id, { preserveFocus = false } = {}) {
  await flushSave();
  closeFindBar();
  const opened = await api.open_note(id ?? null);
  noteId = opened.noteId;
  noteName = opened.name;
  savedSource = opened.source;
  $("editor").value = opened.source;
  $("note-title").textContent = noteName || "no motes yet";
  if (!preserveFocus) focusId = null;
  step = 0;
  paintHighlight();
  markDirty();
  await renderNow();
}

/** Autosave: a write once typing pauses. Manual save (⌘S, the topbar
 *  button) stays, because the pill going from "edited" to "synced" on
 *  its own is a promise, and the button is how you check it was kept.
 *  Nothing here writes anything but the `modoc` property -- see
 *  api.py::save_note. */
function scheduleAutosave() {
  clearTimeout(autosaveTimer);
  autosaveTimer = setTimeout(() => save({ silent: true }), AUTOSAVE_DEBOUNCE_MS);
}

/** Writes now if there is anything to write, and waits for it to land.
 *  Called before anything that replaces the editor's contents --
 *  switching notes, switching gardens, closing the window -- so an edit
 *  made a moment ago cannot be dropped by the debounce still waiting.
 *  The loop is for the case where a write is already out with older
 *  text: that one has to finish before the newer text can go. */
async function flushSave() {
  clearTimeout(autosaveTimer);
  for (let attempt = 0; attempt < 3; attempt++) {
    if (savePromise) await savePromise.catch(() => {});
    if (!noteId || $("editor").value === savedSource) return;
    await save({ silent: true });
  }
}

async function save({ silent = false } = {}) {
  if (!noteId) return;
  clearTimeout(autosaveTimer);
  // A write is already out. This text is newer, so it queues behind
  // that one rather than racing it -- two save_note calls landing out
  // of order would leave the older source on the mote.
  if (savePromise) { scheduleAutosave(); return; }
  const source = $("editor").value;
  if (silent && source === savedSource) return;
  savePromise = api.save_note(noteId, source);
  markDirty();
  try {
    await savePromise;
  } catch (e) {
    toast(String(e), true);
    return;
  } finally {
    savePromise = null;
    markDirty();
  }
  savedSource = source;
  markDirty();
  // Kept typing while the write was out: the pane is ahead of the file
  // again, so start the next one rather than leaving it unsaved.
  if ($("editor").value !== savedSource) scheduleAutosave();
  if (!silent) {
    toast("Note saved.");
    notes = await api.list_notes();
  }
}

// ---- notes search/list window (⌘P) ---------------------------------

let notesVisible = [];
let notesHighlight = -1;

/** A note matches by title if the query is in its name; otherwise it
 *  matches by content if the query turns up in its rendered text, in
 *  which case the row grows a snippet of the text around the hit so a
 *  content-only match still tells the reader why the note is here. */
function matchNotes(query) {
  const q = query.trim().toLowerCase();
  if (!q) return notes.map((note) => ({ note, snippet: null }));
  const titleHits = [];
  const contentHits = [];
  for (const note of notes) {
    if (note.name.toLowerCase().includes(q)) {
      titleHits.push({ note, snippet: null });
      continue;
    }
    const text = note.text || "";
    const idx = text.toLowerCase().indexOf(q);
    if (idx !== -1) contentHits.push({ note, snippet: snippetAround(text, idx, q.length) });
  }
  return [...titleHits, ...contentHits];
}

/** A window of plain text around a match, collapsed to one line and
 *  ellipsized at either edge that was cut, with the hit itself marked so
 *  the row's CSS can pick it out from the rest of the snippet. */
function snippetAround(text, index, length) {
  const radius = 42;
  const start = Math.max(0, index - radius);
  const end = Math.min(text.length, index + length + radius);
  const before = text.slice(start, index).replace(/\s+/g, " ");
  const hit = text.slice(index, index + length);
  const after = text.slice(index + length, end).replace(/\s+/g, " ");
  return {
    before: (start > 0 ? "…" : "") + before,
    hit,
    after: after + (end < text.length ? "…" : ""),
  };
}

function renderNotesResults(query) {
  notesVisible = matchNotes(query);
  notesHighlight = notesVisible.length ? 0 : -1;

  const container = clear($("notes-search-results"));
  if (!notesVisible.length) {
    container.append(el("div", "notes-empty", notes.length ? "No matches" : "No notes in this garden yet."));
    return;
  }
  notesVisible.forEach(({ note, snippet }, i) => {
    const row = el("div", "note-result-row" + (i === notesHighlight ? " highlighted" : ""));
    const dot = el("span", "dot");
    dot.style.cssText = `width:8px;height:8px;border-radius:50%;background:${colorOf(note)}`;
    const text = el("div", "result-text");
    text.append(el("span", "name", note.name));
    if (snippet) {
      const line = el("span", "snippet");
      line.append(
        document.createTextNode(snippet.before),
        el("mark", null, snippet.hit),
        document.createTextNode(snippet.after),
      );
      text.append(line);
    }
    row.append(dot, text);
    row.addEventListener("mousedown", (e) => { e.preventDefault(); selectNoteResult(note.id); });
    container.append(row);
  });
}

function updateNotesHighlight() {
  document.querySelectorAll("#notes-search-results .note-result-row").forEach((row, i) => {
    row.classList.toggle("highlighted", i === notesHighlight);
  });
  const active = document.querySelectorAll("#notes-search-results .note-result-row")[notesHighlight];
  if (active) active.scrollIntoView({ block: "nearest" });
}

async function openNotesWindow() {
  $("notes-overlay").classList.remove("hidden");
  $("notes-search-input").value = "";
  notes = await api.list_notes();
  renderNotesResults("");
  $("notes-search-input").focus();
}

function closeNotesWindow() {
  $("notes-overlay").classList.add("hidden");
}

function selectNoteResult(id) {
  closeNotesWindow();
  loadNote(id);
}

/** A note started on the spot, from the notes window's "+ New note…"
 *  row -- the same entry point Monastery's grid gives a new mote, minus
 *  the date it has no equivalent of here. Name is optional: an empty
 *  answer still opens a fresh page, with name_of()'s id-prefix fallback
 *  standing in until it's given one. */
async function createNote() {
  closeNotesWindow();
  const name = window.prompt("New note name:", "");
  if (name === null) return;
  try {
    const created = await api.create_note(name.trim() || null);
    await loadNote(created.noteId);
  } catch (e) {
    toast(String(e), true);
  }
}

// ---- garden menu ----

async function openGardenMenu() {
  const menu = $("garden-menu");
  if (!menu.classList.contains("hidden")) { closeGardenMenu(); return; }
  const gardens = await api.list_gardens();
  const current = $("garden-name").textContent;
  clear(menu);
  if (!gardens.length) menu.append(el("div", "menu-empty", "No gardens."));
  gardens.forEach((name) => {
    const item = el("button", "menu-item", name);
    item.type = "button";
    if (name === current) item.classList.add("on");
    item.onclick = async () => {
      closeGardenMenu();
      await flushSave();
      const answer = await api.switch_garden(name);
      $("garden-name").textContent = answer.garden;
      closeNotesWindow();
      closePicker();
      await loadNote(null);
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

// ---- find and replace (⌘F) ------------------------------------------
//
// Plain substring search over the editor's own value -- no regex, no
// overlay highlighting of every hit (that would mean reconciling ranges
// against the syntax-highlight pass above). The current match is instead
// the textarea's own selection, moved with setSelectionRange(), which is
// enough to see and to type over.

let findMatches = [];   // [start, end) pairs, current query, current case
let findIndex = -1;
let findCaseSensitive = false;

function findQuery() {
  return $("find-input").value;
}

function computeFindMatches() {
  const query = findQuery();
  findMatches = [];
  if (!query) return;
  const haystack = findCaseSensitive ? $("editor").value : $("editor").value.toLowerCase();
  const needle = findCaseSensitive ? query : query.toLowerCase();
  let from = 0;
  while (true) {
    const at = haystack.indexOf(needle, from);
    if (at === -1) break;
    findMatches.push([at, at + needle.length]);
    from = at + needle.length;
  }
}

/** Picks the match nearest the caret so opening the bar (or editing the
 *  query) lands on what the reader is already looking at, rather than
 *  always snapping back to the top of the note. */
function findNearestIndex() {
  if (!findMatches.length) return -1;
  const caret = $("editor").selectionStart;
  for (let i = 0; i < findMatches.length; i++) {
    if (findMatches[i][0] >= caret) return i;
  }
  return 0;
}

function refreshFindMatches({ keepIndex = false } = {}) {
  const previous = findIndex;
  computeFindMatches();
  if (!findMatches.length) {
    findIndex = -1;
  } else if (keepIndex && previous >= 0) {
    findIndex = Math.min(previous, findMatches.length - 1);
  } else {
    findIndex = findNearestIndex();
  }
  drawFindState();
}

function drawFindState() {
  $("find-count").textContent = findMatches.length ? `${findIndex + 1}/${findMatches.length}` : "0/0";
  if (findIndex >= 0) selectFindMatch(findIndex);
  paintHighlight();
}

/** Selects a match in the textarea without moving focus off the find
 *  input, and scrolls it into view by line rather than by pixel offset,
 *  since the editor wraps and its line height is the one fixed unit. */
function selectFindMatch(index) {
  const editor = $("editor");
  const [start, end] = findMatches[index];
  editor.setSelectionRange(start, end);
  const lineHeight = parseFloat(getComputedStyle(editor).lineHeight);
  const line = editor.value.slice(0, start).split("\n").length - 1;
  const target = line * lineHeight;
  if (target < editor.scrollTop || target > editor.scrollTop + editor.clientHeight - lineHeight * 2) {
    editor.scrollTop = Math.max(0, target - editor.clientHeight / 2);
  }
}

function stepFind(delta) {
  if (!findMatches.length) return;
  findIndex = (findIndex + delta + findMatches.length) % findMatches.length;
  drawFindState();
}

function openFindBar() {
  if (mode === "preview") setMode("split");
  closePicker();
  const editor = $("editor");
  const selected = editor.value.slice(editor.selectionStart, editor.selectionEnd);
  const bar = $("find-bar");
  bar.classList.remove("hidden");
  const input = $("find-input");
  if (selected && !selected.includes("\n")) input.value = selected;
  input.focus();
  input.select();
  refreshFindMatches();
}

function closeFindBar() {
  $("find-bar").classList.add("hidden");
  findMatches = [];
  findIndex = -1;
  paintHighlight();
  $("editor").focus();
}

function replaceCurrentMatch() {
  if (findIndex < 0 || !findMatches.length) return;
  const editor = $("editor");
  const [start, end] = findMatches[findIndex];
  const replacement = $("replace-input").value;
  editor.value = editor.value.slice(0, start) + replacement + editor.value.slice(end);
  editor.setSelectionRange(start + replacement.length, start + replacement.length);
  onEditorInput();
  refreshFindMatches();
}

function replaceAllMatches() {
  if (!findMatches.length) return;
  const editor = $("editor");
  const query = findQuery();
  const replacement = $("replace-input").value;
  const count = findMatches.length;
  editor.value = findCaseSensitive
    ? editor.value.split(query).join(replacement)
    : editor.value.replace(new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"), "gi"), replacement);
  onEditorInput();
  refreshFindMatches();
  toast(`Replaced ${count} match${count === 1 ? "" : "es"}.`);
}

function wireFindBar() {
  const input = $("find-input");
  input.addEventListener("input", () => refreshFindMatches());
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      stepFind(event.shiftKey ? -1 : 1);
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeFindBar();
    }
  });
  $("replace-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      replaceCurrentMatch();
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeFindBar();
    }
  });
  $("find-case").addEventListener("click", () => {
    findCaseSensitive = !findCaseSensitive;
    $("find-case").classList.toggle("on", findCaseSensitive);
    refreshFindMatches({ keepIndex: true });
  });
  $("find-prev").onclick = () => stepFind(-1);
  $("find-next").onclick = () => stepFind(1);
  $("find-close").onclick = closeFindBar;
  $("replace-one").onclick = replaceCurrentMatch;
  $("replace-all").onclick = replaceAllMatches;
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
  editor.addEventListener("blur", () => { closePicker(); flushSave(); });
  editor.addEventListener("keydown", (event) => {
    if (onPickerKey(event)) { event.preventDefault(); return; }
  });
  editor.addEventListener("click", updatePicker);

  $("save").onclick = () => save();
  $("refresh-btn").onclick = async () => {
    const btn = $("refresh-btn");
    btn.disabled = true;
    btn.classList.add("spinning");
    try {
      const result = await api.refresh();
      notes = await api.list_notes();
      const dirty = $("editor").value !== savedSource;
      if (!dirty) await loadNote(noteId, { preserveFocus: true });
      const base = result.synced ? "Synced" : "Refreshed";
      toast(dirty ? `${base} (unsaved edits kept).` : `${base} and refreshed.`);
    } catch (e) {
      toast(String(e), true);
    } finally {
      btn.disabled = false;
      btn.classList.remove("spinning");
    }
  };
  $("data-toggle").onclick = () => { dataOpen = !dataOpen; applyLayout(); };
  document.querySelectorAll("#modes button").forEach((button) => {
    button.onclick = () => setMode(button.dataset.mode);
  });

  $("garden-btn").onclick = openGardenMenu;
  $("notes-btn").onclick = openNotesWindow;
  $("export-pdf-btn").onclick = exportPdf;
  $("notes-new-btn").onclick = createNote;
  $("notes-overlay").addEventListener("mousedown", (event) => {
    if (event.target === $("notes-overlay")) closeNotesWindow();
  });
  $("notes-search-input").addEventListener("input", (event) => renderNotesResults(event.target.value));
  $("notes-search-input").addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (notesHighlight < notesVisible.length - 1) notesHighlight++;
      updateNotesHighlight();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (notesHighlight > 0) notesHighlight--;
      updateNotesHighlight();
    } else if (event.key === "Enter") {
      event.preventDefault();
      const chosen = notesVisible[notesHighlight];
      if (chosen) selectNoteResult(chosen.note.id);
    } else if (event.key === "Escape") {
      closeNotesWindow();
    }
  });
  $("panel-back").onclick = () => { focusId = null; drawPanel(payload.anchor.id); };
  $("problems-btn").onclick = () => {
    dataOpen = true;
    applyLayout();
    $("panel-problems-section").scrollIntoView({ behavior: "smooth" });
  };

  $("tick-up").onclick = () => setStep(step + 1);
  $("tick-down").onclick = () => setStep(step - 1);
  $("apply").onclick = applyStep;

  // Last line of defence: the debounce may still be waiting when the
  // window goes away. pagehide is the one that fires reliably inside
  // pywebview's webview; the write itself is best-effort from here.
  window.addEventListener("pagehide", () => { flushSave(); });
  window.addEventListener("beforeunload", () => { flushSave(); });

  document.addEventListener("click", (event) => {
    if (!event.target.closest(".topbar-menu")) closeGardenMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") { closeGardenMenu(); closeNotesWindow(); closePicker(); closeLauncher(); closeFindBar(); }
    // Window-level rather than scoped to the editor (as this used to be):
    // Save has its own persistent topbar button and dirty-state pill, so
    // Ctrl/Cmd+S reads as "save the note" from anywhere in the window, not
    // just while the caret happens to be sitting in the source pane.
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      save();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "p") {
      event.preventDefault();
      openNotesWindow();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "j") {
      event.preventDefault();
      openLauncher();
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
      event.preventDefault();
      openFindBar();
    }
    if ((event.ctrlKey || event.metaKey) && /^[1-5]$/.test(event.key)) {
      const app = launcherChoices()[Number(event.key) - 1];
      if (app) {
        event.preventDefault();
        launchApp(app.id);
      }
    }
  });

  setupLauncher();
  wireFindBar();
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
];
const THIS_APP = "mora";

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
  try {
    await api.open_app(appId);
  } catch (e) {
    toast(String(e), true);
    return;
  }
  toast(`Opening ${app.name}…`);
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

async function init() {
  api = window.pywebview.api;
  wire();
  applyLayout();
  $("garden-name").textContent = await api.current_garden();
  await loadNote(null);
}

window.addEventListener("pywebviewready", init);
