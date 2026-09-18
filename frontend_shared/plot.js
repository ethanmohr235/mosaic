// frontend_shared/plot.js
//
// The drawing. Hand-written SVG, no library -- the same call Monarch's
// graph and Motion's board make, and for the same reason: what these
// windows draw is specific enough that a charting library would be
// configured into submission rather than used.
//
// Pure with respect to the app: render() is handed a finished model and
// owns no state beyond the node it draws into. Everything that decides
// *what* to draw -- which series exist, what they cost, which pass of a
// refinement produced them -- is the caller's business. That split is
// what lets the same renderer draw a sweep, a surface and a correlation
// without knowing that a sweep is a simulation at all.
//
// Shared, and symlinked into two frontends, for the reason
// docs/architecture.md gives about the inspector: a figure in a Mora
// note and the same view open in Moxie are the same picture, and two
// renderers would drift into being two pictures. Moxie builds its model
// in JavaScript from a live grid it is dragging a cursor across; Mora's
// arrives finished from core/plotview.py. Neither difference reaches
// here -- this takes a model, and a model is all it takes.
//
// Conventions that are not arbitrary, and should survive edits:
//
//   * 2px series strokes, recessive grid, axis ink well below series ink.
//   * A gap in a line is a real gap. Points that failed to solve are
//     never interpolated across -- see segmentsOf().
//   * Marks that overlap carry a 2px ring in the plot's own background
//     colour, so a dot on top of a line still reads as a dot.
//   * Colour never encodes rank. A series keeps its hue when its
//     neighbours are switched off, because app.js assigns by stable id.

const Plot = (() => {
  const NS = "http://www.w3.org/2000/svg";
  const PAD = { left: 62, right: 18, top: 16, bottom: 34 };
  // Enough room for a second axis's labels when a plot carries two
  // dimensions; zero when it carries one, so the common case is not
  // paying for the uncommon one.
  const RIGHT_AXIS = 46;

  const el = (name, attrs) => {
    const node = document.createElementNS(NS, name);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    return node;
  };

  const div = (className, text) => {
    const node = document.createElement("div");
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  };

  // ---- numbers ---------------------------------------------------------

  // A value as a reader wants it: enough significant figures to tell two
  // points apart, never so many that an axis becomes a wall of digits.
  function fmt(v) {
    if (v === null || v === undefined || !isFinite(v)) return "—";
    const a = Math.abs(v);
    if (a === 0) return "0";
    if (a >= 1e6 || a < 1e-4) return v.toExponential(1).replace("e+", "e");
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    if (a >= 1) return v.toFixed(2);
    return v.toPrecision(3).replace(/0+$/, "").replace(/\.$/, "");
  }

  // Tick values on a "nice" step (1, 2 or 5 times a power of ten) rather
  // than range/n. A gridline at 0.847 is arithmetic showing through.
  // Every tick on an axis is written to the same number of decimals --
  // taken from the step, not from each value. Otherwise a row reads
  // "50.0, 100, 150, 200", where the lone decimal looks like precision
  // rather than like formatting.
  function tickFormatter(values) {
    if (values.length < 2) return fmt;
    const step = Math.abs(values[1] - values[0]);
    if (!isFinite(step) || step === 0) return fmt;
    const big = Math.max(...values.map(Math.abs));
    if (big >= 1e6 || (big > 0 && big < 1e-4)) return (v) => v.toExponential(1).replace("e+", "e");
    const decimals = Math.max(0, Math.min(6, Math.ceil(-Math.log10(step)) + 1));
    return (v) => v.toFixed(decimals);
  }

  function ticks(lo, hi, count) {
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
    const from = Math.floor(Math.log10(Math.max(lo, 1e-12)));
    const to = Math.ceil(Math.log10(Math.max(hi, 1e-11)));
    for (let e = from; e <= to; e++) {
      const v = Math.pow(10, e);
      if (v >= lo && v <= hi) out.push(v);
    }
    return out.length >= 2 ? out : [lo, hi];
  }

  // ---- geometry --------------------------------------------------------

  function geometry(model) {
    const right = PAD.right + (model.bands && model.bands.length > 1 ? RIGHT_AXIS : 0);
    const w = Math.max(200, model.width);
    const h = Math.max(160, model.height);
    return {
      w, h,
      left: PAD.left, top: PAD.top,
      right: w - right, bottom: h - PAD.bottom,
      pw: w - PAD.left - right, ph: h - PAD.top - PAD.bottom,
    };
  }

  function scaleX(g, lo, hi) {
    const span = hi - lo || 1;
    return (x) => g.left + ((x - lo) / span) * g.pw;
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

  // ---- lines -----------------------------------------------------------

  // Split a series into runs of consecutive points that actually solved.
  // A hole is a hole: joining across one would draw a straight segment
  // through a region where the model has no answer, which is a lie the
  // eye believes completely.
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
    return run
      .map((p, i) => `${i ? "L" : "M"}${X(p.x).toFixed(1)} ${Y(p.y).toFixed(1)}`)
      .join(" ");
  }

  // ---- the frame -------------------------------------------------------

  function drawFrame(svg, g, model, X, bands) {
    const xt = ticks(model.xLo, model.xHi, g.pw < 380 ? 4 : 7);
    for (const v of xt) {
      svg.appendChild(el("line", {
        x1: X(v), y1: g.top, x2: X(v), y2: g.bottom,
        stroke: "var(--grid)", "stroke-width": 1,
      }));
    }
    const primary = bands[0];
    for (const v of primary.tickValues) {
      svg.appendChild(el("line", {
        x1: g.left, y1: primary.Y(v), x2: g.right, y2: primary.Y(v),
        stroke: "var(--grid)", "stroke-width": 1,
      }));
    }
    svg.appendChild(el("line", {
      x1: g.left, y1: g.bottom, x2: g.right, y2: g.bottom,
      stroke: "var(--grid-strong)", "stroke-width": 1,
    }));
    svg.appendChild(el("line", {
      x1: g.left, y1: g.top, x2: g.left, y2: g.bottom,
      stroke: "var(--grid-strong)", "stroke-width": 1,
    }));
    return xt;
  }

  // Regions where a solve failed, or where the answer broke one of the
  // garden's own constraints. Drawn behind the series, never over them.
  function drawSpans(svg, g, spans, fill, pattern) {
    for (const span of spans) {
      const rect = el("rect", {
        x: span.x0, y: g.top, width: Math.max(1.5, span.x1 - span.x0), height: g.ph,
        fill, "pointer-events": "none",
        class: pattern ? "failed-span" : "violated-span",
      });
      if (pattern) rect.setAttribute("fill", `url(#${pattern})`);
      const title = el("title", {});
      title.textContent = span.message;
      rect.appendChild(title);
      svg.appendChild(rect);
    }
  }

  function defs(svg) {
    const d = el("defs", {});
    // A hatch, so a failed span reads as "no answer here" rather than as
    // a coloured region that might itself be data.
    const hatch = el("pattern", {
      id: "mx-hatch", width: 7, height: 7, patternUnits: "userSpaceOnUse",
      patternTransform: "rotate(45)",
    });
    hatch.appendChild(el("rect", { width: 7, height: 7, fill: "oklch(0.65 0.15 25 / 0.07)" }));
    hatch.appendChild(el("line", {
      x1: 0, y1: 0, x2: 0, y2: 7, stroke: "oklch(0.65 0.15 25 / 0.35)", "stroke-width": 1.5,
    }));
    d.appendChild(hatch);
    svg.appendChild(d);
  }

  // ---- axis labels (HTML, so they can wrap and ellipsize) --------------

  function drawAxisLabels(host, g, model, X, bands, xt) {
    const layer = div("axis-layer");
    Object.assign(layer.style, {
      position: "absolute", inset: "0", pointerEvents: "none",
      fontFamily: "var(--mono)", fontSize: "9.5px", color: "var(--faint)",
    });
    const xFormat = tickFormatter(xt);
    for (const v of xt) {
      const t = div(null, xFormat(v));
      Object.assign(t.style, {
        position: "absolute", left: `${X(v)}px`, top: `${g.bottom + 9}px`,
        width: "70px", marginLeft: "-35px", textAlign: "center", lineHeight: "1",
      });
      layer.appendChild(t);
    }
    bands.forEach((band, i) => {
      const yFormat = tickFormatter(band.tickValues);
      for (const v of band.tickValues) {
        const t = div(null, yFormat(v));
        Object.assign(t.style, {
          position: "absolute", top: `${band.Y(v) - 5}px`, lineHeight: "1",
          ...(i === 0
            ? { left: "0", width: `${g.left - 8}px`, textAlign: "right" }
            : { left: `${g.right + 8}px`, width: `${RIGHT_AXIS - 10}px`, textAlign: "left" }),
          color: bands.length > 1 ? band.color : "var(--faint)",
        });
        layer.appendChild(t);
      }
    });

    const xTitle = div(null, model.xLabel || "");
    Object.assign(xTitle.style, {
      position: "absolute", left: `${g.left}px`, top: `${g.h - 14}px`,
      width: `${g.pw}px`, textAlign: "center", fontSize: "10px", color: "var(--muted)",
    });
    layer.appendChild(xTitle);

    const yTitle = div(null, bands[0].label);
    Object.assign(yTitle.style, {
      position: "absolute", left: "2px", top: `${g.top}px`, height: `${g.ph}px`, width: "14px",
      display: "flex", alignItems: "center", justifyContent: "center",
      writingMode: "vertical-rl", transform: "rotate(180deg)",
      fontSize: "10px", color: bands.length > 1 ? bands[0].color : "var(--muted)",
      whiteSpace: "nowrap",
    });
    layer.appendChild(yTitle);

    if (bands.length > 1) {
      const y2 = div(null, bands[1].label);
      Object.assign(y2.style, {
        position: "absolute", right: "2px", top: `${g.top}px`, height: `${g.ph}px`, width: "14px",
        display: "flex", alignItems: "center", justifyContent: "center",
        writingMode: "vertical-rl", fontSize: "10px", color: bands[1].color, whiteSpace: "nowrap",
      });
      layer.appendChild(y2);
    }
    host.appendChild(layer);
  }

  // ---- modes -----------------------------------------------------------

  function renderLines(host, svg, g, model) {
    const X = scaleX(g, model.xLo, model.xHi);
    const bands = model.bands.map((b, i) => {
      const Y = scaleY(g, b, model.yScale === "log");
      return {
        ...b, Y,
        tickValues: model.yScale === "log" ? logTicks(b.lo, b.hi) : ticks(b.lo, b.hi, 5),
        color: model.bands.length > 1 ? (i === 0 ? "var(--muted)" : "var(--accent)") : "var(--muted)",
      };
    });
    const xt = drawFrame(svg, g, model, X, bands);

    drawSpans(svg, g, (model.failedSpans || []).map((s) => ({
      x0: X(s.x0), x1: X(s.x1), message: s.message,
    })), null, "mx-hatch");
    drawSpans(svg, g, (model.violatedSpans || []).map((s) => ({
      x0: X(s.x0), x1: X(s.x1), message: s.message,
    })), "oklch(0.65 0.15 25 / 0.10)");

    for (const s of model.series) {
      if (s.hidden) continue;
      const Y = bands[s.band || 0].Y;
      for (const run of segmentsOf(s.ghost || [])) {
        if (run.length < 2) continue;
        svg.appendChild(el("path", {
          d: pathOf(run, X, Y), fill: "none", stroke: s.color,
          "stroke-width": 1, "stroke-dasharray": "4 4", opacity: 0.55,
        }));
      }
    }
    for (const s of model.series) {
      if (s.hidden) continue;
      const Y = bands[s.band || 0].Y;
      for (const run of segmentsOf(s.points)) {
        if (run.length < 2) {
          if (run.length === 1) {
            svg.appendChild(el("circle", {
              cx: X(run[0].x), cy: Y(run[0].y), r: 2.2, fill: s.color,
            }));
          }
          continue;
        }
        svg.appendChild(el("path", {
          d: pathOf(run, X, Y), fill: "none", stroke: s.color, "stroke-width": 2,
          "stroke-linejoin": "round", "stroke-linecap": "round",
          class: "series-path", "data-series": s.id,
          ...(s.dash ? { "stroke-dasharray": s.dash } : {}),
        }));
      }
    }

    if (model.cursor && isFinite(model.cursor.x)) {
      const cx = X(model.cursor.x);
      svg.appendChild(el("line", {
        x1: cx, y1: g.top, x2: cx, y2: g.bottom,
        stroke: "oklch(0.95 0 0 / 0.32)", "stroke-width": 1,
      }));
      for (const row of model.cursor.rows || []) {
        if (row.value === null || row.value === undefined) continue;
        const Y = bands[row.band || 0].Y;
        svg.appendChild(el("circle", {
          cx, cy: Y(row.value), r: 3.4, fill: row.color,
          stroke: "var(--plot-bg)", "stroke-width": 2,
        }));
      }
    }
    drawAxisLabels(host, g, { ...model, h: g.h }, X, bands, xt);
  }

  function renderSurface(host, svg, g, model) {
    if (!model.cells.length) return;
    const span = (model.hi ?? 1) - (model.lo ?? 0) || 1;
    const cw = g.pw / Math.max(1, model.drawnCols);
    const ch = g.ph / Math.max(1, model.drawnRows);
    for (const c of model.cells) {
      const t = c.v === null || c.v === undefined
        ? null : Math.pow(Math.max(0, (c.v - model.lo) / span), 0.6);
      const rect = el("rect", {
        x: g.left + c.col * cw, y: g.bottom - (c.row + 1) * ch,
        width: Math.ceil(cw) + 0.5, height: Math.ceil(ch) + 0.5,
        // One hue, dark to light. A magnitude has no categories in it,
        // so the ramp must not either: sweeping the hue as well would
        // invent boundaries the data has not got, and a reader would
        // see the colour change from blue to green as a threshold
        // crossed rather than as a number going up.
        fill: t === null ? "url(#mx-hatch)"
          : `oklch(${(0.20 + 0.68 * t).toFixed(3)} ${(0.02 + 0.13 * t).toFixed(3)} 70)`,
      });
      const title = el("title", {});
      title.textContent = c.error
        ? `${fmt(c.x)}, ${fmt(c.y)} — ${c.error}`
        : `${model.xLabel} ${fmt(c.x)} · ${model.yLabel} ${fmt(c.y)} · ${fmt(c.v)}`;
      rect.appendChild(title);
      svg.appendChild(rect);
    }
    const X = scaleX(g, model.xLo, model.xHi);
    const band = {
      lo: model.yLo, hi: model.yHi, label: model.yLabel,
      Y: scaleY(g, { lo: model.yLo, hi: model.yHi }, false),
      color: "var(--muted)",
    };
    band.tickValues = ticks(band.lo, band.hi, 5);
    const xt = drawFrame(svg, g, model, X, [band]);
    drawAxisLabels(host, g, { ...model, h: g.h }, X, [band], xt);
  }

  function renderDots(host, svg, g, model) {
    const X = scaleX(g, model.xLo, model.xHi);
    const band = {
      lo: model.yLo, hi: model.yHi, label: model.yLabel,
      Y: scaleY(g, { lo: model.yLo, hi: model.yHi }, model.yScale === "log"),
      color: "var(--muted)",
    };
    band.tickValues = model.yScale === "log"
      ? logTicks(band.lo, band.hi) : ticks(band.lo, band.hi, 5);
    const xt = drawFrame(svg, g, model, X, [band]);

    const placed = [];
    const labels = [];
    for (const dot of model.dots) {
      const cx = X(dot.x), cy = band.Y(dot.y);
      const circle = el("circle", {
        cx, cy, r: 5, fill: dot.color,
        // The 2px ring is what keeps two overlapping dots readable as two.
        stroke: "var(--plot-bg)", "stroke-width": 2,
      });
      const title = el("title", {});
      title.textContent = `${dot.label}: ${fmt(dot.x)}, ${fmt(dot.y)}`;
      circle.appendChild(title);
      svg.appendChild(circle);

      // Direct labels, nudged down until they stop colliding. Identity is
      // never colour alone here -- a scatter has no legend to lean on.
      const width = dot.label.length * 5.8 + 6;
      const flip = cx + 12 + width > g.right;
      const left = flip ? cx - 12 - width : cx + 11;
      let top = cy - 6;
      let guard = 0;
      while (guard++ < 16 && placed.some((p) =>
        Math.abs(p.top - top) < 12 && left < p.left + p.width + 8 && p.left < left + width + 8)) {
        top += 12;
      }
      top = Math.min(g.bottom - 12, Math.max(g.top, top));
      placed.push({ top, left, width });
      labels.push({ top, left, width, text: dot.label, flip });
    }

    const layer = div(null);
    Object.assign(layer.style, {
      position: "absolute", inset: "0", pointerEvents: "none",
      fontFamily: "var(--mono)", fontSize: "9.5px", color: "var(--muted)",
    });
    for (const l of labels) {
      const t = div(null, l.text);
      Object.assign(t.style, {
        position: "absolute", left: `${l.left}px`, top: `${l.top}px`,
        width: `${l.width}px`, textAlign: l.flip ? "right" : "left",
        lineHeight: "1", whiteSpace: "nowrap",
      });
      layer.appendChild(t);
    }
    host.appendChild(layer);
    drawAxisLabels(host, g, { ...model, h: g.h }, X, [band], xt);
  }

  // ---- entry point -----------------------------------------------------

  function render(host, model) {
    host.textContent = "";
    const g = geometry(model);
    const svg = el("svg", { width: "100%", height: "100%" });
    Object.assign(svg.style, { position: "absolute", inset: "0", display: "block" });
    defs(svg);

    if (model.mode === "surface") renderSurface(host, svg, g, model);
    else if (model.mode === "correlate") renderDots(host, svg, g, model);
    else renderLines(host, svg, g, model);

    host.appendChild(svg);
    return g;
  }

  return { render, fmt, ticks, geometry, scaleX, scaleY, PAD };
})();
