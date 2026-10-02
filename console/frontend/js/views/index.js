/* The view registry, and the view contract every view follows.
 *
 * A view is one ES module whose default export is a *definition*:
 *
 *   {
 *     id:         "log",                 // view id, named for its verb (`table` answers `find`)
 *     describe:   "command and error log",   // one line, shown in completion
 *     commands:   [{verb: "log", canonical?: "log", complete?(line, cursor) -> rows}],
 *                                        // verbs this view answers; commands[0]'s
 *                                        // canonical line is the one it opens on
 *                                        // (the `p` float's `open @sel`)
 *     followsSel: false,                 // re-run on selection change even without @sel
 *     create(ctx) -> instance,           // one instance per pane showing this view
 *   }
 *
 * `ctx` is {api, store, components, navigate(id), setCommand(command),
 * runCommand(line), prefill(line), isFocused(), setSummary(s), paneAction(viewId, action)};
 * one per pane:
 *   api         the root bridge (js/bridge.js); its namespaces (api.moxie, …) keep
 *               the old app names, which the UI no longer shows
 *   store       js/store.js -- read state.selection / primary(), request
 *               changes with setSelection / toggleSelection, never copy them
 *   components  js/components.js -- table (markTable, swatch), kvGrid,
 *               popupList, paneHeader, h
 *   navigate(id, label?)  select mote `id` (the primary); every linked
 *               pane follows. `label` is what the top bar calls it.
 *   setCommand(command)  replace THIS pane's command in place, for a
 *               gesture that changes what the pane shows (a header-click
 *               sort). Same view instance, no remount, no history entry;
 *               the layout is marked dirty and autosaved, and run() is
 *               called with the new command. Resolves once it has run.
 *               The command must still be one this view answers.
 *   runCommand(line, {replaceHistory}?)  run `line` as if typed at the command line (with
 *               the current selection for @sel): it enters history and
 *               the log, its effect is applied (a write re-runs every
 *               pane), and its message shows in the command line's
 *               status. Resolves to {message, level}. How a view writes:
 *               it never calls a write method on its namespace.
 *               {confirm: true} runs a line that would otherwise ask for a
 *               second ⏎ (`delete`, `garden rm`) as already confirmed: a
 *               view's `d d` is its confirmation.
 *   prefill(line, caret?)  put `line` on the command line for the user to
 *               finish (the scenarios pane's `a`: `set --scenario hot `),
 *               the caret at `caret` (default: the end).
 *   isFocused()  whether this pane is the focused one.
 *   setSummary({summary, level})  update the header between runs (an
 *               editor that saved, or found a problem, while typing).
 *   paneAction(viewId, action)  hand `action` to the pane showing
 *               `viewId` (focused first), as a `pane_action` effect does:
 *               its instance.action(action). Null when there is none.
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
 *   hints()               [[key, label], ...] for the key-hint bar while focused;
 *                         store.emit("hints") when they change (entering an editor)
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
import table from "./table.js";
import props from "./props.js";
import plot from "./plot.js";
import fit from "./fit.js";
import sim from "./sim.js";
import rule from "./rule.js";
import scenarios from "./scenarios.js";
import edit from "./edit.js";
import read from "./read.js";
import notes from "./notes.js";
import worksheet from "./worksheet.js";
import agenda from "./agenda.js";
import week from "./week.js";
import month from "./month.js";
import graph from "./graph.js";
import gardens from "./gardens.js";
import garden from "./garden.js";

export const VIEWS = [log, table, props, plot, fit, sim, rule, scenarios, edit, read, notes, worksheet,
  agenda, week, month, graph, gardens, garden];

export function viewById(id) {
  return VIEWS.find((view) => view.id === id) || null;
}
