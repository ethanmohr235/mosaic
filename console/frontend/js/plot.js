/* The plot renderer: hand-written SVG, no library. Pure -- render() is
 * handed a finished model and owns nothing beyond the node it draws into.
 * What to draw (which series, which pass of the refinement) is the view's
 * business (views/moxie-plot.js); the figure itself comes from
 * core/plotview.py.
 *
 * Ported from frontend_shared/plot.js (which Mora's old window keeps until
 * U6) and restyled to the handoff (docs/design/mosaic-console/README.md →
 * Plot): a 1px --line left and bottom axis, a --track grid, 1.5px series in
 * --s1…--s6, --dim tick labels with the y labels right-aligned, and a
 * dashed --fault edge where the answer breaks one of the garden's own
 * constraints. Every colour is a token.
 *
 * Conventions that are not arbitrary:
 *   * A gap in a line is a real gap: points that failed to solve are never
 *     interpolated across (segmentsOf).
 *   * Marks that overlap carry a ring in --bg, so a dot on a line reads as
 *   * a dot.
 *   * Colour never encodes rank: a series keeps its colour when its
 *     neighbours are hidden, because the view assigns it by series id. */

const NS = "http://www.w3.org/2000/svg";
const PAD = { left: 64, right: 16, top: 12, bottom: 40 };
const RIGHT_AXIS = 56; // a second y axis's labels, only when there is one
export const SERIES = 6; // --s1…--s6; a 7th series repeats hue 1 dashed
const DASHES = [null, "6 3", "2 3", "8 3 2 3"];

function el(name, attrs) {
  const node = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
}

function div(className, text) {
  const node = document.createElement("div");
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

/** A series' stroke: its colour by index in the full legend (hidden ones
 * included), and a dash once the six hues are used up. */
export function seriesStyle(index) {
  return {
    color: `var(--s${(index % SERIES) + 1})`,
    dash: DASHES[Math.floor(index / SERIES) % DASHES.length],
  };
}

// ---- numbers -------------------------------------------------------------

/** A value with enough figures to tell two points apart, never a wall of digits. */
export function fmt(v) {
  if (v === null || v === undefined || !isFinite(v)) return "—";
  const a = Math.abs(v);
  if (a === 0) return "0";
  if (a >= 1e6 || a < 1e-4) return v.toExponential(2).replace("e+", "e");
  if (a >= 100) return v.toFixed(1).replace(/\.0$/, "");
  if (a >= 10) return v.toFixed(2).replace(/0$/, "").replace(/\.$/, "");
  return v.toPrecision(3).replace(/(\.\d*?)0+$/, "$1").replace(/\.$/, "");
}

// Every tick on an axis is written to the same number of decimals, taken
// from the step, so a row never reads "50.0, 100, 150".
function tickFormatter(values) {
  if (values.length < 2) return fmt;
  const step = Math.abs(values[1] - values[0]);
  if (!isFinite(step) || step === 0) return fmt;
  const big = Math.max(...values.map(Math.abs));
  if (big >= 1e6 || (big > 0 && big < 1e-4)) return (v) => v.toExponential(1).replace("e+", "e");
  const decimals = Math.max(0, Math.min(6, Math.ceil(-Math.log10(step))));
  return (v) => v.toFixed(decimals);
}

/** Ticks on a 1-2-5 step: a gridline at 0.847 is arithmetic showing through. */
export function ticks(lo, hi, count) {
  if (!(hi > lo)) return [lo];
  const raw = (hi - lo) / Math.max(1, count);
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) {
    out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  }
  return out.length >= 2 ? out : [lo, hi];
}

function logTicks(lo, hi) {
  const out = [];
  for (let e = Math.floor(Math.log10(Math.max(lo, 1e-12))); e <= Math.ceil(Math.log10(Math.max(hi, 1e-11))); e++) {
    const v = Math.pow(10, e);
    if (v >= lo && v <= hi) out.push(v);
  }
  return out.length >= 2 ? out : [lo, hi];
}

// ---- geometry ------------------------------------------------------------

export function geometry(model) {
  const right = PAD.right + ((model.bands || []).length > 1 ? RIGHT_AXIS : 0);
  const w = Math.max(160, model.width);
  const h = Math.max(120, model.height);
  return {
    w, h, left: PAD.left, top: PAD.top, right: w - right, bottom: h - PAD.bottom,
    pw: w - PAD.left - right, ph: h - PAD.top - PAD.bottom,
  };
}

export function scaleX(g, lo, hi) {
  const span = hi - lo || 1;
  const X = (x) => g.left + ((x - lo) / span) * g.pw;
  X.invert = (px) => lo + ((px - g.left) / g.pw) * span;
  return X;
}

function scaleY(g, band, log) {
  if (log) {
    const lo = Math.log10(Math.max(band.lo, 1e-12));
    const hi = Math.log10(Math.max(band.hi, band.lo * 1.2 || 1e-11));
    const span = hi - lo || 1;
    return (v) => g.bottom - ((Math.log10(Math.max(v, 1e-12)) - lo) / span) * g.ph;
  }
  const span = band.hi - band.lo || 1;
  return (v) => g.bottom - ((v - band.lo) / span) * g.ph;
}

// Runs of consecutive points that solved. A hole is a hole.
function segmentsOf(points) {
  const runs = [];
  let run = [];
  for (const p of points) {
    if (p && isFinite(p.y)) run.push(p);
    else if (run.length) { runs.push(run); run = []; }
  }
  if (run.length) runs.push(run);
  return runs;
}

function pathOf(run, X, Y) {
  return run.map((p, i) => `${i ? "L" : "M"}${X(p.x).toFixed(1)} ${Y(p.y).toFixed(1)}`).join(" ");
}

// ---- the frame -----------------------------------------------------------

function drawFrame(svg, g, X, xt, band) {
  for (const v of xt) {
    svg.appendChild(el("line", { x1: X(v), y1: g.top, x2: X(v), y2: g.bottom, stroke: "var(--track)", "stroke-width": 1 }));
  }
  for (const v of band.tickValues) {
    svg.appendChild(el("line", { x1: g.left, y1: band.Y(v), x2: g.right, y2: band.Y(v), stroke: "var(--track)", "stroke-width": 1 }));
  }
  svg.appendChild(el("line", { x1: g.left, y1: g.bottom + 0.5, x2: g.right, y2: g.bottom + 0.5, stroke: "var(--line)", "stroke-width": 1 }));
  svg.appendChild(el("line", { x1: g.left - 0.5, y1: g.top, x2: g.left - 0.5, y2: g.bottom, stroke: "var(--line)", "stroke-width": 1 }));
}

function defs(svg) {
  const d = el("defs", {});
  // A hatch, so "no answer here" never reads as a coloured region of data.
  const hatch = el("pattern", { id: "plot-hatch", width: 6, height: 6, patternUnits: "userSpaceOnUse", patternTransform: "rotate(45)" });
  hatch.appendChild(el("line", { x1: 0, y1: 0, x2: 0, y2: 6, stroke: "var(--fault)", "stroke-width": 1, opacity: 0.45 }));
  d.appendChild(hatch);
  svg.appendChild(d);
}

function drawSpans(svg, g, X, spans, kind) {
  for (const span of spans || []) {
    const x0 = X(span.x0), x1 = X(span.x1);
    const rect = el("rect", {
      x: x0, y: g.top, width: Math.max(1.5, x1 - x0), height: g.ph, "pointer-events": "none",
      class: `plot-${kind}`,
      fill: kind === "failed" ? "url(#plot-hatch)" : "color-mix(in oklch, var(--fault) 10%, transparent)",
    });
    const title = el("title", {});
    title.textContent = span.message;
    rect.appendChild(title);
    svg.appendChild(rect);
    if (kind === "violated") {
      for (const x of [x0, x1]) {
        svg.appendChild(el("line", { x1: x, y1: g.top, x2: x, y2: g.bottom, stroke: "var(--fault)", "stroke-width": 1, "stroke-dasharray": "4 4" }));
      }
    }
  }
}

// ---- labels (HTML, so they ellipsize) -----------------------------------

function label(layer, text, style, className = "plot-tick") {
  const t = div(className, text);
  Object.assign(t.style, style);
  layer.appendChild(t);
  return t;
}

function drawLabels(host, g, X, xt, bands, xLabel) {
  const layer = div("plot-labels");
  const xFormat = tickFormatter(xt);
  for (const v of xt) {
    label(layer, xFormat(v), { left: `${X(v) - 40}px`, top: `${g.bottom + 4}px`, width: "80px", textAlign: "center" });
  }
  bands.forEach((band, i) => {
    const yFormat = tickFormatter(band.tickValues);
    for (const v of band.tickValues) {
      label(layer, yFormat(v), i === 0
        ? { left: "0", top: `${band.Y(v) - 9}px`, width: `${g.left - 6}px`, textAlign: "right" }
        : { left: `${g.right + 6}px`, top: `${band.Y(v) - 9}px`, width: `${RIGHT_AXIS - 8}px`, textAlign: "left" });
    }
  });
  label(layer, xLabel || "", { left: `${g.left}px`, top: `${g.bottom + 20}px`, width: `${g.pw}px`, textAlign: "center" }, "plot-title");
  label(layer, bands[0].label || "", { left: `${g.left + 6}px`, top: `${g.top}px`, width: `${g.pw / 2}px` }, "plot-title");
  if (bands[1]) {
    label(layer, bands[1].label || "", { left: `${g.left + g.pw / 2}px`, top: `${g.top}px`, width: `${g.pw / 2 - 6}px`, textAlign: "right" }, "plot-title");
  }
  host.appendChild(layer);
}

// ---- modes ---------------------------------------------------------------

function renderLines(host, svg, g, model) {
  const X = scaleX(g, model.xLo, model.xHi);
  const log = model.yScale === "log";
  const bands = model.bands.length ? model.bands.map((b) => ({
    ...b, Y: scaleY(g, b, log), tickValues: log ? logTicks(b.lo, b.hi) : ticks(b.lo, b.hi, 3),
  })) : [{ lo: 0, hi: 1, label: "", Y: scaleY(g, { lo: 0, hi: 1 }, false), tickValues: [0, 1] }];
  const xt = ticks(model.xLo, model.xHi, g.pw < 360 ? 3 : 4);
  drawFrame(svg, g, X, xt, bands[0]);
  drawSpans(svg, g, X, model.failedSpans, "failed");
  drawSpans(svg, g, X, model.violatedSpans, "violated");

  for (const s of model.series) {
    if (s.hidden) continue;
    const Y = (bands[s.band || 0] || bands[0]).Y;
    for (const run of segmentsOf(s.points)) {
      if (run.length === 1) {
        svg.appendChild(el("circle", { cx: X(run[0].x), cy: Y(run[0].y), r: 2, fill: s.color }));
        continue;
      }
      svg.appendChild(el("path", {
        d: pathOf(run, X, Y), fill: "none", stroke: s.color, "stroke-width": 1.5,
        "stroke-linejoin": "round", class: "plot-series", "data-series": s.id,
        ...(s.dash ? { "stroke-dasharray": s.dash } : {}),
      }));
    }
  }

  if (model.cursor && isFinite(model.cursor.x)) {
    const cx = X(model.cursor.x);
    svg.appendChild(el("line", { x1: cx, y1: g.top, x2: cx, y2: g.bottom, stroke: "var(--line-strong)", "stroke-width": 1, class: "plot-cursor" }));
    for (const row of model.cursor.rows || []) {
      if (row.value === null || row.value === undefined) continue;
      const Y = (bands[row.band || 0] || bands[0]).Y;
      svg.appendChild(el("circle", { cx, cy: Y(row.value), r: 3, fill: row.color, stroke: "var(--bg)", "stroke-width": 2 }));
    }
  }
  if (model.mark !== null && model.mark !== undefined && isFinite(model.mark)) {
    const mx = X(model.mark);
    svg.appendChild(el("line", { x1: mx, y1: g.top, x2: mx, y2: g.bottom, stroke: "var(--fg)", "stroke-width": 1, "stroke-dasharray": "2 3", class: "plot-mark" }));
  }
  drawLabels(host, g, X, xt, bands, model.xLabel);
  return X;
}

function renderSurface(host, svg, g, model) {
  const X = scaleX(g, model.xLo, model.xHi);
  const band = { lo: model.yLo, hi: model.yHi, label: model.yLabel, Y: scaleY(g, { lo: model.yLo, hi: model.yHi }, false) };
  band.tickValues = ticks(band.lo, band.hi, 3);
  const span = (model.hi ?? 1) - (model.lo ?? 0) || 1;
  const cw = g.pw / Math.max(1, model.cols);
  const ch = g.ph / Math.max(1, model.rows);
  for (const c of model.cells) {
    // One hue, dark to light: a magnitude has no categories in it, so the
    // ramp invents no boundaries either.
    const t = c.v === null || c.v === undefined ? null : Math.pow(Math.max(0, (c.v - model.lo) / span), 0.6);
    const rect = el("rect", {
      x: g.left + c.c * cw, y: g.bottom - (c.r + 1) * ch, width: Math.ceil(cw) + 0.5, height: Math.ceil(ch) + 0.5,
      fill: t === null ? "url(#plot-hatch)" : `color-mix(in oklch, var(--s1) ${(8 + 92 * t).toFixed(1)}%, var(--bg))`,
      class: "plot-cell",
    });
    const title = el("title", {});
    title.textContent = c.error ? `${fmt(c.x)}, ${fmt(c.y)} — ${c.error}` : `${fmt(c.x)}, ${fmt(c.y)}: ${fmt(c.v)}`;
    rect.appendChild(title);
    svg.appendChild(rect);
  }
  const xt = ticks(model.xLo, model.xHi, 4);
  drawFrame(svg, g, X, [], { tickValues: [] });
  drawLabels(host, g, X, xt, [band], model.xLabel);
  return X;
}

function renderDots(host, svg, g, model) {
  const X = scaleX(g, model.xLo, model.xHi);
  const log = model.yScale === "log";
  const band = { lo: model.yLo, hi: model.yHi, label: model.yLabel, Y: scaleY(g, { lo: model.yLo, hi: model.yHi }, log) };
  band.tickValues = log ? logTicks(band.lo, band.hi) : ticks(band.lo, band.hi, 3);
  const xt = ticks(model.xLo, model.xHi, 4);
  drawFrame(svg, g, X, xt, band);
  const layer = div("plot-labels");
  const placed = [];
  for (const dot of model.dots) {
    const cx = X(dot.x), cy = band.Y(dot.y);
    const circle = el("circle", { cx, cy, r: 4, fill: dot.color, stroke: "var(--bg)", "stroke-width": 2, class: "plot-dot" });
    const title = el("title", {});
    title.textContent = `${dot.label}: ${fmt(dot.x)}, ${fmt(dot.y)}`;
    circle.appendChild(title);
    svg.appendChild(circle);
    // Direct labels, nudged down until they stop colliding: a scatter has
    // no legend to lean on.
    const width = dot.label.length * 7.4 + 4;
    const flip = cx + 10 + width > g.right;
    const left = flip ? cx - 10 - width : cx + 8;
    let top = cy - 9;
    for (let guard = 0; guard < 16 && placed.some((p) => Math.abs(p.top - top) < 16
      && left < p.left + p.width + 6 && p.left < left + width + 6); guard++) top += 16;
    top = Math.min(g.bottom - 16, Math.max(g.top, top));
    placed.push({ top, left, width });
    label(layer, dot.label, { left: `${left}px`, top: `${top}px`, width: `${width}px`, textAlign: flip ? "right" : "left" }, "plot-dot-label");
  }
  host.appendChild(layer);
  drawLabels(host, g, X, xt, [band], model.xLabel);
  return X;
}

/** Draw `model` into `host` (position: relative). Returns {g, X} so the
 * view can map a mouse position to an x. */
export function render(host, model) {
  host.replaceChildren();
  const g = geometry(model);
  const svg = el("svg", { width: g.w, height: g.h, class: "plot-svg" });
  defs(svg);
  let X;
  if (model.mode === "surface") X = renderSurface(host, svg, g, model);
  else if (model.mode === "correlate") X = renderDots(host, svg, g, model);
  else X = renderLines(host, svg, g, model);
  host.prepend(svg);
  return { g, X };
}
