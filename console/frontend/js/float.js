/* The `p` float: the props view (views/props.js) over any layout
 * that has no props pane (docs/console.md → *Inspector*). One component,
 * two hosts -- what you can do to a mote is the same in the pane and
 * here, because it is the same view instance code with the same ctx.
 *
 * `p` opens it on `open @sel`, or focuses the props pane when a layout
 * already has one; `p` again or `esc` closes it. While it is open it has
 * the keys ahead of the focused pane, and it re-runs on a selection
 * change, after a write and on refresh, like a linked pane. The popup
 * style: --bg-raised, 1px --line-strong, no shadow, no leader line. */

import moteProps from "./views/props.js";

const COMMAND = moteProps.commands[0].canonical;
const CLOSE_HINTS = [["esc p", "close"]];

export function createFloat({ store, api, components, panes, onRun }) {
  const { h } = components;
  const header = components.paneHeader();
  const body = h("div", "props-float-body");
  const el = h("div", "props-float", [header.el, body]);
  el.hidden = true;
  el.dataset.keepFocus = "";
  document.body.appendChild(el);
  let instance = null;
  let command = COMMAND;

  function ctx() {
    return {
      api,
      store,
      components,
      navigate: (id, label = null) => store.setSelection([id], label ? { [id]: label } : {}),
      // Walking from a pinned `open #id`: the float re-points itself.
      setCommand: (next) => {
        command = next;
        return rerun();
      },
      runCommand: (line) => onRun(line),
    };
  }

  async function rerun() {
    if (!instance) return;
    let result;
    try {
      result = await instance.run(command);
    } catch (err) {
      result = { summary: String(err && err.message ? err.message : err), level: "fault" };
    }
    header.update({ command, summary: (result && result.summary) || "", detail: (result && result.detail) || "", level: (result && result.level) || "idle",
      focused: true });
  }

  function open() {
    const paneId = panes.findView(moteProps.id);
    if (paneId) {
      store.focus(paneId);
      return;
    }
    if (instance) return;
    command = COMMAND;
    instance = moteProps.create(ctx());
    instance.mount(body, command);
    el.hidden = false;
    rerun();
    store.emit("hints");
  }

  function close() {
    if (!instance) return;
    try {
      instance.unmount();
    } catch (err) {
      console.error("closing the props float failed", err);
    }
    instance = null;
    body.replaceChildren();
    el.hidden = true;
    panes.focusDom();
    store.emit("hints");
  }

  ["selection", "write", "refresh"].forEach((event) => store.subscribe(event, () => {
    if (instance) rerun();
  }));

  return {
    isOpen: () => !!instance,
    toggle: () => (instance ? close() : open()),
    close,
    /** Keys while open: the view's own first; `esc` and `p` close it. */
    onKey(event) {
      if (!instance) return false;
      if (instance.onKey && instance.onKey(event)) return true;
      if (instance.textEditing) return false;
      if (!event.ctrlKey && !event.metaKey && !event.altKey && (event.key === "Escape" || event.key === "p")) {
        close();
        return true;
      }
      return false;
    },
    textEditing: () => !!(instance && instance.textEditing),
    // While its editor is open, `esc` cancels the edit, so it doesn't also say "close".
    hints: () => (instance ? [...(instance.hints ? instance.hints() : []),
      ...(instance.textEditing ? [] : CLOSE_HINTS)] : []),
  };
}
