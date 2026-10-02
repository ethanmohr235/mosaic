/* The canvas graph the `graph` pane draws with -- Monarch's
 * hand-written renderer (it replaced Cytoscape), ported to an ES module
 * and restyled to the handoff's graph rules (docs/console.md → Settled for
 * U8; docs/design/mosaic-console/README.md → Graph styling).
 *
 * Layout: a force layout in which each species owns a direction from the
 * centre, so all of a species' instances cluster on one spoke and the
 * species mote sits nearer the middle on the same spoke. Positions live in
 * world space; the camera maps world → screen as
 * `screen = centre + camera.xy + world × scale`. Nodes, edges and labels
 * are drawn in screen space at fixed pixel sizes, so zooming spreads the
 * graph out rather than inflating it.
 *
 * Style:
 *   - every mote a square stroked in its species colour on a --bg fill; a
 *     species a diamond; a rule a square with a hollow inner square
 *   - the selection filled with its colour, a 1px --bright outline 4px out
 *   - the cursor (keyboard) a --fg outline; a pinned node a --dim dot
 *   - edges touching the primary 1.2px --dim solid, the rest 1px --line
 *     dashed 2 3, each with a small arrowhead; the type label only on edges
 *     touching the primary, the cursor or the pointer
 *   - labels 11px mono --dim to the right of the node, --bright selected
 *
 * Nothing here knows the command line: gestures are callbacks the pane
 * turns into commands (views/graph.js). */

import { hashString } from "./palette.js";

const REPULSION = 1500;
const SAME_SPECIES_SPRING = 0.05;
const CROSS_SPECIES_SPRING = 0.006;
const ANCHOR_PULL = 0.018;
const DAMPING = 0.82;
const EDGE_LENGTH = 80;
const MIN_SCALE = 0.2;
const MAX_SCALE = 3;
const HIT_PX = 10;
const EDGE_HIT_PX = 6;
const DRAG_PX = 4;
const PARALLEL_BOW = 32;
const SETTLED = 0.004;

const HALF = { selected: 7, labeled: 5, plain: 3.5 };   // node half-sizes in px
const LABEL_LIMIT = 60;                                  // labels everywhere at or below this many nodes
const LABEL_CHARS = 24;

const hueOf = (id) => hashString(id) % 360;

export class GraphCanvas {
  /** `callbacks`: onPick(id, event), onOpen(id), onEdge(rel), onEmpty(),
   *  onConnect(fromId, toId), onPinned(id, x, y), onHover(), onSettle(). */
  constructor(canvas, callbacks = {}) {
    this.canvas = canvas;
    this.cb = callbacks;
    this.motes = [];
    this.edges = [];
    this.byId = new Map();
    this.nodes = new Map();        // id -> {x, y, vx, vy, pinned}
    this.speciesOf = new Map();    // id -> species id (the first)
    this.colorOf = () => "";
    this.selected = new Set();
    this.primary = null;
    this.cursor = null;            // a mote id
    this.edgeCursor = null;        // a relationship id
    this.hover = null;
    this.hoverEdge = null;
    this.camera = { x: 0, y: 0, scale: 0.75 };
    this.alpha = 1;
    this.spread = 320;
    this.panTarget = null;
    this.fitUntil = 0;
    this.gesture = null;
    this.width = 0;
    this.height = 0;
    this.dpr = 1;
    this.readTokens();
    this.frame = 0;
    this.timer = 0;
    this.running = false;
    this.destroyed = false;
    this._bind();
    this.resize();
  }

  destroy() {
    this.destroyed = true;
    cancelAnimationFrame(this.frame);
    clearTimeout(this.timer);
    this.observer.disconnect();
    this.canvas.removeEventListener("mousedown", this.onDown);
    this.canvas.removeEventListener("dblclick", this.onDouble);
    this.canvas.removeEventListener("wheel", this.onWheel);
    this.canvas.removeEventListener("mouseleave", this.onLeave);
    window.removeEventListener("mousemove", this.onMove);
    window.removeEventListener("mouseup", this.onUp);
  }

  /* ---- data ---- */

  /** `payload` is api.monarch.view()'s answer; `colorOf(mote)` the
   *  species palette's; `pins` {id: [x, y]}. Nodes already placed keep
   *  their place, so a re-run (a write, a new root) moves nothing that
   *  is still shown. */
  setData(payload, colorOf, pins = {}) {
    const first = this.nodes.size === 0;
    this.motes = payload.motes;
    this.edges = payload.edges;
    this.byId = new Map(this.motes.map((m) => [m.id, m]));
    this.colorOf = colorOf;
    this.speciesIds = new Set(this.motes.filter((m) => m.kinds.includes("species")).map((m) => m.id));
    this.speciesOf = new Map(this.motes.filter((m) => m.speciesId).map((m) => [m.id, m.speciesId]));
    this.spread = 260 + new Set(this.speciesOf.values()).size * 70;
    this.parallel = parallelIndex(this.edges);
    for (const id of [...this.nodes.keys()]) if (!this.byId.has(id)) this.nodes.delete(id);
    for (const mote of this.motes) {
      const pin = pins[mote.id];
      let node = this.nodes.get(mote.id);
      if (!node) {
        const anchor = this.anchorFor(mote.id);
        const seed = hashString(mote.id);
        const angle = (seed % 360) * (Math.PI / 180);
        const radius = 20 + ((seed >>> 9) % 90);
        node = { x: anchor.x + Math.cos(angle) * radius, y: anchor.y + Math.sin(angle) * radius, vx: 0, vy: 0, pinned: false };
        this.nodes.set(mote.id, node);
      }
      const dragging = this.gesture && this.gesture.kind === "drag" && this.gesture.id === mote.id;
      if (pin && !dragging) Object.assign(node, { x: pin[0], y: pin[1], vx: 0, vy: 0, pinned: true });
      else if (!pin && !dragging) node.pinned = false;
    }
    if (this.cursor && !this.byId.has(this.cursor)) this.cursor = null;
    if (this.edgeCursor && !this.edges.some((e) => e.id === this.edgeCursor)) this.edgeCursor = null;
    this.alpha = Math.max(this.alpha, first ? 1 : 0.6);
    if (first) this.fitUntil = SETTLED;
    this.kick();
  }

  anchorFor(id) {
    if (this.speciesIds.has(id)) return this.spoke(id, 0.45);
    const species = this.speciesOf.get(id);
    return species ? this.spoke(species, 1) : { x: 0, y: 0 };
  }

  spoke(speciesId, reach) {
    const angle = (hueOf(speciesId) * Math.PI) / 180;
    return { x: Math.cos(angle) * this.spread * reach, y: Math.sin(angle) * this.spread * reach };
  }

  /** The selection, its primary, and whether to bring the primary into view. */
  setSelection(ids, primary, { pan = true } = {}) {
    this.selected = new Set(ids);
    this.primary = primary;
    if (pan && primary && this.nodes.has(primary)) this.panTo(primary, { onlyIfOff: true });
    this.kick();
  }

  setCursor(id, { pan = true } = {}) {
    this.cursor = id;
    this.edgeCursor = null;
    if (pan && id) this.panTo(id, { onlyIfOff: true });
    this.kick();
  }

  setEdgeCursor(relId) {
    this.edgeCursor = relId;
    this.kick();
  }

  pinnedPositions(ids) {
    const out = {};
    for (const id of ids) {
      const node = this.nodes.get(id);
      if (node) out[id] = [node.x, node.y];
    }
    return out;
  }

  unpin(ids) {
    for (const id of ids || this.nodes.keys()) {
      const node = this.nodes.get(id);
      if (node) node.pinned = false;
    }
    this.alpha = Math.max(this.alpha, 0.5);
    this.kick();
  }

  /* ---- camera ---- */

  /** Frame every node, leaving room on the right for the labels. */
  fit(padding = 30) {
    const points = [...this.nodes.values()];
    if (!points.length || !this.width) return;
    const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs), minY = Math.min(...ys), maxY = Math.max(...ys);
    const longest = Math.min(LABEL_CHARS, Math.max(0, ...this.motes.map((m) => (m.name || "").length)));
    const labelRoom = longest * 7 + 14;
    const scale = clamp(Math.min((this.width - padding * 2 - labelRoom) / Math.max(maxX - minX, 1),
      (this.height - padding * 2) / Math.max(maxY - minY, 1)), MIN_SCALE, MAX_SCALE);
    this.camera = { x: -((minX + maxX) / 2) * scale - labelRoom / 2, y: -((minY + maxY) / 2) * scale, scale };
    this.panTarget = null;
    this.kick();
  }

  /** Fit now and keep fitting until the layout settles. */
  fitWhenSettled() {
    this.fitUntil = SETTLED;
    this.alpha = Math.max(this.alpha, 0.3);
    this.kick();
  }

  zoom(factor, at = null) {
    const point = at || { x: this.width / 2, y: this.height / 2 };
    const world = this.toWorld(point.x, point.y);
    const scale = clamp(this.camera.scale * factor, MIN_SCALE, MAX_SCALE);
    this.camera.x = point.x - this.width / 2 - world.x * scale;
    this.camera.y = point.y - this.height / 2 - world.y * scale;
    this.camera.scale = scale;
    this.panTarget = null;
    this.fitUntil = 0;
    this.kick();
  }

  panTo(id, { onlyIfOff = false } = {}) {
    const node = this.nodes.get(id);
    // While the first fit runs it frames everything, this node included.
    if (!node || this.fitUntil) return;
    if (onlyIfOff) {
      const s = this.toScreen(node);
      const margin = 30;
      if (s.x > margin && s.y > margin && s.x < this.width - margin && s.y < this.height - margin) return;
    }
    this.panTarget = { x: -node.x * this.camera.scale, y: -node.y * this.camera.scale };
    this.fitUntil = 0;
    this.kick();
  }

  toScreen(p) {
    return { x: this.width / 2 + this.camera.x + p.x * this.camera.scale,
      y: this.height / 2 + this.camera.y + p.y * this.camera.scale };
  }

  toWorld(x, y) {
    return { x: (x - this.width / 2 - this.camera.x) / this.camera.scale,
      y: (y - this.height / 2 - this.camera.y) / this.camera.scale };
  }

  /** A node's position relative to the page -- what the smoke test aims
   *  synthetic mouse events at, as there is no element to click. */
  screenPositionOf(id) {
    const node = this.nodes.get(id);
    if (!node) return null;
    const rect = this.canvas.getBoundingClientRect();
    const s = this.toScreen(node);
    return { x: rect.left + s.x, y: rect.top + s.y };
  }

  /** The neighbour of `id` lying closest to direction `degrees` on
   *  screen (0 right, 90 down) -- the arrow keys' walk. */
  neighbourToward(id, degrees) {
    const from = this.nodes.get(id);
    if (!from) return null;
    let best = null, bestDelta = Infinity;
    for (const e of this.edges) {
      const other = e.from === id ? e.to : e.to === id ? e.from : null;
      if (!other || other === id) continue;
      const to = this.nodes.get(other);
      let angle = (Math.atan2(to.y - from.y, to.x - from.x) * 180) / Math.PI;
      if (angle < 0) angle += 360;
      const diff = Math.abs(angle - degrees) % 360;
      const delta = diff > 180 ? 360 - diff : diff;
      if (delta < bestDelta) { bestDelta = delta; best = other; }
    }
    return best;
  }

  /* ---- the loop ---- */

  /** Run frames until the layout and the camera come to rest. rAF with a
   *  timer behind it: a compositor that stops sending frames to a hidden
   *  window would otherwise stall the loop with the canvas blank. */
  kick() {
    if (this.destroyed || this.running) return;
    this.running = true;
    this.schedule();
  }

  schedule() {
    cancelAnimationFrame(this.frame);
    clearTimeout(this.timer);
    this.frame = requestAnimationFrame(this.tick);
    this.timer = setTimeout(this.tick, 250);
  }

  tick = () => {
    if (this.destroyed) return;
    if (this.alpha > SETTLED) this.step();
    if (this.fitUntil) {
      this.fit();
      if (this.alpha <= this.fitUntil) this.fitUntil = 0;
    }
    if (this.panTarget) {
      this.camera.x += (this.panTarget.x - this.camera.x) * 0.18;
      this.camera.y += (this.panTarget.y - this.camera.y) * 0.18;
      if (Math.abs(this.panTarget.x - this.camera.x) < 0.4 && Math.abs(this.panTarget.y - this.camera.y) < 0.4) {
        this.panTarget = null;
      }
    }
    this.draw();
    if (this.alpha > SETTLED || this.panTarget || this.fitUntil || this.gesture) this.schedule();
    else {
      this.running = false;
      if (this.cb.onSettle) this.cb.onSettle();
    }
  };

  step() {
    const ids = [...this.nodes.keys()];
    for (let i = 0; i < ids.length; i++) {
      const a = this.nodes.get(ids[i]);
      for (let j = i + 1; j < ids.length; j++) {
        const b = this.nodes.get(ids[j]);
        const dx = a.x - b.x, dy = a.y - b.y;
        const distSq = Math.max(dx * dx + dy * dy, 1);
        const dist = Math.sqrt(distSq);
        const force = REPULSION / distSq;
        const fx = (dx / dist) * force, fy = (dy / dist) * force;
        if (!a.pinned) { a.vx += fx; a.vy += fy; }
        if (!b.pinned) { b.vx -= fx; b.vy -= fy; }
      }
    }
    // Edges between species pull only weakly: species grouping carries the
    // layout, and every instance_of yanking its instance in would undo it.
    for (const e of this.edges) {
      const a = this.nodes.get(e.from), b = this.nodes.get(e.to);
      if (!a || !b || a === b) continue;
      const k = this.speciesOf.get(e.from) === this.speciesOf.get(e.to) ? SAME_SPECIES_SPRING : CROSS_SPECIES_SPRING;
      const dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const diff = (dist - EDGE_LENGTH) * k;
      const fx = (dx / dist) * diff, fy = (dy / dist) * diff;
      if (!a.pinned) { a.vx += fx; a.vy += fy; }
      if (!b.pinned) { b.vx -= fx; b.vy -= fy; }
    }
    for (const [id, node] of this.nodes) {
      if (node.pinned) { node.vx = 0; node.vy = 0; continue; }
      const anchor = this.anchorFor(id);
      node.vx = (node.vx + (anchor.x - node.x) * ANCHOR_PULL) * DAMPING;
      node.vy = (node.vy + (anchor.y - node.y) * ANCHOR_PULL) * DAMPING;
      node.x += node.vx;
      node.y += node.vy;
    }
    this.alpha *= 0.985;
  }

  /* ---- drawing ---- */

  /** Size the canvas to its pane. Setting a canvas's size clears it, so
   *  this only does so when the size really changed, and paints again
   *  before the frame is shown -- otherwise a pane resize, or another pane
   *  re-rendering, flashes a blank canvas. A pane with no size (detached
   *  mid re-render, hidden) keeps the canvas it has. */
  resize() {
    const rect = this.canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    if (rect.width < 1 || rect.height < 1) return;
    if (rect.width === this.width && rect.height === this.height && dpr === this.dpr) return;
    this.width = rect.width;
    this.height = rect.height;
    this.dpr = dpr;
    this.canvas.width = Math.max(1, Math.round(rect.width * dpr));
    this.canvas.height = Math.max(1, Math.round(rect.height * dpr));
    this.canvas.style.width = `${rect.width}px`;
    this.canvas.style.height = `${rect.height}px`;
    this.readTokens();
    this.draw();
    this.kick();
  }

  /** Canvas can't read var(); the tokens are read once per resize. */
  readTokens() {
    const css = getComputedStyle(document.documentElement);
    const read = (name) => css.getPropertyValue(name).trim();
    this.colors = { bg: read("--bg"), line: read("--line"), dim: read("--dim"), fg: read("--fg"),
      bright: read("--bright"), font: read("--font") || "monospace" };
  }

  paint(color) {
    if (!color) return this.colors.dim;
    const match = /^var\((--[\w-]+)\)$/.exec(color);
    return match ? getComputedStyle(document.documentElement).getPropertyValue(match[1]).trim() : color;
  }

  /** Which nodes carry a label right now. */
  labelled(id, focus) {
    return this.nodes.size <= LABEL_LIMIT || this.camera.scale > 1.1 || this.selected.has(id)
      || id === this.cursor || id === this.hover || focus.has(id);
  }

  draw() {
    const ctx = this.canvas.getContext("2d");
    const c = this.colors;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.fillStyle = c.bg;
    ctx.fillRect(0, 0, this.width, this.height);
    const centre = this.hover || this.cursor || this.primary;
    const focus = new Set();
    for (const e of this.edges) {
      if (e.from === centre) focus.add(e.to);
      if (e.to === centre) focus.add(e.from);
    }
    const screen = new Map([...this.nodes].map(([id, n]) => [id, this.toScreen(n)]));
    this.drawEdges(ctx, screen, centre);
    this.drawNodes(ctx, screen, focus);
  }

  drawEdges(ctx, screen, centre) {
    const c = this.colors;
    ctx.font = `11px ${c.font}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    const labels = [];
    for (const e of this.edges) {
      const a = screen.get(e.from), b = screen.get(e.to);
      if (!a || !b) continue;
      const strong = e.from === this.primary || e.to === this.primary;
      const picked = e.id === this.edgeCursor || e.id === this.hoverEdge;
      ctx.strokeStyle = picked ? c.fg : strong ? c.dim : c.line;
      ctx.lineWidth = picked ? 1.5 : strong ? 1.2 : 1;
      ctx.setLineDash(picked || strong ? [] : [2, 3]);
      const curve = this.curve(a, b, e);
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.quadraticCurveTo(curve.cx, curve.cy, b.x, b.y);
      ctx.stroke();
      ctx.setLineDash([]);
      // The arrowhead stops at the target's edge, not its centre.
      const angle = Math.atan2(b.y - curve.cy, b.x - curve.cx);
      const back = this.half(e.to) + 2;
      ctx.save();
      ctx.translate(b.x - Math.cos(angle) * back, b.y - Math.sin(angle) * back);
      ctx.rotate(angle);
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath();
      ctx.moveTo(0, 0); ctx.lineTo(-5, -2.5); ctx.lineTo(-5, 2.5);
      ctx.closePath();
      ctx.fill();
      ctx.restore();
      if (picked || (centre && (e.from === centre || e.to === centre))) {
        labels.push({ text: e.type, x: (curve.mx + curve.cx) / 2, y: (curve.my + curve.cy) / 2, bright: picked });
      }
    }
    for (const l of labels) {
      const w = ctx.measureText(l.text).width;
      ctx.fillStyle = c.bg;
      ctx.fillRect(l.x - w / 2 - 3, l.y - 7, w + 6, 14);
      ctx.fillStyle = l.bright ? c.fg : c.dim;
      ctx.fillText(l.text, l.x, l.y);
    }
  }

  half(id) {
    if (this.selected.has(id)) return HALF.selected;
    return this.labelled(id, new Set()) ? HALF.labeled : HALF.plain;
  }

  drawNodes(ctx, screen, focus) {
    const c = this.colors;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    // Selected nodes last, so their outline and label sit on top.
    const order = [...this.nodes.keys()].sort((a, b) => Number(this.selected.has(a)) - Number(this.selected.has(b)));
    for (const id of order) {
      const mote = this.byId.get(id);
      const p = screen.get(id);
      if (!mote || p.x < -60 || p.y < -20 || p.x > this.width + 200 || p.y > this.height + 20) continue;
      const selected = this.selected.has(id);
      const labelled = this.labelled(id, focus);
      const r = selected ? HALF.selected : labelled ? HALF.labeled : HALF.plain;
      const color = this.paint(this.colorOf(mote));
      const species = this.speciesIds.has(id);
      const rule = mote.kinds.includes("rule");
      shape(ctx, p, r, species);
      ctx.fillStyle = selected ? color : c.bg;
      ctx.fill();
      ctx.lineWidth = 1.3;
      ctx.strokeStyle = color;
      ctx.stroke();
      if (rule) {
        shape(ctx, p, r * 0.45, false);
        ctx.lineWidth = 1;
        ctx.strokeStyle = selected ? c.bg : color;
        ctx.stroke();
      }
      if (selected) {
        shape(ctx, p, r + 4, species);
        ctx.lineWidth = 1;
        ctx.strokeStyle = c.bright;
        ctx.stroke();
      }
      // The cursor's outline, unless it is on the primary, which is outlined already.
      if ((id === this.cursor && id !== this.primary) || id === this.hover) {
        shape(ctx, p, r + (selected ? 7 : 3), species);
        ctx.lineWidth = 1;
        ctx.strokeStyle = id === this.cursor ? c.fg : c.dim;
        ctx.setLineDash(id === this.cursor ? [] : [2, 2]);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      if (this.nodes.get(id).pinned) {
        ctx.fillStyle = c.dim;
        ctx.fillRect(p.x + r + 1, p.y - r - 3, 2, 2);
      }
      if (labelled) {
        const name = mote.name || id.slice(0, 8);
        const text = name.length > LABEL_CHARS ? `${name.slice(0, LABEL_CHARS - 1)}…` : name;
        ctx.font = `${selected ? 600 : 400} 11px ${c.font}`;
        const x = p.x + r + (selected ? 8 : 5);
        const w = ctx.measureText(text).width;
        ctx.fillStyle = c.bg;
        ctx.fillRect(x - 2, p.y - 7, w + 4, 14);
        ctx.fillStyle = selected || id === this.cursor ? c.bright : c.dim;
        ctx.fillText(text, x, p.y);
      }
    }
  }

  curve(a, b, e) {
    const bow = (this.parallel.get(e.id) || 0) * PARALLEL_BOW * Math.min(this.camera.scale, 1);
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    const angle = Math.atan2(b.y - a.y, b.x - a.x);
    return { mx, my, cx: mx + Math.cos(angle - Math.PI / 2) * bow, cy: my + Math.sin(angle - Math.PI / 2) * bow };
  }

  /* ---- hit testing ---- */

  local(event) {
    const rect = this.canvas.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  }

  nodeAt(pt) {
    let best = null, bestDist = HIT_PX;
    for (const [id, node] of this.nodes) {
      const s = this.toScreen(node);
      const d = Math.max(Math.abs(s.x - pt.x), Math.abs(s.y - pt.y));
      if (d < bestDist) { bestDist = d; best = id; }
    }
    return best;
  }

  edgeAt(pt) {
    let best = null, bestDist = EDGE_HIT_PX;
    for (const e of this.edges) {
      const na = this.nodes.get(e.from), nb = this.nodes.get(e.to);
      if (!na || !nb) continue;
      const a = this.toScreen(na), b = this.toScreen(nb);
      const { cx, cy } = this.curve(a, b, e);
      for (let i = 0; i <= 16; i++) {
        const t = i / 16, it = 1 - t;
        const x = it * it * a.x + 2 * it * t * cx + t * t * b.x;
        const y = it * it * a.y + 2 * it * t * cy + t * t * b.y;
        const d = Math.hypot(x - pt.x, y - pt.y);
        if (d < bestDist) { bestDist = d; best = e; }
      }
    }
    return best;
  }

  /* ---- the mouse ----
   * Left on a node: click picks it, drag moves and pins it, ctrl+drag to
   * another node connects. Left on an edge picks it. Left or middle on
   * empty space pans; a click there clears the edge cursor. Wheel zooms
   * at the pointer; double-click a node opens it. */

  _bind() {
    this.observer = new ResizeObserver(() => this.resize());
    this.observer.observe(this.canvas.parentElement);
    this.canvas.addEventListener("mousedown", this.onDown);
    this.canvas.addEventListener("dblclick", this.onDouble);
    this.canvas.addEventListener("wheel", this.onWheel, { passive: false });
    this.canvas.addEventListener("mouseleave", this.onLeave);
    window.addEventListener("mousemove", this.onMove);
    window.addEventListener("mouseup", this.onUp);
  }

  onDown = (event) => {
    this.fitUntil = 0;
    const pt = this.local(event);
    if (event.button === 1) event.preventDefault();
    if (event.button !== 0 && event.button !== 1) return;
    const id = event.button === 0 ? this.nodeAt(pt) : null;
    if (id && event.ctrlKey) {
      this.gesture = { kind: "connect", from: id, at: pt };
    } else if (id) {
      const node = this.nodes.get(id);
      this.gesture = { kind: "drag", id, start: pt, origin: { x: node.x, y: node.y }, moved: false };
    } else {
      this.gesture = { kind: "pan", start: pt, camera: { ...this.camera }, moved: false, button: event.button };
      this.panTarget = null;
    }
    this.kick();
  };

  onMove = (event) => {
    const pt = this.local(event);
    const g = this.gesture;
    if (!g) {
      if (event.target !== this.canvas) return;
      const id = this.nodeAt(pt);
      const edge = id ? null : this.edgeAt(pt);
      if (id !== this.hover || (edge && edge.id) !== this.hoverEdge) {
        this.hover = id;
        this.hoverEdge = edge ? edge.id : null;
        this.canvas.style.cursor = id || edge ? "pointer" : "default";
        if (this.cb.onHover) this.cb.onHover();
        this.kick();
      }
      return;
    }
    if (g.kind === "connect") {
      g.at = pt;
      this.hover = this.nodeAt(pt);
      this.canvas.style.cursor = "crosshair";
    } else if (g.kind === "drag") {
      g.moved = g.moved || Math.hypot(pt.x - g.start.x, pt.y - g.start.y) > DRAG_PX;
      if (g.moved) {
        const node = this.nodes.get(g.id);
        if (node) {
          node.x = g.origin.x + (pt.x - g.start.x) / this.camera.scale;
          node.y = g.origin.y + (pt.y - g.start.y) / this.camera.scale;
          Object.assign(node, { vx: 0, vy: 0, pinned: true });
          this.alpha = Math.max(this.alpha, 0.3);
        }
      }
    } else if (g.kind === "pan") {
      g.moved = g.moved || Math.hypot(pt.x - g.start.x, pt.y - g.start.y) > DRAG_PX;
      this.camera.x = g.camera.x + (pt.x - g.start.x);
      this.camera.y = g.camera.y + (pt.y - g.start.y);
      this.canvas.style.cursor = "grabbing";
    }
    this.kick();
  };

  onUp = (event) => {
    const g = this.gesture;
    if (!g) return;
    this.gesture = null;
    const pt = this.local(event);
    this.canvas.style.cursor = "default";
    if (g.kind === "connect") {
      const to = this.nodeAt(pt);
      this.hover = null;
      if (to && to !== g.from && this.cb.onConnect) this.cb.onConnect(g.from, to);
    } else if (g.kind === "drag") {
      if (g.moved) {
        const node = this.nodes.get(g.id);
        if (node && this.cb.onPinned) this.cb.onPinned(g.id, node.x, node.y);
      } else if (this.cb.onPick) this.cb.onPick(g.id, event);
    } else if (g.kind === "pan" && !g.moved && g.button === 0) {
      const edge = this.edgeAt(pt);
      if (edge && this.cb.onEdge) this.cb.onEdge(edge);
      else if (!edge && this.cb.onEmpty) this.cb.onEmpty();
    }
    this.kick();
  };

  onDouble = (event) => {
    const id = this.nodeAt(this.local(event));
    if (id && this.cb.onOpen) this.cb.onOpen(id);
  };

  onWheel = (event) => {
    event.preventDefault();
    this.zoom(Math.exp(-event.deltaY * 0.0011), this.local(event));
  };

  onLeave = () => {
    if (this.gesture) return;
    if (this.hover || this.hoverEdge) {
      this.hover = this.hoverEdge = null;
      if (this.cb.onHover) this.cb.onHover();
      this.kick();
    }
  };
}

/** A square (or, for a species, a diamond) of half-size `r` at `p`. */
function shape(ctx, p, r, diamond) {
  ctx.beginPath();
  if (diamond) {
    const d = r * 1.35;
    ctx.moveTo(p.x, p.y - d); ctx.lineTo(p.x + d, p.y); ctx.lineTo(p.x, p.y + d); ctx.lineTo(p.x - d, p.y);
    ctx.closePath();
  } else ctx.rect(p.x - r, p.y - r, r * 2, r * 2);
}

/** Parallel edges between one pair bow apart by an increasing offset, so
 *  two relationships never draw as one line. */
function parallelIndex(edges) {
  const seen = new Map();
  const index = new Map();
  for (const e of edges) {
    const key = [e.from, e.to].sort().join("|");
    const n = seen.get(key) || 0;
    index.set(e.id, n);
    seen.set(key, n + 1);
  }
  return index;
}

function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}
