/* The console's text editor: a note's source (views/edit.js) and the
 * worksheet (views/worksheet.js). docs/console.md → Settled for U6.
 *
 * A real <textarea> sits transparent over a mirror of its text that
 * carries the colour (syntax ranges, find matches, a per-line class and
 * ghost text after a line), so typing, selection, the clipboard and the
 * textarea's own undo stay native while what you see is the mirror. Both
 * live in one wrapper inside one scroller, so they can never scroll apart.
 *
 * Modal, like every text-editing pane (docs/console.md → Keyboard): the
 * editor starts in pane mode; `start()` (the view's `i` / ⏎, or a click)
 * puts the caret in it, and from then on it has every key but `^w` until
 * `esc`. `^f` opens find/replace above the text:
 *
 *   ⏎ / ⇧⏎   next / previous match       tab   find ↔ replace field
 *   ⏎ in replace: replace this one       ^⏎    replace every match
 *   esc      close the bar
 *
 * Replacing goes through execCommand("insertText"), so ^z in the textarea
 * takes a replacement back like any other typing. */

const LINE_SPLIT = /\n/;

function escapeHtml(text) {
  return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/**
 * createEditor({h, className, wrap, highlight, decorate, gutter, onInput, onStart, onLeave})
 *   highlight(text) -> [{start, end, cls}]   syntax ranges (may nest)
 *   decorate(index) -> {cls?, after?, mark?, title?}  line `index` (0-based):
 *                       a class on the line, --ghost text after it, and with
 *                       `gutter`, a mark in the margin column
 *   onInput(text)  every change; onStart() / onLeave() entering / leaving edit mode
 *   onPasteImage(base64, mime) -> text|null   an image on the clipboard: the
 *                       text to put at the caret in its place (null: paste
 *                       nothing). Without it, an image paste does nothing.
 *   snippets {key: [before, after]}  while typing, ^key puts `before` and
 *                       `after` round the selection, on lines of their own,
 *                       and leaves the caret between them.
 */
export function createEditor({ h, className = "", wrap = true, highlight = () => [], decorate = () => null,
  gutter = false, onInput = () => {}, onStart = () => {}, onLeave = () => {}, onPasteImage = null, snippets = {} }) {
  const textarea = h("textarea", "c-editor-text");
  textarea.spellcheck = false;
  textarea.setAttribute("autocapitalize", "off");
  textarea.setAttribute("autocomplete", "off");
  const mirror = h("pre", "c-editor-mirror");
  const margin = h("div", "c-editor-gutter");
  margin.hidden = !gutter;
  const wrapper = h("div", `c-editor-wrap${wrap ? " wrap" : ""}${gutter ? " with-gutter" : ""}`, [margin, mirror, textarea]);
  const scroller = h("div", "c-editor-scroll", wrapper);
  const findInput = h("input", "c-find-input");
  const replaceInput = h("input", "c-find-input");
  const count = h("span", "c-find-count dim");
  [findInput, replaceInput].forEach((i) => { i.type = "text"; i.spellcheck = false; });
  findInput.placeholder = "find";
  replaceInput.placeholder = "replace";
  const findBar = h("div", "c-find", [h("span", "dim", "find"), findInput, h("span", "dim", "→"), replaceInput, count]);
  findBar.hidden = true;
  const el = h("div", `c-editor ${className}`, [findBar, scroller]);

  const st = { editing: false, matches: [], current: -1 };

  /* ---- the mirror ---- */

  // Each line's markup as last painted, so a keystroke rewrites only the
  // lines it changed rather than the whole mirror. Each line is a block of
  // its own (shell.css), so it is also the only line laid out again: as
  // inline runs of one <pre>, every keystroke re-flowed all of them.
  const painted = { lines: [], marks: [] };

  function paint() {
    const text = textarea.value;
    const ranges = [...highlight(text), ...st.matches.map(([s, e], i) => ({
      start: s, end: e, cls: i === st.current ? "find-hit current" : "find-hit" }))]
      .sort((a, b) => a.start - b.start);
    const lines = text.split(LINE_SPLIT);
    let offset = 0;
    let next = 0;
    let open = [];
    const html = [];
    const marks = [];
    lines.forEach((line, index) => {
      const start = offset;
      const end = offset + line.length;
      // One sweep over the ranges (sorted by start), not a filter per line:
      // `open` is every range begun before this line ends and not done.
      while (next < ranges.length && ranges[next].start < end) open.push(ranges[next++]);
      open = open.filter((r) => r.end > start);
      const deco = decorate(index) || {};
      html.push(`<span class="ln${deco.cls ? ` ${deco.cls}` : ""}"${deco.title ? ` title="${escapeHtml(deco.title)}"` : ""}>`
        + segments(line, start, open)
        + (deco.after ? `<span class="ln-after">${escapeHtml(deco.after)}</span>` : "")
        + "</span>");
      if (gutter) marks.push(`<span class="mark ${deco.cls || ""}">${escapeHtml(deco.mark || "")}</span>`);
      offset = end + 1;
    });
    patch(mirror, painted.lines, html);
    painted.lines = html;
    if (gutter) {
      patch(margin, painted.marks, marks);
      painted.marks = marks;
    }
  }

  /** Bring `node` (children `old`) to `next`, touching only the run
   * between their common head and tail. */
  function patch(node, old, next) {
    const max = Math.min(old.length, next.length);
    let head = 0;
    while (head < max && old[head] === next[head]) head += 1;
    let tail = 0;
    while (tail < max - head && old[old.length - 1 - tail] === next[next.length - 1 - tail]) tail += 1;
    const kids = node.children;
    for (let i = old.length - tail - 1; i >= head; i -= 1) kids[i].remove();
    const fresh = next.slice(head, next.length - tail);
    if (!fresh.length) return;
    const tpl = document.createElement("template");
    tpl.innerHTML = fresh.join("");
    node.insertBefore(tpl.content, kids[head] || null);
  }

  /** One line's text cut at every range boundary, each piece wearing
   * every class whose range covers it -- so a command inside a `$…$` span,
   * inside a find match, carries all three. */
  function segments(line, start, ranges) {
    if (!ranges.length) return escapeHtml(line);
    const cuts = new Set([0, line.length]);
    ranges.forEach((r) => {
      cuts.add(Math.max(0, r.start - start));
      cuts.add(Math.min(line.length, r.end - start));
    });
    const points = [...cuts].sort((a, b) => a - b);
    let out = "";
    for (let i = 0; i < points.length - 1; i += 1) {
      const [a, b] = [points[i], points[i + 1]];
      if (a === b) continue;
      const classes = ranges.filter((r) => r.start - start <= a && b <= r.end - start).map((r) => r.cls);
      const piece = escapeHtml(line.slice(a, b));
      out += classes.length ? `<span class="${classes.join(" ")}">${piece}</span>` : piece;
    }
    return out;
  }

  /** Keep the caret's line on screen: the textarea never scrolls itself
   * (it is as tall as its text), so the scroller has to. */
  function revealCaret() {
    const { value, selectionStart } = textarea;
    let index = 0;
    for (let i = value.indexOf("\n"); i !== -1 && i < selectionStart; i = value.indexOf("\n", i + 1)) index += 1;
    const line = mirror.children[index];
    if (!line) return;
    const top = line.offsetTop;
    const bottom = top + line.offsetHeight;
    if (top < scroller.scrollTop) scroller.scrollTop = top;
    else if (bottom > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = bottom - scroller.clientHeight;
  }

  /* ---- modes ---- */

  function start() {
    if (st.editing) {
      textarea.focus();
      return;
    }
    st.editing = true;
    el.classList.add("editing");
    textarea.focus();
    onStart();
  }

  function stop() {
    if (!st.editing) return;
    st.editing = false;
    el.classList.remove("editing");
    textarea.blur();
    closeFind(false);
    onLeave();
  }

  /* ---- find / replace ---- */

  function findMatches() {
    const needle = findInput.value;
    st.matches = [];
    if (needle) {
      const hay = textarea.value.toLowerCase();
      const n = needle.toLowerCase();
      for (let i = hay.indexOf(n); i !== -1; i = hay.indexOf(n, i + Math.max(n.length, 1))) {
        st.matches.push([i, i + n.length]);
      }
    }
    if (st.current >= st.matches.length) st.current = st.matches.length - 1;
    if (st.current < 0 && st.matches.length) st.current = nearest();
    count.textContent = needle ? `${st.matches.length ? st.current + 1 : 0}/${st.matches.length}` : "";
    paint();
  }

  function nearest() {
    const caret = textarea.selectionStart;
    const at = st.matches.findIndex(([s]) => s >= caret);
    return at === -1 ? 0 : at;
  }

  function go(step) {
    if (!st.matches.length) return;
    st.current = (st.current + step + st.matches.length) % st.matches.length;
    const [s, e] = st.matches[st.current];
    textarea.setSelectionRange(s, e);
    count.textContent = `${st.current + 1}/${st.matches.length}`;
    paint();
    revealCaret();
  }

  function openFind() {
    if (!st.editing) start();
    findBar.hidden = false;
    const selected = textarea.value.slice(textarea.selectionStart, textarea.selectionEnd);
    if (selected && !selected.includes("\n")) findInput.value = selected;
    findInput.focus();
    findInput.select();
    findMatches();
  }

  function closeFind(refocus = true) {
    if (findBar.hidden) return;
    findBar.hidden = true;
    st.matches = [];
    st.current = -1;
    paint();
    if (refocus && st.editing) textarea.focus();
  }

  /** Replace through the textarea's own editing, so its ^z undoes it. */
  function insert(from, to, text) {
    textarea.focus();
    textarea.setSelectionRange(from, to);
    if (!document.execCommand("insertText", false, text)) {
      textarea.setRangeText(text, from, to, "end");
      textarea.dispatchEvent(new Event("input", { bubbles: true }));
    }
  }

  function replaceOne() {
    if (!st.matches.length) return;
    const [s, e] = st.matches[st.current];
    insert(s, e, replaceInput.value);
    replaceInput.focus();
    findMatches();
    if (st.matches.length) go(0);
  }

  function replaceAll() {
    const needle = findInput.value;
    if (!needle || !st.matches.length) return;
    let text = textarea.value;
    for (const [s, e] of [...st.matches].reverse()) text = text.slice(0, s) + replaceInput.value + text.slice(e);
    insert(0, textarea.value.length, text);
    replaceInput.focus();
    findMatches();
  }

  /* ---- pasted images ---- */

  /** The clipboard's image, if it holds one, as base64 (no data: prefix). */
  function clipboardImage(event) {
    const items = (event.clipboardData && event.clipboardData.items) || [];
    for (const item of items) {
      if (item.kind === "file" && item.type.startsWith("image/")) return item.getAsFile();
    }
    return null;
  }

  function readBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",", 2)[1] || "");
      reader.onerror = () => reject(reader.error);
      reader.readAsDataURL(file);
    });
  }

  /** Text pastes natively; an image is handed to the view, which stores it
   * and answers with the line to insert -- on a line of its own, since
   * `\image` is a block command. */
  async function onPaste(event) {
    const file = onPasteImage && clipboardImage(event);
    if (!file) return;
    event.preventDefault();
    const text = await onPasteImage(await readBase64(file), file.type);
    if (!text) return;
    const { selectionStart: from, selectionEnd: to, value } = textarea;
    const lead = from > 0 && value[from - 1] !== "\n" ? "\n" : "";
    const tail = value[to] === "\n" ? "" : "\n";
    insert(from, to, `${lead}${text}${tail}`);
  }

  /* ---- snippets ---- */

  function insertSnippet([before, after]) {
    const { selectionStart: from, selectionEnd: to, value } = textarea;
    const lead = from > 0 && value[from - 1] !== "\n" ? "\n" : "";
    const tail = value[to] === "\n" ? "" : "\n";
    const caret = from + lead.length + before.length;
    insert(from, to, `${lead}${before}${value.slice(from, to)}${after}${tail}`);
    textarea.setSelectionRange(caret, caret + (to - from));
    revealCaret();
  }

  /* ---- keys (the view hands them over; true = handled) ---- */

  function onKey(event) {
    const inFind = event.target === findInput || event.target === replaceInput;
    const ctrl = event.ctrlKey;
    if (ctrl && event.key.toLowerCase() === "f") {
      openFind();
      return true;
    }
    if (inFind) {
      if (event.key === "Escape") closeFind();
      else if (event.key === "Tab") (event.target === findInput ? replaceInput : findInput).focus();
      else if (event.key === "Enter" && ctrl) replaceAll();
      else if (event.key === "Enter" && event.target === replaceInput) replaceOne();
      else if (event.key === "Enter") go(event.shiftKey ? -1 : 1);
      else return false;
      return true;
    }
    if (!st.editing) return false;
    if (event.key === "Escape") {
      stop();
      return true;
    }
    if (event.key === "Tab" && !ctrl && !event.altKey) {
      insert(textarea.selectionStart, textarea.selectionEnd, "  ");
      return true;
    }
    const snippet = ctrl && !event.shiftKey && !event.altKey && snippets[event.key.toLowerCase()];
    if (snippet) {
      insertSnippet(snippet);
      return true;
    }
    return false;
  }

  /* ---- wiring ---- */

  const listeners = [
    [textarea, "input", () => {
      if (!findBar.hidden) findMatches();
      else paint();
      revealCaret();
      onInput(textarea.value);
    }],
    [textarea, "focus", () => { if (!st.editing) start(); }],
    [textarea, "paste", onPaste],
    [textarea, "keyup", revealCaret],
    [textarea, "click", revealCaret],
    [findInput, "input", () => { st.current = -1; findMatches(); }],
  ];
  listeners.forEach(([node, name, fn]) => node.addEventListener(name, fn));
  paint();

  return {
    el,
    textarea,
    value: () => textarea.value,
    /** Replace the whole text (a different note was opened). Not undoable
     * in the textarea, on purpose: ^z must not walk into another note. */
    set(text) {
      if (text === textarea.value) return;
      textarea.value = text;
      if (!findBar.hidden) findMatches();
      else paint();
    },
    repaint: paint,
    start,
    stop,
    get editing() {
      return st.editing;
    },
    focus() {
      if (!findBar.hidden && (document.activeElement === findInput || document.activeElement === replaceInput)) return;
      if (st.editing) textarea.focus();
    },
    scrollBy(lines) {
      const lh = parseFloat(getComputedStyle(mirror).lineHeight) || 18;
      scroller.scrollTop += lines * lh;
    },
    scrollTo(where) {
      scroller.scrollTop = where === "end" ? scroller.scrollHeight : 0;
    },
    onKey,
    destroy() {
      listeners.forEach(([node, name, fn]) => node.removeEventListener(name, fn));
    },
  };
}

/* ---- .modoc syntax ranges (the note editor's highlighter) ---- */

const HIGHLIGHT_RE = /((?<!\\)%[^\n]*)|(\\[a-zA-Z]+)|([{}[\]])|(\$[^$\n]*\$)/g;
const MATH_TOKEN_RE = /(\\[a-zA-Z]+)|([{}])/g;
const EQUATION_BLOCK_RE = /\\begin\{equation\}[\s\S]*?\\end\{equation\}/g;

/** Ranges for a note's source: comments, commands (known to the parser,
 * or not -- `known` comes from core/modoc.py through api.mora.open), braces
 * and `$…$` maths, with the commands and braces inside maths layered on
 * top. Inside an equation environment any command is maths, not a typo. */
export function modocRanges(text, known) {
  const ranges = [];
  const zones = [];
  for (const m of text.matchAll(EQUATION_BLOCK_RE)) zones.push([m.index, m.index + m[0].length]);
  const inZone = (i) => zones.some(([s, e]) => i >= s && i < e);
  for (const m of text.matchAll(HIGHLIGHT_RE)) {
    const start = m.index;
    const end = start + m[0].length;
    if (m[1]) ranges.push({ start, end, cls: "hl-comment" });
    else if (m[2]) {
      const name = m[2].slice(1);
      const math = name !== "begin" && name !== "end" && inZone(start);
      ranges.push({ start, end, cls: math ? "hl-math-cmd" : known.has(name) ? "hl-cmd" : "hl-cmd-unknown" });
    } else if (m[3]) ranges.push({ start, end, cls: "hl-brace" });
    else {
      ranges.push({ start, end, cls: "hl-math" });
      for (const t of m[0].slice(1, -1).matchAll(MATH_TOKEN_RE)) {
        const s = start + 1 + t.index;
        ranges.push({ start: s, end: s + t[0].length, cls: t[1] ? "hl-math-cmd" : "hl-brace" });
      }
    }
  }
  return ranges;
}
