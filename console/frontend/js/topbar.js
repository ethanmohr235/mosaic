/* The top bar. Left: the `[mo]saic` wordmark flush to the edge (the
 * same whatever is focused), and the garden's ownership path in --dim
 * (`/ college-fall-2026 / hw4`); a crumb above the open garden `cd`s
 * there (docs/console.md → Settled for U9).
 * Right: the slotted layouts as `1:name` tabs (the active one inverted,
 * with `*` when the tree has drifted from it), the playhead (`tick 12/49
 * ▶`) and `live` when a sim pane has them, then `unsaved layout` in
 * --warn, and `sel X +N · linked in N panes` or the pane count. A tab
 * click switches layout, like its number key. */

export function createTopbar({ root, store, panes, actions, components, onRun = () => {} }) {
  const { h } = components;

  /** One tab per slotted layout, in slot order; `layout load` reaches the rest. */
  function tabs(dirty) {
    const slotted = store.state.layouts.filter((l) => l.slot).sort((a, b) => a.slot - b.slot);
    return slotted.map((layout) => {
      const active = layout.name === store.state.active;
      const tab = h("span", `tab${active ? " active" : ""}`, `${layout.slot}:${layout.name}${active && dirty ? "*" : ""}`);
      tab.addEventListener("click", () => actions.switchLayout(layout.slot));
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
   * every edit is solved (docs/console.md → Settled for U5); `archived ·
   * read-only` while the open garden is archived. */
  function motion() {
    const parts = [];
    const p = store.state.playhead;
    if (p.count) parts.push(h("span", "playhead", `tick ${p.tick}/${p.count - 1} ${p.playing ? "▶" : "‖"}`));
    if (store.state.live) parts.push(h("span", "live lvl-ok", "live"));
    if (store.state.archived) {
      const tag = h("span", "lvl-warn", "archived · read-only");
      tag.title = store.state.archived;
      parts.push(tag);
    }
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
      h("span", "wordmark", "saic"),
      breadcrumb(),
      h("span", "grow"),
      h("span", "tabs", tabs(dirty)),
      ...motion(),
      ...summary(dirty),
    );
  }

  ["layout", "garden", "selection", "panes", "playhead", "live", "archived"].forEach((event) => store.subscribe(event, render));
  render();
  return { render };
}
