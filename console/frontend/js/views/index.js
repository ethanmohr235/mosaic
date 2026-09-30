/* The view registry, and the view contract every view follows.
 *
 * A view is one ES module whose default export is a *definition*:
 *
 *   {
 *     id:         "log",                 // view id: picker, completion, pane header
 *     app:        null | "nolith" | ...  // the app suffix it belongs to (null = the shell)
 *     describe:   "command and error log",   // one line, shown in the picker
 *     commands:   [{verb: "log", complete?(line, cursor) -> rows}],
 *                                        // verbs this view answers; commands[0].verb
 *                                        // is the canonical command the picker places
 *     followsSel: false,                 // re-run on selection change even without @sel
 *     create(ctx) -> instance,           // one instance per pane showing this view
 *   }
 *
 * `ctx` is {api, store, components, navigate(id)}:
 *   api         the root bridge (js/bridge.js); an app's namespace is api.<app>
 *   store       js/store.js -- read state.selection / primary(), request
 *               changes with setSelection / toggleSelection, never copy them
 *   components  js/components.js -- table, kvGrid, popupList, paneHeader, h
 *   navigate(id)  select mote `id` (the primary); every linked pane follows
 *
 * The instance:
 *
 *   mount(el, command)    render into the pane body `el`
 *   run(command)          (re-)run in place: on ⏎, a selection change (when
 *                         the command has @sel or followsSel), after any
 *                         write, on window focus and on `refresh`. Returns
 *                         (or resolves to) {summary, level} for the header,
 *                         level being "ok" | "warn" | "fault" | "idle".
 *   unmount()             remove every listener, timer and animation frame
 *                         the instance added. Leaks are bugs.
 *   hints()               [[key, label], ...] for the key-hint bar while focused
 *   onKey(event)          the view's own keys; return true when handled so
 *                         the shell doesn't also act on it
 *   textEditing?          true while the view is capturing typing: the shell
 *                         then leaves every key but the ^w prefix to it
 *   focus?()              optional: put DOM focus where typing should go
 *
 * Views talk to each other only through the selection and the command
 * line, never directly. Anything that must survive a restart has to be
 * expressible in the command string (docs/console.md → *Panes are commands*).
 *
 * Register a new view by importing it below and adding it to VIEWS. */

import log from "./log.js";

export const VIEWS = [log];

export function viewById(id) {
  return VIEWS.find((view) => view.id === id) || null;
}

/** The command the picker places for a view: its first verb, else its id. */
export function canonicalCommand(view) {
  return (view.commands && view.commands[0] && view.commands[0].verb) || view.id;
}
