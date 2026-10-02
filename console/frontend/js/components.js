/* The shared pieces every view and the shell draw with: table (with
 * markTable and swatch), kvGrid, popupList, paneHeader, plus promptInput
 * (the block-cursor text field) and the h() element helper. These are the only copies (docs/console.md → *Architecture*): a view that needs a
 * table, a key/value grid, a popup list or a pane header uses these
 * rather than growing its own. Styling is in shell.css under the same
 * class names; nothing here sets a color or a size.
 *
 * All four return plain elements (paneHeader returns {el, update}) and
 * attach no listeners outside the element they create, so a view that
 * drops the element has cleaned up after them. */

/** Create an element: h("span", "cls other", "text" | Node | [Node|string...]). */
export function h(tag, className = "", content = null) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  append(node, content);
  return node;
}

function append(node, content) {
  if (content === null || content === undefined) return;
  if (Array.isArray(content)) content.forEach((c) => append(node, c));
  else if (content instanceof Node) node.appendChild(content);
  else node.appendChild(document.createTextNode(String(content)));
}

/**
 * A dense table: a --dim header row, then one line-height row per item,
 * no padding between rows. Selected rows are inverted, and the primary
 * one is bold too. Numbers are right-aligned (`align: "right"`).
 *
 *   columns  [{key, label, width: "1fr" | "max-content" | "12ch" ..., align: "left"|"right",
 *              render?(row, column) -> string|Node, level?(row) -> "ok"|"warn"|"fault"|null}]
 *   rows     the items (ignored when `groups` is given)
 *   groups   [{key, label, count, rows, labelled}]: rows under a group row
 *            (label, count, "select all"); an unlabelled group draws none
 *   rowKey   (row) -> key used for selection and data-key
 *   selected Set/array of keys drawn inverted
 *   primary  the key among them drawn bold (the selection's first)
 *   cursor   {index, col}: the keyboard cursor, distinct from the
 *            selection -- a `›` in the drawn row `index`'s left margin
 *            and an underline under its cell `col` (col may be null)
 *   sort     {key, dir: "asc"|"desc"}: column `key`'s header gets ▲ / ▼
 *   onRow    (row, event, index) -> void, on click; index = drawn row index
 *   onHeader (column, event) -> void, on a header click; without it the
 *            header isn't clickable
 *   onGroup  (group, event) -> void, on a group row's "select all"
 *   canClick (row) -> bool: which rows onRow applies to (default: all)
 *   header   false to omit the header row
 *
 * Rows carry data-key and data-index and cells data-col (the column
 * index), so a view can find a cell again. markTable() re-marks the
 * selection and cursor without redrawing.
 */
export function table({ columns, rows = [], groups = null, rowKey = (r) => r.id, selected = [],
  primary = null, cursor = null, sort = null, onRow = null, onHeader = null, onGroup = null,
  canClick = () => true, header = true }) {
  const template = columns.map((c) => c.width || "1fr").join(" ");
  const root = h("div", "c-table");
  if (header) root.appendChild(headRow(columns, template, sort, onHeader));
  let index = 0;
  for (const group of groups || [{ rows, labelled: false }]) {
    if (group.labelled) root.appendChild(groupRow(group, onGroup));
    for (const row of group.rows) {
      root.appendChild(bodyRow({ row, index, columns, template, rowKey, onRow, canClick }));
      index += 1;
    }
  }
  markTable(root, { selected, primary, cursor });
  return root;
}

/** Re-mark a table()'s selected, primary and cursor rows in place. */
export function markTable(root, { selected = [], primary = null, cursor = null }) {
  const chosen = new Set([...selected].map(String));
  const first = primary === null || primary === undefined ? null : String(primary);
  const col = cursor && cursor.col !== null && cursor.col !== undefined ? cursor.col : -1;
  root.querySelectorAll(":scope > .c-table-row").forEach((line) => {
    const key = line.dataset.key;
    const here = !!cursor && Number(line.dataset.index) === cursor.index;
    line.classList.toggle("selected", key !== undefined && chosen.has(key));
    line.classList.toggle("primary", key !== undefined && key === first);
    line.classList.toggle("cursor", here);
    line.querySelectorAll(":scope > .c-cell").forEach((node) => {
      node.classList.toggle("cursor-cell", here && Number(node.dataset.col) === col);
    });
  });
  root.querySelectorAll(":scope > .c-table-head > .c-cell").forEach((node) => {
    node.classList.toggle("cursor-col", Number(node.dataset.col) === col);
  });
}

function headRow(columns, template, sort, onHeader) {
  const cells = columns.map((c, i) => {
    const arrow = sort && sort.key === c.key ? (sort.dir === "desc" ? " ▼" : " ▲") : "";
    const node = cell(c, `${c.label || ""}${arrow}`, null, i);
    if (onHeader) {
      node.classList.add("clickable");
      node.addEventListener("click", (event) => onHeader(c, event));
    }
    return node;
  });
  return tableRow("c-table-head", cells, template);
}

function groupRow(group, onGroup) {
  const all = h("span", "c-group-all", "select all");
  if (onGroup) {
    all.classList.add("clickable");
    all.addEventListener("click", (event) => onGroup(group, event));
  }
  const count = String(group.count ?? group.rows.length);
  const line = h("div", "c-table-group", [h("span", "c-group-label", group.label || ""),
    h("span", "c-group-count", count), h("span", "grow"), all]);
  line.dataset.group = group.key ?? "";
  return line;
}

function bodyRow({ row, index, columns, template, rowKey, onRow, canClick }) {
  const key = rowKey(row);
  const cells = columns.map((c, i) => cell(c, c.render ? c.render(row, c) : row[c.key], c.level && c.level(row), i));
  const line = tableRow("c-table-row", cells, template);
  if (key !== null && key !== undefined) line.dataset.key = key;
  line.dataset.index = String(index);
  if (onRow && canClick(row)) {
    line.classList.add("clickable");
    line.addEventListener("click", (event) => onRow(row, event, index));
  }
  return line;
}

function tableRow(className, cells, template) {
  const line = h("div", className, cells);
  line.style.gridTemplateColumns = template;
  return line;
}

function cell(column, content, level = null, index = 0) {
  const node = h("span", `c-cell${column.align === "right" ? " right" : ""}`, content ?? "");
  node.dataset.col = String(index);
  if (level) node.classList.add(`lvl-${level}`);
  return node;
}

/**
 * A small square in `color` (a palette value or a var(--token)), drawn
 * as the text glyph ■ so it needs no size of its own: the graph's square
 * nodes, in a table cell before a mote's name. Inside a selected row it
 * turns --bg like everything else.
 */
export function swatch(color) {
  const node = h("span", "c-swatch", "■");
  node.style.setProperty("--swatch", color);
  return node;
}

/**
 * Key/value grid: keys in --dim in a fixed column that never wraps.
 *   pairs  [[key, value], ...]   (value: string | Node)
 *   wide   true for relation-name keys ("blocked by"): the 104px column
 */
export function kvGrid(pairs, { wide = false } = {}) {
  const root = h("div", `c-kv${wide ? " wide" : ""}`);
  pairs.forEach(([key, value]) => {
    root.appendChild(h("span", "c-kv-key", key));
    root.appendChild(h("span", "c-kv-value", value ?? ""));
  });
  return root;
}

/**
 * The popup list used for completion, history and any future picker:
 * a --dim hint header (with optional right-hand count), rows, and a
 * --dim footer of keys, divided by 1px --line borders.
 *
 *   hint, hintRight, footer   strings
 *   rows      [{cells: [string|Node, ...], dim?, current?}]
 *   layout    "key" (value | description), "time" (HH:MM | command)
 *   highlighted  index drawn inverted, or -1
 *   onPick    (index, event) -> void, on click
 */
export function popupList({ hint = "", hintRight = "", rows = [], layout = "key",
  highlighted = -1, footer = "", onPick = null }) {
  const root = h("div", `c-popup layout-${layout}`);
  if (hint || hintRight) {
    root.appendChild(h("div", "c-popup-hint", [h("span", "grow", hint), h("span", "", hintRight)]));
  }
  const list = h("div", "c-popup-rows");
  rows.forEach((row, i) => list.appendChild(popupRow(row, i, highlighted, onPick)));
  root.appendChild(list);
  if (footer) root.appendChild(h("div", "c-popup-foot", footer));
  return root;
}

function popupRow(row, i, highlighted, onPick) {
  const cells = row.cells.map((c, j) => h("span", `c-popup-cell col-${j}`, c));
  const line = h("div", "c-popup-row", cells);
  line.dataset.index = String(i);
  if (row.dim) line.classList.add("dim");
  if (row.current) line.classList.add("current");
  if (i === highlighted) line.classList.add("highlighted");
  if (onPick) {
    line.addEventListener("mousedown", (event) => event.preventDefault());
    line.addEventListener("click", (event) => onPick(i, event));
  }
  return line;
}

/**
 * A pane header: `$ command` (or the view id when there's no command),
 * then a right-aligned summary whose color follows its level. The
 * focused pane's header is inverted.
 *   update({command, label, summary, level, focused})
 */
export function paneHeader({ onClick = null } = {}) {
  const prompt = h("span", "c-head-prompt", "$");
  const command = h("span", "c-head-command");
  const summary = h("span", "c-head-summary");
  const el = h("div", "c-head", [prompt, command, summary]);
  if (onClick) el.addEventListener("click", onClick);

  function update({ command: text = "", label = "", summary: sum = "", level = "idle", focused = false }) {
    const shown = text || label;
    prompt.hidden = !text;
    command.textContent = shown;
    command.title = shown;
    summary.textContent = sum || "";
    summary.className = `c-head-summary lvl-${level || "idle"}`;
    el.classList.toggle("focused", focused);
  }
  return { el, update };
}

/**
 * A one-line text field drawn the console's way: typed text in --fg, a
 * blinking block cursor, and optional ghost text after it in --ghost.
 * Used by the command line and the view picker.
 *
 * A real <input> sits invisibly on top of a mirror of its contents, so
 * clicks, selection and the clipboard stay native while what you see is
 * the mirror.
 *   {el, input, value(), set(text, caret?), setGhost(text), refresh(), destroy()}
 */
export function promptInput({ placeholder = "" } = {}) {
  const input = h("input", "c-input-native");
  input.type = "text";
  input.spellcheck = false;
  input.autocomplete = "off";
  input.setAttribute("autocapitalize", "off");
  const mirror = h("div", "c-input-mirror");
  const el = h("div", "c-input", [mirror, input]);
  let ghost = "";
  let pending = 0;

  function refresh() {
    const text = input.value;
    const start = input.selectionStart ?? text.length;
    const end = input.selectionEnd ?? start;
    const focused = document.activeElement === input;
    const parts = mirrorParts(text, start, end, focused);
    const showGhost = ghost && focused && start === end && start === text.length;
    if (showGhost) parts.push(h("span", "c-input-ghost", ghost));
    else if (!text && placeholder) parts.push(h("span", "c-input-ghost", placeholder));
    mirror.replaceChildren(...parts);
    mirror.scrollLeft = input.scrollLeft;
  }

  // A keydown's own effect on the caret lands after its listeners run.
  function later() {
    clearTimeout(pending);
    pending = setTimeout(refresh, 0);
  }

  const events = [["input", refresh], ["keydown", later], ["keyup", refresh], ["select", refresh],
    ["mouseup", refresh], ["focus", refresh], ["blur", refresh], ["scroll", refresh]];
  events.forEach(([name, fn]) => input.addEventListener(name, fn));
  refresh();

  return {
    el,
    input,
    value: () => input.value,
    set(text, caret = text.length) {
      input.value = text;
      input.setSelectionRange(caret, caret);
      refresh();
    },
    setGhost(text) {
      ghost = text || "";
      refresh();
    },
    refresh,
    destroy() {
      clearTimeout(pending);
      events.forEach(([name, fn]) => input.removeEventListener(name, fn));
    },
  };
}

/** The mirror's spans: the text cut at the selection or cursor. */
function mirrorParts(text, start, end, focused) {
  const cuts = new Set([0, text.length]);
  if (focused) {
    cuts.add(start);
    cuts.add(end);
    if (start === end) cuts.add(Math.min(start + 1, text.length));
  }
  const points = [...cuts].filter((n) => n >= 0 && n <= text.length).sort((a, b) => a - b);
  const parts = [];
  for (let i = 0; i < points.length - 1; i++) {
    const [a, b] = [points[i], points[i + 1]];
    const classes = [];
    if (focused && start !== end && start <= a && b <= end) classes.push("c-input-sel");
    if (focused && start === end && a === start) classes.push("c-input-cursor", "on-char");
    parts.push(h("span", classes.join(" "), text.slice(a, b)));
  }
  if (focused && start === end && start === text.length) parts.push(h("span", "c-input-cursor", ""));
  return parts;
}
