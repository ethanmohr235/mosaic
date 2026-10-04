/* `edit` (`edit @sel|#id|‹name›`, `layout docs`): a note's .modoc source
 * (docs/console.md → Settled for U6).
 *
 * The editor (js/editor.js) is modal: `i` or ⏎ starts typing, `esc` stops.
 * While typing, the text is the store's draft for this note (store.setDraft),
 * which a `read` pane on the same note renders as it changes, and it is
 * saved on every pause (api.mora.save). One editing session -- `i` to `esc`
 * -- is one undo step however many pauses it saved on, and `esc` logs it.
 * A name no mote has yet (`edit Homework 5`) becomes a note on its first
 * save.
 *
 * Problems come from rendering the draft (api.mora.render, its figures
 * memoised), and underline their lines. `edit @sel` follows the selection,
 * but never while you are typing: the note under the caret stays until
 * `esc`. */

import { createEditor, modocRanges } from "../editor.js";

const HINTS = [["i ⏎", "edit"], ["j k", "scroll"], ["g G", "top / end"], ["^f", "find"]];
const EDIT_HINTS = [["esc", "stop editing"], ["^f", "find / replace"], ["^z", "undo typing"], ["^v", "paste a screenshot"]];
const AUTOSAVE_MS = 900;
const CHECK_MS = 400;

function message(err) {
  return String(err && err.message ? err.message : err);
}

let sessions = 0;

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const st = { command: "", seq: 0, note: null, error: null, empty: false, known: new Set(),
    saved: "", session: null, wrote: false, problems: new Map(), stale: false };
  let root = null, editor = null, saveTimer = 0, checkTimer = 0, savePromise = null, checkSeq = 0;

  const key = () => (st.note ? (st.note.noteId || `new:${st.note.name}`) : null);

  /* ---- saving ---- */

  function onInput(text) {
    store.setDraft(key(), text);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(save, AUTOSAVE_MS);
    scheduleCheck();
    store.emit("hints");
  }

  async function save() {
    clearTimeout(saveTimer);
    if (!st.note || !editor) return;
    const text = editor.value();
    if (text === st.saved && st.note.noteId) return;
    if (savePromise) await savePromise;
    const before = key();
    savePromise = api.mora.save(st.note.noteId, st.note.name, text, st.session || `s${++sessions}`);
    let r;
    try {
      r = await savePromise;
    } catch (err) {
      r = { error: message(err) };
    }
    savePromise = null;
    if (r.error) {
      store.log("result", `saving ${st.note.name} failed: ${r.error}`, { level: "fault" });
      return;
    }
    st.saved = text;
    st.wrote = true;
    if (r.created) {
      st.note = { ...st.note, noteId: r.noteId, exists: true };
      store.setDraft(before, null);
      store.setDraft(key(), editor ? editor.value() : text);
      store.log("result", `made the note ${st.note.name}`, { level: "ok" });
    }
    header();
  }

  async function onStart() {
    st.session = `s${++sessions}`;
    st.wrote = false;
    store.setDraft(key(), editor.value());
    store.emit("hints");
  }

  async function onLeave() {
    await save();
    if (st.wrote) {
      store.log("result", `saved ${st.note.name}`, { level: "ok" });
      store.emit("write");
    }
    store.setDraft(key(), null);
    st.session = null;
    st.wrote = false;
    store.emit("hints");
    header();
    if (st.stale) {
      st.stale = false;
      run(st.command);
    }
  }

  /* ---- pasted screenshots ---- */

  /** An image on the clipboard becomes an image mote (api.mora.paste_image,
   * auto-named shot-NNN, one undo step) and the line that shows it. */
  async function onPasteImage(base64) {
    let r;
    try {
      r = await api.mora.paste_image(base64);
    } catch (err) {
      r = { error: message(err) };
    }
    if (r.error) {
      store.log("result", `couldn't paste the image: ${r.error}`, { level: "fault" });
      return null;
    }
    store.log("result", `stored ${r.name} (${Math.ceil(r.bytes / 1024)} KB) -- rename it with: image rename ${r.name} ‹new›`,
      { level: "ok" });
    store.emit("write");
    return `\\image{${r.name}}`;
  }

  /* ---- problems ---- */

  function scheduleCheck() {
    clearTimeout(checkTimer);
    checkTimer = setTimeout(check, CHECK_MS);
  }

  async function check() {
    if (!st.note || !editor) return;
    const seq = ++checkSeq;
    let r;
    try {
      r = await api.mora.render(st.note.noteId, editor.value());
    } catch (err) {
      r = { error: message(err) };
    }
    if (seq !== checkSeq || !editor || r.error) return;
    st.problems = new Map();
    for (const p of r.problems) {
      if (p.line) st.problems.set(p.line - 1, [...(st.problems.get(p.line - 1) || []), p.message]);
    }
    st.words = r.stats.words;
    editor.repaint();
    header();
  }

  function decorate(index) {
    const found = st.problems.get(index);
    return found ? { cls: "ln-problem", title: found.join("\n") } : null;
  }

  /* ---- drawing ---- */

  let lastSummary = null;

  function summary() {
    if (!st.note) return { summary: "", level: "idle" };
    const parts = [];
    if (st.words !== undefined) parts.push(`${st.words} words`);
    const n = [...st.problems.values()].flat().length;
    if (n) parts.push(`${n} problem${n === 1 ? "" : "s"}`);
    if (!st.note.exists) parts.push("new note");
    else if (editor && editor.value() !== st.saved) parts.push("editing");
    return { summary: parts.join(" · "), level: st.problems.size ? "warn" : "ok" };
  }

  /** The header follows saves and checks between runs (a pane's summary
   * is otherwise only set by run()). */
  function header() {
    const s = summary();
    if (lastSummary && lastSummary.summary === s.summary && lastSummary.level === s.level) return;
    lastSummary = s;
    if (root) ctx.setSummary(s);
  }

  function render() {
    if (!root) return;
    if (st.error) {
      editor = null;
      root.replaceChildren(h("div", "pane-error lvl-fault", st.error));
      return;
    }
    if (st.empty) {
      editor = null;
      root.replaceChildren(h("div", "plot-empty", [
        h("div", null, "select a mote to write its note, or:"),
        h("div", "plot-usage", "edit ‹name›     (a new name makes the note on its first save)"),
      ]));
      return;
    }
    if (!editor) {
      editor = createEditor({ h, className: "editor-note", wrap: true,
        highlight: (text) => modocRanges(text, st.known), decorate, onInput, onStart, onLeave, onPasteImage });
      root.replaceChildren(editor.el);
    }
  }

  /* ---- keys ---- */

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

  /* ---- the contract ---- */

  async function run(command) {
    st.command = command;
    if (editor && editor.editing) {
      // Never swap the note out from under the caret: catch up on `esc`.
      st.stale = true;
      return summary();
    }
    const seq = ++st.seq;
    let r;
    try {
      r = await api.mora.open(command, store.state.selection);
    } catch (err) {
      r = { error: message(err) };
    }
    if (seq !== st.seq || !root) return { summary: "", level: "idle" };
    if (r.error || r.empty) {
      Object.assign(st, { note: null, error: r.error || null, empty: !!r.empty });
      render();
      return { summary: r.error || "", level: r.error ? "fault" : "idle" };
    }
    const same = st.note && st.note.noteId && st.note.noteId === r.noteId;
    Object.assign(st, { error: null, empty: false, known: new Set(r.commands || []) });
    st.note = { noteId: r.noteId, name: r.name, exists: r.exists };
    if (!same) {
      st.problems = new Map();
      st.words = undefined;
    }
    render();
    const draft = store.draft(key());
    st.saved = r.source;
    editor.set(draft !== null ? draft : r.source);
    check();
    return summary();
  }

  return {
    mount(el, command) {
      st.command = command;
      root = h("div", "view-editor");
      el.appendChild(root);
    },
    run,
    unmount() {
      clearTimeout(checkTimer);
      if (editor && editor.editing) save();
      else clearTimeout(saveTimer);
      if (st.note) store.setDraft(key(), null);
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
  id: "edit",
  describe: "a note's source, saved as you type (edit)",
  commands: [{ verb: "edit", canonical: "edit @sel" }],
  followsSel: false,
  create,
};
