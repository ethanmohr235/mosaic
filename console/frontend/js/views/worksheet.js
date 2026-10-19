/* `worksheet` (`worksheet`): the garden's calculation as lines of
 * text, on the blueprint surface (docs/console.md → Settled for U6;
 * docs/simulation.md → The worksheet).
 *
 *   bd.x = 80 in            a given          bd.l = ?           an unknown
 *   bd.l = sqrt(…)          an equation      bd.Ax + bd.Bx == 0 a relation
 *   bd.sigma <= 250         a check          … ! ΣFx            names the rule
 *
 * Typing only parses (api.mora.preview): the margin marks each line's kind,
 * a line that can't be written is struck in --fault with the reason on
 * hover, and an unknown or an equation shows the value the garden holds for
 * it now, faded after the line. `esc` applies the whole sheet
 * (api.mora.apply) as one undo step -- with `live` on, a solve follows --
 * and then shows the garden's own reading of it. */

import { createEditor } from "../editor.js";

const HINTS = [["i ⏎", "edit"], ["j k", "scroll"], ["^f", "find"]];
const EDIT_HINTS = [["esc", "apply"], ["^f", "find / replace"], ["^z", "undo typing"]];
const MARKS = { given: "=", unknown: "?", equation: "ƒ", relation: "≡", check: "✓", blank: "" };
const PREVIEW_MS = 150;

function message(err) {
  return String(err && err.message ? err.message : err);
}

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  // `keep`: the text as typed, when applying it reported problems -- the
  // garden's reading would drop the very lines that need fixing.
  const st = { seq: 0, lines: [], error: null, applied: "", previewSeq: 0, keep: null };
  let root = null, editor = null, previewTimer = 0;

  function decorate(index) {
    const line = st.lines[index];
    if (!line) return null;
    if (line.problem) return { cls: "ws-problem", mark: "!", title: line.problem };
    return { cls: `ws-${line.kind}`, mark: MARKS[line.kind] || "", after: line.value ? `  = ${line.value}` : "" };
  }

  function onInput() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(preview, PREVIEW_MS);
    store.emit("hints");
  }

  async function preview() {
    if (!editor) return;
    const seq = ++st.previewSeq;
    let r;
    try {
      r = await api.mora.preview(editor.value());
    } catch (err) {
      r = { error: message(err) };
    }
    if (seq !== st.previewSeq || !editor || r.error) return;
    st.lines = r.lines;
    editor.repaint();
    ctx.setSummary(summary());
  }

  async function onLeave() {
    clearTimeout(previewTimer);
    const text = editor.value();
    if (text === st.applied) {
      store.emit("hints");
      return;
    }
    let r;
    try {
      r = await api.mora.apply(text);
    } catch (err) {
      r = { error: message(err) };
    }
    if (r.error) {
      store.log("result", `the worksheet: ${r.error}`, { level: "fault" });
      return;
    }
    st.keep = r.problems.length ? text : null;
    store.log("result", r.message, { level: r.problems.length ? "warn" : "ok" });
    r.problems.forEach((p) => store.log("result", p, { level: "warn" }));
    if (r.wrote) store.emit("write");
    else run();
    store.emit("hints");
  }

  function summary() {
    const bad = st.lines.filter((l) => l.problem).length;
    const detail = st.lines.flatMap((l, i) => (l.problem ? [`line ${i + 1}: ${l.problem}`] : [])).join("\n");
    const count = st.lines.filter((l) => l.kind !== "blank").length;
    const editing = editor && editor.value() !== st.applied ? " · not applied" : "";
    return { summary: `${count} line${count === 1 ? "" : "s"}${bad ? ` · ${bad} problem${bad === 1 ? "" : "s"}` : ""}${editing}`,
      detail, level: bad ? "warn" : "ok" };
  }

  function render() {
    if (!root) return;
    if (st.error) {
      editor = null;
      root.replaceChildren(h("div", "pane-error lvl-fault", st.error));
      return;
    }
    if (!editor) {
      editor = createEditor({ h, className: "editor-blueprint", wrap: false, gutter: true,
        decorate, onInput, onLeave, onStart: () => store.emit("hints") });
      root.replaceChildren(editor.el);
    }
  }

  const KEYS = {
    i: () => editor.start(), Enter: () => editor.start(),
    j: () => editor.scrollBy(1), ArrowDown: () => editor.scrollBy(1),
    k: () => editor.scrollBy(-1), ArrowUp: () => editor.scrollBy(-1),
    g: () => editor.scrollTo("top"), G: () => editor.scrollTo("end"),
  };

  function onKey(event) {
    if (!editor) return false;
    if (editor.onKey(event)) return true;
    if (editor.editing || event.ctrlKey || event.metaKey || event.altKey) return false;
    const action = KEYS[event.key];
    if (!action) return false;
    event.preventDefault();
    action();
    return true;
  }

  async function run() {
    if (editor && editor.editing) return summary();
    const seq = ++st.seq;
    let r;
    try {
      r = await api.mora.worksheet();
    } catch (err) {
      r = { error: message(err) };
    }
    if (seq !== st.seq || !root) return { summary: "", level: "idle" };
    if (r.error) {
      st.error = r.error;
      render();
      return { summary: r.error, level: "fault" };
    }
    st.error = null;
    render();
    st.applied = st.keep !== null ? st.keep : r.text;
    editor.set(st.applied);
    await preview();
    return summary();
  }

  return {
    mount(el) {
      root = h("div", "view-editor");
      el.appendChild(root);
    },
    run,
    unmount() {
      clearTimeout(previewTimer);
      if (editor) editor.destroy();
      st.seq += 1;
      root = editor = null;
    },
    hints: () => (editor ? (editor.editing ? EDIT_HINTS : HINTS) : []),
    onKey,
    get textEditing() {
      return !!(editor && editor.editing);
    },
    focus() {
      if (editor) editor.focus();
    },
  };
}

export default {
  id: "worksheet",
  describe: "the garden's calculation as lines of text (worksheet)",
  commands: [{ verb: "worksheet" }],
  followsSel: false,
  create,
};
