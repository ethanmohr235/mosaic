/* The key-hint bar: whatever keys mean right now. keys.js decides the
 * list (the ^w prefix, the command line and its popups, or the focused
 * view's hints followed by the shell's); this draws it as key glyphs in
 * --bright followed by --dim labels. */

export function createHintbar({ root, store, keys, components }) {
  const { h } = components;

  function render() {
    root.replaceChildren(...keys.hints().map(([key, label]) =>
      h("span", "hint", [h("span", "hint-key", key), ` ${label}`])));
  }

  ["hints", "layout", "panes", "garden"].forEach((event) => store.subscribe(event, render));
  render();
  return { render };
}
