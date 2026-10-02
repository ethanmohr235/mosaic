/* `mote:props` (`open @sel|#id|name`): one mote's properties and
 * links -- the console's inspector (docs/console.md → *Inspector*, U3).
 *
 * The handoff's layout: the name as a title, a key/value grid with a
 * typed value per property, then `links N` with a `→ relation  target`
 * row per relationship (`←` for one pointing in). The payload is
 * core/inspect.py's via api.mote.inspect, the same one the old windows'
 * floating panel draws.
 *
 * Every edit is a command run through ctx.runCommand, so it lands in the
 * history, the log and undo, and re-runs every pane (docs/console.md →
 * *Settled for U3*):
 *   edit a value     set #id key value        (empty clears: --clear)
 *   add a property   set #id key value
 *   rename a key     rename #id old new
 *   delete           set #id key --clear  /  unlink #id #rel
 *   link             #id -> type -> target
 *   template         template #id #species
 *
 * `open @sel` follows the selection's primary; `open #id` is pinned, and
 * walking a link from a pinned pane re-points the pane instead of the
 * selection. The same component is the `p` float (js/float.js). */

import { createPalette } from "../palette.js";

const HINTS = [["j k", "row"], ["i", "edit"], ["⏎", "edit / follow"], ["a", "add"], ["r", "rename"],
  ["d d", "delete"], ["l", "link"]];
const EDIT_HINTS = [["⏎", "run"], ["esc", "cancel"]];
const READ_ONLY = new Set(["map"]);

function message(err) {
  return String(err && err.message ? err.message : err);
}

/** One command-line argument: bare when safe, else double-quoted. */
function arg(text) {
  const s = String(text);
  return /^[^"'\\\s#-][^"'\\\s]*$/.test(s) ? s : JSON.stringify(s);
}

/** A value's text as an editor starts from it. */
function raw(value) {
  if (value === null || value === undefined) return "";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** What `set` should be handed for a value copied as-is (a template hint). */
function literal(value) {
  return typeof value === "string" ? value : JSON.stringify(value);
}

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const st = { command: "", seq: 0, payload: null, empty: null, error: null, rows: [], cursor: 0,
    cursorKey: null, editing: null, armed: null, palette: null };
  let body = null;
  const offs = [];

  /* ---- rows: every line the cursor can stand on ---- */

  function buildRows(p) {
    const rows = [];
    for (const key of p.keys) {
      const meta = p.kinds[key] || { kind: "text" };
      rows.push({ type: "prop", key, meta, value: p.properties[key], reserved: p.reserved.includes(key) });
    }
    rows.push({ type: "add" });
    for (const hint of p.hints || []) rows.push({ type: "hint", key: hint.key, value: hint.value });
    for (const rel of p.relationships) rows.push({ type: "rel", rel });
    rows.push({ type: "link" });
    if (p.species.id && !p.species.isTemplate && !p.species.isSpecies) rows.push({ type: "template" });
    return rows;
  }

  function rowKey(row) {
    if (row.type === "prop" || row.type === "hint") return `${row.type}:${row.key}`;
    if (row.type === "rel") return `rel:${row.rel.id}`;
    return row.type;
  }

  /* ---- drawing ---- */

  function valueNode(row) {
    const { meta, value } = row;
    if (value === "" || value === null || value === undefined) return h("span", "dim", "—");
    if (meta.kind === "ref") {
      return h("span", "props-link", [components.swatch(st.palette(meta.refSpeciesId)), " ", meta.refLabel]);
    }
    if (meta.kind === "number" && meta.unit) {
      const node = h("span", "", [String(value), h("span", "dim", ` ${meta.unit}`)]);
      if (meta.unitBase) node.title = meta.unitBase;
      return node;
    }
    if (meta.kind === "map") return h("span", "dim", raw(value));
    return raw(value);
  }

  function relNode(rel) {
    const arrow = rel.direction === "out" ? "→" : "←";
    return [
      h("span", "props-rel-key", [h("span", "dim", `${arrow} `), rel.type]),
      h("span", "props-link", [components.swatch(rel.otherIsSpecies ? st.palette(rel.otherId, true)
        : st.palette(rel.otherSpeciesId)), " ", rel.otherLabel]),
    ];
  }

  function rowNode(row, index) {
    let cells;
    if (row.type === "prop") {
      cells = [h("span", `props-key${row.reserved ? " reserved" : ""}`, row.key), h("span", "props-value", valueNode(row))];
    } else if (row.type === "add") {
      cells = [h("span", "props-key dim", "+"), h("span", "props-value dim", "add a property")];
    } else if (row.type === "hint") {
      cells = [h("span", "props-key dim", row.key),
        h("span", "props-value dim", `usually ${raw(row.value) || "set"} — ⏎ adds it`)];
    } else if (row.type === "rel") {
      cells = relNode(row.rel);
    } else if (row.type === "link") {
      cells = [h("span", "props-key dim", "+"), h("span", "props-value dim", "link to a mote")];
    } else {
      cells = [h("span", "props-key dim", "template"),
        h("span", "props-value dim", `make this ${st.payload.species.label}'s template`)];
    }
    const line = h("div", `props-row${index === st.cursor ? " cursor" : ""}`, cells);
    line.dataset.index = String(index);
    line.dataset.key = rowKey(row);
    if (st.armed === index) line.classList.add("armed");
    line.addEventListener("click", (event) => onClick(event, index));
    line.addEventListener("dblclick", () => {
      moveTo(index);
      startEdit();
    });
    return line;
  }

  function render() {
    if (!body) return;
    if (st.error) {
      body.replaceChildren(h("div", "pane-error lvl-fault", st.error));
      return;
    }
    if (st.empty) {
      body.replaceChildren();  // the header says "nothing selected"
      return;
    }
    const p = st.payload;
    if (!p) return;
    const color = p.species.isSpecies ? st.palette(p.id, true) : st.palette(p.species.id);
    const title = h("div", "props-title", [components.swatch(color),
      " ", p.label, p.species.label ? h("span", "dim", `  ${p.species.label}`) : ""]);
    const nodes = [title];
    if (p.follows && p.selectionCount > 1) {
      nodes.push(h("div", "props-note dim",
        `+${p.selectionCount - 1} selected — edits here write ${p.label} only; set @sel writes all`));
    }
    const props = st.rows.map((row, i) => [row, i]);
    const firstRel = st.rows.findIndex((r) => r.type === "rel" || r.type === "link");
    const grid = h("div", "props-grid");
    props.slice(0, firstRel).forEach(([row, i]) => grid.appendChild(rowNode(row, i)));
    nodes.push(grid);
    const links = st.rows.filter((r) => r.type === "rel").length;
    nodes.push(h("div", "props-section dim", `links ${links}`));
    const relGrid = h("div", "props-grid wide");
    props.slice(firstRel).forEach(([row, i]) => relGrid.appendChild(rowNode(row, i)));
    nodes.push(relGrid);
    body.replaceChildren(h("div", "props", nodes));
    reattachEditor();
    reveal();
  }

  function reveal() {
    const node = body && body.querySelector(`.props-row[data-index="${st.cursor}"]`);
    if (node) node.scrollIntoView({ block: "nearest" });
  }

  function mark() {
    if (!body) return;
    body.querySelectorAll(".props-row").forEach((node) => {
      const i = Number(node.dataset.index);
      node.classList.toggle("cursor", i === st.cursor);
      node.classList.toggle("armed", i === st.armed);
    });
    reveal();
  }

  /* ---- cursor ---- */

  function moveTo(index) {
    if (!st.rows.length) return;
    st.cursor = Math.max(0, Math.min(index, st.rows.length - 1));
    st.cursorKey = rowKey(st.rows[st.cursor]);
    st.armed = null;
    mark();
  }

  function onClick(event, index) {
    if (st.editing) return;
    moveTo(index);
    const row = st.rows[index];
    if (event.target.closest(".props-link")) return follow(row);
    if (row.type === "add" || row.type === "link" || row.type === "hint" || row.type === "template") {
      activate();
    }
  }

  /* ---- walking ---- */

  function follow(row) {
    const id = row.type === "rel" ? row.rel.otherId
      : row.type === "prop" && row.meta.kind === "ref" ? row.value : null;
    if (!id) return false;
    const label = row.type === "rel" ? row.rel.otherLabel : row.meta.refLabel;
    if (st.payload.follows) ctx.navigate(id, label);
    else ctx.setCommand(`open #${id}`);
    return true;
  }

  /* ---- writes: every one a command ---- */

  const target = () => `#${st.payload.id}`;

  async function run(line) {
    const outcome = await ctx.runCommand(line);
    return outcome && outcome.level !== "fault";
  }

  function activate() {
    const row = st.rows[st.cursor];
    if (!row) return;
    if (row.type === "rel") return follow(row);
    if (row.type === "prop" && row.meta.kind === "ref") return follow(row);
    if (row.type === "prop" && row.meta.kind === "boolean") {
      return run(`set ${target()} ${arg(row.key)} ${row.value ? "false" : "true"}`);
    }
    if (row.type === "hint") return run(`set ${target()} ${arg(row.key)} ${arg(literal(row.value))}`);
    if (row.type === "template") return run(`template ${target()} #${st.payload.species.id}`);
    if (row.type === "add") return startEdit("add");
    if (row.type === "link") return startEdit("link");
    return startEdit();
  }

  function armOrDelete() {
    const row = st.rows[st.cursor];
    if (!row || !(row.type === "prop" || row.type === "rel")) return;
    if (st.armed !== st.cursor) {
      st.armed = st.cursor;
      mark();
      store.log("result", "d again to delete", { level: "warn" });
      return;
    }
    st.armed = null;
    if (row.type === "prop") run(`set ${target()} ${arg(row.key)} --clear`);
    else run(`unlink ${target()} #${row.rel.id}`);
  }

  /* ---- the one-line editor ---- */

  const PROMPTS = {
    value: (row) => [row.key, raw(row.value)],
    rename: (row) => [`rename ${row.key} to`, row.key],
    add: () => ["key value", ""],
    link: () => ["type target", ""],
  };

  function startEdit(mode = "value") {
    const row = st.rows[st.cursor];
    if (!row) return;
    if (mode === "value" && row.type !== "prop") return activate();
    if (mode === "rename" && row.type !== "prop") return;
    if (mode === "value" && READ_ONLY.has(row.meta.kind)) {
      store.log("result", `${row.key} is a map -- edit it as a statement`, { level: "warn" });
      return;
    }
    const [label, initial] = PROMPTS[mode](row);
    const input = h("input", "c-cell-input");
    input.type = "text";
    input.spellcheck = false;
    input.value = initial;
    const options = row.type === "prop" && row.meta.kind === "enum" && mode === "value"
      ? h("div", "props-options dim", row.meta.options.join(" · ")) : null;
    st.editing = { mode, index: st.cursor, key: rowKey(row), row, input, label, options, moving: false };
    input.addEventListener("blur", () => {
      if (st.editing && st.editing.input === input && !st.editing.moving) cancelEdit();
    });
    render();
    store.emit("hints");
  }

  function reattachEditor() {
    const editing = st.editing;
    if (!editing) return;
    const at = st.rows.findIndex((r) => rowKey(r) === editing.key);
    const node = at >= 0 && body.querySelector(`.props-row[data-index="${at}"]`);
    if (!node) {
      st.editing = null;
      store.emit("hints");
      return;
    }
    editing.moving = true;
    node.replaceChildren(h("span", "props-key dim", editing.label),
      h("span", "props-value", [editing.input, editing.options || ""]));
    editing.input.focus();
    editing.moving = false;
  }

  function closeEditor() {
    const editing = st.editing;
    st.editing = null;
    store.emit("hints");
    render();
    return editing;
  }

  function cancelEdit() {
    closeEditor();
  }

  async function commitEdit() {
    const { mode, row, input } = closeEditor();
    const text = input.value.trim();
    let line = null;
    if (mode === "value") {
      if (text === raw(row.value).trim()) return;
      line = text === "" ? `set ${target()} ${arg(row.key)} --clear`
        : `set ${target()} ${arg(row.key)} ${arg(text)}`;
    } else if (mode === "rename") {
      if (!text || text === row.key) return;
      line = `rename ${target()} ${arg(row.key)} ${arg(text)}`;
    } else if (mode === "add") {
      const [key, ...rest] = text.split(/\s+/);
      if (!key) return;
      line = rest.length ? `set ${target()} ${arg(key)} ${arg(rest.join(" "))}` : `set ${target()} ${arg(key)} ""`;
    } else if (mode === "link") {
      const match = text.match(/^(\S+)\s+(.+)$/);
      if (!match) {
        store.log("result", "link needs a type and a target: blocks chore2", { level: "warn" });
        return;
      }
      line = `${target()} -> ${match[1]} -> ${match[2]}`;
    }
    if (line && !(await run(line))) startEditAgain(mode, input.value);
  }

  function startEditAgain(mode, text) {
    startEdit(mode);
    if (st.editing) st.editing.input.value = text;
  }

  function editorKey(event) {
    if (event.key === "Enter") commitEdit();
    else if (event.key === "Escape") cancelEdit();
    else return false;
    return true;
  }

  /* ---- keys ---- */

  const KEYS = {
    j: () => moveTo(st.cursor + 1),
    ArrowDown: () => moveTo(st.cursor + 1),
    k: () => moveTo(st.cursor - 1),
    ArrowUp: () => moveTo(st.cursor - 1),
    g: () => moveTo(0),
    G: () => moveTo(st.rows.length - 1),
    i: () => startEdit(),
    Enter: activate,
    a: () => startEdit("add"),
    r: () => startEdit("rename"),
    d: armOrDelete,
    l: () => startEdit("link"),
  };

  function onKey(event) {
    if (st.editing) return editorKey(event);
    if (event.ctrlKey || event.metaKey || event.altKey || !st.payload) return false;
    if (event.key === "Escape" && st.armed !== null) {
      st.armed = null;
      mark();
      return true;
    }
    const action = KEYS[event.key];
    if (!action) return false;
    if (event.key !== "d") st.armed = null;
    action();
    return true;
  }

  /* ---- the contract ---- */

  async function runView(command) {
    st.command = command;
    const seq = ++st.seq;
    const ns = api.mote;
    let p;
    try {
      p = ns && typeof ns.inspect === "function"
        ? await ns.inspect(command, store.state.selection) : { error: "this window has no mote api" };
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq || !body) return { summary: "", level: "idle" };
    if (p && p.empty) {
      Object.assign(st, { payload: null, error: null, empty: p.empty, rows: [] });
      render();
      return { summary: "nothing selected", level: "idle" };
    }
    if (!p || p.error) {
      Object.assign(st, { payload: null, empty: null, error: (p && p.error) || "no answer", rows: [] });
      render();
      return { summary: st.error, level: "fault" };
    }
    const sameMote = st.payload && st.payload.id === p.id;
    Object.assign(st, { payload: p, error: null, empty: null, rows: buildRows(p) });
    st.palette = paletteFor(p.speciesUniverse || []);
    const at = sameMote ? st.rows.findIndex((r) => rowKey(r) === st.cursorKey) : -1;
    st.cursor = at >= 0 ? at : 0;
    st.cursorKey = st.rows[st.cursor] ? rowKey(st.rows[st.cursor]) : null;
    if (!sameMote) st.armed = null;
    render();
    const links = p.relationships.length;
    return { summary: `${p.keys.length} properties · ${links} link${links === 1 ? "" : "s"}`, level: "ok" };
  }

  return {
    mount(el, command) {
      st.command = command;
      body = h("div", "view-props scroll");
      el.appendChild(body);
    },
    run: runView,
    unmount() {
      offs.splice(0).forEach((off) => off());
      st.editing = null;
      st.seq += 1;
      body = null;
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

/** species id → color, over the garden's whole species list. With
 * `self`, the mote is itself that species and draws lifted. */
function paletteFor(universe) {
  const palette = createPalette(universe);
  return (speciesId, self = false) => (speciesId
    ? palette.colorOf(self ? { id: speciesId, kinds: ["species"] } : { speciesId })
    : "var(--dim)");
}

export default {
  id: "mote:props",
  app: "te",
  describe: "a mote's properties and links (open)",
  commands: [{ verb: "open", canonical: "open @sel" }],
  followsSel: false,
  create,
};
