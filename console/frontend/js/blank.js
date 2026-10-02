/* The empty pane: what a pane whose command is "" shows -- a fresh split,
 * or the last pane after everything else was closed. It asks for an
 * ordinary command: when it first takes focus the caret goes to the
 * command line, and since it is the focused pane, ⏎ there runs the line
 * into it. It follows the same instance shape as a view (views/index.js)
 * so panes.js mounts, focuses and routes keys to it the same way, but it
 * isn't registered.
 *
 *   esc (here, or on the command line) closes it -- cancels the split.
 *   ⏎ or typing goes to the command line. */

const HINTS = [["⏎", "type a command"], ["esc", "close pane"]];

/** {h} from components; onPrompt(text?) focuses the command line; onCancel(). */
export function createBlank({ components, onPrompt, onCancel }) {
  const { h } = components;
  let prompted = false;

  return {
    blank: true,
    mount(el) {
      el.appendChild(h("div", "blank", [h("span", "blank-prompt", "$"),
        h("span", "blank-hint", "type a command -- it runs here · esc closes this pane")]));
    },
    run: () => ({ summary: "", level: "idle" }),
    unmount() {},
    hints: () => HINTS,
    focus() {
      if (prompted) return;
      prompted = true;
      onPrompt();
    },
    onKey(event) {
      if (event.ctrlKey || event.metaKey || event.altKey) return false;
      if (event.key === "Escape") onCancel();
      else if (event.key === "Enter") onPrompt();
      // Digits stay the shell's: 1–9 switch layouts.
      else if (event.key.length === 1 && !/[1-9]/.test(event.key)) onPrompt(event.key);
      else return false;
      return true;
    },
  };
}
