/* The top bar. Left: the `[mo]` box flush to the edge, the focused
 * pane's app suffix (`saic` for shell views), and the garden's ownership
 * path in --dim (`/ college-fall-2026 / hw4`); a crumb above the open
 * garden `cd`s there (docs/console.md → Settled for U9).
 * Right: the saved layouts as `1:name` tabs (the active one inverted,
 * with `*` when the tree has drifted from it), the playhead (`tick 12/49
 * ▶`) and `live` when Motion has them, then `unsaved layout` in
 * --warn, and `sel X +N · linked in N panes` or the pane count. A tab
 * click switches layout, like its number key. */

export function createTopbar({ root, store, panes, actions, components, onRun = () => {} }) {
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

  /** `unsaved layout` and the selection side by side: a drifted layout
   * must not hide what is selected, since the next command acts on it. */
  function summary(dirty) {
    const parts = dirty ? [h("span", "lvl-warn", "unsaved layout")] : [];
    const sel = store.state.selection;
    const count = panes.count();
    if (!sel.length) {
      if (!dirty) parts.push(h("span", "dim", `${count} pane${count === 1 ? "" : "s"}`));
      return parts;
    }
    const more = sel.length > 1 ? ` +${sel.length - 1}` : "";
    const linked = panes.linkedCount();
    parts.push(h("span", "dim", `sel ${store.label(sel[0])}${more} · linked in ${linked} pane${linked === 1 ? "" : "s"}`));
    return parts;
  }

  /** `tick 12/49 ▶` while a sim pane owns the playhead, and `live` when
   * every edit is solved (docs/console.md → Settled for U5). */
  function motion() {
    const parts = [];
    const p = store.state.playhead;
    if (p.count) parts.push(h("span", "playhead", `tick ${p.tick}/${p.count - 1} ${p.playing ? "▶" : "‖"}`));
    if (store.state.live) parts.push(h("span", "live lvl-ok", "live"));
    return parts;
  }

  function breadcrumb() {
    const crumbs = store.state.crumbs && store.state.crumbs.length ? store.state.crumbs : [store.state.garden];
    const parts = [];
    crumbs.forEach((garden, i) => {
      parts.push(i ? " / " : "/ ");
      if (i === crumbs.length - 1) return parts.push(h("span", "crumb here", garden));
      const crumb = h("span", "crumb up", garden);
      crumb.title = `cd ${garden}`;
      crumb.addEventListener("click", () => onRun(`cd ${/^[\w.-]+$/.test(garden) ? garden : JSON.stringify(garden)}`));
      parts.push(crumb);
    });
    return h("span", "breadcrumb", parts);
  }

  function render() {
    const dirty = store.dirty();
    root.replaceChildren(
      h("span", "mo-box", "mo"),
      h("span", "app-suffix", suffix()),
      breadcrumb(),
      h("span", "grow"),
      h("span", "tabs", tabs(dirty)),
      ...motion(),
      ...summary(dirty),
    );
  }

  ["layout", "garden", "selection", "panes", "playhead", "live"].forEach((event) => store.subscribe(event, render));
  render();
  return { render };
}
