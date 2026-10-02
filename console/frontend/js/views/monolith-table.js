/* `monolith:table` (`find …`): the garden's motes as one
 * dense table. The query, sort, grouping and columns all live in the
 * pane's command; api.monolith.table(command) answers it (the U2 bridge
 * contract) and api.monolith.rewrite() turns a gesture (a header click,
 * `s`, `g`) into the new command, so JS never builds find syntax.
 *
 * Selection is the shell's (store.state.selection), never a copy: rows
 * are highlighted from it, and every gesture writes it back in drawn
 * order, so the first selected row on screen is the primary. The cursor
 * (j/k, h/l) is this pane's own and is not the selection.
 *
 * Edits are commands. An inline edit runs `set @sel <key> <value>` when
 * its row is selected (the whole selection is written) and
 * `set #<id> <key> <value>` otherwise, through ctx.runCommand, so each
 * one is in history, the log and undo, and the write re-runs every pane. */

import { createPalette } from "../palette.js";

const NAME = "builtin:name";
const HINTS = [["j k", "row"], ["h l", "column"], ["space", "toggle"], ["V", "range"], ["a", "all"],
  ["esc", "clear"], ["s", "sort"], ["g", "group"], ["i ⏎", "edit"]];
const EDIT_HINTS = [["⏎", "set"], ["⏎ empty", "clear"], ["esc", "cancel"]];

/* ---------- pure helpers ---------- */

function message(err) {
  return String(err && err.message ? err.message : err);
}

function summarize(p) {
  if (p.matched !== p.total) return `${p.matched} / ${p.total}`;
  return `${p.total} mote${p.total === 1 ? "" : "s"}`;
}

/** `property:due` → `due`, `builtin:name` → `name`: the key `set` takes. */
function propertyKey(columnKey) {
  return columnKey.slice(columnKey.indexOf(":") + 1);
}

/** One command-line argument: bare when it's safe, else double-quoted. */
function arg(text, { rest = false } = {}) {
  const plain = rest ? /^[^"'\\\s-][^"'\\]*$/ : /^[^"'\\\s-][^"'\\\s]*$/;
  return plain.test(text) && text.trim() === text ? text : JSON.stringify(text);
}

/** The text an editor starts from: the raw value when it's a scalar. */
function editText(cell) {
  if (cell.empty) return "";
  if (typeof cell.value === "string" || typeof cell.value === "number") return String(cell.value);
  return cell.text || "";
}

/** A column's width in characters (monospace), capped at --col-max. One
 * spare ch, because WebKit ellipsizes text that fills its box exactly. */
function widthOf(column, rows, index) {
  const head = (column.label || "").length + 2;
  const longest = rows.reduce((n, r) => Math.max(n, (r.cells[index].text || "").length), head) + 1;
  if (column.key === NAME) return `minmax(min(${longest + 2}ch, var(--col-max)), 1fr)`;
  return `min(${longest}ch, var(--col-max))`;
}

/* ---------- the view ---------- */

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const st = { command: "", seq: 0, payload: null, error: null, palette: null, columns: [], drawn: [],
    cursor: { index: 0, col: 0 }, cursorId: null, anchorId: null, editing: null, grid: null };
  let body = null;
  const offs = [];

  /* ---- columns and rows ---- */

  function renderCell(row, column) {
    const cell = row.cells[column.index];
    if (column.key === NAME) return [components.swatch(st.palette.colorOf(row)), " ", cell.text || ""];
    return cell.empty ? "" : cell.text;
  }

  function buildColumns(p, rows) {
    return p.columns.map((c, index) => ({
      key: c.key, label: c.label, kind: c.kind, editable: c.editable, index,
      align: c.kind === "number" ? "right" : "left",
      width: widthOf(c, rows, index),
      render: renderCell,
    }));
  }

  function adopt(p) {
    st.payload = p;
    st.error = null;
    st.palette = createPalette(p.speciesIds || []);
    st.drawn = p.groups.flatMap((g) => g.rows);
    st.columns = buildColumns(p, st.drawn);
    const at = st.drawn.findIndex((r) => r.id === st.cursorId);
    st.cursor.index = Math.max(0, Math.min(at >= 0 ? at : st.cursor.index, st.drawn.length - 1));
    st.cursor.col = Math.max(0, Math.min(st.cursor.col, st.columns.length - 1));
    st.cursorId = st.drawn[st.cursor.index] ? st.drawn[st.cursor.index].id : null;
  }

  /* ---- drawing ---- */

  function render() {
    if (!body) return;
    if (st.error) {
      st.grid = null;
      body.replaceChildren(h("div", "pane-error lvl-fault", st.error));
      return;
    }
    if (!st.payload) return;
    st.grid = components.table({
      columns: st.columns, groups: st.payload.groups, rowKey: (r) => r.id,
      selected: store.state.selection, primary: store.primary(), cursor: st.cursor, sort: st.payload.sort,
      onRow, onHeader, onGroup,
    });
    body.replaceChildren(st.grid);
    if (!st.drawn.length) body.appendChild(h("div", "c-table-empty", "no motes match"));
    reattachEditor();
    reveal();
  }

  function mark() {
    if (!st.grid) return;
    components.markTable(st.grid, { selected: store.state.selection, primary: store.primary(), cursor: st.cursor });
  }

  function cellEl(index, col) {
    return st.grid && st.grid.querySelector(`.c-table-row[data-index="${index}"] > .c-cell[data-col="${col}"]`);
  }

  function reveal() {
    const node = cellEl(st.cursor.index, st.cursor.col);
    if (node) node.scrollIntoView({ block: "nearest", inline: "nearest" });
  }

  /* ---- cursor ---- */

  function moveTo(index, col = st.cursor.col) {
    if (!st.drawn.length) return;
    st.cursor.index = Math.max(0, Math.min(index, st.drawn.length - 1));
    st.cursor.col = Math.max(0, Math.min(col, st.columns.length - 1));
    st.cursorId = st.drawn[st.cursor.index].id;
    mark();
    reveal();
  }

  /* ---- selection (always the store's, written in drawn order) ---- */

  function labelsFor(ids) {
    const want = new Set(ids);
    return Object.fromEntries(st.drawn.filter((r) => want.has(r.id)).map((r) => [r.id, r.name]));
  }

  function select(ids) {
    const want = new Set(ids);
    const drawn = [...new Set(st.drawn.map((r) => r.id))].filter((id) => want.has(id));
    const rest = ids.filter((id) => !drawn.includes(id));
    store.setSelection([...drawn, ...rest], labelsFor(ids));
  }

  function toggle(id) {
    const now = store.state.selection;
    select(now.includes(id) ? now.filter((x) => x !== id) : [...now, id]);
    st.anchorId = id;
  }

  function anchorIndex() {
    for (const id of [st.anchorId, store.primary()]) {
      const at = st.drawn.findIndex((r) => r.id === id);
      if (id && at >= 0) return at;
    }
    return st.cursor.index;
  }

  function selectRange(to) {
    const from = anchorIndex();
    const [a, b] = from <= to ? [from, to] : [to, from];
    select(st.drawn.slice(a, b + 1).map((r) => r.id));
  }

  /* ---- mouse ---- */

  function onRow(row, event, index) {
    if (st.editing) return;
    const node = event.target.closest && event.target.closest(".c-cell");
    moveTo(index, node ? Number(node.dataset.col) : st.cursor.col);
    if (event.shiftKey) selectRange(index);
    else if (event.ctrlKey) toggle(row.id);
    else {
      select([row.id]);
      st.anchorId = row.id;
    }
  }

  function onHeader(column) {
    st.cursor.col = column.index;
    rewrite({ sort: column.key });
  }

  function onGroup(group) {
    select(group.rows.map((r) => r.id));
    st.anchorId = group.rows.length ? group.rows[0].id : st.anchorId;
  }

  function onDblClick(event) {
    const node = event.target.closest(".c-table-row > .c-cell");
    if (!node || st.editing) return;
    moveTo(Number(node.parentElement.dataset.index), Number(node.dataset.col));
    startEdit();
  }

  /* ---- command rewrites (sort, group) ---- */

  async function rewrite(change) {
    let result;
    try {
      result = await api.monolith.rewrite(st.command, change);
    } catch (err) {
      result = { error: message(err) };
    }
    if (!result || result.error) {
      store.log("result", (result && result.error) || "can't rewrite the command", { level: "fault" });
      return;
    }
    await ctx.setCommand(result.command);
  }

  function groupByCursor() {
    const column = st.columns[st.cursor.col];
    if (!column) return;
    rewrite({ group: st.payload.group === column.key ? null : column.key });
  }

  /* ---- inline editing ---- */

  function startEdit(initial = null) {
    const row = st.drawn[st.cursor.index];
    const column = st.columns[st.cursor.col];
    const node = cellEl(st.cursor.index, st.cursor.col);
    if (!row || !column || !node) return;
    const cell = row.cells[column.index];
    if (!(cell.editable ?? column.editable)) {
      store.log("result", `${column.label} can't be edited here`, { level: "warn" });
      return;
    }
    const input = h("input", "c-cell-input");
    input.type = "text";
    input.spellcheck = false;
    input.value = initial ?? editText(cell);
    st.editing = { id: row.id, index: st.cursor.index, col: st.cursor.col, key: propertyKey(column.key),
      original: editText(cell), input, moving: false };
    const stop = (event) => event.stopPropagation();
    input.addEventListener("click", stop);
    input.addEventListener("dblclick", stop);
    input.addEventListener("blur", () => {
      if (st.editing && st.editing.input === input && !st.editing.moving) cancelEdit();
    });
    placeEditor(node);
    store.emit("hints");
  }

  function placeEditor(node) {
    const { input } = st.editing;
    st.editing.moving = true;
    node.replaceChildren(input);
    input.focus();
    st.editing.moving = false;
  }

  /** A re-render while editing (a write elsewhere, a refresh) keeps the editor. */
  function reattachEditor() {
    if (!st.editing) return;
    const at = st.drawn.findIndex((r) => r.id === st.editing.id);
    const node = at >= 0 ? cellEl(at, st.editing.col) : null;
    if (!node) {
      st.editing = null;
      store.emit("hints");
      return;
    }
    st.editing.index = at;
    placeEditor(node);
  }

  function closeEditor() {
    const editing = st.editing;
    st.editing = null;
    store.emit("hints");
    if (editing) render();
    return editing;
  }

  function cancelEdit() {
    closeEditor();
  }

  async function commitEdit() {
    const { id, key, original, input } = closeEditor();
    const value = input.value.trim();
    if (value === original.trim()) return;
    const target = store.state.selection.includes(id) ? "@sel" : `#${id}`;
    const line = value === "" ? `set ${target} ${arg(key)} --clear` : `set ${target} ${arg(key)} ${arg(value, { rest: true })}`;
    const outcome = await ctx.runCommand(line);
    if (outcome && outcome.level === "fault" && st.drawn[st.cursor.index] && st.drawn[st.cursor.index].id === id) {
      startEdit(input.value);
    }
  }

  function editorKey(event) {
    if (event.key === "Enter") commitEdit();
    else if (event.key === "Escape") cancelEdit();
    else return false;
    return true;
  }

  /* ---- keys ---- */

  const KEYS = {
    j: () => moveTo(st.cursor.index + 1),
    ArrowDown: () => moveTo(st.cursor.index + 1),
    k: () => moveTo(st.cursor.index - 1),
    ArrowUp: () => moveTo(st.cursor.index - 1),
    h: () => moveTo(st.cursor.index, st.cursor.col - 1),
    ArrowLeft: () => moveTo(st.cursor.index, st.cursor.col - 1),
    l: () => moveTo(st.cursor.index, st.cursor.col + 1),
    ArrowRight: () => moveTo(st.cursor.index, st.cursor.col + 1),
    " ": () => st.cursorId && toggle(st.cursorId),
    V: () => st.drawn.length && selectRange(st.cursor.index),
    a: () => select(st.drawn.map((r) => r.id)),
    s: () => st.columns[st.cursor.col] && rewrite({ sort: st.columns[st.cursor.col].key }),
    g: groupByCursor,
    i: () => startEdit(),
    Enter: () => startEdit(),
  };

  function onKey(event) {
    if (st.editing) return editorKey(event);
    if (event.ctrlKey || event.metaKey || event.altKey || !st.payload) return false;
    if (event.key === "Escape") {
      if (!store.state.selection.length) return false;
      store.setSelection([]);
      return true;
    }
    const action = KEYS[event.key];
    if (!action) return false;
    action();
    return true;
  }

  /* ---- the contract ---- */

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    const ns = api.monolith;
    let p;
    try {
      p = ns && typeof ns.table === "function" ? await ns.table(command) : { error: "this window has no monolith api" };
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq || !body) return { summary: "", level: "idle" };
    if (!p || p.error) {
      Object.assign(st, { payload: null, error: (p && p.error) || "no answer" });
      render();
      return { summary: st.error, level: "fault" };
    }
    adopt(p);
    render();
    return { summary: summarize(p), level: "ok" };
  }

  return {
    mount(el, command) {
      st.command = command;
      body = h("div", "view-table scroll");
      body.addEventListener("dblclick", onDblClick);
      el.appendChild(body);
      offs.push(store.subscribe("selection", mark));
    },
    run,
    unmount() {
      offs.splice(0).forEach((off) => off());
      if (body) body.removeEventListener("dblclick", onDblClick);
      st.editing = null;
      st.seq += 1;
      body = null;
      st.grid = null;
    },
    hints: () => (st.editing ? EDIT_HINTS : HINTS),
    onKey,
    get textEditing() {
      return !!st.editing;
    },
    focus() {
      if (st.editing) st.editing.input.focus();
    },
  };
}

export default {
  id: "monolith:table",
  app: "nolith",
  describe: "table of motes (find)",
  commands: [{ verb: "find" }],
  followsSel: false,
  create,
};
