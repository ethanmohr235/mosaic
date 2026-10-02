/* `log`: the session's commands, their results and their errors, oldest
 * first, with each entry's level. A row that names a mote selects it on
 * click (shift or ctrl adds it to the selection instead), so a
 * statement typed at the command line can be picked back up later. */

const HINTS = [["j k", "scroll"], ["g G", "top / bottom"], ["click", "select mote"]];

function clock(date) {
  return date.toTimeString().slice(0, 8);
}

function text(entry, h) {
  if (entry.kind === "command") return [h("span", "dim", "$ "), entry.text];
  return entry.text;
}

function columns(h) {
  return [
    { key: "time", width: "max-content", render: (e) => h("span", "dim", clock(e.time)) },
    { key: "text", width: "1fr", render: (e) => text(e, h), level: (e) => (e.level === "warn" || e.level === "fault" ? e.level : null) },
  ];
}

function create(ctx) {
  const { store, components } = ctx;
  const { h } = components;
  let body = null;
  const offs = [];

  function atBottom() {
    return body.scrollTop + body.clientHeight >= body.scrollHeight - 2;
  }

  function onRow(entry, event) {
    if (event.shiftKey || event.ctrlKey) store.toggleSelection(entry.moteId);
    else ctx.navigate(entry.moteId);
  }

  function render() {
    if (!body) return;
    const stick = atBottom();
    const grid = components.table({
      columns: columns(h),
      rows: store.state.log,
      rowKey: (e) => e.moteId,
      selected: store.state.selection,
      onRow,
      canClick: (e) => !!e.moteId,
      header: false,
    });
    body.replaceChildren(grid);
    if (stick) body.scrollTop = body.scrollHeight;
  }

  return {
    mount(el) {
      body = h("div", "view-log scroll");
      el.appendChild(body);
      offs.push(store.subscribe("log", render), store.subscribe("selection", render));
      render();
      body.scrollTop = body.scrollHeight;
    },
    // The log grows between runs (a failed command re-runs no pane), so a
    // count here would go stale; the rows carry their own levels.
    run() {
      render();
      return { summary: "", level: "idle" };
    },
    unmount() {
      offs.splice(0).forEach((off) => off());
      body = null;
    },
    hints: () => HINTS,
    onKey(event) {
      if (!body || event.ctrlKey || event.metaKey || event.altKey) return false;
      const rowH = parseFloat(getComputedStyle(body).lineHeight) || 18;
      const moves = { j: rowH, k: -rowH, g: -body.scrollHeight, G: body.scrollHeight };
      if (!(event.key in moves)) return false;
      body.scrollTop += moves[event.key];
      return true;
    },
  };
}

export default {
  id: "log",
  app: null,
  describe: "command and error log",
  commands: [{ verb: "log" }],
  followsSel: false,
  create,
};
