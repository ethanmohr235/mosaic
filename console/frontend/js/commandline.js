/* The command line: the `[mo]` prompt, the typed line with its block
 * cursor and ghost completion, the completion and history popups, and
 * the status on the right (a match count, the last result, or an error
 * in --fault).
 *
 * Parsing and completion live in Python (core/commands.py via
 * api.complete / api.run); this module only draws and routes keys.
 *
 *   ⏎      run in the focused pane (or as a statement)
 *   ⌥⏎     run into a new pane
 *   tab    take the highlighted completion
 *   ↑ ↓    move through the completion popup; ↑ on an empty line opens history
 *   ^r     search history
 *   esc    close the popup, then return to the pane
 *
 * A delete that needs confirming keeps its text; a second ⏎ on the same
 * line confirms it. */

const HINTS = {
  line: [["⏎", "run"], ["⌥⏎", "run in new pane"], ["tab", "complete"], ["↑", "history"], ["esc", "back to pane"]],
  completion: [["⏎", "run"], ["⌥⏎", "run in new pane"], ["tab", "complete"], ["↑↓", "choose"], ["esc", "close"]],
  history: [["⏎", "re-run"], ["⌥⏎", "to new pane"], ["e", "edit"], ["^r", "search history"], ["esc", "close"]],
  confirm: [["⏎", "confirm"], ["esc", "cancel"]],
};

export function createCommandLine({ root, popupEl, store, api, components, onSubmit, onLeave }) {
  const { h } = components;
  // `$` like a pane header's command; `mo ` typed at the start of the line
  // is drawn as the [mo] box, so `mo tion` reads as the app's lockup.
  const field = components.promptInput({ lockup: true });
  const status = h("span", "cmd-status");
  root.replaceChildren(h("span", "cmd-prompt", "$"), field.el, status);

  const st = { mode: null, comp: null, hist: null, confirmLine: null, seq: 0 };

  /* ---------- status ---------- */

  function setStatus(text = "", level = "idle") {
    status.textContent = text;
    status.className = `cmd-status lvl-${level}`;
    status.title = text;
  }

  function changed() {
    store.emit("hints");
  }

  /* ---------- completion ---------- */

  async function requestCompletion() {
    const text = field.value();
    const caret = field.input.selectionStart ?? text.length;
    const seq = ++st.seq;
    if (!text.trim()) {
      closePopup();
      return;
    }
    let comp;
    try {
      comp = await api.complete(text, caret);
    } catch (err) {
      console.warn("completion failed", err);
      return;
    }
    if (seq !== st.seq || st.mode === "history" || !isActive()) return;
    st.comp = { ...comp, rows: comp.rows || [], highlighted: 0, navigated: false };
    st.mode = st.comp.rows.length ? "completion" : null;
    field.setGhost(comp.ghost || "");
    renderPopup();
  }

  function ghostFor(row) {
    const text = field.value();
    const typed = text.slice(st.comp.replaceFrom || 0, field.input.selectionStart ?? text.length);
    return row.value.startsWith(typed) ? row.value.slice(typed.length) : "";
  }

  function moveCompletion(step) {
    const n = st.comp.rows.length;
    st.comp.highlighted = (st.comp.highlighted + step + n) % n;
    st.comp.navigated = true;
    field.setGhost(ghostFor(st.comp.rows[st.comp.highlighted]));
    renderPopup();
  }

  function applyCompletion(index) {
    const row = st.comp && st.comp.rows[index];
    if (!row) return;
    const text = field.value();
    const from = st.comp.replaceFrom || 0;
    const caret = field.input.selectionStart ?? text.length;
    field.set(text.slice(0, from) + row.value + text.slice(caret), from + row.value.length);
    requestCompletion();
  }

  function completionPopup() {
    const { comp } = st;
    const apps = new Set(store.state.apps.map((a) => a.suffix));
    const appMode = (comp.hint || "").startsWith("mo ");
    const rows = comp.rows.map((row) => {
      const describe = `${row.description || ""}${row.current ? " · current" : ""}`;
      const cells = appMode ? [apps.has(row.value) ? "mo" : "", row.value, describe] : [row.value, describe];
      return { cells, dim: row.dim, current: row.current };
    });
    const sel = store.primary();
    const footer = appMode ? "tab complete · ⏎ open · ⌥⏎ open in new pane"
      : `tab complete · ⏎ run · ↑ history${sel ? ` · @sel = ${store.label(sel)}` : ""}`;
    return components.popupList({
      hint: comp.hint || "", rows, layout: appMode ? "app" : "key",
      highlighted: comp.highlighted, footer,
      onPick: (i) => applyCompletion(i),
    });
  }

  /* ---------- history ---------- */

  function historyEntries(filter) {
    const seen = new Set();
    const out = [];
    const needle = filter.toLowerCase();
    for (let i = store.state.history.length - 1; i >= 0; i -= 1) {
      const entry = store.state.history[i];
      if (seen.has(entry.command)) continue;
      seen.add(entry.command);
      if (!needle || entry.command.toLowerCase().includes(needle)) out.unshift(entry);
    }
    return out;
  }

  function openHistory(filtering = false) {
    const draft = st.hist ? st.hist.draft : field.value();
    st.hist = { filter: "", filtering, draft, entries: historyEntries(""), highlighted: 0 };
    st.hist.highlighted = st.hist.entries.length - 1;
    st.mode = "history";
    field.setGhost("");
    previewHistory();
  }

  function refilter() {
    st.hist.entries = historyEntries(st.hist.filter);
    st.hist.highlighted = st.hist.entries.length - 1;
    previewHistory();
  }

  function previewHistory() {
    const entry = st.hist.entries[st.hist.highlighted];
    field.set(entry ? entry.command : st.hist.draft);
    renderPopup();
  }

  function moveHistory(step) {
    const next = st.hist.highlighted + step;
    if (next >= st.hist.entries.length) {
      field.set(st.hist.draft);
      closePopup();
      return;
    }
    st.hist.highlighted = Math.max(0, next);
    previewHistory();
  }

  function historyPopup() {
    const { hist } = st;
    const total = hist.entries.length;
    const position = total ? total - hist.highlighted : 0;
    const filter = hist.filtering ? ` · search: ${hist.filter}` : "";
    const popup = components.popupList({
      hint: `history · ${store.state.garden}${filter}`,
      hintRight: `${position} / ${store.state.history.length}`,
      rows: hist.entries.map((e) => ({ cells: [(e.time || "").slice(11, 16), e.command] })),
      layout: "time",
      highlighted: hist.highlighted,
      footer: "⏎ re-run · e edit · ⌥⏎ to new pane · type to filter",
      onPick: (i) => {
        hist.highlighted = i;
        previewHistory();
        closePopup();
      },
    });
    return popup;
  }

  function historyKey(event) {
    const { hist } = st;
    const key = event.key;
    const printable = key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey;
    if (key === "ArrowUp") moveHistory(-1);
    else if (key === "ArrowDown") moveHistory(1);
    else if (key === "Enter") {
      closePopup();
      submit(field.value(), { newPane: event.altKey });
    } else if (key === "Escape") {
      field.set(hist.draft);
      closePopup();
    } else if (key === "Tab" || key === "ArrowRight" || (key === "e" && !hist.filtering)) closePopup();
    else if (event.ctrlKey && key.toLowerCase() === "r") {
      hist.filtering = true;
      renderPopup();
    } else if (key === "Backspace" && hist.filtering) {
      hist.filter = hist.filter.slice(0, -1);
      refilter();
    } else if (printable) {
      hist.filtering = true;
      hist.filter += key;
      refilter();
    } else return;
    event.preventDefault();
  }

  /* ---------- popup ---------- */

  function renderPopup() {
    if (st.mode === "completion" && st.comp && st.comp.rows.length) {
      popupEl.replaceChildren(completionPopup());
      setStatus(`${st.comp.rows.filter((r) => !r.dim).length} matches`);
    } else if (st.mode === "history" && st.hist) {
      popupEl.replaceChildren(historyPopup());
    } else {
      popupEl.replaceChildren();
    }
    popupEl.hidden = !popupEl.firstChild;
    const current = popupEl.querySelector(".c-popup-row.highlighted");
    if (current) current.scrollIntoView({ block: "nearest" });
    changed();
  }

  function closePopup() {
    const wasCompletion = st.mode === "completion";
    st.mode = null;
    st.comp = null;
    st.hist = null;
    field.setGhost("");
    if (wasCompletion) setStatus("");
    renderPopup();
  }

  /* ---------- running ---------- */

  async function submit(line, { newPane = false } = {}) {
    const text = line.trim();
    if (!text) return;
    const confirm = st.confirmLine === text;
    closePopup();
    setStatus("…");
    let outcome;
    try {
      outcome = await onSubmit(text, { newPane, confirm });
    } catch (err) {
      outcome = { message: String(err && err.message ? err.message : err), level: "fault" };
    }
    finish(text, outcome || {});
  }

  function finish(text, outcome) {
    if (outcome.needsConfirm) {
      st.confirmLine = text;
      setStatus(`${outcome.message} · ⏎ again to confirm`, "warn");
      changed();
      return;
    }
    st.confirmLine = null;
    setStatus(outcome.message || "", outcome.level === "fault" ? "fault" : "idle");
    if (outcome.level === "fault") {
      changed();
      return;
    }
    field.set("");
    leave();
  }

  function leave() {
    field.input.blur();
    closePopup();
    onLeave();
  }

  /* ---------- keys ---------- */

  function onKeyDown(event) {
    if (st.mode === "history") {
      historyKey(event);
      return;
    }
    const key = event.key;
    const comp = st.mode === "completion" ? st.comp : null;
    if (key === "Enter") {
      event.preventDefault();
      if (comp && comp.navigated) applyCompletion(comp.highlighted);
      submit(field.value(), { newPane: event.altKey });
    } else if (key === "Tab") {
      event.preventDefault();
      if (comp) applyCompletion(comp.highlighted);
    } else if (key === "ArrowUp" || key === "ArrowDown") {
      event.preventDefault();
      if (comp) moveCompletion(key === "ArrowUp" ? -1 : 1);
      else if (key === "ArrowUp" && !field.value()) openHistory(false);
    } else if (event.ctrlKey && key.toLowerCase() === "r") {
      event.preventDefault();
      openHistory(true);
    } else if (key === "Escape") {
      event.preventDefault();
      if (st.mode) closePopup();
      else if (st.confirmLine) cancelConfirm();
      else leave();
    }
  }

  function cancelConfirm() {
    st.confirmLine = null;
    field.set("");
    setStatus("cancelled");
    changed();
  }

  function onInput() {
    if (st.confirmLine && field.value().trim() !== st.confirmLine) {
      st.confirmLine = null;
      setStatus("");
    }
    if (st.mode !== "completion") setStatus("");
    requestCompletion();
    changed();
  }

  field.input.addEventListener("keydown", onKeyDown);
  field.input.addEventListener("input", onInput);
  field.input.addEventListener("focus", changed);
  field.input.addEventListener("blur", () => {
    if (st.mode) closePopup();
    changed();
  });
  root.addEventListener("mousedown", (event) => {
    if (event.target !== field.input && event.button === 0) {
      event.preventDefault();
      focus();
    }
  });

  /* ---------- public ---------- */

  function isActive() {
    return document.activeElement === field.input;
  }

  function focus(text = null, caret = null) {
    field.input.focus();
    if (text !== null) field.set(text, caret === null ? text.length : caret);
    field.refresh();
  }

  return {
    isActive,
    focus,
    /** `e` / header click: load a pane's command for editing. */
    load: (text, caret = null) => focus(text, caret),
    blur: () => {
      if (isActive()) field.input.blur();
    },
    run: submit,
    setStatus,
    value: () => field.value(),
    hints() {
      if (st.confirmLine) return HINTS.confirm;
      return HINTS[st.mode] || HINTS.line;
    },
  };
}
