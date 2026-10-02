/* Running a line: send it to api.run and apply the one effect that comes
 * back (the U1 bridge contract). Every run is logged, and returns an
 * outcome for the command line's status: {message, level, needsConfirm?}.
 *
 *   statement  log it; select the mote it named; a write re-runs every pane
 *   open_view  the focused pane gets the command (^⏎ / ⇧⏎: a new split below / right does)
 *   layout     save / load / rm / list
 *   garden     `cd` (or renaming the open garden): the workspace is
 *              re-booted on the new garden
 *   undo/redo  log what was undone; every pane re-runs
 *   refresh    every pane re-runs
 *   open_layout  a built-in layout (`layout calc`): its panes take
 *              the focused pane's place (^⏎ / ⇧⏎: a new split's)
 *   live       `solve --live on|off`: the top bar's `live`
 *   playhead   `step`, `play`, `pause`: the shared playhead
 *   pane_action  a line that acts on a pane of one view (`plot save ‹name›`,
 *              `plot export png`): the focused such pane's instance.action()
 *   error      shown in --fault on the command line and in the log */

import * as T from "./tree.js";

function message(err) {
  return String(err && err.message ? err.message : err);
}

export function createRunner({ store, api, actions, panes, autosave, bootWorkspace }) {
  function statement(e) {
    if (e.needsConfirm) {
      store.log("result", e.message, { level: "warn", moteId: e.moteId });
      return { message: e.message, level: "warn", needsConfirm: true };
    }
    const level = e.ok ? "ok" : "fault";
    store.log("result", e.message || (e.ok ? "ok" : "failed"), { level, moteId: e.moteId });
    if (e.ok && e.moteId) {
      store.setSelection([e.moteId], e.moteName ? { [e.moteId]: e.moteName } : {});
    }
    if (e.wrote) store.emit("write");
    return { message: e.message, level };
  }

  function openView(e, { newPane }) {
    if (newPane) actions.openInNewPane(e.command, newPane);
    else actions.setCommand(e.command);
    store.log("result", `${e.view}: ${e.command}`, { level: "ok" });
    return { message: "", level: "ok" };
  }

  async function layout(e) {
    let text;
    if (e.op === "save") {
      const saved = await api.layout_put(e.name, T.toJSON(store.state.tree));
      store.setLayouts(saved.layouts, saved.active);
      text = `saved layout ${e.name}`;
    } else if (e.op === "load") {
      store.setLayouts(store.state.layouts, e.name);
      store.setTree(T.fromJSON(e.tree));
      text = `layout ${e.name}`;
    } else {
      store.setLayouts(e.layouts, e.active);
      const names = (e.layouts || []).map((l) => (l.slot ? `${l.slot}:${l.name}` : l.name)).join(", ");
      const builtins = (e.builtins || []).length ? ` · built in: ${e.builtins.join(", ")}` : "";
      text = e.op === "rm" ? `removed layout ${e.name}` : `${names || "no saved layouts"}${builtins}`;
    }
    store.log("result", text, { level: "ok" });
    return { message: text, level: "ok" };
  }

  function garden(e) {
    bootWorkspace(e.boot);
    const text = e.message || `garden ${e.boot.garden}`;
    store.log("result", text, { level: "ok" });
    return { message: text, level: "ok" };
  }

  function undone(kind, label) {
    const text = label ? `${kind === "undo" ? "undone" : "redone"}: ${label}` : `nothing to ${kind}`;
    store.log("result", text, { level: label ? "ok" : "idle" });
    if (label) store.emit("refresh");
    return { message: text, level: "ok" };
  }

  function refresh(e) {
    store.log("result", e.message || "refreshed", { level: "ok" });
    store.emit("refresh");
    return { message: e.message || "", level: "ok" };
  }

  function simple(e) {
    const level = e.effect === "error" ? "fault" : "ok";
    const text = e.message || (level === "fault" ? "error" : "");
    store.log("result", text || e.effect, { level });
    return { message: text, level };
  }

  function openLayout(e, { newPane }) {
    actions.openLayout(e.tree, { newPane, focusCommand: e.focus || null });
    store.log("result", `layout ${e.name}`, { level: "ok" });
    return { message: "", level: "ok" };
  }

  function live(e) {
    store.setLive(e.live);
    store.log("result", e.message, { level: "ok" });
    return { message: e.message, level: "ok" };
  }

  function playhead(e) {
    const p = store.state.playhead;
    if (!p.count) return simple({ effect: "error", message: "no run to move through -- open a sim pane" });
    if (e.op === "step") {
      store.setPlaying(false);
      store.step(e.n, e.mode);
    } else {
      store.setPlaying(e.op === "play");
    }
    const text = `tick ${store.state.playhead.tick}/${p.count - 1}`;
    return { message: text, level: "ok" };
  }

  async function paneAction(e) {
    const instance = panes && panes.instanceFor(e.view);
    if (!instance || typeof instance.action !== "function") {
      return simple({ effect: "error", message: `no ${e.view} pane to ${e.action} -- open one first` });
    }
    const outcome = (await instance.action(e)) || { message: "", level: "ok" };
    // A pane that ran its own line (the full `plot save …`) was logged by it.
    if (!outcome.logged) store.log("result", outcome.message || e.action, { level: outcome.level === "fault" ? "fault" : "ok" });
    return { message: outcome.message, level: outcome.level };
  }

  const HANDLERS = {
    pane_action: paneAction,
    open_layout: openLayout,
    live,
    playhead,
    statement,
    open_view: openView,
    layout,
    garden,
    undo: (e) => undone("undo", e.label),
    redo: (e) => undone("redo", e.label),
    refresh,
    error: simple,
  };

  /** Run one command line. Resolves to the command line's outcome. */
  async function run(line, { newPane = false, confirm = false, replaceHistory = false } = {}) {
    await autosave.flush();
    if (!confirm) store.addHistory(line, { replace: replaceHistory });
    store.log("command", line);
    let effect;
    try {
      effect = await api.run(line, store.state.selection, confirm, replaceHistory);
    } catch (err) {
      effect = { effect: "error", message: message(err) };
    }
    const handler = HANDLERS[effect && effect.effect];
    if (!handler) return simple({ effect: "error", message: `unknown effect ${JSON.stringify(effect)}` });
    try {
      return await handler(effect, { line, newPane });
    } catch (err) {
      return simple({ effect: "error", message: message(err) });
    }
  }

  /** Ctrl+Z / Ctrl+Shift+Z: the same as typing `undo` / `redo`, minus the history entry. */
  async function undo(kind) {
    try {
      const result = await (kind === "undo" ? api.undo() : api.redo());
      return undone(kind, result && result.label);
    } catch (err) {
      return simple({ effect: "error", message: message(err) });
    }
  }

  return { run, undo };
}

/** Debounced working_put of the current tree on every tree change. */
export function createAutosave({ store, api, delay = 500 }) {
  let timer = 0;
  let pending = false;

  async function flush() {
    if (!pending) return;
    pending = false;
    clearTimeout(timer);
    try {
      await api.working_put(T.toJSON(store.state.tree));
    } catch (err) {
      store.log("result", `autosaving the layout failed: ${message(err)}`, { level: "fault" });
    }
  }

  store.subscribe("layout", (what) => {
    if (!what || !what.tree) return;
    pending = true;
    clearTimeout(timer);
    timer = setTimeout(flush, delay);
  });
  return { flush };
}
