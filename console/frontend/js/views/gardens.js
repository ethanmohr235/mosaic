/* `gardens` (`gardens [--archived]`, `layout gardens`): every garden as
 * the ownership tree (docs/console.md → Settled for U9).
 *
 * Owned gardens sit under their owner's garden, labelled with the cover
 * (name, due, status); then the loose gardens; then the archived ones,
 * folded unless the line says `--archived` (`Z` rewrites it). The cursor
 * hands its garden to the `garden` pane (ctx.paneAction), which shows it.
 *
 * Every act is a command:
 *   ⏎  cd ‹garden›            z  garden archive|unarchive ‹garden›
 *   n  garden new …           r  garden rename ‹garden› …
 *   d d  garden rm ‹garden› (runs) s  garden sync ‹garden› …
 *   E  garden export ‹garden› (a file dialog)   I  garden import … */

const HINTS = [["j k", "move"], ["⏎", "open (cd)"], ["n", "new"], ["r", "rename"], ["d d", "remove"],
  ["z", "archive"], ["s", "sync"], ["E I", "export, import"], ["Z", "show archived"]];

function message(err) {
  return String(err && err.message ? err.message : err);
}

/** A garden name as one word of a command line. */
export function arg(text) {
  return /^[^"'\\\s-][^"'\\\s]*$/.test(text) ? text : JSON.stringify(text);
}

function create(ctx) {
  const { api, components } = ctx;
  const { h } = components;
  const st = { command: "gardens", seq: 0, payload: null, error: null, rows: [], cursor: 0, armed: null };
  let body = null;

  const here = () => st.rows[st.cursor] || null;

  function render() {
    if (!body) return;
    if (st.error) return body.replaceChildren(h("div", "pane-error lvl-fault", st.error));
    if (!st.payload) return;
    const p = st.payload;
    const groups = p.groups.map((g) => ({
      key: g.key, rows: g.rows, labelled: true, count: g.key === "archived" ? g.count : g.rows.length,
      label: g.key === "archived" && g.folded && g.count ? "archived" : g.label,
    })).filter((g) => g.count);
    const grid = components.table({
      columns: [
        { key: "garden", label: "garden", width: "minmax(14ch, 1fr)", render: (r) => h("span",
          `home-garden${r.current ? " current" : ""}`, `${"  ".repeat(r.depth)}${r.depth ? "└ " : ""}${r.garden}`) },
        { key: "cover", label: "cover", width: "minmax(10ch, 1fr)", render: (r) => h("span", "dim", r.cover ? r.cover.name : "") },
        { key: "due", label: "due", width: "11ch", render: (r) => (r.cover && r.cover.due ? r.cover.due.slice(0, 10) : "") },
        { key: "status", label: "status", width: "9ch", render: (r) => (r.cover && r.cover.status) || "" },
        { key: "solve", label: "solve", width: "6ch", render: (r) => (r.health
          ? h("span", `lvl-${r.health.solve.state}`, r.health.solve.problems ? `!${r.health.solve.problems}` : "ok") : "") },
        { key: "checks", label: "checks", width: "7ch", render: (r) => (r.health && r.health.checks
          ? h("span", `lvl-${r.health.checks.state}`,
            `${r.health.checks.total - r.health.checks.failing - r.health.checks.unchecked}/${r.health.checks.total}`) : "") },
        { key: "motes", label: "motes", width: "7ch", align: "right",
          render: (r) => (r.motes === null ? h("span", "lvl-warn", "?") : String(r.motes)) },
        { key: "marks", label: "", width: "4ch", render: (r) => h("span", r.problems.length ? "lvl-warn" : "dim",
          `${r.current ? "●" : ""}${r.syncing ? "⇅" : ""}${r.problems.length ? "!" : ""}`) },
      ],
      groups, rowKey: (r) => r.key,
      selected: st.armed !== null ? [st.rows[st.armed].key] : [],
      cursor: { index: st.cursor, col: null },
      onRow: (row) => pointAt(st.rows.findIndex((r) => r.key === row.key)),
    });
    grid.querySelectorAll(".c-group-all").forEach((n) => n.remove());
    const parts = [grid];
    if (!st.rows.length) parts.push(h("div", "c-table-empty", "no gardens"));
    if (p.problems.length) parts.push(h("div", "home-problems lvl-warn", p.problems.join("\n")));
    body.replaceChildren(...parts);
    const line = grid.querySelector(`.c-table-row[data-index="${st.cursor}"]`);
    if (line) line.scrollIntoView({ block: "nearest" });
  }

  /** The cursor's garden goes to the detail pane. */
  function pointAt(index) {
    if (!st.rows.length) return;
    st.cursor = Math.max(0, Math.min(st.rows.length - 1, index));
    st.armed = null;
    render();
    const row = here();
    if (row) ctx.paneAction("garden", { garden: row.garden });
  }

  async function remove() {
    const row = here();
    if (!row) return;
    if (st.armed !== st.cursor) {
      st.armed = st.cursor;
      ctx.store.log("result", "d again to remove", { level: "warn" });
      render();
      return;
    }
    st.armed = null;
    render();
    await ctx.runCommand(`garden rm ${arg(row.garden)}`, { confirm: true });
  }

  const on = (f) => () => { const row = here(); if (row) f(row); };
  const KEYS = {
    j: () => pointAt(st.cursor + 1), ArrowDown: () => pointAt(st.cursor + 1),
    k: () => pointAt(st.cursor - 1), ArrowUp: () => pointAt(st.cursor - 1),
    g: () => pointAt(0), G: () => pointAt(st.rows.length - 1),
    Enter: on((r) => ctx.runCommand(`cd ${arg(r.garden)}`)),
    n: () => ctx.prefill("garden new "),
    r: on((r) => ctx.prefill(`garden rename ${arg(r.garden)} `)),
    d: remove,
    z: on((r) => ctx.runCommand(`garden ${r.archived ? "unarchive" : "archive"} ${arg(r.garden)}`)),
    s: on((r) => ctx.prefill(`garden sync ${arg(r.garden)} `)),
    E: on((r) => ctx.runCommand(`garden export ${arg(r.garden)}`)),
    I: () => ctx.prefill("garden import "),
    Z: () => ctx.setCommand(st.command.includes("--archived") ? "gardens" : "gardens --archived"),
  };

  function onKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey || !st.payload) return false;
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

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    let p;
    try {
      p = await api.home.gardens(command);
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
    const rows = p.groups.flatMap((g) => g.rows).map((r, i) => ({ ...r, key: `${r.garden}:${i}` }));
    let at = 0;
    p.groups.forEach((g) => { g.rows = rows.slice(at, at += g.rows.length); });
    Object.assign(st, { payload: p, error: null, rows });
    const again = before ? rows.findIndex((r) => r.garden === before.garden) : rows.findIndex((r) => r.current);
    st.cursor = again >= 0 ? again : Math.min(st.cursor, Math.max(rows.length - 1, 0));
    render();
    const archived = p.groups.find((g) => g.key === "archived");
    return { summary: `${p.total} garden${p.total === 1 ? "" : "s"}${archived && archived.count ? ` · ${archived.count} archived` : ""}`
      + `${p.problems.length ? ` · ${p.problems.length} problem${p.problems.length === 1 ? "" : "s"}` : ""}`,
    level: p.problems.length ? "warn" : "ok" };
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
  id: "gardens",
  describe: "every garden: the ownership tree, loose and archived (gardens)",
  commands: [{ verb: "gardens" }],
  followsSel: false,
  create,
};
