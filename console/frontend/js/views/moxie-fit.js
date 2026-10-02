/* `moxie:fit` (`fit …`): measured points, the model fitted through them,
 * and what the fit found (docs/console.md → Settled for `fit`).
 *
 * The pane is its command. api.moxie.fit(command) answers it -- core/fit.py
 * reads the garden under the session lock and solves outside it -- and
 * nothing is written: `a` (or clicking *apply*) runs the same line with
 * `--apply` on the command line, so the write is in the history, the log
 * and undo like any other. */

import { render as draw, fmt, seriesStyle } from "../plot.js";

const HINTS = [["a", "apply the fit"], ["h l", "cursor"], ["esc", "clear cursor"]];

function message(err) {
  return String(err && err.message ? err.message : err);
}

function padded(lo, hi) {
  if (!(hi > lo)) {
    const step = Math.abs(hi) * 0.5 || 1;
    return [lo - step, hi + step];
  }
  const pad = (hi - lo) * 0.08;
  return [lo - pad, hi + pad];
}

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const st = { command: "", seq: 0, payload: null, error: null, cursor: null };
  let root = null, plotEl = null, statusEl = null, railEl = null, resize = null;

  function model() {
    const p = st.payload;
    const rect = plotEl.getBoundingClientRect();
    const xs = p.points.map((pt) => pt.x);
    const ys = [...p.points.map((pt) => pt.y), ...p.curve.map((c) => c[1]).filter((v) => v !== null)];
    const [xLo, xHi] = padded(Math.min(...xs), Math.max(...xs));
    const [yLo, yHi] = padded(Math.min(...ys), Math.max(...ys));
    const { color } = seriesStyle(0);
    const at = cursorPoint();
    return {
      mode: "sweep", width: rect.width, height: rect.height, xLo, xHi, yScale: "linear",
      xLabel: p.xLabel,
      bands: [{ lo: yLo, hi: yHi, label: `${p.yLabel} (fitted) · ${p.points.length} points` }],
      series: [{ id: "fit", band: 0, color, points: p.curve.map((c) => (c[1] === null ? null : { x: c[0], y: c[1] })) }],
      points: p.points.map((pt) => ({ x: pt.x, y: pt.y, label: pt.name, color: "var(--fg)" })),
      cursor: at ? { x: at.x, rows: [{ value: at.fitted, band: 0, color }] } : null,
    };
  }

  function cursorPoint() {
    return st.cursor === null || !st.payload ? null : st.payload.points[st.cursor] || null;
  }

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
        h("div", null, "fit inputs so the model passes through measured points:"),
        h("div", "plot-usage", "fit ‹m.y› vs ‹m.x› --data ‹ykey› vs ‹xkey› --free ‹m.k›…"),
        h("div", "plot-usage", "  [--from ‹import›] [--scenario ‹s›]"),
        h("div", null, "nothing is written until you apply it (a, or --apply)"),
      ]));
      plotEl = statusEl = railEl = null;
      return;
    }
    if (!plotEl) {
      plotEl = h("div", "plot-area");
      plotEl.addEventListener("mousedown", onPlotDown);
      statusEl = h("div", "plot-status");
      railEl = h("div", "plot-rail scroll");
      root.replaceChildren(h("div", "plot-main", [plotEl, statusEl]), railEl);
    }
    drawPlot();
    drawRail();
  }

  function drawPlot() {
    if (!plotEl || !st.payload || st.payload.empty) return;
    railEl.hidden = root.clientWidth < 640;
    st.geometry = draw(plotEl, model());
    drawStatus();
  }

  /** Only what is at the cursor: the fit's own numbers are the header's. */
  function drawStatus() {
    const parts = [h("span", "plot-status-fill")];
    const at = cursorPoint();
    if (at) parts.push(h("span", null, `${at.name}: x ${fmt(at.x)} · y ${fmt(at.y)} · fit ${fmt(at.fitted)}`));
    statusEl.replaceChildren(...parts);
  }

  function section(title, rows, right = "") {
    return h("div", "plot-rail-section", [h("div", "plot-rail-head", [h("span", null, title), h("span", "plot-rail-meta", right)]), ...rows]);
  }

  function row(left, right, className = "") {
    return h("div", `plot-rail-row ${className}`, [h("span", "plot-ref", left), h("span", "plot-num", right)]);
  }

  function drawRail() {
    const p = st.payload;
    const sections = [section("fit", p.params.map((x) => {
      const sigma = x.sigma === null ? "" : ` ± ${fmt(x.sigma)}`;
      const r = row(x.ref, `${fmt(x.value)}${sigma}${x.unit ? ` ${x.unit}` : ""}`);
      r.title = `was ${fmt(x.start)}`;
      return r;
    }), p.converged ? `${p.iterations} iter` : "not converged")];
    const stats = [];
    if (p.rms !== null) stats.push(row("RMS", fmt(p.rms)));
    if (p.r2 !== null && p.r2 !== undefined) stats.push(row("R²", p.r2.toFixed(4)));
    stats.push(row("points", `${p.points.length - p.skipped.length} of ${p.points.length}`));
    sections.push(section("residuals", stats));
    const notes = [...p.skipped.map((s) => h("div", "plot-rail-note lvl-warn", `${s.name}: ${s.why}`))];
    if (p.message) notes.push(h("div", `plot-rail-note ${p.converged ? "" : "lvl-warn"}`, p.message));
    if (notes.length) sections.push(section("notes", notes));
    if (p.rms !== null) {
      const apply = h("div", "plot-rail-row plot-apply", "a  apply: write values, σ and source");
      apply.addEventListener("click", applyFit);
      sections.push(h("div", "plot-rail-section", [apply]));
    }
    railEl.replaceChildren(...sections);
  }

  function level() {
    const p = st.payload;
    if (!p || p.rms === null) return "warn";
    return p.converged && !p.skipped.length && !p.message ? "ok" : "warn";
  }

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    let p;
    try {
      p = api.moxie && typeof api.moxie.fit === "function" ? await api.moxie.fit(command) : { error: "this window has no moxie api" };
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq || !root) return { summary: "", level: "idle" };
    if (!p || p.error) {
      Object.assign(st, { payload: null, error: (p && p.error) || "no answer" });
      render();
      return { summary: st.error, level: "fault" };
    }
    st.payload = p;
    st.error = null;
    if (st.cursor !== null && !p.empty && st.cursor >= p.points.length) st.cursor = null;
    render();
    if (p.empty) return { summary: "", level: "idle" };
    const values = p.params.map((x) => `${x.ref.split(".").pop()} ${fmt(x.value)}`).join(", ");
    const r2 = p.r2 !== null && p.r2 !== undefined ? ` · R² ${p.r2.toFixed(4)}` : "";
    return { summary: p.rms === null ? p.message : `${values} · RMS ${fmt(p.rms)}${r2}`, level: level() === "ok" ? "ok" : "warn" };
  }

  async function applyFit() {
    if (!st.payload || st.payload.empty || st.payload.rms === null) return;
    await ctx.runCommand(st.payload.applyCommand);
  }

  function moveCursor(step) {
    const n = st.payload.points.length;
    if (!n) return;
    st.cursor = st.cursor === null ? (step > 0 ? 0 : n - 1) : Math.max(0, Math.min(n - 1, st.cursor + step));
    drawPlot();
  }

  function onPlotDown(event) {
    if (event.button !== 0 || !st.geometry || !st.payload) return;
    const rect = plotEl.getBoundingClientRect();
    const x = st.geometry.X.invert(event.clientX - rect.left);
    let best = 0;
    st.payload.points.forEach((pt, i) => {
      if (Math.abs(pt.x - x) < Math.abs(st.payload.points[best].x - x)) best = i;
    });
    st.cursor = best;
    drawPlot();
  }

  const KEYS = {
    a: applyFit,
    h: () => moveCursor(-1), ArrowLeft: () => moveCursor(-1),
    l: () => moveCursor(1), ArrowRight: () => moveCursor(1),
  };

  function onKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey || !st.payload || st.payload.empty) return false;
    if (event.key === "Escape" && st.cursor !== null) {
      st.cursor = null;
      drawPlot();
      return true;
    }
    const action = KEYS[event.key];
    if (!action) return false;
    action();
    return true;
  }

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
      if (resize) resize.disconnect();
      resize = null;
      st.seq += 1;
      root = plotEl = statusEl = railEl = null;
    },
    hints: () => (st.payload && !st.payload.empty ? HINTS : []),
    onKey,
  };
}

export default {
  id: "moxie:fit",
  app: "xie",
  describe: "fit inputs to measured points (fit)",
  commands: [{ verb: "fit" }],
  followsSel: false,
  create,
};
