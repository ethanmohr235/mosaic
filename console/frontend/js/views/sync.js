/* `sync`: every garden's sync state on this machine (docs/console.md →
 * Sync), answered by core/sync_commands.py::pane.
 *
 * Name clashes come first, each asking its question ("compass also has a
 * garden named 'x': merge or rename?"); then one row per garden from the
 * last pass, then who is syncing. Every act is a command:
 *   m  sync merge ‹garden›     (a clash: join the server's garden)
 *   r  sync rename ‹garden› …  (a clash: move this one aside; prefills)
 *   l  sync local|share ‹garden›
 *   n  sync now                ⏎  cd ‹garden› */

import { arg } from "./gardens.js";

const HINTS = [["j k", "move"], ["m", "merge"], ["r", "rename"], ["l", "local / share"], ["n", "sync now"], ["⏎", "open (cd)"]];

const MARKS = { synced: "⇅", pending: "↑", local: "○", conflict: "?", waiting: "…", error: "×", new: "+" };
const LEVELS = { synced: "dim", pending: "dim", local: "dim", new: "dim", conflict: "lvl-warn", waiting: "lvl-warn", error: "lvl-fault" };

function message(err) {
  return String(err && err.message ? err.message : err);
}

/** The mark Home and this pane put beside a garden for its sync state. */
export function syncMark(state) {
  return MARKS[state] || "";
}

function create(ctx) {
  const { api, components } = ctx;
  const { h } = components;
  const st = { command: "sync", seq: 0, payload: null, error: null, rows: [], cursor: 0 };
  let body = null;

  const here = () => st.rows[st.cursor] || null;

  function head(p) {
    if (!p.account) return h("div", "home-problems lvl-warn", "not signed in -- run `monoss sync login ‹url›` once on this machine");
    const last = p.last || {};
    const worker = p.worker.running ? `${p.worker.kind || "a worker"} syncing` : "no worker running";
    return components.kvGrid([
      ["machine", `${p.account.device} → ${p.account.url}`],
      ["last pass", last.ok === false ? h("span", "lvl-fault", `${p.finished}: ${last.error}`) : p.finished],
      ["worker", h("span", p.worker.running ? "" : "lvl-warn", worker)],
    ]);
  }

  function render() {
    if (!body) return;
    if (st.error) return body.replaceChildren(h("div", "pane-error lvl-fault", st.error));
    const p = st.payload;
    if (!p) return;
    const ask = st.rows.filter((r) => r.state === "conflict");
    const rest = st.rows.filter((r) => r.state !== "conflict");
    const grid = components.table({
      columns: [
        { key: "mark", label: "", width: "2ch", render: (r) => h("span", LEVELS[r.state] || "dim", syncMark(r.state)) },
        { key: "name", label: "garden", width: "minmax(14ch, 1fr)",
          render: (r) => h("span", `home-garden${r.name === p.current ? " current" : ""}`, r.name) },
        { key: "state", label: "state", width: "9ch", render: (r) => h("span", LEVELS[r.state] || "", r.state) },
        { key: "pending", label: "to send", width: "8ch", align: "right", render: (r) => (r.pending ? String(r.pending) : "") },
        { key: "held", label: "held", width: "6ch", align: "right", render: (r) => (r.held ? String(r.held) : "") },
        { key: "detail", label: "", width: "minmax(20ch, 2fr)", render: (r) => h("span", r.state === "conflict" ? "lvl-warn" : "dim", r.detail || "") },
      ],
      groups: [
        { key: "ask", label: "to answer", labelled: true, rows: ask, count: ask.length },
        { key: "gardens", label: "gardens", labelled: true, rows: rest, count: rest.length },
      ].filter((g) => g.count),
      rowKey: (r) => r.key,
      selected: [],
      cursor: { index: st.cursor, col: null },
      onRow: (row) => pointAt(st.rows.findIndex((r) => r.key === row.key)),
    });
    grid.querySelectorAll(".c-group-all").forEach((n) => n.remove());
    const parts = [head(p), grid];
    if (p.notes.length) parts.push(h("div", "home-problems dim", p.notes.join("\n")));
    body.replaceChildren(...parts);
    const line = grid.querySelector(`.c-table-row[data-index="${st.cursor}"]`);
    if (line) line.scrollIntoView({ block: "nearest" });
  }

  function pointAt(index) {
    if (!st.rows.length) return;
    st.cursor = Math.max(0, Math.min(st.rows.length - 1, index));
    render();
  }

  const on = (f) => () => { const row = here(); if (row) f(row); };
  const onClash = (f) => on((r) => {
    if (r.state === "conflict") f(r);
    else ctx.store.log("result", `${r.name} has no name clash to answer`, { level: "warn" });
  });
  const KEYS = {
    j: () => pointAt(st.cursor + 1), ArrowDown: () => pointAt(st.cursor + 1),
    k: () => pointAt(st.cursor - 1), ArrowUp: () => pointAt(st.cursor - 1),
    g: () => pointAt(0), G: () => pointAt(st.rows.length - 1),
    Enter: on((r) => ctx.runCommand(`cd ${arg(r.name)}`)),
    m: onClash((r) => ctx.runCommand(`sync merge ${arg(r.name)}`)),
    r: onClash((r) => ctx.prefill(`sync rename ${arg(r.name)} `)),
    l: on((r) => ctx.runCommand(`sync ${r.state === "local" ? "share" : "local"} ${arg(r.name)}`)),
    n: () => ctx.runCommand("sync now"),
  };

  function onKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey || !st.payload) return false;
    const action = KEYS[event.key];
    if (!action) return false;
    action();
    return true;
  }

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    let p;
    try {
      p = await api.home.sync(command);
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq || !body) return { summary: "", level: "idle" };
    if (!p || p.error) {
      Object.assign(st, { payload: null, error: (p && p.error) || "no answer", rows: [] });
      render();
      return { summary: st.error, level: "fault" };
    }
    const before = here();
    const clashes = p.conflicts.map((c) => ({ name: c.name, state: "conflict", detail: c.question }));
    const rows = [...clashes, ...p.rows].map((r, i) => ({ ...r, key: `${r.name}:${r.state}:${i}` }));
    Object.assign(st, { payload: p, error: null, rows });
    const again = before ? rows.findIndex((r) => r.name === before.name) : 0;
    st.cursor = Math.max(0, again);
    render();
    if (!p.account) return { summary: "not signed in", level: "warn" };
    if (p.last && p.last.ok === false) return { summary: "offline", detail: p.last.error, level: "fault" };
    if (clashes.length) return { summary: `${clashes.length} to answer`, level: "warn" };
    return { summary: `${p.rows.length} garden${p.rows.length === 1 ? "" : "s"} · ${p.finished}`, level: "ok" };
  }

  return {
    mount(el) {
      body = h("div", "view-table view-gardens scroll");
      el.appendChild(body);
    },
    run,
    unmount() {
      st.seq += 1;
      body = null;
    },
    hints: () => (st.payload ? HINTS : []),
    onKey,
  };
}

export default {
  id: "sync",
  describe: "every garden's sync state, and name clashes to answer (sync)",
  commands: [{ verb: "sync" }],
  followsSel: false,
  create,
};
