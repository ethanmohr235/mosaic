/* The flow board: the solve as columns of HTML cards with SVG wires
 * underneath (docs/console.md → Settled for `graph --flow`).
 *
 * core/flowview.py decides every order -- which column, which place in
 * it, which row first -- so this file only places what it is given with
 * CSS (flex columns), measures the ports once they are laid out, and
 * draws the wires between them:
 *
 *   r:‹card›         a rule card, or a folded group's card
 *   v:‹stub›:‹key›   a row of a stub card
 *   y:‹col›:‹n›      a relay: a wire passing through a column
 *
 * A wire leaves its port's right edge and enters the next one's left,
 * turning in the gutter between. Every wire in one gutter that leaves
 * the same port shares one vertical track (the trunk); the tracks are
 * spread across the gutter by the height they leave at, so trunks do
 * not lie on each other. The cursor's wires, followed through relays,
 * are drawn at full strength and the rest faint.
 *
 * Each band (one independent calculation) is its own row of columns,
 * stacked; gutters and the cursor's h/l are per band, j/k run on into
 * the next band. A relay is drawn as the stretch of wire across its
 * column, so a long wire is one unbroken line.
 *
 * The cursor is a port id: a card or a row. Pan and zoom are a CSS
 * transform on the world, so cards and maths stay text at any zoom. */

import { h } from "./components.js";
import { fmt } from "./plot.js";

const SVG = "http://www.w3.org/2000/svg";
const MIN_SCALE = 0.2;
const MAX_SCALE = 2.5;
const MARGIN = 16;

export const SOURCE_MARK = {
  given: "=", solved: "←", assumed: "~", external: "↗", measured: "m", fitted: "≈", unsolved: "?",
};
export const CHECK_MARK = { pass: "✓", fail: "✗", "not checked": "–" };
const CHECK_LEVEL = { pass: "ok", fail: "fault", "not checked": "warn" };

export class FlowBoard {
  constructor(root, handlers = {}) {
    this.root = root;
    this.on = handlers;
    this.world = h("div", "flow-world");
    this.svg = document.createElementNS(SVG, "svg");
    this.svg.classList.add("flow-wires");
    this.sheet = h("div", "flow-sheet");
    this.world.append(this.svg, this.sheet);
    root.append(this.world);
    this.camera = { x: MARGIN, y: MARGIN, scale: 1 };
    this.cursor = null;
    this.fitted = false;
    this.reset();
    this.listen();
  }

  reset() {
    this.ports = new Map();      // port id → element
    this.info = new Map();       // cursor id → {kind, card|row, stub, group}
    this.stubEls = new Map();    // stub id → element
    this.valueCells = [];        // {el, row}: what a playhead tick rewrites
    this.paths = [];             // {segment, path}
  }

  /* ---- data ---- */

  setData(payload, palette, valueOf) {
    this.payload = payload;
    this.palette = palette;
    this.valueOf = valueOf;
    this.reset();
    this.sheet.replaceChildren(...payload.bands.map((band, b) =>
      h("div", "flow-band", h("div", "flow-cols", band.columns.map((col, c) => this.column(col, `${b}.${c}`))))));
    if (this.cursor && !this.info.has(this.cursor)) this.cursor = null;
    this.markCursor();
    requestAnimationFrame(() => {
      if (!this.fitted) {
        this.fit();
        this.fitted = true;
      }
      this.drawWires();
    });
  }

  has(id) {
    return this.info.has(id);
  }

  describe(id) {
    return this.info.get(id) || null;
  }

  column(col, c) {
    const node = h("div", `flow-col ${col.kind}`);
    node.dataset.col = c;
    for (const id of col.items) {
      const g = this.payload.groups[id];
      if (id.startsWith("y:")) node.append(this.relay(id, c));
      else if (g.cards.length || g.stubs.length) node.append(this.group(g, c));
    }
    return node;
  }

  relay(id, c) {
    const el = h("div", "flow-relay");
    el.dataset.col = c;
    this.ports.set(id, el);
    return el;
  }

  group(g, c) {
    const framed = ["givens", "answers", "fault"].includes(g.kind) || g.coupled
      || (g.fold && g.fold.kind === "block") || g.cards.length > 1;
    const node = h("div", `flow-group ${g.kind} lvl-${g.level}${framed ? " framed" : ""}${g.coupled ? " coupled" : ""}`);
    const head = groupHead(g);
    if (head) node.append(h("div", "flow-group-head", head));
    const cards = h("div", "flow-cards", g.cards.map((id) => this.card(this.payload.cards[id], g, c)));
    const stubs = h("div", "flow-stubs", g.stubs.map((id) => this.stub(this.payload.stubs[id], g, c)));
    node.append(h("div", "flow-group-body", [g.cards.length ? cards : "", g.stubs.length ? stubs : ""]));
    return node;
  }

  card(card, g, c) {
    const id = `r:${card.id}`;
    const el = card.fold
      ? h("div", "flow-card folded", h("div", "flow-card-title", `${card.title} ▸`))
      : h("div", `flow-card${card.constraint ? " constraint" : ""}`, [
        card.name || card.anchor
          ? h("div", "flow-card-name", [card.name, card.anchor ? `${card.name ? " · " : ""}on ${card.anchor}` : ""])
          : "",
        equation(card),
        cardFoot(card),
      ]);
    this.target(el, id, c, { kind: card.fold ? "fold" : "card", card, group: g });
    return el;
  }

  stub(s, g, c) {
    const color = this.palette.colorOf({ id: s.moteId, speciesId: s.speciesId, kinds: [] });
    const head = h("div", "flow-stub-head", [s.name, s.siblings.length ? h("span", "dim", ` ·${s.siblings.length + 1}`) : ""]);
    head.style.color = color;
    const rows = h("div", "flow-rows", s.rows.map((row) => this.row(row, s, g, c)));
    const el = h("div", "flow-stub", [head, rows, s.more ? h("div", "flow-more", `+${s.more} more`) : ""]);
    el.style.borderColor = color;
    el.addEventListener("pointerenter", () => this.lightSiblings(s, true));
    el.addEventListener("pointerleave", () => this.lightSiblings(s, false));
    this.stubEls.set(s.id, el);
    return el;
  }

  row(row, s, g, c) {
    const value = h("span", "flow-val");
    this.valueCells.push({ el: value, row });
    const marks = h("span", "flow-marks", [
      h("span", `src src-${row.source}`, SOURCE_MARK[row.source] || "·"),
      row.check ? h("span", `chk lvl-${CHECK_LEVEL[row.check]}`, CHECK_MARK[row.check]) : "",
    ]);
    const el = h("div", `flow-row${row.problem ? " problem" : ""}`, [h("span", "flow-key", row.key), value, marks]);
    if (row.problem) el.title = row.problem;
    this.target(el, `v:${s.id}:${row.key}`, c, { kind: "row", row, stub: s, group: g });
    this.writeValue(value, row);
    return el;
  }

  target(el, id, c, info) {
    el.dataset.cur = id;
    el.dataset.col = c;
    this.ports.set(id, el);
    this.info.set(id, { ...info, id, col: c });
    el.addEventListener("click", (event) => {
      event.stopPropagation();
      if (this.on.onPick) this.on.onPick(id);
    });
    el.addEventListener("dblclick", (event) => {
      event.stopPropagation();
      if (this.on.onOpen) this.on.onOpen(id);
    });
  }

  writeValue(el, row) {
    const value = this.valueOf(row);
    const sigma = row.sigma ? ` ± ${fmt(row.sigma)}` : "";
    el.textContent = `${fmt(value)}${sigma}${row.unit ? ` ${row.unit}` : ""}`;
  }

  refreshValues() {
    for (const { el, row } of this.valueCells) this.writeValue(el, row);
  }

  lightSiblings(s, on) {
    for (const id of s.siblings) {
      const el = this.stubEls.get(id);
      if (el) el.classList.toggle("sib", on);
    }
  }

  setSelection(ids) {
    const chosen = new Set(ids || []);
    for (const [id, info] of this.info) {
      const moteId = info.kind === "row" ? info.stub.moteId : info.card && info.card.ruleMoteId;
      this.ports.get(id).classList.toggle("selected", !!moteId && chosen.has(moteId));
    }
  }

  /* ---- wires ---- */

  /** Port rectangles in world coordinates (the transform taken off). */
  rects() {
    const base = this.world.getBoundingClientRect();
    const s = this.camera.scale;
    return (el) => {
      const r = el.getBoundingClientRect();
      return { l: (r.left - base.left) / s, r: (r.right - base.left) / s,
        t: (r.top - base.top) / s, b: (r.bottom - base.top) / s };
    };
  }

  drawWires() {
    if (!this.payload) return;
    const rect = this.rects();
    const colRect = new Map([...this.sheet.querySelectorAll(".flow-col")].map((el) => [el.dataset.col, rect(el)]));
    const ends = this.payload.segments.map((seg) => this.ends(seg, rect)).filter(Boolean);
    const tracks = this.tracks(ends, colRect);
    this.svg.replaceChildren();
    this.paths = ends.map((e) => {
      const x = tracks.get(e);
      const path = document.createElementNS(SVG, "path");
      path.setAttribute("d", `M${e.a.x},${e.a.y}H${x}V${e.b.y}H${e.b.x}`);
      this.svg.append(path);
      return { segment: e.segment, path };
    });
    this.svg.setAttribute("width", this.sheet.scrollWidth);
    this.svg.setAttribute("height", this.sheet.scrollHeight);
    this.markWires();
  }

  ends(segment, rect) {
    const from = this.ports.get(segment.from);
    const to = this.ports.get(segment.to);
    if (!from || !to) return null;
    const a = rect(from), b = rect(to);
    return { segment, colA: from.dataset.col, colB: to.dataset.col,
      a: { x: a.r, y: (a.t + a.b) / 2 }, b: { x: b.l, y: (b.t + b.b) / 2 } };
  }

  /** The x of each wire's vertical run: one track per (gutter, source
   *  port), spread across the gutter in the order the sources sit. */
  tracks(ends, colRect) {
    const gutters = new Map();
    for (const e of ends) {
      const key = e.colA === e.colB ? `in:${e.colA}:${Math.round(e.a.x)}` : `g:${e.colB}`;
      if (!gutters.has(key)) gutters.set(key, []);
      gutters.get(key).push(e);
    }
    const out = new Map();
    for (const [key, list] of gutters) {
      const before = key.startsWith("g:") ? colRect.get(previousColumn(list[0].colB)) : null;
      const [lo, hi] = before
        ? [before.r, colRect.get(list[0].colB).l]
        : [Math.min(...list.map((e) => e.a.x)), Math.min(...list.map((e) => e.b.x))];
      const sources = [...new Set(list.map((e) => e.segment.from))]
        .sort((p, q) => list.find((e) => e.segment.from === p).a.y - list.find((e) => e.segment.from === q).a.y);
      const step = (hi - lo) / (sources.length + 1);
      for (const e of list) out.set(e, lo + step * (sources.indexOf(e.segment.from) + 1));
    }
    return out;
  }

  /** The cursor's wires, followed through relays both ways. */
  hotSegments() {
    const hot = new Set();
    if (!this.cursor) return hot;
    const reached = new Set([this.cursor]);
    let grew = true;
    while (grew) {
      grew = false;
      this.paths.forEach(({ segment }, i) => {
        if (hot.has(i) || !(reached.has(segment.from) || reached.has(segment.to))) return;
        hot.add(i);
        for (const end of [segment.from, segment.to]) {
          if (end.startsWith("y:") && !reached.has(end)) {
            reached.add(end);
            grew = true;
          }
        }
      });
    }
    return hot;
  }

  markWires() {
    const hot = this.hotSegments();
    const relays = new Set();
    this.paths.forEach(({ path, segment }, i) => {
      path.classList.toggle("hot", hot.has(i));
      if (hot.has(i)) [segment.from, segment.to].filter((p) => p.startsWith("y:")).forEach((p) => relays.add(p));
    });
    for (const [id, el] of this.ports) if (id.startsWith("y:")) el.classList.toggle("hot", relays.has(id));
  }

  /* ---- cursor ---- */

  setCursor(id, { pan = true } = {}) {
    this.cursor = id && this.info.has(id) ? id : null;
    this.markCursor();
    this.markWires();
    if (pan && this.cursor) this.reveal(this.ports.get(this.cursor));
  }

  markCursor() {
    for (const el of this.sheet.querySelectorAll(".cursor")) el.classList.remove("cursor");
    if (this.cursor) this.ports.get(this.cursor).classList.add("cursor");
  }

  /** The next cursor target `direction` of `from`: down/up in its column
   *  by position (on into the next band at the end), left/right to the
   *  nearest in the next column of its band that has any. */
  neighbour(from, direction) {
    const rect = this.rects();
    const spots = [...this.info.keys()].map((id) => {
      const r = rect(this.ports.get(id));
      const [band, col] = this.info.get(id).col.split(".").map(Number);
      return { id, band, col, x: (r.l + r.r) / 2, y: (r.t + r.b) / 2 };
    });
    const here = spots.find((s) => s.id === from);
    if (!here) return spots.length ? spots[0].id : null;
    const nearest = (list, by) => list.sort((p, q) => by(p) - by(q))[0];
    if (direction === "up" || direction === "down") {
      const down = direction === "down";
      const same = spots.filter((s) => s.band === here.band && s.col === here.col).sort((p, q) => p.y - q.y || p.x - q.x);
      const next = same[same.findIndex((s) => s.id === from) + (down ? 1 : -1)];
      if (next) return next.id;
      const band = spots.filter((s) => s.band === here.band + (down ? 1 : -1));
      if (!band.length) return null;
      const edge = down ? Math.min(...band.map((s) => s.y)) : Math.max(...band.map((s) => s.y));
      return nearest(band.filter((s) => Math.abs(s.y - edge) < 40), (s) => Math.abs(s.x - here.x)).id;
    }
    const cols = [...new Set(spots.filter((s) => s.band === here.band).map((s) => s.col))].sort((p, q) => p - q);
    const next = cols[cols.indexOf(here.col) + (direction === "right" ? 1 : -1)];
    if (next === undefined) return null;
    return nearest(spots.filter((s) => s.band === here.band && s.col === next), (s) => Math.abs(s.y - here.y)).id;
  }

  firstTarget() {
    return this.info.size ? [...this.info.keys()][0] : null;
  }

  /* ---- camera ---- */

  apply() {
    const { x, y, scale } = this.camera;
    this.world.style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
    if (this.on.onCamera) this.on.onCamera();
  }

  fit() {
    const w = this.sheet.scrollWidth, ht = this.sheet.scrollHeight;
    const vw = this.root.clientWidth, vh = this.root.clientHeight;
    if (!w || !ht || !vw || !vh) return this.apply();
    const scale = clamp(Math.min((vw - 2 * MARGIN) / w, (vh - 2 * MARGIN) / ht), MIN_SCALE, 1);
    this.camera = { scale, x: Math.max(MARGIN, (vw - w * scale) / 2), y: MARGIN };
    this.apply();
  }

  zoom(factor, at = { x: this.root.clientWidth / 2, y: this.root.clientHeight / 2 }) {
    const { x, y, scale } = this.camera;
    const next = clamp(scale * factor, MIN_SCALE, MAX_SCALE);
    this.camera = { scale: next, x: at.x - ((at.x - x) * next) / scale, y: at.y - ((at.y - y) * next) / scale };
    this.apply();
  }

  /** Pan just enough to bring `el` into view. */
  reveal(el) {
    if (!el) return;
    const box = this.root.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    let dx = 0, dy = 0;
    if (r.left < box.left + MARGIN) dx = box.left + MARGIN - r.left;
    else if (r.right > box.right - MARGIN) dx = Math.max(box.right - MARGIN - r.right, box.left + MARGIN - r.left);
    if (r.top < box.top + MARGIN) dy = box.top + MARGIN - r.top;
    else if (r.bottom > box.bottom - MARGIN) dy = Math.max(box.bottom - MARGIN - r.bottom, box.top + MARGIN - r.top);
    if (!dx && !dy) return;
    this.camera = { ...this.camera, x: this.camera.x + dx, y: this.camera.y + dy };
    this.apply();
  }

  listen() {
    this.onWheel = (event) => {
      event.preventDefault();
      const box = this.root.getBoundingClientRect();
      this.zoom(event.deltaY < 0 ? 1.1 : 1 / 1.1, { x: event.clientX - box.left, y: event.clientY - box.top });
    };
    this.onDown = (event) => {
      if (event.button !== 0 || event.target.closest("[data-cur]")) return;
      const start = { x: event.clientX, y: event.clientY, cx: this.camera.x, cy: this.camera.y };
      const move = (e) => {
        this.camera = { ...this.camera, x: start.cx + e.clientX - start.x, y: start.cy + e.clientY - start.y };
        this.apply();
      };
      const up = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    };
    this.root.addEventListener("wheel", this.onWheel, { passive: false });
    this.root.addEventListener("pointerdown", this.onDown);
  }

  destroy() {
    this.root.removeEventListener("wheel", this.onWheel);
    this.root.removeEventListener("pointerdown", this.onDown);
    this.world.remove();
  }
}

/** `b.c` → `b.(c-1)`: the column a gutter opens after. */
function previousColumn(key) {
  const [b, c] = key.split(".");
  return `${b}.${Number(c) - 1}`;
}

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}

function groupHead(g) {
  if (g.kind === "givens") return "givens";
  if (g.kind === "answers") return "answers";
  if (g.kind === "fault") return g.health || "not solved";
  if (g.fold) return g.fold.kind === "block" ? g.fold.title : null;
  if (g.coupled) return `coupled · ${g.equations} eq · ${g.unknowns} unknowns`;
  return null;
}

/** KaTeX, never trusted; an equation it cannot set shows as text. */
function equation(card) {
  const node = h("div", "flow-eq");
  if (card.tex && window.katex) {
    try {
      window.katex.render(card.tex, node, { displayMode: false, throwOnError: false, strict: false });
      return node;
    } catch (err) {
      node.replaceChildren();
    }
  }
  node.classList.add("raw");
  node.textContent = card.equation;
  return node;
}

function cardFoot(card) {
  if (!card.status) return "";
  const level = CHECK_LEVEL[card.status] || "idle";
  if (!card.constraint) return h("div", `flow-card-foot lvl-${level}`, `residual ${fmt(card.offBy)}`);
  const what = card.status === "pass" ? `pass · margin ${fmt(card.offBy)}` : `${card.status} · ${card.detail}`;
  return h("div", `flow-card-foot lvl-${level}`, what);
}
