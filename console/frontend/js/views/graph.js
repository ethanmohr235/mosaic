/* `graph` (`graph [@sel|#id|name] [--depth N] …`, `layout graph`): a
 * mote's neighbourhood, or the whole garden, on the canvas graph
 * (js/graph.js; docs/console.md → Settled for U8).
 *
 * The line says what is drawn; core/graphview.py answers it. With a `@sel`
 * root the graph re-roots as the selection moves. Pins (a dragged node
 * stays where it was put) are the garden's UI state, kept beside it by
 * api.monarch, not in the line.
 *
 * Every edit is a command on the command line, never a call:
 *   ctrl+drag A → B   `A -> ‹type› -> B`, the caret on the type
 *   n                 `‹cursor› -> related_to -> `, to name a new mote
 *   d d               `delete ‹cursor›`, or `unlink …` on an edge
 *
 * Keys:
 *   h j k l / arrows  walk to the neighbour that way    ⏎  select
 *   o / double-click  open (props, new pane)            f  fit
 *   + -  zoom         0  100% at the cursor              [ ]  depth
 *   u  unpin          U  unpin every node                esc  clear */

import { GraphCanvas } from "../graph.js";
import { createPalette } from "../palette.js";

const HINTS = [["h j k l", "walk"], ["⏎", "select"], ["o", "open"], ["[ ]", "depth"], ["f", "fit"],
  ["+ -", "zoom"], ["n", "new"], ["d d", "delete"], ["u U", "unpin"], ["^drag", "link"]];
const DIRECTIONS = { l: 0, ArrowRight: 0, j: 90, ArrowDown: 90, h: 180, ArrowLeft: 180, k: 270, ArrowUp: 270 };
const SIMPLE = /^[A-Za-z_][\w]*$/;

function message(err) {
  return String(err && err.message ? err.message : err);
}

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const st = { command: "", seq: 0, payload: null, error: null, armed: null, palette: createPalette([]) };
  let root = null, canvas = null, graph = null, corners = null, errorBox = null;

  const node = (id) => (st.payload ? st.payload.motes.find((m) => m.id === id) : null);
  const edge = (id) => (st.payload ? st.payload.edges.find((e) => e.id === id) : null);

  /** How a statement names a mote: its name when that is one plain word
   *  no other mote here shares, else `#id`. */
  function ref(id) {
    const mote = node(id);
    const name = mote && mote.name;
    if (name && SIMPLE.test(name) && st.payload.motes.filter((m) => m.name === name).length === 1) return name;
    return `#${id}`;
  }

  /* ---- running ---- */

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    let p, pins = {};
    try {
      [p, pins] = await Promise.all([api.monarch.view(command, store.state.selection), api.monarch.pins()]);
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq || !root) return { summary: "", level: "idle" };
    if (!p || p.error) {
      // An empty @sel is a state, not a fault: say what to do, as props does.
      st.empty = /@sel is empty/.test((p && p.error) || "");
      st.error = st.empty ? "nothing selected" : (p && p.error) || "no answer";
      st.payload = null;
      draw();
      return { summary: st.empty ? "nothing selected" : st.error, level: st.empty ? "idle" : "fault" };
    }
    st.error = null;
    st.payload = p;
    st.palette = createPalette(p.speciesIds);
    graph.setData(p, (m) => st.palette.colorOf(m), pins || {});
    const primary = store.primary();
    graph.setSelection(store.state.selection, primary);
    if (!graph.cursor || !node(graph.cursor)) graph.setCursor(node(primary) ? primary : p.root, { pan: false });
    draw();
    const what = p.root ? `${p.rootName} · depth ${p.depth}` : "garden";
    return { summary: `${what} · ${p.shown}/${p.total}${p.problems.length ? ` · ${p.problems[0]}` : ""}`,
      level: p.problems.length ? "warn" : "ok" };
  }

  /** The DOM around the canvas: the error, and the corner notes. */
  function draw() {
    if (!root) return;
    errorBox.hidden = !st.error || st.empty;  // the header already says so
    errorBox.textContent = st.error || "";
    errorBox.className = `pane-error ${st.empty ? "dim" : "lvl-fault"}`;
    canvas.hidden = !!st.error;
    corners.hidden = !!st.error;
    if (st.error || !st.payload) return;
    const p = st.payload;
    corners.topLeft.textContent = `force · ${p.root ? `depth ${p.depth}` : "whole garden"}`
      + `${p.relTypes.some((t) => t.hidden) ? ` · hiding ${p.relTypes.filter((t) => t.hidden).map((t) => t.type).join(",")}` : ""}`;
    corners.bottomRight.textContent = `${Math.round(graph.camera.scale * 100)}% · ${p.shown}/${p.total} shown`;
    corners.bottomLeft.textContent = st.armed ? "d again to delete" : focusText();
    corners.bottomLeft.classList.toggle("lvl-warn", !!st.armed);
  }

  /** What the bottom-left corner says about the edge or node in focus. */
  function focusText() {
    const e = edge(graph.hoverEdge || graph.edgeCursor);
    if (e) return `${node(e.from)?.name ?? "?"} → ${e.type} → ${node(e.to)?.name ?? "?"}`;
    const m = node(graph.hover || graph.cursor);
    if (!m) return "";
    const pinned = graph.nodes.get(m.id)?.pinned ? " · pinned" : "";
    return `${m.name}${m.speciesName ? ` · ${m.speciesName}` : ""}${m.kinds.length ? ` · ${m.kinds.join(", ")}` : ""}${pinned}`;
  }

  async function rewrite(change) {
    let r;
    try {
      r = await api.monarch.rewrite(st.command, change);
    } catch (err) {
      r = { error: message(err) };
    }
    if (r.error) store.log("result", r.error, { level: "warn" });
    else await ctx.setCommand(r.command);
  }

  /* ---- gestures → commands ---- */

  function pick(id) {
    st.armed = null;
    graph.setCursor(id, { pan: false });
    const m = node(id);
    ctx.navigate(id, m ? m.name : undefined);
    draw();
  }

  function open(id = graph.cursor) {
    if (id) ctx.runCommand(`open #${id}`, { newPane: true });
  }

  function connect(from, to) {
    const head = `${ref(from)} -> `;
    ctx.prefill(`${head} -> ${ref(to)}`, head.length);
  }

  function newMote() {
    if (graph.cursor) ctx.prefill(`${ref(graph.cursor)} -> related_to -> `);
    else ctx.prefill("");
  }

  async function remove() {
    const target = graph.edgeCursor || graph.cursor;
    if (!target) return;
    if (st.armed !== target) {
      st.armed = target;
      store.log("result", "d again to delete", { level: "warn" });
      draw();
      return;
    }
    st.armed = null;
    draw();
    const e = edge(graph.edgeCursor);
    if (e) await ctx.runCommand(`unlink #${e.from} #${e.id}`, { confirm: true });
    else await ctx.runCommand(`delete ${ref(graph.cursor)}`, { confirm: true });
  }

  async function savePin(id, x, y) {
    const r = await api.monarch.pin({ [id]: [x, y] });
    if (r && r.error) store.log("result", r.error, { level: "fault" });
    draw();
  }

  async function unpin(all) {
    const ids = all ? null : graph.cursor ? [graph.cursor] : [];
    if (ids && !ids.length) return;
    graph.unpin(ids);
    const r = await api.monarch.unpin(ids);
    if (r && r.error) store.log("result", r.error, { level: "fault" });
    draw();
  }

  function walk(degrees) {
    const from = graph.cursor || store.primary() || (st.payload && st.payload.root);
    if (!from || !node(from)) {
      if (st.payload && st.payload.motes.length) graph.setCursor(st.payload.motes[0].id);
      draw();
      return;
    }
    const next = graph.neighbourToward(from, degrees);
    if (next) graph.setCursor(next);
    draw();
  }

  const KEYS = {
    Enter: () => graph.cursor && pick(graph.cursor), o: () => open(),
    f: () => graph.fit(), "+": () => graph.zoom(1.25), "=": () => graph.zoom(1.25), "-": () => graph.zoom(0.8),
    0: () => {
      graph.zoom(1 / graph.camera.scale);
      if (graph.cursor) graph.panTo(graph.cursor);
    },
    "[": () => rewrite({ depth: -1 }), "]": () => rewrite({ depth: 1 }),
    u: () => unpin(false), U: () => unpin(true), n: newMote, d: remove,
  };

  function onKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey || !st.payload) return false;
    if (event.key === "Escape") {
      if (!st.armed && !graph.edgeCursor) return false;
      st.armed = null;
      graph.setEdgeCursor(null);
      draw();
      return true;
    }
    if (event.key in DIRECTIONS) {
      st.armed = null;
      walk(DIRECTIONS[event.key]);
      return true;
    }
    const action = KEYS[event.key];
    if (!action) return false;
    if (event.key !== "d") st.armed = null;
    action();
    draw();
    return true;
  }

  const offSelection = store.subscribe("selection", () => {
    if (!graph || !st.payload) return;
    graph.setSelection(store.state.selection, store.primary());
    draw();
  });
  // Other panes re-rendering move this one's element in the page; paint
  // again once they have, in case the canvas was resized or dropped.
  let repaint = 0;
  const offLayout = store.subscribe("layout", () => {
    cancelAnimationFrame(repaint);
    repaint = requestAnimationFrame(() => {
      if (!graph) return;
      graph.resize();
      graph.draw();
    });
  });
  const off = () => {
    offSelection();
    offLayout();
    cancelAnimationFrame(repaint);
  };

  return {
    mount(el) {
      root = h("div", "view-graph");
      canvas = h("canvas", "graph-canvas");
      errorBox = h("div", "pane-error lvl-fault");
      errorBox.hidden = true;
      corners = h("div", "graph-corners");
      corners.topLeft = h("span", "graph-corner top-left");
      corners.bottomLeft = h("span", "graph-corner bottom-left");
      corners.bottomRight = h("span", "graph-corner bottom-right");
      corners.append(corners.topLeft, corners.bottomLeft, corners.bottomRight);
      root.append(canvas, corners, errorBox);
      el.appendChild(root);
      graph = new GraphCanvas(canvas, {
        onPick: (id) => pick(id),
        onOpen: (id) => open(id),
        onEdge: (e) => { st.armed = null; graph.setEdgeCursor(e.id); draw(); },
        onEmpty: () => { st.armed = null; graph.setEdgeCursor(null); draw(); },
        onConnect: connect,
        onPinned: savePin,
        onHover: draw,
        onSettle: draw,
      });
      canvas.graph = graph;  // the smoke test reads node positions off it
    },
    run,
    unmount() {
      off();
      st.seq += 1;
      if (graph) graph.destroy();
      root = canvas = graph = corners = errorBox = null;
    },
    hints: () => (st.payload ? HINTS : []),
    onKey,
  };
}

export default {
  id: "graph",
  describe: "a mote's neighbourhood, or the garden, as a graph (graph)",
  commands: [{ verb: "graph", canonical: "graph @sel --depth 2" }],
  followsSel: false,
  create,
};
