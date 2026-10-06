/* The flow mode of `graph` (`graph [@sel|#id|name|m.k] --flow [--depth N]`):
 * the solve, left to right -- givens, one column per step, answers
 * (docs/console.md → Settled for `graph --flow`). views/graph.js picks
 * this or the force graph by the line.
 *
 * core/flowview.py lays it out (api.monarch.flow); js/flow.js draws it.
 * The solve follows the shared playhead's run (its scenario, and a
 * value per tick); a write re-runs the pane, so `live` needs nothing
 * here. Fold state is the pane's own, sent with every run: `z` and `Z`
 * change it and run again, since a fold changes the layout.
 *
 * Every edit is a command on the command line, never a call:
 *   e   `set m.k ‹value› ‹unit›` on a row, `set #rule equation …` on a rule
 *
 * Keys:
 *   h j k l / arrows  walk              ⏎  select         o  open
 *   z  fold under the cursor  Z  all     [ ]  depth         f  fit
 *   + -  zoom   0  100%                  space , .  the playhead */

import { FlowBoard, CHECK_MARK, SOURCE_MARK } from "../flow.js";
import { createPalette } from "../palette.js";
import { KEEP_HINT, hasKnobs, keepKnobs, knobSummary } from "../knobs.js";
import { PLAYHEAD_HINTS, playheadKey, at } from "../playhead.js";
import { fmt } from "../plot.js";

const HINTS = [["h j k l", "walk"], ["⏎", "select"], ["o", "open"], ["e", "set"], ["z Z", "fold"],
  ["[ ]", "depth"], ["f", "fit"], ["+ -", "zoom"]];
const DIRECTIONS = { h: "left", ArrowLeft: "left", l: "right", ArrowRight: "right",
  j: "down", ArrowDown: "down", k: "up", ArrowUp: "up" };
const PLAYHEAD_KEYS = new Set([" ", ",", "."]);  // [ ] are depth here

function message(err) {
  return String(err && err.message ? err.message : err);
}

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const st = { command: "", seq: 0, payload: null, error: null, empty: false, folds: {}, runKey: null };
  let root = null, board = null, corners = null, errorBox = null;
  const offs = [];

  const valueOf = (row) => (row.series ? at(row.series, store.state.playhead.tick) : row.value);

  /* ---- running ---- */

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    const playRun = store.state.playhead.run || null;
    let p;
    try {
      p = await api.monarch.flow(command, store.state.selection, playRun, st.folds);
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq || !root) return { summary: "", level: "idle" };
    st.runKey = JSON.stringify(playRun);
    if (!p || p.error) {
      st.empty = /@sel is empty/.test((p && p.error) || "");
      st.error = st.empty ? "nothing selected" : (p && p.error) || "no answer";
      st.payload = null;
      draw();
      return { summary: st.error, level: st.empty ? "idle" : "fault" };
    }
    Object.assign(st, { error: null, empty: false, payload: p });
    board.setData(p, createPalette(p.speciesIds), valueOf);
    board.setSelection(store.state.selection);
    if (!board.cursor) board.setCursor(board.firstTarget(), { pan: false });
    draw();
    return summary(p);
  }

  function summary(p) {
    const s = p.summary;
    const parts = [p.root ? `${p.rootName} · depth ${p.depth}` : "flow", `${s.steps} step${s.steps === 1 ? "" : "s"}`];
    if (s.coupled) parts.push(`${s.coupled} coupled`);
    parts.push(`${s.answers} answer${s.answers === 1 ? "" : "s"}`);
    if (s.checks) parts.push(`checks ${s.passed}/${s.checks}`);
    if (p.solve.state !== "solved") parts.push(p.solve.message);
    if (knobSummary(p)) parts.push(knobSummary(p));
    const level = p.solve.state === "failed" ? "fault"
      : p.solve.state === "partial" || s.passed < s.checks ? "warn" : "ok";
    return { summary: parts.join(" · "), level };
  }

  function draw() {
    if (!root) return;
    errorBox.hidden = !st.error || st.empty;
    errorBox.textContent = st.error || "";
    board.root.hidden = !!st.error;
    corners.hidden = !!st.error;
    if (st.error || !st.payload) return;
    const p = st.payload;
    corners.topLeft.textContent = `flow${p.scenario ? ` · under ${scenarioName()}` : ""}`
      + `${store.state.playhead.run && store.state.playhead.run.ticks ? ` · tick ${store.state.playhead.tick}` : ""}`;
    corners.bottomLeft.textContent = focusText();
    corners.bottomRight.textContent = `${Math.round(board.camera.scale * 100)}%`;
  }

  function scenarioName() {
    const run = store.state.playhead.run;
    return run && run.scenario ? run.scenario : "a scenario";
  }

  /** What the footer says about the cursor. */
  function focusText() {
    const info = board.describe(board.cursor);
    if (!info) return "";
    if (info.kind === "fold") return `${info.card.title} · z unfolds`;
    if (info.kind === "card") return [info.card.name, info.card.equation].filter(Boolean).join(" · ");
    const r = info.row;
    const bits = [`${r.ref} = ${fmt(valueOf(r))}${r.unit ? ` ${r.unit}` : ""}`, `${SOURCE_MARK[r.source] || ""} ${r.source}`];
    if (r.check) bits.push(`${CHECK_MARK[r.check]} ${r.check}`);
    if (r.problem) bits.push(r.problem);
    return bits.join(" · ");
  }

  /* ---- gestures → commands ---- */

  function pick(id) {
    board.setCursor(id, { pan: false });
    const info = board.describe(id);
    if (info && info.kind === "row") ctx.navigate(info.stub.moteId, info.stub.name);
    else if (info && info.kind === "card") ctx.navigate(info.card.ruleMoteId, info.card.name);
    draw();
  }

  function moteOf(info) {
    if (!info) return null;
    return info.kind === "row" ? info.stub.moteId : info.kind === "card" ? info.card.ruleMoteId : null;
  }

  function open(id = board.cursor) {
    const mote = moteOf(board.describe(id));
    if (mote) ctx.runCommand(`open #${mote}`, { newPane: true });
  }

  function prefill() {
    const info = board.describe(board.cursor);
    if (!info) return;
    if (info.kind === "row") {
      const r = info.row;
      const value = r.value === null || r.value === undefined ? "" : String(r.value);
      ctx.prefill(`set ${r.ref} ${value}${value && r.unit ? ` ${r.unit}` : ""}`);
    } else if (info.kind === "card") {
      ctx.prefill(`set #${info.card.ruleMoteId} equation ${info.card.equation}`);
    }
  }

  function fold(all) {
    const p = st.payload;
    if (!p) return;
    if (!p.folds.length) {
      store.log("result", "nothing to fold here", { level: "warn" });
      return;
    }
    if (all) {
      const open = !p.folds.some((f) => f.folded);
      for (const f of p.folds) st.folds[f.id] = open;
    } else {
      const info = board.describe(board.cursor);
      const id = info && info.group && info.group.folds[0];
      if (!id) {
        store.log("result", "nothing to fold here", { level: "warn" });
        return;
      }
      const now = p.folds.find((f) => f.id === id);
      st.folds[id] = !(now && now.folded);
    }
    run(st.command).then((r) => ctx.setSummary(r));
  }

  async function rewrite(change) {
    if (!st.payload || !st.payload.root) return;
    let r;
    try {
      r = await api.monarch.rewrite(st.command, change);
    } catch (err) {
      r = { error: message(err) };
    }
    if (r.error) store.log("result", r.error, { level: "warn" });
    else await ctx.setCommand(r.command);
  }

  function walk(direction) {
    const next = board.cursor ? board.neighbour(board.cursor, direction) : board.firstTarget();
    if (next) board.setCursor(next);
    draw();
  }

  const KEYS = {
    Enter: () => board.cursor && pick(board.cursor), o: () => open(), e: prefill,
    W: () => keepKnobs(ctx, st.payload),
    z: () => fold(false), Z: () => fold(true),
    "[": () => rewrite({ depth: -1 }), "]": () => rewrite({ depth: 1 }),
    f: () => board.fit(), "+": () => board.zoom(1.25), "=": () => board.zoom(1.25), "-": () => board.zoom(0.8),
    0: () => {
      board.zoom(1 / board.camera.scale);
      if (board.cursor) board.reveal(board.ports.get(board.cursor));
    },
  };

  function onKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey || !st.payload) return false;
    if (PLAYHEAD_KEYS.has(event.key) && playheadKey(store, event)) return true;
    if (event.key in DIRECTIONS) {
      walk(DIRECTIONS[event.key]);
      return true;
    }
    const action = KEYS[event.key];
    if (!action) return false;
    action();
    draw();
    return true;
  }

  /* ---- what it follows ---- */

  offs.push(store.subscribe("selection", () => {
    if (board && st.payload) board.setSelection(store.state.selection);
  }));
  offs.push(store.subscribe("playhead", (what) => {
    if (!board || !st.payload) return;
    if (what && what.run) {
      if (JSON.stringify(store.state.playhead.run || null) !== st.runKey) {
        run(st.command).then((r) => ctx.setSummary(r));
      }
    } else if (what && what.tick) {
      board.refreshValues();
      draw();
    }
  }));
  // Other panes re-rendering can resize this one: measure the ports again.
  let repaint = 0;
  offs.push(store.subscribe("layout", () => {
    cancelAnimationFrame(repaint);
    repaint = requestAnimationFrame(() => board && board.drawWires());
  }));

  return {
    mount(el) {
      root = h("div", "view-graph view-flow");
      const surface = h("div", "flow-surface");
      errorBox = h("div", "pane-error lvl-fault");
      errorBox.hidden = true;
      corners = h("div", "graph-corners");
      corners.topLeft = h("span", "graph-corner top-left");
      corners.bottomLeft = h("span", "graph-corner bottom-left");
      corners.bottomRight = h("span", "graph-corner bottom-right");
      corners.append(corners.topLeft, corners.bottomLeft, corners.bottomRight);
      root.append(surface, corners, errorBox);
      el.appendChild(root);
      board = new FlowBoard(surface, {
        onPick: pick,
        onOpen: (id) => open(id),
        onCamera: () => draw(),
      });
      surface.flow = board;  // the smoke test reads the board off it
    },
    run,
    unmount() {
      offs.forEach((off) => off());
      cancelAnimationFrame(repaint);
      st.seq += 1;
      if (board) board.destroy();
      root = board = corners = errorBox = null;
    },
    hints: () => (st.payload ? [...HINTS, ...(hasKnobs(st.payload) ? [KEEP_HINT] : []),
      ...(store.state.playhead.count ? PLAYHEAD_HINTS.slice(0, 2) : [])] : []),
    onKey,
  };
}

export default { create };
