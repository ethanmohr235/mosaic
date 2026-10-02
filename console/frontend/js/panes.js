/* The workspace: draws the store's split tree as tiled panes and owns
 * each pane's view instance.
 *
 * A pane is a command plus its rendered output (docs/console.md →
 * *Panes are commands*). Its view is found with api.resolve(command),
 * created, mounted into the pane body, then run; the header shows
 * `$ command` and the summary/level run() returned. A pane whose
 * command is "" is blank (blank.js) and asks for a command.
 *
 * Re-run rules: panes whose command has @sel, or whose view followsSel,
 * re-run on a selection change; every pane re-runs after a write and on
 * `refresh` (window focus, the verb, undo/redo).
 *
 * Panes are keyed by the tree leaf's id, so re-rendering the tree after
 * a split, a swap or a resize moves the existing pane elements rather
 * than re-mounting their views. */

import * as T from "./tree.js";
import { viewById } from "./views/index.js";
import { createBlank } from "./blank.js";

const GUIDE_LINGER_MS = 1000;

export function createPanes({ root, store, api, components, onEdit, onPrompt, onCancel, onRun,
  onPrefill, isCommandLineActive }) {
  const { h } = components;
  const panes = new Map();
  const splitEls = new Map();
  const guide = createGuide(h);
  let guideTimer = 0;

  /** The ctx a view instance gets (views/index.js): shared, plus two per-pane calls. */
  function ctxFor(pane) {
    return {
      api,
      store,
      components,
      navigate: (id, label = null) => store.setSelection([id], label ? { [id]: label } : {}),
      setCommand: (command) => setPaneCommand(pane, command),
      runCommand: (line, options) => onRun(line, options),
      prefill: (line, caret = null) => onPrefill(line, caret),
      setSummary: ({ summary = "", level = "idle" } = {}) => {
        if (panes.get(pane.id) !== pane) return;
        Object.assign(pane, { summary, level });
        updateHeader(pane);
      },
      isFocused: () => store.state.focusedId === pane.id,
      paneAction: (viewId, action) => {
        const instance = instanceFor(viewId);
        return instance && typeof instance.action === "function" ? instance.action(action) : null;
      },
    };
  }

  /* ---------- pane records ---------- */

  function createPane(id) {
    const header = components.paneHeader({ onClick: () => onEdit(id) });
    const body = h("div", "pane-body");
    const el = h("section", "pane", [header.el, body]);
    el.dataset.pane = id;
    el.addEventListener("mousedown", (event) => {
      if (event.button !== 1) store.focus(id);
    });
    const pane = { id, el, header, body, instance: null, view: null, command: null,
      runCommand: "", summary: "", level: "idle", token: 0 };
    panes.set(id, pane);
    return pane;
  }

  function updateHeader(pane) {
    pane.header.update({
      command: pane.command,
      label: pane.command ? "" : "new pane",
      summary: pane.summary,
      level: pane.level,
      focused: pane.id === store.state.focusedId,
    });
    pane.el.classList.toggle("focused", pane.id === store.state.focusedId);
  }

  function teardown(pane) {
    if (pane.instance) {
      try {
        pane.instance.unmount();
      } catch (err) {
        console.error(`unmounting ${pane.view ? pane.view.id : "blank pane"} failed`, err);
      }
    }
    Object.assign(pane, { instance: null, view: null, summary: "", level: "idle" });
    pane.body.replaceChildren();
  }

  function showError(pane, message) {
    teardown(pane);
    pane.body.appendChild(h("div", "pane-error lvl-fault", message));
    Object.assign(pane, { summary: message, level: "fault" });
    updateHeader(pane);
    store.emit("panes");
  }

  /* ---------- mounting ---------- */

  async function setContent(pane, command) {
    pane.command = command;
    const token = ++pane.token;
    teardown(pane);
    updateHeader(pane);
    if (!command) {
      mountInstance(pane, blankFor(pane), null, "");
      return;
    }
    let resolved;
    try {
      resolved = await api.resolve(command);
    } catch (err) {
      resolved = { error: String(err && err.message ? err.message : err) };
    }
    if (token !== pane.token) return;
    if (!resolved || resolved.error) return showError(pane, (resolved && resolved.error) || "can't resolve");
    const view = viewById(resolved.view);
    if (!view) return showError(pane, `no view named ${resolved.view} in this window`);
    if (mountInstance(pane, view.create(ctxFor(pane)), view, resolved.command || command)) await runPane(pane);
  }

  /** ctx.setCommand: the view rewrote its own command (a header-click
   * sort, say). Swap it in place -- same view instance, no remount, no
   * history entry -- mark the layout (dirty, autosaved) and re-run. */
  function setPaneCommand(pane, command) {
    if (panes.get(pane.id) !== pane || !pane.instance || !command) return Promise.resolve();
    pane.command = command;
    pane.runCommand = command;
    store.setTree(T.setCommand(store.state.tree, pane.id, command));
    return runPane(pane);
  }

  function blankFor(pane) {
    return createBlank({
      components,
      onPrompt: (text = null) => onPrompt(text),
      onCancel: () => onCancel(pane.id),
    });
  }

  function mountInstance(pane, instance, view, command) {
    Object.assign(pane, { instance, view, runCommand: command });
    try {
      instance.mount(pane.body, command);
    } catch (err) {
      showError(pane, `mount failed: ${err.message || err}`);
      return false;
    }
    updateHeader(pane);
    store.emit("panes");
    if (pane.id === store.state.focusedId) focusDom();
    return true;
  }

  async function runPane(pane) {
    if (!pane.instance || !pane.view) return;
    const token = pane.token;
    let result;
    try {
      result = await pane.instance.run(pane.runCommand);
    } catch (err) {
      result = { summary: String(err && err.message ? err.message : err), level: "fault" };
    }
    if (token !== pane.token) return;
    pane.summary = (result && result.summary) || "";
    pane.level = (result && result.level) || "idle";
    updateHeader(pane);
    store.emit("panes");
  }

  /* ---------- tree → DOM ---------- */

  function render() {
    splitEls.clear();
    const seen = new Set();
    const top = build(store.state.tree, [], seen);
    top.style.flex = "1 1 0px";
    root.replaceChildren(top, guide.el);
    for (const [id, pane] of panes) {
      if (seen.has(id)) continue;
      pane.token += 1;
      teardown(pane);
      panes.delete(id);
    }
    panes.forEach(updateHeader);
    focusDom();
  }

  function build(node, path, seen) {
    if (T.isLeaf(node)) return placeLeaf(node, seen);
    const box = h("div", `split ${node.split}`);
    splitEls.set(path.join("."), box);
    node.children.forEach((child, i) => {
      if (i > 0) box.appendChild(divider(node.split, path, i - 1));
      const el = build(child, [...path, i], seen);
      el.style.flex = `${node.sizes[i]} 1 0px`;
      box.appendChild(el);
    });
    return box;
  }

  function placeLeaf(node, seen) {
    seen.add(node.id);
    const pane = panes.get(node.id) || createPane(node.id);
    if (pane.command !== node.command) setContent(pane, node.command);
    return pane.el;
  }

  /* ---------- dividers ---------- */

  function divider(dir, path, index) {
    const el = h("div", `divider ${dir}`);
    el.dataset.path = path.join(".");
    el.dataset.index = String(index);
    el.addEventListener("mousedown", (event) => startDrag(event, dir, path, index));
    return el;
  }

  function startDrag(event, dir, path, index) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const box = splitEls.get(path.join("."));
    const a = box.children[index * 2];
    const b = box.children[index * 2 + 2];
    const node = T.nodeAt(store.state.tree, path);
    const room = node.sizes[index] + node.sizes[index + 1];
    const ra = a.getBoundingClientRect();
    const rb = b.getBoundingClientRect();
    const start = dir === "row" ? ra.left : ra.top;
    const span = dir === "row" ? ra.width + rb.width : ra.height + rb.height;
    let fraction = node.sizes[index] / room;
    document.body.classList.add(`dragging-${dir}`);

    const move = (e) => {
      const pos = (dir === "row" ? e.clientX : e.clientY) - start;
      fraction = Math.min(Math.max(pos / span, 0.05 / room), 1 - 0.05 / room);
      a.style.flexGrow = String(room * fraction);
      b.style.flexGrow = String(room * (1 - fraction));
      showGuide(path, index, room * fraction);
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      document.body.classList.remove(`dragging-${dir}`);
      hideGuide();
      store.setTree(T.moveDivider(store.state.tree, path, index, fraction));
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  /* ---------- the resize guide ---------- */

  /** Show `◀ ─── 50% ─── ▶` over the divider after child `index` of the split at `path`. */
  function showGuide(path, index, share = null) {
    const box = splitEls.get(path.join("."));
    if (!box) return;
    const node = T.nodeAt(store.state.tree, path);
    const pct = Math.round(100 * (share ?? node.sizes[index]));
    const a = box.children[index * 2].getBoundingClientRect();
    const b = box.children[index * 2 + 2].getBoundingClientRect();
    guide.place(root.getBoundingClientRect(), node.split, a, b, pct);
  }

  function hideGuide() {
    clearTimeout(guideTimer);
    guide.hide();
  }

  /** Keyboard resizes: show the guide, then hide it once the keys stop. */
  function flashGuide(path, index) {
    showGuide(path, index);
    clearTimeout(guideTimer);
    guideTimer = setTimeout(hideGuide, GUIDE_LINGER_MS);
  }

  /* ---------- focus ---------- */

  function focusDom() {
    if (isCommandLineActive()) return;
    const pane = panes.get(store.state.focusedId);
    if (pane && pane.instance && pane.instance.focus) {
      pane.instance.focus();
      return;
    }
    const active = document.activeElement;
    if (active && active !== document.body && root.contains(active)) active.blur();
  }

  /** Where a `pane_action` effect goes (`plot save ‹name›`, `plot
   * export …`) and ctx.paneAction: the focused pane when it shows
   * `viewId`, else the first pane that does. Null when none does. */
  function instanceFor(viewId) {
    const focused = panes.get(store.state.focusedId);
    if (focused && focused.view && focused.view.id === viewId) return focused.instance;
    for (const pane of panes.values()) if (pane.view && pane.view.id === viewId) return pane.instance;
    return null;
  }

  /* ---------- re-running ---------- */

  const isLinked = (pane) => !!pane.view
    && (pane.view.followsSel || (pane.runCommand || "").includes("@sel"));

  const runAll = () => Promise.all([...panes.values()].map(runPane));
  const runLinked = () => Promise.all([...panes.values()].filter(isLinked).map(runPane));

  store.subscribe("layout", (what) => {
    if (what && what.tree) render();
    else if (what && what.focus) {
      panes.forEach(updateHeader);
      focusDom();
      store.emit("panes");
    }
  });
  store.subscribe("selection", runLinked);
  store.subscribe("write", runAll);
  store.subscribe("refresh", runAll);

  return {
    render,
    runAll,
    rerun: (id) => (panes.has(id) ? runPane(panes.get(id)) : null),
    focusDom,
    flashGuide,
    hideGuide,
    focused: () => panes.get(store.state.focusedId) || null,
    /** The id of a pane showing view `viewId`, or null (the `p` float asks). */
    findView(viewId) {
      for (const [id, pane] of panes) if (pane.view && pane.view.id === viewId) return id;
      return null;
    },
    /** Where a `pane_action` effect goes (`plot save ‹name›`, `plot
     * export …`): the focused pane when it shows `viewId`, else the first
     * pane that does. Null when none does. */
    instanceFor,
    linkedCount: () => [...panes.values()].filter(isLinked).length,
    count: () => panes.size,
    /** Pane id → its on-screen rect, for ^w h/j/k/l. */
    rects() {
      const out = new Map();
      panes.forEach((pane, id) => out.set(id, pane.el.getBoundingClientRect()));
      return out;
    },
  };
}

function createGuide(h) {
  const pct = h("span", "guide-pct");
  const el = h("div", "resize-guide", [
    h("span", "", "◀"), h("span", "guide-line"), pct, h("span", "guide-line"), h("span", "", "▶")]);
  el.hidden = true;
  return {
    el,
    place(origin, dir, a, b, percent) {
      pct.textContent = `${percent}%`;
      el.hidden = false;
      const left = a.left - origin.left;
      const width = dir === "row" ? b.right - a.left : a.width;
      el.style.left = `${left}px`;
      el.style.width = `${width}px`;
      const guideH = el.offsetHeight;
      const top = dir === "row" ? a.bottom - origin.top - guideH : a.bottom - origin.top - guideH / 2;
      el.style.top = `${top}px`;
    },
    hide() {
      el.hidden = true;
    },
  };
}
