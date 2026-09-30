/* The shell's one store. Everything more than one module needs to agree
 * on lives here: the garden, the selection, the split tree and which
 * pane is focused, the saved layouts, and the session log.
 *
 * Views read from it and ask it for changes (`setSelection`); a view
 * never keeps its own copy of the selection (docs/console.md →
 * *Selection*). Changes are announced with `emit`, and anything
 * interested `subscribe`s:
 *
 *   selection  the selection set or its primary changed
 *   write      something in the window wrote to the garden -- every pane re-runs
 *   refresh    re-read everything (window regained focus, `refresh`, undo/redo)
 *   layout     the tree, the focused pane, the layouts or the dirty flag changed
 *   garden     the workspace switched garden (the whole state was replaced)
 *   log        a session-log entry was added
 *
 * The tree held here is the *internal* tree (tree.js): leaves carry a
 * stable `id` the panes are keyed by. `tree.toJSON` strips those ids
 * for the bridge. */

import * as T from "./tree.js";

const LOG_CAP = 1000;

export function createStore() {
  const listeners = new Map();
  const state = {
    garden: "",
    gardens: [],
    apps: [],
    verbs: [],
    selection: [],
    labels: new Map(),
    tree: T.leaf(""),
    focusedId: null,
    layouts: [],
    active: null,
    history: [],
    log: [],
  };
  state.focusedId = T.firstLeaf(state.tree).id;

  function subscribe(event, fn) {
    if (!listeners.has(event)) listeners.set(event, new Set());
    listeners.get(event).add(fn);
    return () => listeners.get(event).delete(fn);
  }

  function emit(event, detail) {
    for (const fn of [...(listeners.get(event) || [])]) {
      try {
        fn(detail);
      } catch (err) {
        console.error(`store listener for ${event} failed`, err);
      }
    }
  }

  const store = { state, subscribe, emit };
  Object.assign(store, selectionOps(state, emit));
  Object.assign(store, treeOps(state, emit));
  Object.assign(store, sessionOps(state, emit));
  return store;
}

function selectionOps(state, emit) {
  return {
    /** The primary (first) selected mote id, or null. */
    primary: () => state.selection[0] || null,

    /** Replace the selection. `ids` is ordered; its first item is the primary. */
    setSelection(ids, labels = {}) {
      const next = [...new Set((ids || []).filter(Boolean))];
      for (const [id, label] of Object.entries(labels)) state.labels.set(id, label);
      if (sameList(next, state.selection)) return;
      state.selection = next;
      emit("selection", next);
    },

    /** Shift/ctrl-click: add `id` to the set, or drop it if already there. */
    toggleSelection(id, label) {
      if (label) state.labels.set(id, label);
      const has = state.selection.includes(id);
      const next = has ? state.selection.filter((x) => x !== id) : [...state.selection, id];
      state.selection = next;
      emit("selection", next);
    },

    /** A short human label for a mote id: its name if a view told us, else `#` + 8 chars. */
    label(id) {
      return state.labels.get(id) || `#${String(id).slice(0, 8)}`;
    },
  };
}

function treeOps(state, emit) {
  return {
    /** Swap in a new tree (and optionally a new focused pane). */
    setTree(tree, focusedId = state.focusedId) {
      state.tree = tree;
      state.focusedId = T.findLeaf(tree, focusedId) ? focusedId : T.firstLeaf(tree).id;
      emit("layout", { tree: true });
    },

    focus(id) {
      if (id === state.focusedId || !T.findLeaf(state.tree, id)) return;
      state.focusedId = id;
      emit("layout", { focus: true });
    },

    focusedLeaf: () => T.findLeaf(state.tree, state.focusedId),

    setLayouts(layouts, active) {
      state.layouts = layouts || [];
      state.active = active ?? null;
      emit("layout", { layouts: true });
    },

    /** True when the current tree no longer matches the active saved layout. */
    dirty() {
      if (!state.active) return false;
      const saved = state.layouts.find((l) => l.name === state.active);
      if (!saved) return false;
      return !T.sameShape(T.toJSON(state.tree), saved.tree);
    },
  };
}

function sessionOps(state, emit) {
  return {
    /** Replace everything garden-scoped with a Boot payload from Python. */
    applyBoot(boot) {
      state.garden = boot.garden;
      state.gardens = boot.gardens || [];
      state.apps = boot.apps || [];
      state.verbs = boot.verbs || [];
      state.layouts = boot.layouts || [];
      state.active = boot.active ?? null;
      state.history = boot.history || [];
      state.selection = [];
      state.labels = new Map();
      emit("garden", boot);
    },

    /** Mirror a line Python just appended to the garden's history. */
    addHistory(command, { replace = false } = {}) {
      const entry = { time: localIso(new Date()), command };
      if (replace && state.history.length) state.history[state.history.length - 1] = entry;
      else state.history.push(entry);
    },

    /** Add a session-log entry. level: "ok" | "warn" | "fault" | "idle". */
    log(kind, text, { level = "idle", moteId = null } = {}) {
      const entry = { time: new Date(), kind, text, level, moteId, garden: state.garden };
      state.log.push(entry);
      if (state.log.length > LOG_CAP) state.log.splice(0, state.log.length - LOG_CAP);
      emit("log", entry);
      return entry;
    },
  };
}

/** "2026-09-29T17:05:00" in local time, the shape history entries use. */
function localIso(date) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
    + `T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function sameList(a, b) {
  return a.length === b.length && a.every((x, i) => x === b[i]);
}
