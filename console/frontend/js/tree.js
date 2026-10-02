/* Pure split-tree operations. No DOM, no store: every function takes a
 * tree and returns a new one (the input is never mutated), so panes.js
 * and keys.js can compose them freely and the store can compare before
 * and after.
 *
 * On the bridge (and on disk) a tree is the contract's JSON:
 *   leaf   {command: "find type:task"}        ("" = an empty pane: blank.js)
 *   split  {split: "row"|"col", sizes: [0.5, 0.5], children: [...]}
 * `row` puts children side by side (a vertical divider, `^w v`); `col`
 * stacks them (`^w s`). Sizes are fractions summing to 1.
 *
 * Inside the shell every leaf also carries a stable `id`, which is what
 * a mounted pane is keyed by; `fromJSON`/`toJSON` add and strip it. A
 * split is addressed by its *path*: the child indices from the root. */

const MIN_SIZE = 0.05;
const STEP = 0.05;
let counter = 0;

function newId() {
  counter += 1;
  return `p${counter}`;
}

function clone(tree) {
  return JSON.parse(JSON.stringify(tree));
}

export function leaf(command = "") {
  return { id: newId(), command };
}

export const isLeaf = (node) => !node.split;

/* ---------- JSON ---------- */

/** Bridge JSON → internal tree. Anything malformed becomes one empty pane. */
export function fromJSON(json) {
  try {
    return adopt(json);
  } catch (err) {
    console.warn("unreadable layout tree; starting from an empty pane", err);
    return leaf("");
  }
}

function adopt(node) {
  if (node && typeof node.command === "string" && !node.split) return leaf(node.command);
  const ok = node && (node.split === "row" || node.split === "col")
    && Array.isArray(node.children) && node.children.length > 0;
  if (!ok) throw new Error(`not a tree node: ${JSON.stringify(node)}`);
  const children = node.children.map(adopt);
  if (children.length === 1) return children[0];
  return { split: node.split, sizes: normalize(node.sizes, children.length), children };
}

/** Internal tree → bridge JSON (ids dropped, sizes rounded). */
export function toJSON(node) {
  if (isLeaf(node)) return { command: node.command };
  return {
    split: node.split,
    sizes: node.sizes.map((s) => Math.round(s * 10000) / 10000),
    children: node.children.map(toJSON),
  };
}

/** Whether two JSON trees are the same layout (sizes compared to 0.1%). */
export function sameShape(a, b) {
  return JSON.stringify(roughly(a)) === JSON.stringify(roughly(b));
}

function roughly(node) {
  if (!node || !node.split) return { command: node ? node.command : "" };
  return {
    split: node.split,
    sizes: (node.sizes || []).map((s) => Math.round(s * 1000)),
    children: (node.children || []).map(roughly),
  };
}

function normalize(sizes, n) {
  const valid = Array.isArray(sizes) && sizes.length === n
    && sizes.every((s) => typeof s === "number" && s > 0);
  if (!valid) return Array(n).fill(1 / n);
  const total = sizes.reduce((a, b) => a + b, 0);
  return sizes.map((s) => s / total);
}

/* ---------- lookup ---------- */

export function leaves(node, out = []) {
  if (isLeaf(node)) out.push(node);
  else node.children.forEach((child) => leaves(child, out));
  return out;
}

export const firstLeaf = (node) => leaves(node)[0];

export const findLeaf = (node, id) => leaves(node).find((l) => l.id === id) || null;

/** Child-index path from the root to the leaf `id`, or null. */
export function pathTo(node, id, path = []) {
  if (isLeaf(node)) return node.id === id ? path : null;
  for (let i = 0; i < node.children.length; i += 1) {
    const found = pathTo(node.children[i], id, [...path, i]);
    if (found) return found;
  }
  return null;
}

export function nodeAt(node, path) {
  return path.reduce((n, i) => n.children[i], node);
}

/* ---------- edits ---------- */

export function setCommand(tree, id, command) {
  const next = clone(tree);
  const target = findLeaf(next, id);
  if (target) target.command = command;
  return next;
}

/**
 * Put the subtree `json` (bridge JSON, e.g. `mo tion`'s three panes) where
 * leaf `id` was, taking its share. Returns {tree, focusId}: the first new
 * leaf whose command is `focusCommand`, else the first new leaf.
 */
export function replaceLeaf(tree, id, json, focusCommand = null) {
  const path = pathTo(tree, id);
  if (!path) return { tree, focusId: id };
  const sub = fromJSON(json);
  const fresh = leaves(sub);
  const focus = fresh.find((l) => l.command === focusCommand) || fresh[0];
  if (path.length === 0) return { tree: sub, focusId: focus.id };
  const next = clone(tree);
  nodeAt(next, path.slice(0, -1)).children[path[path.length - 1]] = sub;
  return { tree: next, focusId: focus.id };
}

/**
 * Split leaf `id` into two along `dir` ("row" | "col"); the new pane
 * (with `command`) goes after it. When the leaf's parent already splits
 * that way the new pane joins it as a sibling, as vim does, taking half
 * of the leaf's share. Returns {tree, id} of the new pane.
 */
export function split(tree, id, dir, command = "") {
  const fresh = leaf(command);
  const next = insertBeside(tree, id, dir, fresh, true);
  return next ? { tree: next, id: fresh.id } : { tree, id: null };
}

/** Put leaf `node` beside leaf `id` along `dir`, after it or before it,
 * taking half of its share. Null when `id` isn't in the tree. */
function insertBeside(tree, id, dir, node, after) {
  const next = clone(tree);
  const path = pathTo(next, id);
  if (!path) return null;
  const parent = path.length ? nodeAt(next, path.slice(0, -1)) : null;
  const index = path[path.length - 1];
  if (parent && parent.split === dir) {
    const half = parent.sizes[index] / 2;
    parent.children.splice(after ? index + 1 : index, 0, node);
    parent.sizes.splice(index, 1, half, half);
    return next;
  }
  const old = nodeAt(next, path);
  const pair = { split: dir, sizes: [0.5, 0.5], children: after ? [old, node] : [node, old] };
  if (!parent) return pair;
  parent.children[index] = pair;
  return next;
}

const EDGE_DIR = { left: "row", right: "row", top: "col", bottom: "col" };

/**
 * Dock leaf `fromId` against an edge ("left" | "right" | "top" |
 * "bottom") of leaf `toId`: it leaves its place, which closes as `close`
 * does, and splits `toId` that way. The moved leaf keeps its id, so its
 * pane isn't re-mounted. Unchanged when the two are the same leaf.
 */
export function move(tree, fromId, toId, edge) {
  const moving = findLeaf(tree, fromId);
  if (!moving || fromId === toId || !findLeaf(tree, toId) || !EDGE_DIR[edge]) return tree;
  const without = close(tree, fromId).tree;
  const after = edge === "right" || edge === "bottom";
  return insertBeside(without, toId, EDGE_DIR[edge], { ...moving }, after) || tree;
}

/** Leaves `a` and `b` trade places, each keeping its id. */
export function swapLeaves(tree, a, b) {
  const pa = pathTo(tree, a);
  const pb = pathTo(tree, b);
  if (!pa || !pb || a === b) return tree;
  const next = clone(tree);
  const at = (path) => [nodeAt(next, path.slice(0, -1)), path[path.length - 1]];
  const [parentA, ia] = at(pa);
  const [parentB, ib] = at(pb);
  const leafA = parentA.children[ia];
  parentA.children[ia] = parentB.children[ib];
  parentB.children[ib] = leafA;
  return next;
}

/**
 * Remove leaf `id`. Its share goes to its neighbours; a split left with
 * one child collapses into it. Closing the last pane leaves one empty
 * pane. Returns {tree, focusId}: the pane focus should move to.
 */
export function close(tree, id) {
  const path = pathTo(tree, id);
  if (!path || path.length === 0) {
    const fresh = leaf("");
    return { tree: fresh, focusId: fresh.id };
  }
  const next = clone(tree);
  const parent = nodeAt(next, path.slice(0, -1));
  const index = path[path.length - 1];
  parent.children.splice(index, 1);
  parent.sizes.splice(index, 1);
  parent.sizes = normalize(parent.sizes, parent.children.length);
  const neighbour = parent.children[Math.max(0, index - 1)];
  const focusId = isLeaf(neighbour) ? neighbour.id : leaves(neighbour).at(-1).id;
  return { tree: collapse(next), focusId };
}

function collapse(node) {
  if (isLeaf(node)) return node;
  const children = node.children.map(collapse);
  if (children.length === 1) return children[0];
  return { ...node, children };
}

/** Make leaf `id` the only pane. */
export function only(tree, id) {
  const target = findLeaf(tree, id);
  return target ? { ...target } : tree;
}

/** Every split's children get equal shares. */
export function equalize(node) {
  if (isLeaf(node)) return { ...node };
  const n = node.children.length;
  return { split: node.split, sizes: Array(n).fill(1 / n), children: node.children.map(equalize) };
}

/** Swap leaf `id` with its next sibling (the previous one if it's last). */
export function swap(tree, id) {
  const path = pathTo(tree, id);
  if (!path || path.length === 0) return tree;
  const next = clone(tree);
  const parent = nodeAt(next, path.slice(0, -1));
  const i = path[path.length - 1];
  const j = i + 1 < parent.children.length ? i + 1 : i - 1;
  [parent.children[i], parent.children[j]] = [parent.children[j], parent.children[i]];
  return next;
}

/* ---------- sizes ---------- */

/**
 * Grow (`delta` > 0) or shrink leaf `id`'s share of its nearest ancestor
 * split along `dir`, trading with the neighbouring child. `<`/`>` use
 * "row" and fall back to "col" when there is no row split above.
 * Returns {tree, path, index} -- the split and divider that moved -- or
 * null when there's nothing to resize.
 */
export function resize(tree, id, dir, delta = STEP) {
  const path = pathTo(tree, id);
  if (!path) return null;
  const found = nearestSplit(tree, path, dir) || nearestSplit(tree, path, dir === "row" ? "col" : "row");
  if (!found) return null;
  const { splitPath, childIndex } = found;
  const node = nodeAt(tree, splitPath);
  const other = childIndex + 1 < node.children.length ? childIndex + 1 : childIndex - 1;
  const sizes = [...node.sizes];
  const room = sizes[childIndex] + sizes[other];
  sizes[childIndex] = clamp(sizes[childIndex] + delta, MIN_SIZE, room - MIN_SIZE);
  sizes[other] = room - sizes[childIndex];
  return {
    tree: setSizes(tree, splitPath, sizes),
    path: splitPath,
    index: Math.min(childIndex, other),
  };
}

function nearestSplit(tree, path, dir) {
  for (let depth = path.length - 1; depth >= 0; depth -= 1) {
    const splitPath = path.slice(0, depth);
    if (nodeAt(tree, splitPath).split === dir) return { splitPath, childIndex: path[depth] };
  }
  return null;
}

/** Replace the sizes of the split at `path`. */
export function setSizes(tree, path, sizes) {
  const next = clone(tree);
  const node = nodeAt(next, path);
  node.sizes = normalize(sizes, node.children.length);
  return next;
}

/**
 * Move the divider between children `index` and `index + 1` of the split
 * at `path` so that child `index` ends at `fraction` of the pair's room.
 */
export function moveDivider(tree, path, index, fraction) {
  const node = nodeAt(tree, path);
  const sizes = [...node.sizes];
  const room = sizes[index] + sizes[index + 1];
  sizes[index] = clamp(room * fraction, MIN_SIZE, room - MIN_SIZE);
  sizes[index + 1] = room - sizes[index];
  return setSizes(tree, path, sizes);
}

function clamp(value, lo, hi) {
  return Math.min(Math.max(value, lo), hi);
}

/* ---------- focus by geometry ---------- */

/**
 * The pane `^w h/j/k/l` moves to from `fromId`: among the panes lying
 * wholly on that side, the nearest one that overlaps it on the other
 * axis (ties broken by the larger overlap). `rects` maps pane id →
 * {left, top, right, bottom}. Returns an id or null.
 */
export function focusDir(rects, fromId, dir) {
  const from = rects.get(fromId);
  if (!from) return null;
  let best = null;
  for (const [id, r] of rects) {
    if (id === fromId) continue;
    const score = directionalScore(from, r, dir);
    if (score && (!best || score.gap < best.gap
        || (score.gap === best.gap && score.overlap > best.overlap))) {
      best = { id, ...score };
    }
  }
  return best ? best.id : null;
}

function directionalScore(from, r, dir) {
  const EPS = 2;
  const horizontal = dir === "h" || dir === "l";
  const gap = {
    h: from.left - r.right, l: r.left - from.right,
    k: from.top - r.bottom, j: r.top - from.bottom,
  }[dir];
  if (gap === undefined || gap < -EPS) return null;
  const overlap = horizontal
    ? Math.min(from.bottom, r.bottom) - Math.max(from.top, r.top)
    : Math.min(from.right, r.right) - Math.max(from.left, r.left);
  if (overlap <= 0) return null;
  return { gap: Math.max(gap, 0), overlap };
}
