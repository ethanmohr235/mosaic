/* `motion:scenarios` (`scenarios`, `layout calc`): the garden's scenarios and
 * what each overrides, beside the garden's own value (docs/console.md →
 * Settled for U5).
 *
 * The first row is the garden as it stands. ⏎ runs the sim pane under
 * the scenario at the cursor, `v` compares the sim pane against it
 * (ctx.paneAction; with no sim pane, a new one opens). Editing is
 * commands: `a` puts `set --scenario ‹s› ` on the command line to add an
 * override, `n` starts a new scenario the same way, and `d d` clears the
 * override at the cursor (`set --scenario ‹s› ‹m.k› --clear`). */

import { fmt } from "../plot.js";
import { PLAYHEAD_HINTS, playheadKey } from "../playhead.js";

const HINTS = [["j k", "move"], ["⏎", "run under"], ["v", "compare"], ["a", "add override"],
  ["n", "new scenario"], ["d d", "clear override"], ...PLAYHEAD_HINTS];
const GARDEN = "(garden)";

function message(err) {
  return String(err && err.message ? err.message : err);
}

function arg(text) {
  return /^[^"'\\\s-][^"'\\\s]*$/.test(text) ? text : JSON.stringify(text);
}

function shown(value) {
  if (value === null || value === undefined) return "";
  return typeof value === "number" ? fmt(value) : String(value);
}

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const st = { seq: 0, payload: null, error: null, rows: [], cursor: 0, armed: null };
  let body = null;
  const offs = [];

  function buildRows(scenarios) {
    const rows = [{ type: "scenario", name: null, label: GARDEN, count: null }];
    for (const s of scenarios) {
      rows.push({ type: "scenario", name: s.name, label: s.name, count: s.overrides.length });
      for (const o of s.overrides) rows.push({ type: "override", name: s.name, ...o });
    }
    return rows.map((r, i) => ({ ...r, index: i }));
  }

  function active() {
    const run = store.state.playhead.run;
    return run ? run.scenario || null : undefined;
  }

  function render() {
    if (!body) return;
    if (st.error) return body.replaceChildren(h("div", "pane-error lvl-fault", st.error));
    if (!st.payload) return;
    const running = active();
    const grid = components.table({
      columns: [
        { key: "label", label: "scenario", width: "minmax(12ch, 1fr)", render: (r) => (r.type === "scenario"
          ? h("span", `motion-scenario${r.name === running ? " running" : ""}`,
            `${r.name === running ? "▶ " : ""}${r.label}`)
          : h("span", null, `  ${r.ref}`)) },
        { key: "value", label: "value", width: "10ch", align: "right",
          render: (r) => (r.type === "scenario" ? h("span", "dim", r.count === null ? "" : `${r.count}`) : shown(r.value)) },
        { key: "garden", label: "garden", width: "10ch", align: "right",
          render: (r) => h("span", "dim", r.type === "override" ? shown(r.garden) : "") },
        { key: "unit", label: "", width: "max-content", render: (r) => h("span", "dim", r.unit || "") },
      ],
      rows: st.rows, rowKey: (r) => String(r.index),
      selected: st.armed !== null ? [String(st.armed)] : [],
      cursor: { index: st.cursor, col: null },
      onRow: (row) => {
        st.cursor = row.index;
        render();
      },
    });
    const parts = [grid];
    if (st.rows.length === 1) {
      parts.push(h("div", "c-table-empty", "no scenarios"));
    }
    body.replaceChildren(...parts);
  }

  /* ---- gestures ---- */

  const here = () => st.rows[st.cursor] || null;

  function moveTo(index) {
    st.cursor = Math.max(0, Math.min(st.rows.length - 1, index));
    st.armed = null;
    render();
  }

  async function toSim(op) {
    const row = here();
    if (!row) return;
    const name = row.name;
    const done = await ctx.paneAction("motion:state", { op, name });
    if (done) return;
    const flag = op === "vs" ? `--vs ${arg(name || "garden")}` : name ? `--scenario ${arg(name)}` : "";
    await ctx.runCommand(`sim ${flag}`.trim(), { newPane: true });
  }

  function add() {
    const row = here();
    if (!row || !row.name) {
      ctx.prefill("set --scenario ");
      return;
    }
    ctx.prefill(`set --scenario ${arg(row.name)} `);
  }

  async function clear() {
    const row = here();
    if (!row || row.type !== "override") return;
    if (st.armed !== row.index) {
      st.armed = row.index;
      render();
      store.log("result", "d again to clear the override", { level: "warn" });
      return;
    }
    st.armed = null;
    await ctx.runCommand(`set --scenario ${arg(row.name)} ${arg(row.ref)} --clear`);
  }

  const KEYS = {
    j: () => moveTo(st.cursor + 1), ArrowDown: () => moveTo(st.cursor + 1),
    k: () => moveTo(st.cursor - 1), ArrowUp: () => moveTo(st.cursor - 1),
    g: () => moveTo(0), G: () => moveTo(st.rows.length - 1),
    Enter: () => toSim("scenario"),
    v: () => toSim("vs"),
    a: add,
    n: () => ctx.prefill("set --scenario "),
    d: clear,
  };

  function onKey(event) {
    if (!st.payload) return false;
    if (playheadKey(store, event)) return true;
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    if (event.key === "Escape" && st.armed !== null) {
      st.armed = null;
      render();
      return true;
    }
    const action = KEYS[event.key];
    if (!action) return false;
    if (event.key !== "d") st.armed = null;
    action();
    return true;
  }

  offs.push(store.subscribe("playhead", (what) => {
    if (what && what.run) render();
  }));

  async function run() {
    const seq = ++st.seq;
    let p;
    try {
      p = api.motion && typeof api.motion.scenarios === "function" ? await api.motion.scenarios()
        : { error: "this window has no motion api" };
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq || !body) return { summary: "", level: "idle" };
    if (!p || p.error) {
      Object.assign(st, { payload: null, error: (p && p.error) || "no answer" });
      render();
      return { summary: st.error, level: "fault" };
    }
    Object.assign(st, { payload: p, error: null, rows: buildRows(p.scenarios) });
    st.cursor = Math.min(st.cursor, st.rows.length - 1);
    render();
    const n = p.scenarios.length;
    return { summary: `${n} scenario${n === 1 ? "" : "s"}`, level: "ok" };
  }

  return {
    mount(el) {
      body = h("div", "view-table scroll");
      el.appendChild(body);
    },
    run,
    unmount() {
      offs.splice(0).forEach((off) => off());
      st.seq += 1;
      body = null;
    },
    hints: () => (st.payload ? HINTS : []),
    onKey,
  };
}

export default {
  id: "motion:scenarios",
  app: "tion",
  describe: "the garden's scenarios and what each overrides (scenarios)",
  commands: [{ verb: "scenarios" }],
  followsSel: false,
  create,
};
