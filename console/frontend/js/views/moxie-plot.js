/* `moxie:plot` (`mo xie`, `plot …`): a value against another, re-solved
 * from the garden at every point and never written back
 * (docs/console.md → Settled for U4).
 *
 * The pane is its command. api.moxie.plot(command) answers it -- core/
 * plotcmd.py resolves the line and core/plotview.py draws it -- and every
 * gesture that changes what is shown (a knob dragged, a zoom, log y, a
 * series switched off) goes through api.moxie.rewrite() to a new command
 * and ctx.setCommand(), so a layout restores exactly this plot. The cursor
 * and the knob/series the keys point at are this pane's own transient
 * state.
 *
 * Each run asks twice: a coarse pass (the same range, fewer points) that
 * lands in about a frame, then the full one. A reply that a newer request
 * has overtaken is dropped. While a knob is being dragged only coarse
 * passes of the draft line are drawn; letting go rewrites the command.
 *
 * Writes are commands: `plot save ‹name›` and `plot export csv|png` come
 * here as instance.action() (the pane_action effect); save runs the full
 * `plot save ‹name› ‹this line›` on the command line so it is in the
 * history, the log and undo. */

import { render as draw, fmt, seriesStyle } from "../plot.js";

const HINTS = [["h l", "cursor"], ["[ ]", "series"], ["t", "show/hide"], ["z", "zoom"], ["Z", "zoom out"],
  ["y", "log y"]];
const KNOB_HINTS = [["j k", "knob"], ["- +", "move knob"]];
const NUDGE = 1 / 20; // one `-`/`+` press moves a knob this share of its range

function message(err) {
  return String(err && err.message ? err.message : err);
}

function points(raw) {
  return (raw || []).map((p) => (p ? { x: p[0], y: p[1] } : null));
}

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const st = { command: "", seq: 0, payload: null, error: null, cursor: null, mark: null,
    series: 0, knob: 0, draft: null, dragging: null, sensitivity: null, sensSeq: 0, geometry: null };
  let root = null, plotEl = null, statusEl = null, railEl = null;
  let resize = null;
  let sensTimer = 0;

  /* ---- the model plot.js draws ---- */

  function styles() {
    const out = new Map();
    (st.payload.series || []).forEach((s, index) => out.set(s.id, { ...seriesStyle(index), ref: s.ref, hidden: s.hidden }));
    return out;
  }

  function model() {
    const f = st.payload.figure;
    const rect = plotEl.getBoundingClientRect();
    const base = { mode: f.mode, width: rect.width, height: rect.height, xLo: f.xLo, xHi: f.xHi, yLo: f.yLo, yHi: f.yHi,
      xLabel: f.xLabel, yLabel: f.yLabel, yScale: f.yScale };
    if (f.mode === "surface") return { ...base, cells: f.cells, rows: f.rows, cols: f.cols, lo: f.lo, hi: f.hi };
    if (f.mode === "correlate") {
      return { ...base, dots: f.dots.map((d) => ({ ...d, label: d.name, color: "var(--s1)" })) };
    }
    const style = styles();
    const series = f.series.map((s) => ({ id: s.id, band: s.band, points: points(s.points), ...style.get(s.id) }));
    return {
      ...base, bands: f.bands, series,
      failedSpans: f.failed, violatedSpans: f.violated,
      cursor: st.cursor === null ? null : { x: st.cursor, rows: readout().map((r) => ({ value: r.value, band: r.band, color: r.color })) },
      mark: st.mark,
    };
  }

  /** Each drawn series' value at the cursor: the nearest solved point. */
  function readout() {
    if (!st.payload || st.cursor === null || st.payload.figure.mode !== "sweep") return [];
    const style = styles();
    return st.payload.figure.series.map((s) => {
      let best = null;
      for (const p of s.points) {
        if (p && (best === null || Math.abs(p[0] - st.cursor) < Math.abs(best[0] - st.cursor))) best = p;
      }
      return { id: s.id, label: s.label, unit: s.unit, band: s.band, color: style.get(s.id).color,
        x: best ? best[0] : null, value: best ? best[1] : null };
    });
  }

  /* ---- drawing ---- */

  function render() {
    if (!root) return;
    if (st.error) {
      root.replaceChildren(h("div", "pane-error lvl-fault", st.error));
      plotEl = statusEl = railEl = null;
      return;
    }
    if (!st.payload) return;
    if (st.payload.empty) {
      root.replaceChildren(h("div", "plot-empty", [
        h("div", null, "plot a value against another, re-solving the garden at every point:"),
        h("div", "plot-usage", "plot ‹m.y› [‹m.y›…] vs ‹m.x› lo:hi [--set ‹m.k›=‹v›] [--log]"),
        h("div", "plot-usage", "plot ‹m.z› vs ‹m.x› lo:hi by ‹m.y› lo:hi"),
        h("div", "plot-usage", "plot ‹key› vs ‹key› --across"),
        h("div", "plot-usage", "plot ‹saved plot›"),
      ]));
      plotEl = statusEl = railEl = null;
      return;
    }
    if (!plotEl) {
      plotEl = h("div", "plot-area");
      plotEl.addEventListener("mousedown", onPlotDown);
      plotEl.addEventListener("dblclick", zoomOut);
      statusEl = h("div", "plot-status");
      railEl = h("div", "plot-rail scroll");
      root.replaceChildren(h("div", "plot-main", [plotEl, statusEl]), railEl);
    }
    drawPlot();
    drawRail();
  }

  function drawPlot() {
    if (!plotEl || !st.payload || st.payload.empty) return;
    const f = st.payload.figure;
    if (f.error && !(f.series || []).some((s) => s.points.some(Boolean)) && !(f.cells || []).length && !(f.dots || []).length) {
      plotEl.replaceChildren(h("div", "pane-error lvl-fault", f.error));
      st.geometry = null;
    } else {
      st.geometry = draw(plotEl, model());
    }
    drawStatus();
  }

  function drawStatus() {
    if (!statusEl) return;
    const p = st.payload;
    const f = p.figure;
    const parts = [];
    const dot = h("span", `plot-dot-status lvl-${level()}`, "●");
    parts.push(h("span", null, [dot, ` ${f.solves} solve${f.solves === 1 ? "" : "s"} ${p.seconds}s${p.coarse ? " · refining" : ""}`]));
    if (f.mode === "sweep") {
      const drawn = (p.series || []).filter((s) => !s.hidden).length;
      parts.push(h("span", null, `${drawn} of ${(p.series || []).length} series`));
    }
    if (f.mode === "surface" && f.lo !== null) {
      const what = ((p.series || [])[0] || {}).ref || "value";
      parts.push(h("span", null, `${what} ${fmt(f.lo)} – ${fmt(f.hi)}, dark to light`));
    }
    if (p.xPinned) parts.push(h("span", "lvl-warn", "x pinned"));
    if ((f.broken || []).length) parts.push(h("span", "lvl-fault", `breaks ${f.broken.join(", ")}`));
    if (f.failed && f.failed.length) parts.push(h("span", "lvl-fault", `${f.failed.length} span${f.failed.length === 1 ? "" : "s"} unsolved`));
    parts.push(h("span", "plot-status-fill"));
    const rows = readout();
    if (rows.length) {
      parts.push(h("span", null, [`x ${fmt(st.cursor)}`, ...rows.filter((r) => r.value !== null).flatMap((r) => [" · ",
        h("span", null, `${r.label.split(".").pop()} ${fmt(r.value)}${r.unit ? ` ${r.unit}` : ""}`)])]));
    }
    statusEl.replaceChildren(...parts);
  }

  function railSection(title, rows, right = "") {
    return h("div", "plot-rail-section", [h("div", "plot-rail-head", [h("span", null, title), h("span", "plot-rail-meta", right)]), ...rows]);
  }

  function drawRail() {
    if (!railEl) return;
    const p = st.payload;
    const sections = [];
    if ((p.series || []).length) {
      sections.push(railSection("series", p.series.map((s, index) => {
        const { color, dash } = seriesStyle(index);
        const swatch = h("span", "plot-swatch");
        swatch.style.background = s.hidden ? "transparent" : color;
        swatch.style.borderColor = color;
        if (dash) swatch.classList.add("dashed");
        const row = h("div", `plot-rail-row${index === st.series ? " cursor" : ""}${s.hidden ? " hidden" : ""}`,
          [swatch, h("span", "plot-ref", s.ref)]);
        if (st.payload.figure.mode === "sweep") {
          row.title = s.hidden ? "click to show" : "click to hide";
          row.addEventListener("click", () => { st.series = index; toggleSeries(); });
        }
        return row;
      })));
    }
    if ((p.knobs || []).length) {
      sections.push(railSection("knobs", p.knobs.map((k, index) => knobRow(k, index)), "not written"));
    }
    const sens = st.sensitivity || [];
    if (sens.length && st.cursor !== null) {
      sections.push(railSection("sensitivity", sens.map((r) => h("div", "plot-rail-row", [
        h("span", "plot-ref", r.ref), h("span", "plot-num", `${r.value >= 0 ? "+" : ""}${r.value.toFixed(2)}`),
      ])), `at x ${fmt(st.cursor)}`));
    }
    if (!sections.length) {
      sections.push(h("div", "plot-rail-note", "no knobs -- add one with --set ‹m.k›=‹v›"));
    }
    railEl.replaceChildren(...sections);
  }

  function knobValue(index) {
    const k = st.payload.knobs[index];
    return st.dragging && st.dragging.index === index ? st.dragging.value : k.value;
  }

  function knobRow(k, index) {
    const value = knobValue(index);
    const share = k.hi > k.lo ? (value - k.lo) / (k.hi - k.lo) : 0.5;
    const fill = h("div", "plot-knob-fill");
    fill.style.width = `${Math.max(0, Math.min(1, share)) * 100}%`;
    const track = h("div", "plot-knob-track", [fill]);
    track.addEventListener("mousedown", (event) => startKnobDrag(event, index, track));
    return h("div", `plot-knob${index === st.knob ? " cursor" : ""}`, [
      h("div", "plot-rail-row", [h("span", "plot-ref", k.ref), h("span", "plot-num", `${fmt(value)}${k.unit ? ` ${k.unit}` : ""}`)]),
      track,
    ]);
  }

  function level() {
    const f = st.payload && st.payload.figure;
    if (!f) return "idle";
    if (f.error || (f.broken || []).length) return "warn";
    if ((f.failed || []).length) return "warn";
    return "ok";
  }

  function summary() {
    const p = st.payload;
    if (p.empty) return "";
    const f = p.figure;
    const what = p.name ? `${p.name} · ` : "";
    if (f.error) return `${what}${f.error}`;
    return `${what}${f.solves} solves · ${p.seconds}s`;
  }

  /* ---- running ---- */

  async function ask(command, coarse) {
    const ns = api.moxie;
    try {
      return ns && typeof ns.plot === "function" ? await ns.plot(command, coarse) : { error: "this window has no moxie api" };
    } catch (err) {
      return { error: message(err) };
    }
  }

  function adopt(p) {
    const hintsChanged = !st.payload || !p.figure || !st.payload.figure || st.payload.figure.mode !== p.figure.mode
      || (st.payload.knobs || []).length !== (p.knobs || []).length;
    const firstDraw = !st.payload || st.payload.empty;
    st.payload = p;
    st.error = null;
    st.series = Math.max(0, Math.min(st.series, (p.series || []).length - 1));
    st.knob = Math.max(0, Math.min(st.knob, (p.knobs || []).length - 1));
    if (p.figure && p.figure.mode === "sweep") {
      const { xLo, xHi } = p.figure;
      if (st.cursor === null || st.cursor < xLo || st.cursor > xHi || firstDraw) st.cursor = null;
    } else {
      st.cursor = null;
    }
    if (st.mark !== null && p.figure && (st.mark < p.figure.xLo || st.mark > p.figure.xHi)) st.mark = null;
    if (hintsChanged) store.emit("hints");
  }

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    const coarse = await ask(command, true);
    if (seq !== st.seq || !root) return { summary: "", level: "idle" };
    if (!coarse || coarse.error) {
      Object.assign(st, { payload: null, error: (coarse && coarse.error) || "no answer" });
      render();
      return { summary: st.error, level: "fault" };
    }
    adopt(coarse);
    render();
    if (coarse.empty) return { summary: "", level: "idle" };
    const full = await ask(command, false);
    if (seq !== st.seq || !root) return { summary: "", level: "idle" };
    if (full && !full.error) {
      adopt(full);
      render();
      scheduleSensitivity();
    }
    return { summary: summary(), level: level() === "ok" ? "ok" : "warn" };
  }

  async function rewrite(change) {
    let result;
    try {
      result = await api.moxie.rewrite(st.command, change);
    } catch (err) {
      result = { error: message(err) };
    }
    if (!result || result.error) {
      store.log("result", (result && result.error) || "can't rewrite the plot", { level: "fault" });
      return null;
    }
    await ctx.setCommand(result.command);
    return result.command;
  }

  /* ---- cursor, zoom, series, log ---- */

  function sweepXs() {
    const f = st.payload && st.payload.figure;
    if (!f || f.mode !== "sweep") return [];
    const xs = new Set();
    for (const s of f.series) for (const p of s.points) if (p) xs.add(p[0]);
    return [...xs].sort((a, b) => a - b);
  }

  function moveCursor(steps) {
    const xs = sweepXs();
    if (!xs.length) return;
    let at = st.cursor === null ? (steps > 0 ? -1 : xs.length) : xs.findIndex((x) => x >= st.cursor - 1e-12);
    if (at < 0 && st.cursor !== null) at = xs.length - 1;
    st.cursor = xs[Math.max(0, Math.min(xs.length - 1, at + steps))];
    drawPlot();
    scheduleSensitivity();
  }

  function setCursorAt(clientX) {
    if (!st.geometry || !st.payload || st.payload.figure.mode !== "sweep") return;
    const rect = plotEl.getBoundingClientRect();
    const x = st.geometry.X.invert(clientX - rect.left);
    const f = st.payload.figure;
    st.cursor = Math.max(f.xLo, Math.min(f.xHi, x));
    moveCursor(0);
  }

  function zoomKey() {
    if (st.cursor === null) return;
    if (st.mark === null) {
      st.mark = st.cursor;
      drawPlot();
      return;
    }
    const range = [st.mark, st.cursor];
    st.mark = null;
    if (range[0] === range[1]) { drawPlot(); return; }
    rewrite({ range });
  }

  function zoomOut() {
    const f = st.payload && st.payload.figure;
    if (!f || f.mode !== "sweep") return;
    const mid = (f.xLo + f.xHi) / 2, half = f.xHi - f.xLo;
    rewrite({ range: [mid - half, mid + half] });
  }

  function onPlotDown(event) {
    if (event.button !== 0 || !st.geometry) return;
    const startX = event.clientX;
    let moved = false;
    const move = (e) => {
      if (Math.abs(e.clientX - startX) > 4) moved = true;
      setCursorAt(e.clientX);
      if (moved && st.mark === null) {
        const rect = plotEl.getBoundingClientRect();
        st.mark = st.geometry.X.invert(startX - rect.left);
      }
    };
    const up = (e) => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      if (moved && st.mark !== null && st.cursor !== null && st.mark !== st.cursor) {
        const range = [st.mark, st.cursor];
        st.mark = null;
        rewrite({ range });
      } else {
        st.mark = null;
        setCursorAt(e.clientX);
      }
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    setCursorAt(event.clientX);
  }

  function toggleSeries() {
    if (!st.payload || st.payload.figure.mode !== "sweep") return;
    const s = (st.payload.series || [])[st.series];
    if (s) rewrite({ hide: s.ref });
  }

  function pickSeries(delta) {
    const n = (st.payload.series || []).length;
    if (!n) return;
    st.series = (st.series + delta + n) % n;
    drawRail();
  }

  /* ---- knobs ---- */

  function pickKnob(delta) {
    const n = (st.payload.knobs || []).length;
    if (!n) return;
    st.knob = (st.knob + delta + n) % n;
    drawRail();
  }

  function nudgeKnob(direction) {
    const k = (st.payload.knobs || [])[st.knob];
    if (!k) return;
    const step = (k.hi - k.lo) * NUDGE || Math.abs(k.value) * NUDGE || NUDGE;
    rewrite({ set: { ref: k.ref, value: Number((k.value + direction * step).toPrecision(6)) } });
  }

  function startKnobDrag(event, index, track) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const k = st.payload.knobs[index];
    st.knob = index;
    const valueAt = (clientX) => {
      const rect = track.getBoundingClientRect();
      const share = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
      return Number((k.lo + share * (k.hi - k.lo)).toPrecision(4));
    };
    st.dragging = { index, value: valueAt(event.clientX), busy: false, pending: false };
    const preview = async () => {
      const drag = st.dragging;
      if (!drag) return;
      if (drag.busy) { drag.pending = true; return; }
      drag.busy = true;
      let draft = null;
      try {
        draft = await api.moxie.rewrite(st.command, { set: { ref: k.ref, value: drag.value } });
      } catch (err) {
        draft = null;
      }
      const p = draft && !draft.error ? await ask(draft.command, true) : null;
      if (st.dragging === drag && p && !p.error && !p.empty) {
        adopt(p);
        drawPlot();
      }
      drag.busy = false;
      if (drag.pending) { drag.pending = false; preview(); }
    };
    const move = (e) => {
      st.dragging.value = valueAt(e.clientX);
      drawRail();
      preview();
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      const drag = st.dragging;
      st.dragging = null;
      rewrite({ set: { ref: k.ref, value: drag.value } });
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
    drawRail();
    preview();
  }

  /* ---- sensitivity at the cursor ---- */

  function scheduleSensitivity() {
    clearTimeout(sensTimer);
    if (!st.payload || !(st.payload.knobs || []).length || st.cursor === null) {
      if (st.sensitivity) { st.sensitivity = null; drawRail(); }
      return;
    }
    sensTimer = setTimeout(async () => {
      const seq = ++st.sensSeq;
      let result;
      try {
        result = await api.moxie.sensitivity(st.command, st.cursor);
      } catch (err) {
        result = { error: message(err) };
      }
      if (seq !== st.sensSeq || !root) return;
      st.sensitivity = result && result.rows ? result.rows : null;
      drawRail();
    }, 120);
  }

  /* ---- keys ---- */

  const KEYS = {
    h: () => moveCursor(-1), ArrowLeft: () => moveCursor(-1),
    l: () => moveCursor(1), ArrowRight: () => moveCursor(1),
    H: () => moveCursor(-10), L: () => moveCursor(10),
    "[": () => pickSeries(-1), "]": () => pickSeries(1),
    t: toggleSeries,
    j: () => pickKnob(1), ArrowDown: () => pickKnob(1),
    k: () => pickKnob(-1), ArrowUp: () => pickKnob(-1),
    "-": () => nudgeKnob(-1), "+": () => nudgeKnob(1), "=": () => nudgeKnob(1),
    z: zoomKey,
    Z: zoomOut,
    y: () => st.payload.figure && rewrite({ log: st.payload.figure.yScale !== "log" }),
  };

  function onKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey || !st.payload || st.payload.empty) return false;
    if (event.key === "Escape" && (st.mark !== null || st.cursor !== null)) {
      st.mark = null;
      st.cursor = null;
      drawPlot();
      scheduleSensitivity();
      return true;
    }
    const action = KEYS[event.key];
    if (!action) return false;
    action();
    return true;
  }

  /* ---- pane actions: plot save / plot export ---- */

  /** The plot as a standalone SVG: every var(--x) resolved (an image has
   * no stylesheet) and the HTML labels written in as <text>. */
  function svgDataUrl() {
    const svg = plotEl && plotEl.querySelector("svg");
    if (!svg) return null;
    const clone = svg.cloneNode(true);
    const style = getComputedStyle(document.documentElement);
    const resolve = (value) => value.replace(/var\((--[\w-]+)\)/g, (_m, name) => style.getPropertyValue(name).trim());
    clone.querySelectorAll("*").forEach((node) => {
      for (const attr of ["stroke", "fill"]) {
        const value = node.getAttribute(attr);
        if (value && value.includes("var(")) node.setAttribute(attr, resolve(value));
      }
    });
    const bg = document.createElementNS("http://www.w3.org/2000/svg", "rect");
    Object.entries({ x: 0, y: 0, width: "100%", height: "100%", fill: resolve("var(--bg)") })
      .forEach(([k, v]) => bg.setAttribute(k, v));
    clone.insertBefore(bg, clone.firstChild);
    const origin = plotEl.getBoundingClientRect();
    plotEl.querySelectorAll(".plot-labels > div").forEach((label) => {
      const rect = label.getBoundingClientRect();
      const css = getComputedStyle(label);
      const text = document.createElementNS("http://www.w3.org/2000/svg", "text");
      const anchor = css.textAlign === "right" ? "end" : css.textAlign === "center" ? "middle" : "start";
      const x = anchor === "end" ? rect.right : anchor === "middle" ? (rect.left + rect.right) / 2 : rect.left;
      Object.entries({ x: x - origin.left, y: rect.top - origin.top + rect.height * 0.7, "text-anchor": anchor,
        fill: css.color, "font-family": css.fontFamily, "font-size": css.fontSize })
        .forEach(([k, v]) => text.setAttribute(k, v));
      text.textContent = label.textContent;
      clone.appendChild(text);
    });
    return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(new XMLSerializer().serializeToString(clone))}`;
  }

  function rasterize() {
    const url = svgDataUrl();
    if (!url) return Promise.resolve(null);
    const rect = plotEl.getBoundingClientRect();
    if (!rect.width || !rect.height) return Promise.resolve(null);
    return new Promise((resolve) => {
      const img = new Image();
      img.onload = () => {
        const scale = 2;
        const canvas = document.createElement("canvas");
        canvas.width = Math.round(rect.width * scale);
        canvas.height = Math.round(rect.height * scale);
        const g = canvas.getContext("2d");
        g.scale(scale, scale);
        g.drawImage(img, 0, 0);
        resolve(canvas.toDataURL("image/png"));
      };
      img.onerror = () => resolve(null);
      img.src = url;
    });
  }

  async function action(e) {
    if (e.action === "save") {
      const body = st.command.replace(/^plot\s*/, "");
      if (!body) return { message: "nothing to save -- this pane shows no plot yet", level: "fault" };
      const name = /^[\w.-]+$/.test(e.name) ? e.name : JSON.stringify(e.name);
      const outcome = await ctx.runCommand(`plot save ${name} ${body}`);
      return { ...outcome, logged: true };
    }
    if (e.action === "export") {
      let result;
      try {
        if (e.format === "csv") {
          result = await api.moxie.export_csv(st.command, e.path || null);
        } else {
          const data = await rasterize();
          result = data ? await api.moxie.export_png(data, (st.payload && st.payload.name) || "plot", e.path || null)
            : { error: "nothing drawn to export" };
        }
      } catch (err) {
        result = { error: message(err) };
      }
      if (!result || result.error) return { message: (result && result.error) || "export failed", level: "fault" };
      if (result.cancelled) return { message: "export cancelled", level: "ok" };
      return { message: `exported ${result.path}`, level: "ok" };
    }
    return { message: `plot can't ${e.action}`, level: "fault" };
  }

  /* ---- the contract ---- */

  return {
    mount(el, command) {
      st.command = command;
      root = h("div", "view-plot");
      el.appendChild(root);
      resize = new ResizeObserver(() => drawPlot());
      resize.observe(root);
    },
    run,
    unmount() {
      clearTimeout(sensTimer);
      if (resize) resize.disconnect();
      resize = null;
      st.seq += 1;
      st.sensSeq += 1;
      st.dragging = null;
      root = plotEl = statusEl = railEl = null;
    },
    hints: () => {
      if (!st.payload || st.payload.empty || !st.payload.figure) return [];
      const knobs = (st.payload.knobs || []).length ? KNOB_HINTS : [];
      if (st.payload.figure.mode === "sweep") return [...HINTS, ...knobs];
      return st.payload.figure.mode === "correlate" ? [["y", "log y"], ...knobs] : knobs;
    },
    onKey,
    action,
  };
}

export default {
  id: "moxie:plot",
  app: "xie",
  describe: "plot values against another, re-solved (plot)",
  commands: [{ verb: "plot" }],
  followsSel: false,
  create,
};
