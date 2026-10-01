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
 *   playhead   the shared playhead moved, started/stopped, or a sim pane
 *              claimed it with its run ({tick, count, playing, run} changed)
 *   live       `solve --live on|off` flipped the workspace's live toggle
 *   draft      an editor's unsaved text for a note changed ({key}); a read
 *              pane on the same note re-renders from it
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
    live: false,
    // One shared playhead (docs/console.md → Settled for U5): every Motion
    // pane reads `tick`; the sim pane that last ran or was focused owns it
    // and publishes its tick `count` and `run` ({ticks, scenario, solver}).
    playhead: { tick: 0, count: 0, playing: false, run: null, owner: null },
    // Unsaved note text by note key (its id, or `new:‹name›` before the
    // first save), so `read` shows what `edit` is typing (Settled for U6).
    drafts: new Map(),
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
  Object.assign(store, playheadOps(state, emit));
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
      state.live = !!boot.live;
      state.crumbs = boot.crumbs || [boot.garden];
      state.playhead = { tick: 0, count: 0, playing: false, run: null, owner: null };
      state.drafts = new Map();
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

function playheadOps(state, emit) {
  const clamp = (tick, count) => Math.max(0, Math.min(Math.max(count - 1, 0), Math.round(tick)));
  return {
    /** Move the playhead to `tick` (clamped to the owner's run). */
    seek(tick) {
      const p = state.playhead;
      const next = clamp(tick, p.count);
      if (next === p.tick) return;
      p.tick = next;
      emit("playhead", { tick: true });
    },
    /** `step +N` / `step N`. */
    step(n, mode = "by") {
      this.seek(mode === "to" ? n : state.playhead.tick + n);
    },
    setPlaying(playing) {
      const p = state.playhead;
      if (playing && p.count < 2) return;
      if (!!playing === p.playing) return;
      p.playing = !!playing;
      emit("playhead", { playing: true });
    },
    /** A sim pane publishing its run. `owner` is the pane's own token;
     * `force` takes the playhead from another pane (it was focused). */
    claimPlayhead(owner, { count, run }, { force = false } = {}) {
      const p = state.playhead;
      if (p.owner && p.owner !== owner && !force) return false;
      const changed = p.owner !== owner || p.count !== count || JSON.stringify(p.run) !== JSON.stringify(run);
      Object.assign(p, { owner, count, run });
      p.tick = clamp(p.tick, count);
      if (count < 2) p.playing = false;
      if (changed) emit("playhead", { run: true });
      return true;
    },
    releasePlayhead(owner) {
      const p = state.playhead;
      if (p.owner !== owner) return;
      Object.assign(p, { owner: null, count: 0, run: null, playing: false });
      emit("playhead", { run: true });
    },
    setDraft(key, text) {
      if (text === null || text === undefined) state.drafts.delete(key);
      else state.drafts.set(key, text);
      emit("draft", { key });
    },
    draft: (key) => (state.drafts.has(key) ? state.drafts.get(key) : null),
    setLive(live) {
      state.live = !!live;
      emit("live", state.live);
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
