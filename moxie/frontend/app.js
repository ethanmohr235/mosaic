// moxie/frontend/app.js
//
// Moxie's state and wiring. plot.js draws; this decides what.
//
// The window holds a *grid* of plots rather than one, because the
// question it exists for -- how does this value move when that one
// changes -- is almost never asked once. Reading σ against E beside σ
// against Γ is the whole point, and a tab strip cannot do it.
//
// # Series, cases and what a sweep costs
//
// A series is (mote, property, scenario, extra override). Several series
// that differ only in which property they read share one solve per
// point, so the cost of a plot is *cases* x points, not series x points
// -- see core/sweep.py. casesFor() below is where that collapse happens,
// and the run rail states the number it arrives at rather than capping
// it: a user adding a twelfth series should see what they just bought.
//
// # Colour
//
// Six hues, assigned by stable series id and never by position, so
// switching one series off never repaints the others. Six because that
// is how many stay separable on this background under every pairwise
// colour-vision check when any series may overlap any other; a seventh
// takes hue one again with a dash pattern, which is a second channel
// rather than a colour nobody can tell from another. See style.css.
//
// # Refinement
//
// Every sweep runs twice: a coarse pass (every eighth point) that lands
// in about the time of a frame, then the rest. Both passes come back
// labelled with their index in the axis, and mergePoints() lays the
// second over the first. Dragging a knob therefore always redraws
// immediately and sharpens after, rather than freezing and then jumping.

const THIS_APP = "moxie";
const SERIES_HUES = 6;
// Second channel past six series. Never a seventh hue: a generated one
// is a colour the reader cannot reliably tell from one already in use.
const DASHES = [null, "6 3", "2 3", "8 3 2 3"];
const MAX_KNOBS = 8;

let api = null;
let board = null;
let plots = [];
let activePlot = 0;
let knobs = [];
let search = "";
let generation = 0;
let sensitivity = [];
let toastTimer = null;
let resizeTimer = null;
// Nothing is written back over the saved session until it has been read.
// The first render happens inside the opening refresh() -- before the
// restore -- so without this the window would save its own empty grid
// over your last one every single time it opened, and a restore would
// never once succeed.
let sessionReady = false;

const $ = (id) => document.getElementById(id);

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

const uid = () => Math.random().toString(36).slice(2, 9);
const fmt = (v) => Plot.fmt(v);

// ---- the model -------------------------------------------------------

function moteById(id) { return (board.motes || []).find((m) => m.id === id) || null; }

function propOf(moteId, key) {
  const mote = moteById(moteId);
  return mote ? (mote.properties || []).find((p) => p.key === key) || null : null;
}

function labelOf(moteId, key) {
  const mote = moteById(moteId);
  return `${mote ? mote.name : moteId.slice(0, 8)}.${key}`;
}

function newPlot(x, series) {
  return {
    id: uid(),
    mode: "sweep",
    x: x || null,
    y: null,
    series: series || [],
    baseline: null,
    ghost: false,
    yScale: "linear",
    data: null,
    cursor: null,
    busy: false,
  };
}

function plot() { return plots[activePlot] || null; }

// A series' colour follows its own identity, never its rank in the
// visible list: switching one off must not repaint its neighbours.
function seriesStyle(p, s) {
  const index = p.series.findIndex((x) => x.id === s.id);
  return {
    color: `var(--series-${(index % SERIES_HUES) + 1})`,
    dash: DASHES[Math.floor(index / SERIES_HUES) % DASHES.length],
  };
}

function knobOverrides() {
  const out = {};
  for (const k of knobs) {
    (out[k.mote] = out[k.mote] || {})[k.key] = k.value;
  }
  return out;
}

function caseKeyOf(s) {
  return `sc:${s.scenario || "-"}|ex:${s.extra ? `${s.extra.mote}.${s.extra.key}=${s.extra.value}` : "-"}`;
}

// The distinct what-ifs a plot asks for. Several series differing only
// in which property they read collapse to one case here, which is the
// whole of why a plot with eight curves usually costs one solve a point.
function casesFor(p) {
  const map = new Map();
  for (const s of p.series) {
    const key = caseKeyOf(s);
    if (map.has(key)) continue;
    const overrides = knobOverrides();
    if (s.extra) {
      overrides[s.extra.mote] = { ...(overrides[s.extra.mote] || {}), [s.extra.key]: s.extra.value };
    }
    map.set(key, { key, scenario: s.scenario || null, overrides });
  }
  if (p.ghost && p.baseline) {
    map.set("ghost", { key: "ghost", scenario: p.baseline, overrides: knobOverrides() });
  }
  if (!map.size) map.set("base", { key: "base", scenario: null, overrides: knobOverrides() });
  return [...map.values()];
}

function specOf(p) {
  const spec = {
    x: p.x,
    cases: casesFor(p),
    series: p.series.map((s) => ({
      id: s.id, mote: s.mote, key: s.key, case: caseKeyOf(s),
    })),
  };
  if (p.ghost && p.baseline) {
    for (const s of p.series) {
      spec.series.push({ id: `ghost:${s.id}`, mote: s.mote, key: s.key, case: "ghost" });
    }
  }
  return spec;
}

// ---- running ---------------------------------------------------------

function mergePoints(existing, incoming) {
  const byIndex = new Map((existing ? existing.points : []).map((p) => [p.i, p]));
  for (const p of incoming.points) byIndex.set(p.i, p);
  return {
    ...incoming,
    // Both passes solved real points, so the cost reported is their sum.
    // Reporting only the refining pass would understate every sweep by
    // its own coarse pass, which is the one the user actually waited for.
    solves: (existing ? existing.solves : 0) + incoming.solves,
    seconds: Math.round(((existing ? existing.seconds : 0) + incoming.seconds) * 1e4) / 1e4,
    failures: (existing ? existing.failures : 0) + incoming.failures,
    points: [...byIndex.values()].sort((a, b) => a.i - b.i),
  };
}

async function runPlot(p) {
  // Dispatch on mode *before* testing for an axis and series: a
  // correlation has neither by design -- it reads two property names off
  // every mote in the garden -- so a sweep's precondition applied first
  // would quietly stop it ever running.
  if (p.mode === "surface") return runSurface(p);
  if (p.mode === "correlate") return runCorrelate(p);
  if (!p.x || !p.series.length) { p.data = null; renderAll(); return; }

  const spec = specOf(p);
  const gen = ++generation;
  p.busy = true;
  renderGrid();

  const coarse = await call(api.sweep(spec, coarseIndices(p.x.steps), gen, p.id));
  if (!coarse) { p.busy = false; renderAll(); return; }
  if (coarse.cancelled) return;                       // a newer pass owns this plot
  p.data = coarse;
  renderGrid();

  const done = new Set(coarse.points.map((q) => q.i));
  const rest = [];
  for (let i = 0; i <= p.x.steps; i++) if (!done.has(i)) rest.push(i);
  if (rest.length) {
    const fine = await call(api.sweep(spec, rest, gen, p.id));
    if (!fine || fine.cancelled) return;
    p.data = mergePoints(p.data, fine);
  }
  p.busy = false;
  renderAll();
  refreshSensitivity();
}

// Mirrors moxie/api.py's own stride. Computed here rather than fetched
// so a dragged slider costs one bridge round trip per pass, not two.
function coarseIndices(steps) {
  const out = [];
  for (let i = 0; i <= steps; i += 8) out.push(i);
  if (out[out.length - 1] !== steps) out.push(steps);
  return out;
}

async function runSurface(p) {
  if (!p.y || !p.output) { p.data = null; renderAll(); return; }
  const gen = ++generation;
  p.busy = true;
  renderGrid();
  const spec = {
    x: p.x, y: p.y, output: p.output,
    cases: casesFor(p).slice(0, 1),
  };
  for (const stride of [4, 1]) {
    const grid = await call(api.surface(spec, stride, gen, p.id));
    if (!grid) break;
    if (grid.cancelled) return;
    p.data = grid;
    p.stride = stride;
    renderGrid();
  }
  p.busy = false;
  renderAll();
}

async function runCorrelate(p) {
  if (!p.xKey || !p.yKey) { p.data = null; renderAll(); return; }
  p.busy = true;
  renderGrid();
  const dots = await call(api.correlate({
    xKey: p.xKey, yKey: p.yKey, cases: casesFor(p).slice(0, 1),
  }));
  p.data = dots ? { dots } : null;
  p.busy = false;
  renderAll();
}

function scheduleRun(p) {
  clearTimeout(p.timer);
  p.timer = setTimeout(() => runPlot(p), 40);
}

function runAll() { for (const p of plots) scheduleRun(p); }

// ---- turning results into something drawable -------------------------

function seriesPoints(p, prefix) {
  const out = new Map();
  for (const s of p.series) out.set(s.id, []);
  for (const point of (p.data && p.data.points) || []) {
    for (const s of p.series) {
      const value = point.v[prefix ? `${prefix}${s.id}` : s.id];
      out.get(s.id).push(value === undefined || value === null
        ? null : { x: point.x, y: value });
    }
  }
  return out;
}

// Group series by what they measure. One band is one Y axis; two puts a
// second axis on the right; three or more has no honest shared scale, so
// the plot normalizes each series to its own range and says so.
function bandsFor(p, pointsById) {
  const groups = [];
  for (const s of p.series) {
    if (s.hidden) continue;
    const prop = propOf(s.mote, s.key);
    const dimension = prop ? prop.dimension : null;
    let band = groups.find((g) => g.dimension === dimension);
    if (!band) {
      band = { dimension, unit: prop ? prop.unit : null, series: [], keys: [], lo: Infinity, hi: -Infinity };
      groups.push(band);
    }
    band.series.push(s.id);
    if (!band.keys.includes(s.key)) band.keys.push(s.key);
    if (band.unit !== (prop ? prop.unit : null)) band.unit = null;
    for (const pt of pointsById.get(s.id) || []) {
      if (!pt) continue;
      band.lo = Math.min(band.lo, pt.y);
      band.hi = Math.max(band.hi, pt.y);
    }
  }
  for (const band of groups) {
    if (!isFinite(band.lo)) { band.lo = 0; band.hi = 1; }
    // A garden that declares no units still has something true to put on
    // the axis: the name of what is being plotted. "value" tells nobody
    // anything, and most gardens here declare no units at all.
    band.label = band.keys.join(" / ") + (band.unit ? ` (${band.unit})` : "");
    padBand(band, p.yScale === "log");
  }
  return groups;
}

function padBand(band, log) {
  if (!isFinite(band.lo) || !isFinite(band.hi)) { band.lo = 0; band.hi = 1; }
  if (log) {
    band.lo = Math.max(band.lo, 1e-9);
    band.hi = Math.max(band.hi, band.lo * 10);
    return;
  }
  // Equal *enough* to be degenerate: several motes agreeing on a value
  // to fourteen decimal places is one value as far as an axis goes, and
  // padding by a proportion of a span that small would leave the axis
  // labelled with sixteen identical numbers.
  const span = band.hi - band.lo;
  if (span <= Math.abs(band.hi) * 1e-9) {
    const step = Math.abs(band.hi) * 0.5 || 1;
    band.lo -= step;
    band.hi += step;
    return;
  }
  const pad = (band.hi - band.lo) * 0.06;
  band.lo -= pad;
  band.hi += pad;
  if (band.lo > 0 && band.lo < (band.hi - band.lo) * 0.4) band.lo = 0;
}

// Contiguous runs of x where something was true -- a failed solve, or a
// broken constraint. Drawn as one shaded span rather than 40 stripes.
function spansOf(points, predicate, message) {
  const spans = [];
  let open = null;
  for (const point of points) {
    const hit = predicate(point);
    if (hit && !open) open = { x0: point.x, x1: point.x, message: message(point) };
    else if (hit && open) open.x1 = point.x;
    else if (!hit && open) { spans.push(open); open = null; }
  }
  if (open) spans.push(open);
  return spans;
}

function sweepModel(p, width, height) {
  const pointsById = seriesPoints(p);
  const ghostById = p.ghost && p.baseline ? seriesPoints(p, "ghost:") : null;
  const bands = bandsFor(p, pointsById);
  const points = (p.data && p.data.points) || [];
  const cursorRows = [];
  const model = {
    mode: "sweep", width, height,
    xLo: p.x.lo, xHi: p.x.hi, xLabel: axisLabel(p.x),
    yScale: p.yScale,
    bands: bands.length ? bands : [{ lo: 0, hi: 1, label: "value" }],
    series: p.series.map((s) => ({
      id: s.id, hidden: s.hidden, label: s.label, ...seriesStyle(p, s),
      band: Math.max(0, bands.findIndex((b) => b.series.includes(s.id))),
      points: pointsById.get(s.id) || [],
      ghost: ghostById ? ghostById.get(s.id) || [] : null,
    })),
    failedSpans: spansOf(points, (q) => Object.keys(q.errors).length,
      (q) => Object.values(q.errors)[0]),
    violatedSpans: spansOf(points, (q) => q.violated && q.violated.length,
      () => (p.data.brokenConstraints || []).join("\n") || "a constraint is not satisfied"),
    cursor: null,
  };
  if (p.cursor !== null && points.length) {
    const nearest = points.reduce((best, q) =>
      Math.abs(q.x - p.cursor) < Math.abs(best.x - p.cursor) ? q : best, points[0]);
    for (const s of p.series) {
      if (s.hidden) continue;
      const value = nearest.v[s.id];
      cursorRows.push({
        color: seriesStyle(p, s).color,
        band: Math.max(0, bands.findIndex((b) => b.series.includes(s.id))),
        value: value === undefined ? null : value,
        label: s.label,
        ghost: ghostById ? nearest.v[`ghost:${s.id}`] : undefined,
      });
    }
    model.cursor = { x: nearest.x, rows: cursorRows, point: nearest };
  }
  return model;
}

function surfaceModel(p, width, height) {
  const data = p.data || { cells: [] };
  const stride = p.stride || 1;
  const cols = new Set(), rows = new Set();
  for (const c of data.cells) { cols.add(c.c); rows.add(c.r); }
  const colIndex = new Map([...cols].sort((a, b) => a - b).map((v, i) => [v, i]));
  const rowIndex = new Map([...rows].sort((a, b) => a - b).map((v, i) => [v, i]));
  return {
    mode: "surface", width, height,
    cells: data.cells.map((c) => ({
      col: colIndex.get(c.c), row: rowIndex.get(c.r),
      x: c.x, y: c.y, v: c.v, error: c.error,
    })),
    drawnCols: colIndex.size, drawnRows: rowIndex.size,
    lo: data.lo, hi: data.hi, stride,
    xLo: p.x.lo, xHi: p.x.hi, yLo: p.y.lo, yHi: p.y.hi,
    xLabel: axisLabel(p.x), yLabel: axisLabel(p.y),
  };
}

function correlateModel(p, width, height) {
  const dots = (p.data && p.data.dots) || [];
  const xs = dots.map((d) => d.x), ys = dots.map((d) => d.y);
  // The dots' own range, not one forced to include 0 and 1: a garden
  // whose currents are all around 0.03 would otherwise draw every mote
  // squashed onto the axis line, which is the one thing a correlation
  // must not do. padBand() handles the degenerate cases.
  const xBand = { lo: Math.min(...xs), hi: Math.max(...xs) };
  const yBand = { lo: Math.min(...ys), hi: Math.max(...ys) };
  padBand(xBand, false);
  padBand(yBand, p.yScale === "log");
  return {
    mode: "correlate", width, height,
    dots: dots.map((d, i) => ({
      ...d, label: d.name,
      color: `var(--series-${(i % SERIES_HUES) + 1})`,
    })),
    xLo: xBand.lo, xHi: xBand.hi, yLo: yBand.lo, yHi: yBand.hi,
    xLabel: p.xKey, yLabel: p.yKey, yScale: p.yScale,
  };
}

function axisLabel(axis) {
  if (!axis) return "";
  const prop = propOf(axis.mote, axis.key);
  const unit = prop && prop.unit ? ` (${prop.unit})` : "";
  return `${labelOf(axis.mote, axis.key)}${unit}`;
}

// ---- rendering: the grid ---------------------------------------------

function renderGrid() {
  const grid = $("grid");
  grid.style.setProperty("--cols", plots.length > 1 ? 2 : 1);
  grid.textContent = "";
  if (!plots.length) {
    const empty = document.createElement("div");
    empty.className = "plot-note";
    empty.innerHTML = "Pick a property in the left rail to sweep,<br>then one to plot against it.";
    grid.appendChild(empty);
    return;
  }
  plots.forEach((p, index) => grid.appendChild(plotCell(p, index)));
}

function plotCell(p, index) {
  const cell = document.createElement("div");
  cell.className = `plot-cell${index === activePlot ? " active" : ""}`;
  cell.dataset.plot = String(index);
  cell.dataset.mode = p.mode;
  cell.onmousedown = () => { if (activePlot !== index) { activePlot = index; renderAll(); } };

  cell.appendChild(plotHead(p, index));

  const body = document.createElement("div");
  body.className = "plot-body";
  cell.appendChild(body);
  cell.appendChild(plotFoot(p));

  requestAnimationFrame(() => drawInto(body, p));
  return cell;
}

function plotHead(p, index) {
  const head = document.createElement("div");
  head.className = "plot-head";

  const title = document.createElement("span");
  title.className = "plot-title";
  title.textContent = plotTitle(p);
  head.appendChild(title);

  const sub = document.createElement("span");
  sub.className = "plot-sub";
  sub.textContent = plotSubtitle(p);
  head.appendChild(sub);

  head.appendChild(Object.assign(document.createElement("span"), { className: "spacer" }));

  if (p.mode === "sweep" || p.mode === "correlate") {
    for (const [scale, label] of [["linear", "lin"], ["log", "log"]]) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.textContent = label;
      btn.className = p.yScale === scale ? "on" : "";
      btn.onclick = () => { p.yScale = scale; renderAll(); };
      head.appendChild(btn);
    }
  }
  const csv = document.createElement("button");
  csv.type = "button";
  csv.textContent = "csv";
  csv.title = "Export these points";
  csv.onclick = () => exportCsv(p);
  head.appendChild(csv);

  const png = document.createElement("button");
  png.type = "button";
  png.textContent = "png";
  png.title = "Export this plot as an image";
  png.onclick = (e) => exportPng(p, e.currentTarget.closest(".plot-cell"));
  head.appendChild(png);

  const close = document.createElement("button");
  close.type = "button";
  close.textContent = "×";
  close.title = "Remove this plot";
  close.onclick = () => {
    plots.splice(index, 1);
    activePlot = Math.max(0, Math.min(activePlot, plots.length - 1));
    renderAll();
  };
  head.appendChild(close);
  return head;
}

function plotTitle(p) {
  if (p.mode === "correlate") return `${p.yKey || "y"} against ${p.xKey || "x"}`;
  const ys = p.series.filter((s) => !s.hidden).map((s) => s.key);
  const unique = [...new Set(ys)];
  const y = unique.length === 1 ? unique[0] : `${unique.length} properties`;
  return p.x ? `${y} against ${p.x.key}` : y;
}

function plotSubtitle(p) {
  if (!p.data) return p.busy ? "solving…" : "";
  if (p.mode === "correlate") return `${(p.data.dots || []).length} motes · one solve`;
  if (p.mode === "surface") return `${p.data.solves} solves · ${p.data.seconds}s`;
  const bits = [`${p.data.solves} solves`, `${p.data.seconds}s`];
  if (p.data.pinned) bits.push("x pinned");
  if (p.data.failures) bits.push(`${p.data.failures} failed`);
  return bits.join(" · ");
}

function drawInto(body, p) {
  const rect = body.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  if (!p.data) {
    const note = document.createElement("div");
    note.className = "plot-note";
    note.textContent = p.busy ? "solving…" : emptyMessage(p);
    body.textContent = "";
    body.appendChild(note);
    if (p.busy) body.appendChild(Object.assign(document.createElement("div"), { className: "busy-bar" }));
    return;
  }
  const model = p.mode === "surface" ? surfaceModel(p, rect.width, rect.height)
    : p.mode === "correlate" ? correlateModel(p, rect.width, rect.height)
      : sweepModel(p, rect.width, rect.height);
  const g = Plot.render(body, model);
  if (p.busy) body.appendChild(Object.assign(document.createElement("div"), { className: "busy-bar" }));
  if (p.mode === "sweep") {
    attachCursor(body, p, g, model);
    if (model.cursor) body.appendChild(readout(p, g, model));
  }
}

function emptyMessage(p) {
  if (p.mode === "correlate") return "Pick two property names to correlate.";
  if (!p.x) return "Pick a property to sweep.";
  if (!p.series.length) return "Pick a property to plot against it.";
  return "Nothing solved yet.";
}

function attachCursor(body, p, g, model) {
  body.onmousemove = (event) => {
    const rect = body.getBoundingClientRect();
    const px = event.clientX - rect.left;
    const fraction = (px - g.left) / g.pw;
    p.cursor = model.xLo + fraction * (model.xHi - model.xLo);
    drawInto(body, p);
  };
  body.onmouseleave = () => {
    if (p.cursor === null) return;
    p.cursor = null;
    drawInto(body, p);
    refreshSensitivity();
  };
  body.onmouseup = () => refreshSensitivity();
}

function readout(p, g, model) {
  const box = document.createElement("div");
  box.className = "readout";
  const X = Plot.scaleX(g, model.xLo, model.xHi);
  const left = Math.min(g.right - 218, Math.max(g.left + 8, X(model.cursor.x) + 14));
  box.style.left = `${left}px`;
  box.style.top = `${g.top}px`;

  const head = document.createElement("div");
  head.className = "readout-head";
  head.textContent = `${p.x.key} = ${fmt(model.cursor.x)}`;
  box.appendChild(head);

  for (const row of model.cursor.rows) {
    const line = document.createElement("div");
    line.className = "readout-row";
    const dot = document.createElement("span");
    dot.className = "readout-dot";
    dot.style.background = row.color;
    line.appendChild(dot);
    line.appendChild(Object.assign(document.createElement("span"),
      { className: "readout-label", textContent: row.label }));
    line.appendChild(Object.assign(document.createElement("span"),
      { className: "readout-value", textContent: fmt(row.value) }));
    if (row.ghost !== undefined && row.ghost !== null && row.value !== null) {
      const delta = row.value - row.ghost;
      const span = document.createElement("span");
      const small = Math.abs(delta) < Math.abs(row.value || 1) * 1e-6;
      span.className = `readout-delta ${small ? "flat" : delta > 0 ? "up" : "down"}`;
      span.textContent = `${delta >= 0 ? "+" : "−"}${fmt(Math.abs(delta))}`;
      line.appendChild(span);
    }
    box.appendChild(line);
  }

  const foot = document.createElement("div");
  foot.className = "readout-foot";
  const point = model.cursor.point;
  foot.textContent = Object.keys(point.errors).length
    ? Object.values(point.errors)[0]
    : point.violated && point.violated.length ? "breaks a constraint here"
      : p.ghost && p.baseline ? `Δ against ${scenarioName(p.baseline)}` : `${p.data.caseCount} case(s)`;
  box.appendChild(foot);
  return box;
}

function plotFoot(p) {
  const foot = document.createElement("div");
  foot.className = "plot-foot";
  if (p.mode === "sweep" && p.x) foot.appendChild(rangeControls(p));

  foot.appendChild(Object.assign(document.createElement("span"), { className: "spacer" }));

  // A legend is always present for two or more series -- identity must
  // never be carried by colour alone.
  if (p.series.length && p.mode !== "correlate") {
    const legend = document.createElement("div");
    legend.className = "legend";
    for (const s of p.series) {
      const item = document.createElement("div");
      item.className = `legend-item${s.hidden ? " off" : ""}`;
      item.dataset.series = s.id;
      const swatch = document.createElement("span");
      const style = seriesStyle(p, s);
      swatch.className = "legend-swatch";
      swatch.style.borderTopColor = style.color;
      if (style.dash) swatch.style.borderTopStyle = "dashed";
      item.appendChild(swatch);
      item.appendChild(document.createTextNode(s.label));
      item.title = "Click to hide · shift-click to remove";
      item.onclick = (event) => {
        if (event.shiftKey) p.series = p.series.filter((x) => x.id !== s.id);
        else s.hidden = !s.hidden;
        renderAll();
        if (event.shiftKey) scheduleRun(p);
      };
      legend.appendChild(item);
    }
    foot.appendChild(legend);
  }
  return foot;
}

function rangeControls(p) {
  const wrap = document.createElement("div");
  wrap.className = "legend";
  const label = document.createElement("span");
  label.className = "mono-note";
  label.textContent = `${p.x.key} range`;
  wrap.appendChild(label);

  for (const bound of ["lo", "hi"]) {
    const input = document.createElement("input");
    input.value = String(p.x[bound]);
    input.style.width = "68px";
    input.style.fontFamily = "var(--mono)";
    input.style.fontSize = "10.5px";
    input.onchange = () => {
      const value = parseFloat(input.value);
      if (!isFinite(value)) { input.value = String(p.x[bound]); return; }
      p.x[bound] = value;
      if (p.x.lo === p.x.hi) p.x.hi = p.x.lo + 1;
      scheduleRun(p);
    };
    wrap.appendChild(input);
  }

  const steps = document.createElement("input");
  steps.type = "range";
  steps.min = "20"; steps.max = "400"; steps.step = "20";
  steps.value = String(p.x.steps);
  steps.style.width = "84px";
  steps.oninput = () => { p.x.steps = parseInt(steps.value, 10); scheduleRun(p); };
  wrap.appendChild(steps);
  wrap.appendChild(Object.assign(document.createElement("span"),
    { className: "mono-note", textContent: `${p.x.steps} steps` }));
  return wrap;
}

// ---- rendering: the left rail ----------------------------------------

function renderRail() {
  const list = $("mote-list");
  list.textContent = "";
  const q = search.trim().toLowerCase();
  const rows = (board.motes || []).filter((m) =>
    !q || m.name.toLowerCase().includes(q)
    || (m.properties || []).some((p) => p.key.toLowerCase().includes(q)));

  $("mote-count").textContent = `${rows.length}/${(board.motes || []).length}`;
  if (!rows.length) {
    list.appendChild(Object.assign(document.createElement("div"), {
      className: "rail-empty",
      textContent: (board.motes || []).length
        ? "No mote matches that."
        : "No mote in this garden holds a number yet. Give one a numeric property, or declare a freedom for the solver to find.",
    }));
    return;
  }
  for (const mote of rows) list.appendChild(moteCard(mote));
}

function moteCard(mote) {
  const p = plot();
  const card = document.createElement("div");
  const isX = p && p.x && p.x.mote === mote.id;
  card.className = `mote-card${isX ? " active" : ""}`;
  card.dataset.mote = mote.id;

  const top = document.createElement("div");
  top.className = "mote-top";
  top.appendChild(Object.assign(document.createElement("span"),
    { className: "mote-name", textContent: mote.name }));
  if (mote.species) {
    top.appendChild(Object.assign(document.createElement("span"),
      { className: "mote-species", textContent: mote.species }));
  }
  top.onclick = () => Inspector.open(mote.id);
  top.oncontextmenu = (event) => moteMenu(event, mote);
  card.appendChild(top);

  const chips = document.createElement("div");
  chips.className = "prop-chips";
  for (const prop of mote.properties) chips.appendChild(propChip(mote, prop));
  card.appendChild(chips);
  return card;
}

function propChip(mote, prop) {
  const p = plot();
  const chip = document.createElement("button");
  chip.type = "button";
  const plotted = p && p.series.some((s) => s.mote === mote.id && s.key === prop.key);
  const isX = p && p.x && p.x.mote === mote.id && p.x.key === prop.key;
  chip.className = `prop-chip${prop.free ? " free" : ""}${plotted ? " plotted" : ""}${isX ? " is-x" : ""}`;
  chip.dataset.mote = mote.id;
  chip.dataset.key = prop.key;
  chip.textContent = prop.key;
  chip.title = [
    prop.value === null ? "not solved yet" : `${fmt(prop.value)}${prop.unit ? ` ${prop.unit}` : ""}`,
    prop.free ? "the solver determines this" : "a given",
    "click: plot as y · alt-click: sweep as x · shift-click: pin as a knob",
  ].join("\n");
  chip.onclick = (event) => {
    if (event.altKey) setX(mote, prop);
    else if (event.shiftKey) pinKnob(mote, prop);
    else toggleSeries(mote, prop);
  };
  chip.oncontextmenu = (event) => propMenu(event, mote, prop);
  return chip;
}

// ---- acting on a property --------------------------------------------

function ensurePlot() {
  if (!plots.length) plots.push(newPlot());
  activePlot = Math.min(activePlot, plots.length - 1);
  return plots[activePlot];
}

function setX(mote, prop) {
  const p = ensurePlot();
  p.x = {
    mote: mote.id, key: prop.key,
    lo: prop.suggestLo, hi: prop.suggestHi, steps: 200,
  };
  if (p.data) p.data = null;
  renderAll();
  scheduleRun(p);
}

function toggleSeries(mote, prop) {
  const p = ensurePlot();
  if (p.mode === "correlate") {
    if (!p.xKey) p.xKey = prop.key;
    else p.yKey = prop.key;
    renderAll();
    scheduleRun(p);
    return;
  }
  const existing = p.series.find((s) => s.mote === mote.id && s.key === prop.key && !s.extra && !s.scenario);
  if (existing) p.series = p.series.filter((s) => s.id !== existing.id);
  else {
    p.series.push({
      id: uid(), mote: mote.id, key: prop.key,
      label: labelOf(mote.id, prop.key), scenario: null, extra: null, hidden: false,
    });
    // Sweeping something against itself draws a diagonal and teaches
    // nothing, so a first series suggests an x rather than becoming one.
    if (!p.x) {
      const other = mote.properties.find((q) => q.key !== prop.key && !q.free)
        || mote.properties.find((q) => q.key !== prop.key);
      if (other) setX(mote, other);
    }
  }
  renderAll();
  scheduleRun(p);
}

function pinKnob(mote, prop) {
  if (knobs.some((k) => k.mote === mote.id && k.key === prop.key)) return;
  if (knobs.length >= MAX_KNOBS) { toast(`${MAX_KNOBS} knobs is the most that fit the rail.`); return; }
  const value = prop.value === null ? 1 : prop.value;
  knobs.push({
    mote: mote.id, key: prop.key, label: labelOf(mote.id, prop.key),
    value, lo: prop.suggestLo, hi: prop.suggestHi,
  });
  renderAll();
  runAll();
}

// Inspect and Inspect-in-new-tab come from ContextMenu.inspectItems, so
// the first entries of a right-click menu read identically in all six
// windows. What follows them is this window's own.

function moteMenu(event, mote) {
  event.preventDefault();
  ContextMenu.open(event.clientX, event.clientY,
    ContextMenu.inspectItems(mote.id, { x: event.clientX, y: event.clientY }));
}

function propMenu(event, mote, prop) {
  event.preventDefault();
  const anchor = { x: event.clientX, y: event.clientY };
  ContextMenu.open(event.clientX, event.clientY, [
    ...ContextMenu.inspectItems(mote.id, anchor),
    "-",
    { label: `Plot ${prop.key} as y`, run: () => toggleSeries(mote, prop) },
    { label: `Sweep ${prop.key} as x`, run: () => setX(mote, prop) },
    { label: `Pin ${prop.key} as a knob`, run: () => pinKnob(mote, prop) },
  ]);
}

// ---- rendering: the right rail ---------------------------------------

function renderPanel() {
  const body = $("panel-body");
  body.textContent = "";
  const p = plot();

  body.appendChild(group("knobs", knobs.length
    ? knobs.map(knobRow)
    : [note("Shift-click a property in the left rail to pin it here. A knob overrides that value for every sweep and is never written to the garden.")]));

  if (board.scenarios && board.scenarios.length && p) body.appendChild(baselineGroup(p));

  body.appendChild(group("sensitivity", sensitivity.length
    ? sensitivity.map(sensRow)
    : [note(knobs.length
      ? "Hover a plot to rank the knobs by how much they move the value there."
      : "Pin a knob to see how much it moves the plotted value.")]));

  if (p) body.appendChild(group("run", runStats(p).map(statRow)));

  body.appendChild(group("saved plots", (board.plots || []).length
    ? (board.plots || []).map(savedRow)
    : [note("Save as mote keeps this plot's specification — not its points — in the garden, so reopening it re-solves what the garden says now.")]));
}

function group(label, children) {
  const wrap = document.createElement("div");
  wrap.className = "panel-group";
  wrap.appendChild(Object.assign(document.createElement("div"),
    { className: "panel-label", textContent: label }));
  for (const child of children) wrap.appendChild(child);
  return wrap;
}

function note(text) {
  return Object.assign(document.createElement("div"), { className: "card", textContent: text });
}

function knobRow(knob, index) {
  const row = document.createElement("div");
  row.className = "knob";
  const top = document.createElement("div");
  top.className = "knob-top";
  top.appendChild(Object.assign(document.createElement("span"),
    { className: "knob-name", textContent: knob.label }));
  top.appendChild(Object.assign(document.createElement("span"),
    { className: "knob-value", textContent: fmt(knob.value) }));
  const drop = document.createElement("button");
  drop.type = "button";
  drop.className = "knob-drop";
  drop.textContent = "×";
  drop.title = "Unpin";
  drop.onclick = () => { knobs.splice(index, 1); renderAll(); runAll(); };
  top.appendChild(drop);
  row.appendChild(top);

  const slider = document.createElement("input");
  slider.type = "range";
  slider.min = String(knob.lo);
  slider.max = String(knob.hi);
  slider.step = String(Math.abs(knob.hi - knob.lo) / 200 || 0.01);
  slider.value = String(knob.value);
  slider.oninput = () => {
    knob.value = parseFloat(slider.value);
    top.querySelector(".knob-value").textContent = fmt(knob.value);
    runAll();
  };
  row.appendChild(slider);
  return row;
}

function baselineGroup(p) {
  const select = document.createElement("select");
  select.style.width = "100%";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "— none —";
  select.appendChild(none);
  for (const scenario of board.scenarios) {
    const option = document.createElement("option");
    option.value = scenario.id;
    option.textContent = scenario.name;
    if (p.baseline === scenario.id) option.selected = true;
    select.appendChild(option);
  }
  select.onchange = () => {
    p.baseline = select.value || null;
    p.ghost = !!p.baseline;
    renderAll();
    scheduleRun(p);
  };
  return group("baseline", [
    select,
    note("A baseline is one of the garden's own scenarios. Its curve is drawn dashed beside each series, and the readout shows the difference."),
  ]);
}

function sensRow(row) {
  const line = document.createElement("div");
  line.className = "sens-row";
  line.appendChild(Object.assign(document.createElement("span"),
    { className: "sens-name", textContent: row.label || `${row.key}` }));
  const track = document.createElement("div");
  track.className = "sens-track";
  const fill = document.createElement("div");
  fill.className = "sens-fill";
  const widest = Math.max(...sensitivity.map((r) => Math.abs(r.value)), 1e-9);
  fill.style.width = `${(Math.abs(row.value) / widest) * 100}%`;
  fill.style.background = row.value >= 0 ? "var(--accent)" : "var(--danger)";
  track.appendChild(fill);
  line.appendChild(track);
  line.appendChild(Object.assign(document.createElement("span"), {
    className: "sens-value",
    textContent: `${row.value >= 0 ? "+" : "−"}${Math.abs(row.value).toFixed(2)}`,
  }));
  return line;
}

function runStats(p) {
  const cases = casesFor(p).length;
  const points = p.x ? p.x.steps + 1 : 0;
  const stats = [
    { label: "series", value: String(p.series.length) },
    { label: "solve cases", value: String(cases) },
    { label: "solves per sweep", value: p.mode === "sweep" ? String(cases * points) : "—" },
  ];
  if (p.data && p.data.seconds !== undefined) {
    stats.push({ label: "last sweep", value: `${p.data.seconds}s` });
  }
  if (p.data && p.data.failures) {
    stats.push({ label: "points that failed", value: String(p.data.failures), tone: "bad" });
  }
  if (p.data && p.data.pinned) {
    stats.push({ label: "x is a freedom", value: "pinned", tone: "warn" });
  }
  for (const broken of (p.data && p.data.brokenConstraints) || []) {
    stats.push({ label: broken, value: "broken", tone: "bad" });
  }
  return stats;
}

function statRow(stat) {
  const row = document.createElement("div");
  row.className = "stat-row";
  row.appendChild(Object.assign(document.createElement("span"),
    { className: "stat-label", textContent: stat.label }));
  row.appendChild(Object.assign(document.createElement("span"),
    { className: `stat-value ${stat.tone || ""}`, textContent: stat.value }));
  return row;
}

function savedRow(saved) {
  const row = document.createElement("div");
  row.className = "plot-row";
  const open = document.createElement("button");
  open.type = "button";
  open.textContent = saved.name;
  open.onclick = () => openSaved(saved);
  row.appendChild(open);
  const remove = document.createElement("button");
  remove.type = "button";
  remove.textContent = "×";
  remove.onclick = async () => {
    await call(api.delete_plot(saved.id));
    await refresh();
  };
  row.appendChild(remove);
  return row;
}

function scenarioName(id) {
  const found = (board.scenarios || []).find((s) => s.id === id);
  return found ? found.name : "baseline";
}

// ---- sensitivity ------------------------------------------------------

async function refreshSensitivity() {
  const p = plot();
  const first = p && p.series.find((s) => !s.hidden);
  if (!p || !first || !knobs.length || p.cursor === null || p.mode !== "sweep") {
    if (sensitivity.length) { sensitivity = []; renderPanel(); }
    return;
  }
  const rows = await call(api.sensitivity({
    x: p.x,
    output: { mote: first.mote, key: first.key },
    cases: casesFor(p).slice(0, 1),
    knobs: knobs.map((k) => ({ mote: k.mote, key: k.key, label: k.label, value: k.value })),
  }, p.cursor));
  sensitivity = rows || [];
  renderPanel();
}

// ---- saving and exporting --------------------------------------------

async function savePlot() {
  const p = plot();
  if (!p) return;
  const name = window.prompt("Name this plot", plotTitle(p));
  if (!name) return;
  const saved = await call(api.save_plot(name, {
    mode: p.mode, x: p.x, y: p.y, output: p.output,
    xKey: p.xKey, yKey: p.yKey, yScale: p.yScale,
    baseline: p.baseline, ghost: p.ghost,
    series: p.series.map((s) => ({
      mote: s.mote, key: s.key, label: s.label, scenario: s.scenario, extra: s.extra,
    })),
    knobs: knobs.map((k) => ({ mote: k.mote, key: k.key, value: k.value })),
  }, p.savedId || null));
  if (!saved) return;
  p.savedId = saved.id;
  toast(saved.created ? "Saved as a mote." : "Updated.");
  await refresh();
}

function openSaved(saved) {
  const spec = saved.plot || {};
  const p = newPlot(spec.x || null, (spec.series || []).map((s) => ({
    id: uid(), mote: s.mote, key: s.key,
    label: s.label || labelOf(s.mote, s.key),
    scenario: s.scenario || null, extra: s.extra || null, hidden: false,
  })));
  Object.assign(p, {
    mode: spec.mode || "sweep", y: spec.y || null, output: spec.output || null,
    xKey: spec.xKey, yKey: spec.yKey, yScale: spec.yScale || "linear",
    baseline: spec.baseline || null, ghost: !!spec.ghost, savedId: saved.id,
  });
  knobs = (spec.knobs || []).map((k) => {
    const prop = propOf(k.mote, k.key);
    return {
      mote: k.mote, key: k.key, label: labelOf(k.mote, k.key), value: k.value,
      lo: prop ? prop.suggestLo : k.value / 2, hi: prop ? prop.suggestHi : k.value * 2,
    };
  });
  plots.push(p);
  activePlot = plots.length - 1;
  renderAll();
  scheduleRun(p);
}

async function exportCsv(p) {
  if (!p.data) { toast("Nothing to export yet."); return; }
  if (p.mode === "correlate") {
    const rows = (p.data.dots || []).map((d) => [d.name, d.x, d.y]);
    await call(api.export_csv(["mote", p.xKey, p.yKey], rows, plotTitle(p)));
    return;
  }
  if (p.mode === "surface") {
    const rows = p.data.cells.map((c) => [c.x, c.y, c.v]);
    await call(api.export_csv([p.x.key, p.y.key, "value"], rows, plotTitle(p)));
    return;
  }
  const visible = p.series.filter((s) => !s.hidden);
  const columns = [p.x.key, ...visible.map((s) => s.label)];
  const rows = p.data.points.map((point) =>
    [point.x, ...visible.map((s) => {
      const value = point.v[s.id];
      return value === undefined ? "" : value;
    })]);
  const result = await call(api.export_csv(columns, rows, plotTitle(p)));
  if (result && result.path) toast(`Wrote ${result.path}`);
}

// The SVG is what the plot *is*, so the image is made from it rather
// than from a second drawing that could drift out of step with it.
async function exportPng(p, cell) {
  const svg = cell.querySelector("svg");
  if (!svg) { toast("Nothing to export yet."); return; }
  const rect = cell.getBoundingClientRect();
  const clone = svg.cloneNode(true);
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  clone.setAttribute("width", rect.width);
  clone.setAttribute("height", rect.height);
  inlineColors(clone);
  const blob = new Blob(
    [`<svg xmlns="http://www.w3.org/2000/svg" width="${rect.width}" height="${rect.height}">`
      + `<rect width="100%" height="100%" fill="${cssValue("--plot-bg")}"/>`
      + clone.innerHTML + "</svg>"],
    { type: "image/svg+xml" });
  const url = URL.createObjectURL(blob);
  const image = new Image();
  image.onload = async () => {
    const canvas = document.createElement("canvas");
    canvas.width = rect.width * 2;
    canvas.height = rect.height * 2;
    const ctx = canvas.getContext("2d");
    ctx.scale(2, 2);
    ctx.drawImage(image, 0, 0);
    URL.revokeObjectURL(url);
    const result = await call(api.export_png(canvas.toDataURL("image/png"), plotTitle(p)));
    if (result && result.path) toast(`Wrote ${result.path}`);
  };
  image.onerror = () => { URL.revokeObjectURL(url); toast("Could not render the image.", true); };
  image.src = url;
}

// A serialized SVG leaves the document, and with it every CSS variable
// it referred to -- so the variables are resolved before it goes.
function cssValue(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function inlineColors(node) {
  for (const attr of ["fill", "stroke"]) {
    const value = node.getAttribute && node.getAttribute(attr);
    if (value && value.startsWith("var(")) {
      node.setAttribute(attr, cssValue(value.slice(4, -1).trim()));
    }
  }
  for (const child of node.children || []) inlineColors(child);
}

// ---- the session -----------------------------------------------------
//
// A grid of plots takes real effort to set up, so it survives closing
// the window -- per garden, since a mote id means nothing in another
// one. What is kept is the *specification* only, never the points: they
// are re-solved on open, so a session reopened after the garden changed
// shows what the garden says now rather than what it said last night.
// Saving a plot as a mote (save_plot) is the durable, shareable version
// of the same idea; this is just where you left off.

function sessionKey() { return `moxie.session.${board ? board.garden : "?"}`; }

function serializePlot(p) {
  return {
    mode: p.mode, x: p.x, y: p.y, output: p.output,
    xKey: p.xKey, yKey: p.yKey, yScale: p.yScale,
    baseline: p.baseline, ghost: p.ghost, savedId: p.savedId,
    series: p.series.map((s) => ({
      mote: s.mote, key: s.key, label: s.label,
      scenario: s.scenario, extra: s.extra, hidden: s.hidden,
    })),
  };
}

function saveSession() {
  if (!board || !sessionReady) return;
  try {
    localStorage.setItem(sessionKey(), JSON.stringify({
      plots: plots.map(serializePlot),
      knobs: knobs.map((k) => ({ mote: k.mote, key: k.key, value: k.value })),
      active: activePlot,
    }));
  } catch (error) {
    // Storage can be unavailable or full. A forgotten layout is a
    // disappointment, not a failure, and must never break the window.
  }
}

// A session can outlive the motes it names -- something was deleted or
// renamed in another window between sessions. Anything that no longer
// resolves is dropped quietly rather than drawn as a plot that cannot
// solve, which would look like a bug in the solver.
function restoreSession() {
  let saved = null;
  try { saved = JSON.parse(localStorage.getItem(sessionKey()) || "null"); } catch (error) { return false; }
  if (!saved || !Array.isArray(saved.plots)) return false;

  const alive = (moteId, key) => !!propOf(moteId, key);
  plots = saved.plots.map((spec) => {
    const p = newPlot(spec.x && alive(spec.x.mote, spec.x.key) ? spec.x : null,
      (spec.series || []).filter((s) => alive(s.mote, s.key)).map((s) => ({
        id: uid(), mote: s.mote, key: s.key,
        label: s.label || labelOf(s.mote, s.key),
        scenario: s.scenario || null, extra: s.extra || null, hidden: !!s.hidden,
      })));
    return Object.assign(p, {
      mode: spec.mode || "sweep", y: spec.y || null, output: spec.output || null,
      xKey: spec.xKey, yKey: spec.yKey, yScale: spec.yScale || "linear",
      baseline: spec.baseline || null, ghost: !!spec.ghost, savedId: spec.savedId,
    });
  }).filter((p) => p.x || p.series.length || p.xKey);

  knobs = (saved.knobs || []).filter((k) => alive(k.mote, k.key)).map((k) => {
    const prop = propOf(k.mote, k.key);
    return {
      mote: k.mote, key: k.key, label: labelOf(k.mote, k.key), value: k.value,
      lo: prop.suggestLo, hi: prop.suggestHi,
    };
  });
  activePlot = Math.max(0, Math.min(saved.active || 0, plots.length - 1));
  return plots.length > 0;
}

// ---- gardens, launcher, wiring ---------------------------------------

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
      plots = [];
      knobs = [];
      sensitivity = [];
      // A garden switch re-reads that garden's own session.
      sessionReady = false;
      await refresh();
      const carried = restoreSession();
      sessionReady = true;
      if (carried) { renderAll(); runAll(); } else { saveSession(); }
    };
    menu.appendChild(btn);
  }
  const manage = document.createElement("button");
  manage.type = "button";
  manage.textContent = "Manage gardens…";
  manage.onclick = async () => { menu.classList.add("hidden"); await call(api.open_gardens()); };
  menu.appendChild(manage);
  menu.classList.remove("hidden");
}

function wire() {
  $("garden-btn").onclick = openGardenMenu;
  document.addEventListener("mousedown", (event) => {
    const menu = $("garden-menu");
    if (!menu.classList.contains("hidden") && !menu.contains(event.target)
        && event.target !== $("garden-btn")) {
      menu.classList.add("hidden");
    }
  });

  $("mote-search").oninput = (event) => { search = event.target.value; renderRail(); };
  $("add-plot-btn").onclick = () => {
    const current = plot();
    plots.push(newPlot(current && current.x ? { ...current.x } : null, []));
    activePlot = plots.length - 1;
    renderAll();
  };
  $("save-plot-btn").onclick = savePlot;
  $("refresh-btn").onclick = async () => {
    await call(api.refresh());
    await refresh();
  };
  $("switch-app-btn").onclick = Launcher.open;

  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(renderGrid, 120);
  });

  renderModeChips();
}

function renderModeChips() {
  const host = $("mode-chips");
  host.textContent = "";
  for (const [id, label] of [["sweep", "Sweep"], ["surface", "Surface"], ["correlate", "Correlate"]]) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.textContent = label;
    const p = plot();
    btn.className = p && p.mode === id ? "on" : "";
    btn.onclick = () => {
      const current = ensurePlot();
      current.mode = id;
      current.data = null;
      if (id === "surface" && !current.y && current.x) current.y = { ...current.x, steps: 24 };
      if (id === "surface" && !current.output && current.series.length) {
        current.output = { mote: current.series[0].mote, key: current.series[0].key };
      }
      if (id === "correlate" && !current.xKey && current.x) current.xKey = current.x.key;
      if (id === "correlate" && !current.yKey && current.series.length) {
        current.yKey = current.series[0].key;
      }
      renderAll();
      scheduleRun(current);
    };
    host.appendChild(btn);
  }
}

function renderAll() {
  saveSession();
  $("garden-name").textContent = board ? board.garden : "…";
  $("run-label").textContent = runLabel();
  renderModeChips();
  renderRail();
  renderGrid();
  renderPanel();
}

function runLabel() {
  const p = plot();
  if (!p || !p.x) return "";
  const cases = casesFor(p).length;
  const points = p.x.steps + 1;
  if (p.mode !== "sweep") return `${p.mode} · ${cases} case${cases === 1 ? "" : "s"}`;
  return `${cases} case${cases === 1 ? "" : "s"} × ${points} points = ${cases * points} solves`;
}

async function refresh() {
  const next = await call(api.board());
  if (!next) return;
  board = next;
  renderAll();
}

async function init() {
  api = window.pywebview.api;
  Inspector.install({
    api,
    onChanged: () => { refresh().then(runAll); },
    onOpenMora: () => toast("Opening Mora…"),
  });
  CommandBar.install({
    api,
    onChanged: () => { refresh().then(runAll); },
    onNavigate: (moteId) => Inspector.open(moteId),
  });
  Undo.install({ api, toast, onChanged: () => { refresh().then(runAll); } });
  Launcher.install({
    api,
    thisApp: THIS_APP,
    getGardenName: () => board ? board.garden : "",
    getFocus: () => { const p = plot(); return p && p.x ? p.x.mote : null; },
    onError: (message) => toast(message, true),
  });
  wire();
  await refresh();
  // A focused mote is an explicit instruction from the command line and
  // outranks where you happened to leave off.
  if (board && board.focus) {
    const mote = moteById(board.focus);
    if (mote && mote.properties.length) {
      const y = mote.properties.find((p) => p.free) || mote.properties[0];
      sessionReady = true;
      toggleSeries(mote, y);
      return;
    }
  }
  const restored = restoreSession();
  sessionReady = true;
  if (restored) { renderAll(); runAll(); } else { saveSession(); }
}

window.addEventListener("pywebviewready", init);
