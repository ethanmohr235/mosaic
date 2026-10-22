/* The key-hint bar: whatever keys mean right now. keys.js decides the
 * list (the ^w prefix, the command line and its popups, or the focused
 * view's hints followed by the shell's); this draws it as key glyphs in
 * --bright followed by --dim labels. */

export function createHintbar({ root, store, keys, components }) {
  const { h } = components;

  // Typing emits "hints" on every keystroke; the bar is redrawn only when
  // what it says changed.
  let shown = null;

  function render() {
    const hints = keys.hints();
    const said = JSON.stringify(hints);
    if (said === shown) return;
    shown = said;
    root.replaceChildren(...hints.map(([key, label]) =>
      h("span", "hint", [h("span", "hint-key", key), ` ${label}`])));
  }

  ["hints", "layout", "panes", "garden"].forEach((event) => store.subscribe(event, render));
  render();
  return { render };
}
