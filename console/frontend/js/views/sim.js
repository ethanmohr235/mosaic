/* `sim` (`sim …`, `layout calc`): a run of the garden, tick by tick,
 * as a watch list (docs/console.md → Settled for U5).
 *
 * One row per watched value: its state (solved, varied, pinned, given),
 * its value at the playhead's tick, Δ/tick, where that value sits in its
 * range over the run, and with `--vs` the other run's value and the
 * difference. Under the table, the timeline: the cursor row's series,
 * its min and max in --dim, constraint breaks and a failed tick in
 * --fault, and the playhead.
 *
 * api.motion.sim(command) answers with every row's whole series, so the
 * playhead scrubs without asking again. The playhead is the store's: this
 * pane claims it when it runs (unless another sim pane holds it) and when
 * it is focused, publishing its tick count and run for the rule pane.
 * Nothing here writes; `v` rewrites the line (`--vs garden`). */

import { fmt } from "../plot.js";
import { KEEP_HINT, hasKnobs, keepKnobs, knobSummary } from "../knobs.js";
import { PLAYHEAD_HINTS, playheadKey, at } from "../playhead.js";

const HINTS = [["j k", "row"], ["⏎", "select mote"], ["v", "vs garden"], ...PLAYHEAD_HINTS];
const STATE_LEVEL = { solved: "ok", pinned: "warn", varied: null, given: "idle" };
const SVG = "http://www.w3.org/2000/svg";
const PAD = 10;

function message(err) {
  return String(err && err.message ? err.message : err);
}

function svg(tag, attrs = {}, className = "") {
  const node = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  if (className) node.setAttribute("class", className);
  return node;
}

function extent(values) {
  const finite = values.filter((v) => v !== null && v !== undefined && isFinite(v));
  if (!finite.length) return null;
  return [Math.min(...finite), Math.max(...finite)];
}

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const token = {};
  const st = { command: "", seq: 0, payload: null, error: null, cursor: 0, cursorKey: null };
  let root = null, tableEl = null, timelineEl = null, statusEl = null, resize = null;
  const offs = [];

  const tick = () => store.state.playhead.tick;
  const rowKey = (r) => `${r.moteId}|${r.key}`;

  /* ---- the values at the tick ---- */

  function valueAt(row) {
    return at(row.series, tick());
  }

  function delta(row) {
    const t = Math.min(tick(), row.series.length - 1);
    if (t < 0) return null;
    const now = row.series[t];
    const before = t === 0 ? row.start : row.series[t - 1];
    return now === null || before === null ? null : now - before;
  }

  function vsAt(row) {
    return row.vs ? at(row.vs, tick()) : null;
  }

  function rangeBar(row) {
    const span = extent(row.series);
    const value = valueAt(row);
    const bar = h("span", "range-bar");
    if (span && value !== null) {
      const [lo, hi] = span;
      const mark = h("span", "range-mark");
      mark.style.left = `${hi > lo ? ((value - lo) / (hi - lo)) * 100 : 50}%`;
      bar.appendChild(mark);
      bar.title = `${fmt(lo)} … ${fmt(hi)}`;
    }
    return bar;
  }

  function columns() {
    const cols = [
      { key: "ref", label: "value", width: "minmax(12ch, 1fr)" },
      { key: "state", label: "state", width: "7ch", level: (r) => STATE_LEVEL[r.state] },
      { key: "now", label: `@${tick()}`, width: "10ch", align: "right", render: (r) => fmt(valueAt(r)) },
      { key: "delta", label: "Δ/tick", width: "10ch", align: "right", render: (r) => fmt(delta(r)) },
      { key: "range", label: "range", width: "12ch", render: rangeBar },
    ];
    if (st.payload && st.payload.vs) {
      cols.push({ key: "vs", label: st.payload.vs, width: "10ch", align: "right", render: (r) => fmt(vsAt(r)) });
      cols.push({ key: "dvs", label: "Δ", width: "10ch", align: "right",
        render: (r) => { const a = valueAt(r), b = vsAt(r); return fmt(a === null || b === null ? null : a - b); } });
    }
    cols.push({ key: "unit", label: "", width: "max-content", render: (r) => h("span", "dim", r.unit || "") });
    return cols;
  }

  /* ---- drawing ---- */

  function render() {
    if (!root) return;
    if (st.error) {
      root.replaceChildren(h("div", "pane-error lvl-fault", st.error));
      tableEl = timelineEl = statusEl = null;
      return;
    }
    if (!st.payload) return;
    if (!tableEl) {
      tableEl = h("div", "scroll");
      timelineEl = h("div", "motion-timeline");
      timelineEl.addEventListener("mousedown", onTimelineDown);
      statusEl = h("div", "plot-status");
      root.replaceChildren(tableEl, timelineEl, statusEl);
    }
    drawTable();
    drawTimeline();
    drawStatus();
  }

  function drawTable() {
    const p = st.payload;
    const grid = components.table({
      columns: columns(), rows: p.rows, rowKey,
      selected: [], cursor: { index: st.cursor, col: null },
      onRow: (row, _event, index) => {
        st.cursor = index;
        st.cursorKey = rowKey(row);
        select(row);
        render();
      },
    });
    const notes = [];
    if (!p.rows.length) notes.push(h("div", "c-table-empty", "nothing to watch"));
    if (p.hidden) notes.push(h("div", "c-table-empty", `${p.hidden} more values`));
    p.problems.forEach((text) => notes.push(h("div", "c-table-empty lvl-warn", text)));
    tableEl.replaceChildren(grid, ...notes);
    const line = grid.querySelector(`.c-table-row[data-index="${st.cursor}"]`);
    if (line) line.scrollIntoView({ block: "nearest" });
  }

  function drawTimeline() {
    const p = st.payload;
    const width = timelineEl.clientWidth, height = timelineEl.clientHeight;
    const art = svg("svg", { width, height }, "motion-svg");
    timelineEl.replaceChildren(art);
    if (width < 4 * PAD || height < 3 * PAD || p.ticks < 1) return;
    const span = Math.max(p.ticks - 1, 1);
    const X = (t) => PAD + (t / span) * (width - 2 * PAD);
    const top = PAD, bottom = height - 2 * PAD;
    art.appendChild(svg("line", { x1: PAD, x2: width - PAD, y1: bottom, y2: bottom }, "tl-axis"));
    for (const t of [0, Math.round(span / 2), span]) {
      const label = svg("text", { x: X(t), y: height - 4, "text-anchor": t === 0 ? "start" : t === span ? "end" : "middle" }, "tl-label");
      label.textContent = String(t);
      art.appendChild(label);
    }
    if (p.count < p.ticks) {
      art.appendChild(svg("rect", { x: X(Math.max(p.count, 0)), y: top, width: Math.max(0, X(span) - X(Math.max(p.count, 0))),
        height: bottom - top }, "tl-lost"));
    }
    const row = p.rows[st.cursor];
    if (row) drawSeries(art, row, X, span, top, bottom);
    for (const m of p.markers) {
      const x = X(m.tick);
      const node = m.kind === "failed"
        ? svg("line", { x1: x, x2: x, y1: top, y2: bottom }, "tl-failed")
        : svg("rect", { x: x - 1.5, y: bottom - 5, width: 3, height: 5 }, "tl-break");
      const title = svg("title");
      title.textContent = `tick ${m.tick}: ${m.text}`;
      node.appendChild(title);
      art.appendChild(node);
    }
    const x = X(Math.min(tick(), span));
    art.appendChild(svg("line", { x1: x, x2: x, y1: top - 4, y2: bottom }, "tl-playhead"));
  }

  function drawSeries(art, row, X, span, top, bottom) {
    const values = extent([...row.series, ...(row.vs || [])]);
    if (!values) return;
    const [lo, hi] = values;
    const Y = (v) => (hi > lo ? bottom - 4 - ((v - lo) / (hi - lo)) * (bottom - top - 8) : (top + bottom) / 2);
    const path = (series) => series.map((v, t) => (v === null ? null : `${X(t)},${Y(v)}`))
      .filter(Boolean).join(" ");
    if (row.vs) art.appendChild(svg("polyline", { points: path(row.vs) }, "tl-vs"));
    art.appendChild(svg("polyline", { points: path(row.series) }, "tl-series"));
    for (const [t, label] of [[row.minTick, "min"], [row.maxTick, "max"]]) {
      if (t === null || t === undefined || hi === lo) continue;
      art.appendChild(svg("line", { x1: X(t), x2: X(t), y1: top, y2: bottom }, "tl-extreme"));
      const right = X(t) > X(span) - 6 * PAD;
      const text = svg("text", { x: X(t) + (right ? -3 : 3), y: top + 8, "text-anchor": right ? "end" : "start" },
        "tl-label");
      text.textContent = label;
      art.appendChild(text);
    }
  }

  /** Only the cursor row: the tick is the top bar's, the run's facts the header's. */
  function drawStatus() {
    const p = st.payload;
    const row = p.rows[st.cursor];
    const parts = [h("span", "plot-status-fill")];
    if (row) {
      const lo = row.minTick === null ? "" : ` · min ${fmt(row.series[row.minTick])} @${row.minTick}`;
      const hi = row.maxTick === null ? "" : ` · max ${fmt(row.series[row.maxTick])} @${row.maxTick}`;
      parts.push(h("span", null, `${row.ref} ${fmt(valueAt(row))}${row.unit ? ` ${row.unit}` : ""}${lo}${hi}`));
    }
    statusEl.replaceChildren(...parts);
  }

  /* ---- gestures ---- */

  function select(row) {
    ctx.navigate(row.moteId, row.ref.startsWith("#") ? null : row.ref.slice(0, row.ref.lastIndexOf(".")));
  }

  function moveTo(index) {
    const n = st.payload.rows.length;
    if (!n) return;
    st.cursor = Math.max(0, Math.min(n - 1, index));
    st.cursorKey = rowKey(st.payload.rows[st.cursor]);
    render();
  }

  function seekFromMouse(event) {
    const p = st.payload;
    const rect = timelineEl.getBoundingClientRect();
    const span = Math.max(p.ticks - 1, 1);
    const t = ((event.clientX - rect.left - PAD) / (rect.width - 2 * PAD)) * span;
    store.setPlaying(false);
    claim(true);
    store.seek(t);
  }

  function onTimelineDown(event) {
    if (event.button !== 0 || !st.payload) return;
    event.preventDefault();
    seekFromMouse(event);
    const move = (e) => seekFromMouse(e);
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  async function rewrite(change) {
    try {
      const result = await api.motion.rewrite(st.command, change);
      if (result.error) store.log("result", result.error, { level: "fault" });
      else await ctx.setCommand(result.command);
    } catch (err) {
      store.log("result", message(err), { level: "fault" });
    }
  }

  /** From the scenarios pane (ctx.paneAction): run under / against a scenario. */
  async function action(e) {
    if (e.op === "scenario") await rewrite({ scenario: e.name || null });
    else if (e.op === "vs") await rewrite({ vs: e.name || null });
    return { message: "", level: "ok" };
  }

  const KEYS = {
    j: () => moveTo(st.cursor + 1), ArrowDown: () => moveTo(st.cursor + 1),
    k: () => moveTo(st.cursor - 1), ArrowUp: () => moveTo(st.cursor - 1),
    g: () => moveTo(0), G: () => moveTo(st.payload.rows.length - 1),
    Enter: () => { const row = st.payload.rows[st.cursor]; if (row) select(row); },
    v: () => rewrite({ vs: st.payload.vs ? null : "garden" }),
    W: () => keepKnobs(ctx, st.payload),
  };

  function onKey(event) {
    if (!st.payload) return false;
    if (playheadKey(store, event)) return true;
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    const action_ = KEYS[event.key];
    if (!action_) return false;
    action_();
    return true;
  }

  /* ---- the playhead ---- */

  function claim(force = false) {
    const p = st.payload;
    if (!p || p.error && !p.count) return;
    store.claimPlayhead(token, { count: p.count, run: { ticks: p.ticks, scenario: p.scenario, solver: p.solver, sets: p.sets } },
      { force });
  }

  offs.push(store.subscribe("playhead", (what) => {
    if (!st.payload || !root) return;
    if (what && (what.tick || what.run)) render();
  }));
  offs.push(store.subscribe("layout", (what) => {
    if (what && what.focus && ctx.isFocused()) claim(true);
  }));

  /* ---- the contract ---- */

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    let p;
    try {
      p = api.motion && typeof api.motion.sim === "function" ? await api.motion.sim(command)
        : { error: "this window has no motion api" };
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq || !root) return { summary: "", level: "idle" };
    if (!p || p.error) {
      Object.assign(st, { payload: null, error: (p && p.error) || "no answer" });
      store.releasePlayhead(token);
      tableEl = null;
      render();
      return { summary: st.error, level: "fault" };
    }
    st.payload = p;
    st.error = null;
    const at_ = p.rows.findIndex((r) => rowKey(r) === st.cursorKey);
    st.cursor = at_ >= 0 ? at_ : Math.min(st.cursor, Math.max(p.rows.length - 1, 0));
    claim(ctx.isFocused());
    render();
    const breaks = p.markers.filter((m) => m.kind === "constraint").length;
    const pinned = p.rows.filter((r) => r.state === "pinned").length;
    const parts = [`${p.rows.length} value${p.rows.length === 1 ? "" : "s"}`];
    if (breaks) parts.push(`${breaks} break${breaks === 1 ? "" : "s"}`);
    if (pinned) parts.push(`${pinned} pinned`);
    if (p.error) parts.push(`failed @${p.failedAt}: ${p.error}`);
    if (p.vsError) parts.push(`${p.vs}: ${p.vsError}`);
    if (knobSummary(p)) parts.push(knobSummary(p));
    return { summary: parts.join(" · "), level: p.error || p.vsError ? "fault" : breaks || pinned ? "warn" : "ok" };
  }

  return {
    mount(el, command) {
      st.command = command;
      root = h("div", "view-motion");
      el.appendChild(root);
      resize = new ResizeObserver(() => { if (timelineEl && st.payload) drawTimeline(); });
      resize.observe(root);
    },
    run,
    unmount() {
      offs.splice(0).forEach((off) => off());
      if (resize) resize.disconnect();
      resize = null;
      store.releasePlayhead(token);
      st.seq += 1;
      root = tableEl = timelineEl = statusEl = null;
    },
    hints: () => (st.payload ? [...HINTS, ...(hasKnobs(st.payload) ? [KEEP_HINT] : [])] : []),
    onKey,
    action,
  };
}

export default {
  id: "sim",
  describe: "run the garden tick by tick and watch its values (sim)",
  commands: [{ verb: "sim" }],
  followsSel: false,
  create,
};
