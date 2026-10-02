/* Workspace actions: what the ^w keys, the mouse and command effects do
 * to the tree. Each is a pure tree.js op applied to the store; panes.js
 * redraws from the store's `layout` event. Kept apart from keys.js so
 * the same action is one function whether it came from a key, a click
 * or a command. */

import * as T from "./tree.js";

/** A `newPane` option as a split direction: "col" (below) or "row" (right). */
const splitDir = (newPane) => (newPane === "col" ? "col" : "row");

export function createActions({ store, panes, api }) {
  const focusedId = () => store.state.focusedId;
  const tree = () => store.state.tree;

  function split(dir, command = "") {
    const result = T.split(tree(), focusedId(), dir, command);
    if (result.id) store.setTree(result.tree, result.id);
    return result.id;
  }

  function close(id = focusedId()) {
    const result = T.close(tree(), id);
    store.setTree(result.tree, result.focusId);
  }

  function resize(delta) {
    const result = T.resize(tree(), focusedId(), "row", delta);
    if (!result) return;
    store.setTree(result.tree);
    panes.flashGuide(result.path, result.index);
  }

  function focusDir(dir) {
    const id = T.focusDir(panes.rects(), focusedId(), dir);
    if (id) store.focus(id);
  }

  /** Give pane `id` (default: the focused one) a new command; it re-mounts. */
  function setCommand(command, id = focusedId()) {
    const current = T.findLeaf(tree(), id);
    if (current && current.command === command) {
      panes.rerun(id);
      return;
    }
    store.setTree(T.setCommand(tree(), id, command), id);
  }

  /** Switch to saved layout number `n` (1-based, save order). */
  async function switchLayout(n) {
    const saved = store.state.layouts[n - 1];
    if (!saved) return false;
    try {
      const layoutTree = (await api.layout_get(saved.name)) || saved.tree;
      await api.layout_set_active(saved.name);
      store.setLayouts(store.state.layouts, saved.name);
      store.setTree(T.fromJSON(layoutTree));
    } catch (err) {
      store.log("result", `layout ${saved.name}: ${err && err.message ? err.message : err}`, { level: "fault" });
      return false;
    }
    return true;
  }

  return {
    split,
    close,
    resize,
    focusDir,
    setCommand,
    switchLayout,
    only: () => store.setTree(T.only(tree(), focusedId())),
    equalize: () => store.setTree(T.equalize(tree())),
    swap: () => store.setTree(T.swap(tree(), focusedId())),
    /** ^⏎ / ⇧⏎: split the focused pane and run `command` in the new one,
     * below ("col") or to the right ("row"; also what `true` means). */
    openInNewPane: (command, dir = "row") => split(splitDir(dir), command),
    /** An app that opens several panes (`mo tion`): they take the focused
     * pane's place (^⏎ / ⇧⏎: a new split's), focus on `focusCommand`'s pane. */
    openLayout(json, { newPane = false, focusCommand = null } = {}) {
      // One tree change: a split drawn on its own would be a blank pane,
      // which takes the command line for a moment.
      const base = newPane ? T.split(tree(), focusedId(), splitDir(newPane), "") : { tree: tree(), id: focusedId() };
      if (!base.id) return;
      const result = T.replaceLeaf(base.tree, base.id, json, focusCommand);
      store.setTree(result.tree, result.focusId);
    },
  };
}
