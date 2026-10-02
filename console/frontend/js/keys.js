/* Keyboard routing, pane-first (docs/console.md → *Keyboard and mouse*).
 *
 * One capture-phase keydown listener on window decides who gets a key:
 *   1. `^w` and the key after it are always the shell's -- the tiling
 *      prefix works everywhere, text fields and the command line included.
 *   2. The command line handles its own keys while it has focus.
 *   3. Ctrl+Z / Ctrl+Shift+Z (or Ctrl+Y) undo and redo, unless a text
 *      field has focus (it keeps its own native undo).
 *   4. The focused view's onKey() goes next; true means it's handled.
 *   5. A text-editing view (or any text field) keeps every other key.
 *      (While the `p` float is open, steps 4 and 5 are the float's: it has
 *      the keys ahead of the focused pane.)
 *   6. Otherwise the shell: `:` or `/` to the command line, `e` edit the
 *      pane's command, `x` close the pane, `p` the props float, `1`–`9`
 *      switch layouts.
 *
 * After `^w`: v s split · h j k l focus · < > resize · o only · = equalize
 * · x swap. `esc` or any other key cancels the prefix. */

const TILING_HINTS = [["^w v", "vsplit"], ["^w s", "split"], ["^w hjkl", "focus"], ["^w < >", "resize"],
  ["^w o", "only"], ["^w =", "equalize"], ["^w x", "swap"]];
const SHELL_HINTS = [[":", "command"], ["e", "edit pane command"], ["x", "close pane"], ["p", "props"],
  ["^w", "tile"], ["1–9", "layouts"]];
const MODIFIERS = new Set(["Shift", "Control", "Alt", "Meta", "AltGraph", "CapsLock"]);

function isTextField(el) {
  if (!(el instanceof Element)) return false;
  const tag = el.tagName.toLowerCase();
  return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
}

export function createKeys({ store, panes, actions, commandLine, runner, float }) {
  let prefix = false;

  function tiling(key) {
    const moves = { h: "h", j: "j", k: "k", l: "l", ArrowLeft: "h", ArrowDown: "j", ArrowUp: "k", ArrowRight: "l" };
    commandLine.blur();
    if (key === "v") actions.split("row");
    else if (key === "s") actions.split("col");
    else if (moves[key]) actions.focusDir(moves[key]);
    else if (key === "<") actions.resize(-0.05);
    else if (key === ">") actions.resize(0.05);
    else if (key === "o") actions.only();
    else if (key === "=") actions.equalize();
    else if (key === "x") actions.swap();
    panes.focusDom();
  }

  function shell(event) {
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    const key = event.key;
    if (key === ":" || key === "/") commandLine.focus();
    else if (key === "e") editFocused();
    else if (key === "x") actions.close();
    else if (key === "p") float.toggle();
    else if (/^[1-9]$/.test(key)) actions.switchLayout(Number(key));
    else return false;
    return true;
  }

  function editFocused() {
    const pane = panes.focused();
    commandLine.load(pane ? pane.command || "" : "");
  }

  function undoKey(event) {
    if (!event.ctrlKey) return false;
    const key = event.key.toLowerCase();
    if (key === "z") runner.undo(event.shiftKey ? "redo" : "undo").then(report);
    else if (key === "y") runner.undo("redo").then(report);
    else return false;
    return true;
  }

  function report(outcome) {
    commandLine.setStatus(outcome.message, outcome.level === "fault" ? "fault" : "idle");
  }

  function consume(event) {
    event.preventDefault();
    event.stopPropagation();
  }

  function onKeyDown(event) {
    if (prefix) {
      if (MODIFIERS.has(event.key)) return;
      prefix = false;
      consume(event);
      if (event.key !== "Escape") tiling(event.key);
      store.emit("hints");
      return;
    }
    if (event.ctrlKey && !event.shiftKey && !event.altKey && event.key.toLowerCase() === "w") {
      prefix = true;
      consume(event);
      store.emit("hints");
      return;
    }
    if (commandLine.isActive()) return;
    const inField = isTextField(event.target);
    if (!inField && undoKey(event)) return consume(event);
    if (float.isOpen()) {
      if (float.onKey(event)) return consume(event);
      if (float.textEditing() || inField) return;
      if (shell(event)) consume(event);
      return;
    }
    const pane = panes.focused();
    const instance = pane && pane.instance;
    if (instance && instance.onKey && instance.onKey(event)) return consume(event);
    if ((instance && instance.textEditing) || inField) return;
    if (shell(event)) consume(event);
  }

  window.addEventListener("keydown", onKeyDown, true);

  return {
    pending: () => prefix,
    hints() {
      if (prefix) return TILING_HINTS;
      if (commandLine.isActive()) return commandLine.hints();
      if (float.isOpen()) {
        return float.textEditing() ? [...float.hints(), ["^w", "tile"]] : [...float.hints(), ...SHELL_HINTS];
      }
      const pane = panes.focused();
      const own = pane && pane.instance && pane.instance.hints ? pane.instance.hints() : [];
      // A text-editing view has every key but ^w, so the shell's don't apply.
      if (pane && pane.instance && pane.instance.textEditing) return [...own, ["^w", "tile"]];
      return [...own, ...SHELL_HINTS];
    },
    editFocused,
  };
}
