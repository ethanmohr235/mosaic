/* `mora:read` (`read @sel|#id|‹name›`, `mo ra`): a note, rendered
 * (docs/console.md → Settled for U6).
 *
 * Drawn on its own surface -- the dark gray paper tokens (--paper-*) --
 * so a rendered document never reads as the editor. Everything it says
 * comes from core/document.py through api.mora.render: what a `\val` is,
 * what `\ref` reads as, which rows `\given` lists. This only typesets:
 * KaTeX for maths (the global `katex`, vendor/katex), js/plot.js for a
 * `\graph`.
 *
 * It renders the store's draft for its note when an `edit` pane is
 * typing in it (store "draft"), else the stored text, so it follows
 * unsaved typing. A link selects its mote; problems are listed at the end. */

import { render as drawPlot, figureModel } from "../plot.js";

const HINTS = [["j k", "scroll"], ["g G", "top / end"], ["click", "select a linked mote"]];
const DRAFT_MS = 180;
const FIGURE_H = 240;

function message(err) {
  return String(err && err.message ? err.message : err);
}

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const st = { command: "", seq: 0, note: null, payload: null, error: null, empty: false };
  let root = null, page = null, resize = null, draftTimer = 0;
  const figures = [];
  const offs = [];

  const key = () => (st.note ? (st.note.noteId || `new:${st.note.name}`) : null);

  /* ---- inline ---- */

  function inline(nodes) {
    return nodes.map((n) => {
      if (n.type === "text") return document.createTextNode(n.text);
      if (n.type === "styled") return h(n.style === "bold" ? "strong" : n.style === "code" || n.style === "mono" ? "code" : "em", null, n.text);
      if (n.type === "math") return math(n.tex, false);
      if (n.type === "ref") return h("span", n.error ? "doc-broken" : "doc-ref", n.text);
      if (n.type === "value") {
        const node = h("span", n.error ? "doc-broken" : "doc-value", n.text);
        if (n.error) node.title = n.error;
        return node;
      }
      if (n.type === "link") {
        const node = h("span", n.moteId ? "doc-link" : "doc-broken", n.text);
        if (n.moteId) node.addEventListener("click", () => ctx.navigate(n.moteId, n.text));
        else node.title = n.error || "";
        return node;
      }
      return document.createTextNode("");
    });
  }

  /** KaTeX, never trusted (no \href, no \includegraphics in a note that
   * may have arrived over sync); TeX it can't set shows as the raw text. */
  function math(tex, display) {
    const node = h(display ? "div" : "span", display ? "doc-math display" : "doc-math");
    try {
      window.katex.render(tex, node, { displayMode: display, throwOnError: false, strict: false });
    } catch (err) {
      node.classList.add("raw");
      node.textContent = tex;
    }
    return node;
  }

  /* ---- blocks ---- */

  function block(b) {
    switch (b.type) {
      case "heading": return h(b.level === 1 ? "h1" : "h2", "doc-heading", inline(b.content));
      case "paragraph": return h("p", "doc-p", inline(b.content));
      case "list": return h(b.ordered ? "ol" : "ul", "doc-list", b.items.map((i) => h("li", null, inline(i))));
      case "equation": return numbered(math(b.tex, true), b.number);
      case "cited": return cited(b);
      case "graph": return graph(b);
      case "table": return table(b);
      case "work": return work(b);
      case "section": return section(b);
      default: return h("div", "doc-broken", `line ${b.line}: nothing to draw`);
    }
  }

  function numbered(body, number, tail = null) {
    return h("div", "doc-eq", [h("div", "doc-eq-body", [body, tail || ""]),
      h("span", "doc-eq-number", number ? `(${number})` : "")]);
  }

  function cited(b) {
    if (b.error) return h("div", "doc-broken", `\\rule{${b.rule}}: ${b.error}`);
    return numbered(h("div", "doc-math display doc-code", b.equation), b.number, h("div", "doc-caption", `— ${b.name}`));
  }

  function caption(b) {
    if (!b.number && !(b.caption || []).length) return "";
    return h("div", "doc-caption", [`${b.word} ${b.number}`, (b.caption || []).length ? ": " : "", ...inline(b.caption || [])]);
  }

  function graph(b) {
    const f = b.figure || {};
    const area = h("div", "doc-figure-area");
    const node = h("figure", "doc-figure", [area, caption(b)]);
    if (f.error) area.replaceChildren(h("div", "doc-broken", `\\graph{${b.view}}: ${f.error}`));
    else figures.push({ area, figure: f });
    return node;
  }

  function table(b) {
    const head = h("tr", null, b.header.map((c) => h("th", null, inline(c))));
    const rows = b.rows.map((r) => h("tr", null, r.map((c) => h("td", null, inline(c)))));
    return h("div", "doc-table-wrap", [h("table", "doc-table", [head, ...rows]), caption(b)]);
  }

  function work(b) {
    if (b.error) return h("div", "doc-broken", `\\work{${b.target}}: ${b.error}`);
    if (!b.steps.length) {
      return h("div", "doc-work", [h("div", null, `${b.label} = ${b.result}`), b.note ? h("div", "doc-note", b.note) : ""]);
    }
    const lines = b.steps.map((s) => h("div", `doc-work-step ${s.kind}`, [
      s.kind === "result" ? `${b.label} = ${s.text}` : s.text, s.note ? h("span", "doc-note", `  ${s.note}`) : ""]));
    if (b.alsoDetermined.length) lines.push(h("div", "doc-note", `found together with ${b.alsoDetermined.join(", ")}`));
    return h("div", "doc-work", lines);
  }

  function section(b) {
    const head = h("div", "doc-section-head", b.heading);
    const parts = [head];
    if (b.error) parts.push(h("div", "doc-broken", b.error));
    if (b.kind === "checks") {
      if (!(b.checks || []).length) parts.push(h("div", "doc-note", "no checks"));
      for (const c of b.checks || []) {
        const level = c.passed ? "ok" : c.status === "fail" ? "fault" : "warn";
        parts.push(h("div", "doc-check", [h("span", `lvl-${level}`, c.passed ? "✓" : c.status === "fail" ? "✗" : "?"),
          ` ${c.equation}`, c.detail ? h("span", "doc-note", `  ${c.detail}`) : ""]));
      }
    } else if (b.kind === "verification") {
      const rows = (b.comparisons || []).map((c) => h("tr", null, [h("td", null, c.ref), h("td", null, c.hand),
        h("td", null, c.external), h("td", c.error === null ? "doc-note" : Math.abs(c.error) > 5 ? "lvl-warn" : null,
          c.error === null ? (c.problem || "—") : `${c.error >= 0 ? "+" : ""}${c.error.toFixed(2)}%`)]));
      parts.push(rows.length ? h("table", "doc-table", [h("tr", null, ["", "hand", "external", "error"].map((t) => h("th", null, t))), ...rows])
        : h("div", "doc-note", "nothing to verify -- mark an external value with `checks`"));
    } else {
      const rows = (b.rows || []).map((r) => h("tr", null, [h("td", null, r.ref), h("td", "doc-num", r.value),
        h("td", "doc-note", r.source)]));
      parts.push(rows.length ? h("table", "doc-table plain", rows) : h("div", "doc-note", "(none)"));
    }
    return h("div", "doc-section", parts);
  }

  /* ---- drawing ---- */

  function drawFigures() {
    for (const { area, figure } of figures) {
      const width = area.clientWidth;
      if (width < 40) continue;
      area.style.height = `${FIGURE_H}px`;
      drawPlot(area, figureModel(figure, width, FIGURE_H));
    }
  }

  function render() {
    if (!root) return;
    if (st.error) return root.replaceChildren(h("div", "pane-error lvl-fault", st.error));
    if (st.empty) return root.replaceChildren(h("div", "plot-empty", "select a note, or: read ‹name›"));
    if (!st.payload) return;
    figures.length = 0;
    const top = page ? page.scrollTop : 0;
    const blocks = st.payload.blocks.map(block);
    if (!blocks.length) blocks.push(h("p", "doc-note", st.note && st.note.exists ? "(an empty note)" : "(a new note -- nothing written yet)"));
    const problems = st.payload.problems;
    if (problems.length) {
      blocks.push(h("div", "doc-problems", [h("div", "doc-section-head", `${problems.length} problem${problems.length === 1 ? "" : "s"}`),
        ...problems.map((p) => h("div", "doc-problem", [h("span", "doc-note", p.line ? `line ${p.line}  ` : ""), p.message]))]));
    }
    page = h("div", "doc-page scroll", h("article", "doc", blocks));
    root.replaceChildren(page);
    page.scrollTop = top;
    drawFigures();
  }

  function summary() {
    if (!st.payload) return { summary: "", level: "idle" };
    const n = st.payload.problems.length;
    const editing = store.draft(key()) !== null ? " · draft" : "";
    return { summary: `${st.payload.stats.words} words${n ? ` · ${n} problem${n === 1 ? "" : "s"}` : ""}${editing}`,
      level: n ? "warn" : "ok" };
  }

  async function draw() {
    const seq = ++st.seq;
    const draft = store.draft(key());
    let p;
    try {
      p = await api.mora.render(st.note.noteId, draft !== null ? draft : st.note.source);
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq || !root) return null;
    if (p.error) {
      st.error = p.error;
      render();
      return { summary: p.error, level: "fault" };
    }
    Object.assign(st, { payload: p, error: null });
    render();
    return summary();
  }

  offs.push(store.subscribe("draft", (what) => {
    if (!st.note || !what || what.key !== key()) return;
    clearTimeout(draftTimer);
    draftTimer = setTimeout(async () => {
      const s = await draw();
      if (s) ctx.setSummary(s);
    }, DRAFT_MS);
  }));

  /* ---- keys ---- */

  function scroll(lines) {
    if (page) page.scrollTop += lines * 18;
  }

  const KEYS = {
    j: () => scroll(1), ArrowDown: () => scroll(1), k: () => scroll(-1), ArrowUp: () => scroll(-1),
    g: () => { if (page) page.scrollTop = 0; }, G: () => { if (page) page.scrollTop = page.scrollHeight; },
    " ": () => scroll(10),
  };

  function onKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey || !page) return false;
    const action = KEYS[event.key];
    if (!action) return false;
    action();
    return true;
  }

  /* ---- the contract ---- */

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    let r;
    try {
      r = await api.mora.open(command, store.state.selection);
    } catch (err) {
      r = { error: message(err) };
    }
    if (seq !== st.seq || !root) return { summary: "", level: "idle" };
    if (r.error || r.empty) {
      Object.assign(st, { note: null, payload: null, error: r.error || null, empty: !!r.empty });
      render();
      return { summary: r.error || "", level: r.error ? "fault" : "idle" };
    }
    st.empty = false;
    st.note = { noteId: r.noteId, name: r.name, source: r.source, exists: r.exists };
    return (await draw()) || { summary: "", level: "idle" };
  }

  return {
    mount(el, command) {
      st.command = command;
      root = h("div", "view-doc");
      el.appendChild(root);
      resize = new ResizeObserver(() => drawFigures());
      resize.observe(root);
    },
    run,
    unmount() {
      offs.splice(0).forEach((off) => off());
      clearTimeout(draftTimer);
      if (resize) resize.disconnect();
      st.seq += 1;
      root = page = resize = null;
    },
    hints: () => (st.payload ? HINTS : []),
    onKey,
  };
}

export default {
  id: "mora:read",
  app: "ra",
  describe: "a note, rendered; follows unsaved typing (read)",
  commands: [{ verb: "read", canonical: "read @sel" }],
  followsSel: false,
  create,
};
