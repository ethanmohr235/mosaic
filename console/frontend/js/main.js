/* Boot: wait for the bridge, wire the shell's modules together, and put
 * the garden's workspace on screen.
 *
 * On boot (and after every `cd`) the tree comes from the garden's
 * autosaved working tree, else its active saved layout, else a single
 * `log` pane -- so a first launch shows something that reacts to the
 * first command typed. */

import { ready } from "./bridge.js";
import * as components from "./components.js";
import { createStore } from "./store.js";
import * as T from "./tree.js";
import { createPanes } from "./panes.js";
import { createActions } from "./actions.js";
import { createRunner, createAutosave } from "./effects.js";
import { createCommandLine } from "./commandline.js";
import { createKeys } from "./keys.js";
import { createTopbar } from "./topbar.js";
import { createHintbar } from "./hintbar.js";
import { createFloat } from "./float.js";
import { createPlayer } from "./playhead.js";
import { createTooltip } from "./tooltip.js";

const FIRST_PANE = "log";

function initialTree(boot) {
  if (boot.working) return T.fromJSON(boot.working);
  const active = (boot.layouts || []).find((l) => l.name === boot.active);
  if (active) return T.fromJSON(active.tree);
  return T.leaf(FIRST_PANE);
}

function wire(api) {
  const $ = (id) => document.getElementById(id);
  const store = createStore();
  const shell = {};

  shell.panes = createPanes({
    root: $("workspace"), store, api, components,
    onEdit: (id) => {
      store.focus(id);
      shell.keys.editFocused();
    },
    onPrompt: (text) => shell.commandLine.focus(text),
    onCancel: (id) => shell.actions.close(id),
    onRun: (line, options) => shell.runLine(line, options),
    onPrefill: (line, caret) => shell.commandLine.load(line, caret),
    isCommandLineActive: () => !!shell.commandLine && shell.commandLine.isActive(),
  });
  shell.actions = createActions({ store, panes: shell.panes, api });
  const autosave = createAutosave({ store, api });

  const bootWorkspace = (boot) => {
    store.applyBoot(boot);
    const tree = initialTree(boot);
    store.setTree(tree, T.firstLeaf(tree).id);
  };
  const runner = createRunner({ store, api, actions: shell.actions, panes: shell.panes, autosave, bootWorkspace });

  // ctx.runCommand: a view's own line (an inline edit's `set …`) runs
  // exactly as if typed (history, log, effects) and reports on the
  // command line's status, the way ^z does.
  shell.runLine = async (line, options = {}) => {
    const outcome = await runner.run(line, options);
    shell.commandLine.setStatus(outcome.message || "", outcome.level === "fault" ? "fault" : "idle");
    return outcome;
  };

  shell.commandLine = createCommandLine({
    root: $("cmdline"), popupEl: $("popup"), store, api, components,
    onSubmit: (line, options) => runner.run(line, options),
    onLeave: ({ cancelled }) => {
      // esc out of a blank pane's prompt cancels the split it came from.
      const pane = shell.panes.focused();
      if (cancelled && pane && pane.instance && pane.instance.blank && shell.panes.count() > 1) {
        shell.actions.close(pane.id);
      } else shell.panes.focusDom();
    },
  });
  shell.float = createFloat({ store, api, components, panes: shell.panes,
    onRun: (line) => shell.runLine(line) });
  shell.keys = createKeys({ store, panes: shell.panes, actions: shell.actions,
    commandLine: shell.commandLine, runner, float: shell.float });
  createTopbar({ root: $("topbar"), store, panes: shell.panes, actions: shell.actions, components,
    onRun: (line) => shell.runLine(line) });
  createPlayer({ store });
  createTooltip();
  createHintbar({ root: $("hintbar"), store, keys: shell.keys, components });

  // Writes from outside the window (MCP, CLI, sync) show up when it
  // regains focus, as Monolith does.
  window.addEventListener("focus", () => store.emit("refresh"));
  window.addEventListener("pagehide", () => autosave.flush());
  return { store, bootWorkspace };
}

function showFatal(err) {
  console.error("the console failed to start", err);
  const pane = document.createElement("div");
  pane.className = "pane-error lvl-fault";
  pane.textContent = `the console failed to start: ${err && err.message ? err.message : err}`;
  document.getElementById("workspace").replaceChildren(pane);
}

async function main() {
  try {
    const api = await ready();
    const { store, bootWorkspace } = wire(api);
    bootWorkspace(await api.boot());
    store.log("result", `garden ${store.state.garden}`, { level: "ok" });
  } catch (err) {
    showFatal(err);
  }
}

main();
