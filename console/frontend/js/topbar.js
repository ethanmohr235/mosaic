/* The top bar. Left: the `[mo]` box flush to the edge, the focused
 * pane's app suffix (`saic` for shell views), and `/ garden` in --dim.
 * Right: the saved layouts as `1:name` tabs (the active one inverted,
 * with `*` when the tree has drifted from it), then `unsaved layout` in
 * --warn, or `sel X +N · linked in N panes`, or the pane count. A tab
 * click switches layout, like its number key. */

export function createTopbar({ root, store, panes, actions, components }) {
  const { h } = components;

  function suffix() {
    const pane = panes.focused();
    return (pane && pane.view && pane.view.app) || "saic";
  }

  function tabs(dirty) {
    return store.state.layouts.map((layout, i) => {
      const active = layout.name === store.state.active;
      const tab = h("span", `tab${active ? " active" : ""}`, `${i + 1}:${layout.name}${active && dirty ? "*" : ""}`);
      tab.addEventListener("click", () => actions.switchLayout(i + 1));
      return tab;
    });
  }

  function summary(dirty) {
    if (dirty) return h("span", "lvl-warn", "unsaved layout");
    const sel = store.state.selection;
    const count = panes.count();
    if (!sel.length) return h("span", "dim", `${count} pane${count === 1 ? "" : "s"}`);
    const more = sel.length > 1 ? ` +${sel.length - 1}` : "";
    const linked = panes.linkedCount();
    return h("span", "dim", `sel ${store.label(sel[0])}${more} · linked in ${linked} pane${linked === 1 ? "" : "s"}`);
  }

  function render() {
    const dirty = store.dirty();
    root.replaceChildren(
      h("span", "mo-box", "mo"),
      h("span", "app-suffix", suffix()),
      h("span", "breadcrumb", `/ ${store.state.garden}`),
      h("span", "grow"),
      h("span", "tabs", tabs(dirty)),
      summary(dirty),
    );
  }

  ["layout", "garden", "selection", "panes"].forEach((event) => store.subscribe(event, render));
  render();
  return { render };
}
