/* The empty pane: a `view>` prompt with a filter over the registered
 * views. It is what a pane whose command is "" shows -- a fresh split,
 * or the last pane after everything else was closed. It follows the
 * same instance shape as a view (views/index.js) so panes.js mounts,
 * focuses and routes keys to it the same way, but it isn't registered:
 * you can't pick the picker.
 *
 *   ⏎ places the highlighted view, esc cancels the split (closes the pane). */

import { VIEWS } from "./views/index.js";

const HINTS = [["⏎", "place view"], ["↑↓", "choose"], ["esc", "cancel split"]];

function matches(filter) {
  const needle = filter.trim().toLowerCase();
  return VIEWS.filter((v) => !needle || v.id.toLowerCase().includes(needle)
    || (v.describe || "").toLowerCase().includes(needle));
}

/** {h, promptInput} from components; onPlace(view); onCancel(). */
export function createPicker({ components, onPlace, onCancel }) {
  const { h } = components;
  let field = null;
  let list = null;
  let highlighted = 0;

  function shown() {
    return field ? matches(field.value()) : VIEWS;
  }

  function render() {
    if (!list) return;
    const views = shown();
    highlighted = Math.min(highlighted, Math.max(views.length - 1, 0));
    list.replaceChildren(...views.map((view, i) => {
      const row = h("div", `picker-row${i === highlighted ? " highlighted" : ""}`,
        [h("span", "", view.id), h("span", "picker-describe", view.describe || "")]);
      row.dataset.view = view.id;
      row.addEventListener("mousedown", (e) => e.preventDefault());
      row.addEventListener("click", () => onPlace(view));
      return row;
    }));
    if (!views.length) list.appendChild(h("div", "picker-empty", "no view matches"));
  }

  function onInput() {
    highlighted = 0;
    render();
  }

  return {
    textEditing: true,
    mount(el) {
      field = components.promptInput();
      field.input.addEventListener("input", onInput);
      list = h("div", "picker-list");
      el.appendChild(h("div", "picker", [
        h("div", "picker-prompt", [h("span", "picker-label", "view>"), field.el]), list]));
      render();
    },
    run: () => ({ summary: "", level: "idle" }),
    unmount() {
      if (field) {
        field.input.removeEventListener("input", onInput);
        field.destroy();
      }
      field = null;
      list = null;
    },
    hints: () => HINTS,
    focus() {
      if (field && document.activeElement !== field.input) field.input.focus();
    },
    onKey(event) {
      const views = shown();
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        const step = event.key === "ArrowDown" ? 1 : -1;
        highlighted = (highlighted + step + views.length) % Math.max(views.length, 1);
        render();
        return true;
      }
      if (event.key === "Enter") {
        if (views[highlighted]) onPlace(views[highlighted]);
        return true;
      }
      if (event.key === "Escape") {
        onCancel();
        return true;
      }
      return false;
    },
  };
}
