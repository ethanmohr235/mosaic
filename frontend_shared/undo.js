/* Ctrl/Cmd+Z and Ctrl/Cmd+Shift+Z (or Ctrl+Y), wired to the generic
 * api.undo()/api.redo() every desktop app's Api now exposes
 * (api_common.py's UndoableApi). The stack itself lives in Python --
 * this is only the keyboard binding and the toast that reports what
 * happened.
 *
 * Host apps drive it with one call, same shape as Inspector's and
 * CommandBar's:
 *
 *   Undo.install({ api, toast, onChanged })   once, after the bridge is up
 *   Undo.undo()   Undo.redo()                 also callable directly, e.g.
 *                                              from a toast's own action
 *                                              button -- same effect as
 *                                              the keyboard shortcut.
 *
 * onChanged() is called after a successful undo or redo so the caller
 * re-renders whatever it's showing, the same as after any other mutating
 * call.
 *
 * Skipped while focus is in a text field (isTypingElsewhere, same check
 * commandbar.js uses for "/"): a field being actively typed into keeps
 * its own native undo, and mora's editor -- a textarea -- already has its
 * own Ctrl+Z stack for exactly this reason, so it falls out of scope here
 * without a special case.
 *
 * Kept in frontend_shared/ and symlinked into each app's frontend/, like
 * inspector.js and commandbar.js; see docs/architecture.md. */

const Undo = (() => {
  "use strict";

  let api = null;
  let toast = () => {};
  let onChanged = () => {};

  function isTypingElsewhere(target) {
    if (!(target instanceof Node)) return false;
    const tag = (target.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select"
      || (target.isContentEditable === true);
  }

  function install(options) {
    api = options.api;
    toast = options.toast || (() => {});
    onChanged = options.onChanged || (() => {});

    document.addEventListener("keydown", (event) => {
      if (!(event.ctrlKey || event.metaKey) || isTypingElsewhere(event.target)) return;

      const key = event.key.toLowerCase();
      if (key === "z" && !event.shiftKey) {
        event.preventDefault();
        runUndo();
      } else if ((key === "z" && event.shiftKey) || key === "y") {
        event.preventDefault();
        runRedo();
      }
    });
  }

  async function runUndo() {
    const answer = await api.undo();
    if (answer && answer.undone) {
      toast(`Undone: ${answer.undone}`);
      onChanged();
    } else {
      toast("Nothing to undo.");
    }
  }

  async function runRedo() {
    const answer = await api.redo();
    if (answer && answer.redone) {
      toast(`Redone: ${answer.redone}`);
      onChanged();
    } else {
      toast("Nothing to redo.");
    }
  }

  return { install, undo: runUndo, redo: runRedo };
})();
