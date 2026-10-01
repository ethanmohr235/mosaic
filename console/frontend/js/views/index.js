/* The view registry, and the view contract every view follows.
 *
 * A view is one ES module whose default export is a *definition*:
 *
 *   {
 *     id:         "log",                 // view id: picker, completion, pane header
 *     app:        null | "nolith" | ...  // the app suffix it belongs to (null = the shell)
 *     describe:   "command and error log",   // one line, shown in the picker
 *     commands:   [{verb: "log", canonical?: "log", complete?(line, cursor) -> rows}],
 *                                        // verbs this view answers; commands[0]'s
 *                                        // canonical line (else its verb) is what
 *                                        // the picker places
 *     followsSel: false,                 // re-run on selection change even without @sel
 *     create(ctx) -> instance,           // one instance per pane showing this view
 *   }
 *
 * `ctx` is {api, store, components, navigate(id), setCommand(command),
 * runCommand(line), prefill(line), isFocused(), setSummary(s), paneAction(viewId, action)};
 * one per pane:
 *   api         the root bridge (js/bridge.js); an app's namespace is api.<app>
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
import monolithTable from "./monolith-table.js";
import moteProps from "./mote-props.js";
import moxiePlot from "./moxie-plot.js";
import moxieFit from "./moxie-fit.js";
import motionState from "./motion-state.js";
import motionRule from "./motion-rule.js";
import motionScenarios from "./motion-scenarios.js";
import moraEdit from "./mora-edit.js";
import moraRead from "./mora-read.js";
import moraNotes from "./mora-notes.js";
import moraWorksheet from "./mora-worksheet.js";
import monasteryAgenda from "./monastery-agenda.js";
import monasteryWeek from "./monastery-week.js";
import monasteryMonth from "./monastery-month.js";
import monarchGraph from "./monarch-graph.js";
import mosaicGardens from "./mosaic-gardens.js";
import mosaicGarden from "./mosaic-garden.js";

export const VIEWS = [log, monolithTable, moteProps, moxiePlot, moxieFit, motionState, motionRule,
  motionScenarios, moraEdit, moraRead, moraNotes, moraWorksheet, monasteryAgenda, monasteryWeek, monasteryMonth,
  monarchGraph, mosaicGardens, mosaicGarden];

export function viewById(id) {
  return VIEWS.find((view) => view.id === id) || null;
}

/** The command the picker places for a view: its first command's
 * `canonical` line (`open @sel`), else that command's verb, else its id. */
export function canonicalCommand(view) {
  const first = view.commands && view.commands[0];
  return (first && (first.canonical || first.verb)) || view.id;
}
