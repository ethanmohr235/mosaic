// ui/frontend/graph.js
//
// Canvas graph renderer for MonOSS. Replaces Cytoscape, which was dropped
// for three reasons: its cose layout can't cluster motes by species, its
// fit() on every selection was the jarring "snap back" the graph used to
// do, and owning the mouse pipeline outright removes the gesture conflicts
// that made ctrl+drag-to-connect so painful to land on top of a library.
//
// Coordinates: nodes live in world space; the camera maps world -> screen
// as `screen = center + camera.xy + world * scale`.

const REPULSION = 1500;
const SAME_SPECIES_SPRING = 0.05;
const CROSS_SPECIES_SPRING = 0.006;
const ANCHOR_PULL = 0.018;
const DAMPING = 0.82;
const EDGE_REST_LENGTH = 80;
const MIN_SCALE = 0.2;
const MAX_SCALE = 3;
const INITIAL_SCALE = 0.75;
const HIT_RADIUS = 14;
const EDGE_HIT_RADIUS = 12;
const DRAG_THRESHOLD = 4;
const PARALLEL_EDGE_BOW = 32;

// Deterministic string hash. Used for both species hues and each node's
// starting offset, so a garden lays out the same way and keeps the same
// colours every time it's opened -- the stability the old saved-positions
// code existed to provide, without pinning anything.
function hashString(text) {
  let h = 1779033703 ^ text.length;
  for (let i = 0; i < text.length; i++) {
    h = Math.imul(h ^ text.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  h = Math.imul(h ^ (h >>> 16), 2246822519);
  h = Math.imul(h ^ (h >>> 13), 3266489917);
  return (h ^= h >>> 16) >>> 0;
}

/** Set-equality for two id arrays -- used to skip redundant select() calls
 *  during live box-select, where mousemove fires far more often than the
 *  hit set actually changes. */
function _sameIds(a, b) {
  const setB = b instanceof Set ? b : new Set(b);
  if (a.length !== setB.size) return false;
  return a.every((id) => setB.has(id));
}

function hueFor(seed) {
  return hashString(seed) % 360;
}

class MonossGraph {
  constructor(canvas, callbacks = {}) {
    this.canvas = canvas;
    this.callbacks = callbacks;
    this.motes = [];
    this.relationships = [];
    this.speciesIds = new Set();
    this.speciesOf = new Map();   // mote id -> [species id]
    this.nodes = new Map();       // mote id -> {x, y, vx, vy, pinned}
    this.hiddenSpecies = new Set();
    this.hiddenRelTypes = new Set();
    this.selectedIds = new Set();
    this.selectedRelIds = new Set();
    this.hoverId = null;
    this.connectSourceId = null;
    this.simActive = false;        // a preview is open -- draws the dashed viewport border
    this.simHighlightIds = new Set(); // motes with a property changed as of the scrubbed step
    this.camera = { x: 0, y: 0, scale: INITIAL_SCALE };
    this.alpha = 1;
    this.panTarget = null;
    this.spread = 320;

    this._bindEvents();
    this._resize();
    this._scheduleFrame();
  }

  /** Drive the loop from requestAnimationFrame, but fall back to a timer
   *  if rAF goes quiet.
   *
   *  On Wayland, rAF is driven by compositor frame callbacks, so a window
   *  that is occluded or on another workspace stops receiving them
   *  entirely -- the loop then stalls for good and the canvas stays blank,
   *  including for any state change made while it was away. Whichever
   *  source fires first cancels the other, so a normally-presented window
   *  never pays for the fallback. */
  _scheduleFrame() {
    cancelAnimationFrame(this._frame);
    clearTimeout(this._timer);
    this._frame = requestAnimationFrame(this._tick);
    this._timer = setTimeout(this._tick, 250);
  }

  // ---- data ----

  setData(payload, pinnedPositions = {}) {
    this.motes = payload.motes || [];
    this.relationships = payload.relationships || [];
    this.speciesIds = new Set(payload.species_ids || []);

    // Species membership is derived from the instance_of edges already in
    // the payload rather than fetched separately. In whole-garden mode the
    // species mote is always present; under a hop limit it may not be, and
    // those instances simply render uncoloured.
    this.speciesOf = new Map();
    for (const rel of this.relationships) {
      if (rel.type !== "instance_of") continue;
      if (!this.speciesOf.has(rel.from_id)) this.speciesOf.set(rel.from_id, []);
      this.speciesOf.get(rel.from_id).push(rel.to_id);
    }

    // Species sectors need to sit further apart as the number of species
    // grows, or their clusters overlap into one blob again.
    const speciesCount = new Set([...this.speciesOf.values()].map((s) => s[0])).size;
    this.spread = 260 + speciesCount * 70;

    this._syncNodes(pinnedPositions);
    this.alpha = Math.max(this.alpha, 0.9);
  }

  /** Frame everything currently visible.
   *
   *  Called when a garden loads and from Reset view -- deliberately NOT on
   *  selection. Re-fitting every time a mote was clicked is exactly the
   *  "snaps back to some prioritised view" behaviour that made the old
   *  graph so jarring to navigate; selection now only pans. */
  fitToContent(padding = 90) {
    const points = [];
    for (const [id, node] of this.nodes) {
      if (this.isMoteVisible(id)) points.push(node);
    }
    if (points.length === 0) return;

    const xs = points.map((p) => p.x), ys = points.map((p) => p.y);
    const minX = Math.min(...xs), maxX = Math.max(...xs);
    const minY = Math.min(...ys), maxY = Math.max(...ys);
    const width = Math.max(maxX - minX, 1), height = Math.max(maxY - minY, 1);

    const scale = Math.max(MIN_SCALE, Math.min(MAX_SCALE,
      Math.min((this.cssWidth - padding * 2) / width, (this.cssHeight - padding * 2) / height)));
    this.camera.scale = scale;
    this.camera.x = -((minX + maxX) / 2) * scale;
    this.camera.y = -((minY + maxY) / 2) * scale;
    this.panTarget = null;
  }

  /** Keep re-framing until the layout settles, so a freshly opened garden
   *  ends up neatly composed rather than framed against its first,
   *  pre-simulation positions. */
  fitWhenSettled() {
    this._fitUntilAlpha = 0.08;
  }

  _syncNodes(pinnedPositions) {
    const live = new Set(this.motes.map((m) => m.id));
    for (const id of [...this.nodes.keys()]) {
      if (!live.has(id)) this.nodes.delete(id);
    }
    for (const mote of this.motes) {
      if (this.nodes.has(mote.id)) continue;
      const pinned = pinnedPositions[mote.id];
      if (pinned) {
        this.nodes.set(mote.id, { x: pinned.x, y: pinned.y, vx: 0, vy: 0, pinned: true });
        continue;
      }
      const anchor = this._anchorFor(mote.id);
      const seed = hashString(mote.id);
      const angle = (seed % 360) * (Math.PI / 180);
      const radius = 20 + ((seed >>> 9) % 90);
      this.nodes.set(mote.id, {
        x: anchor.x + Math.cos(angle) * radius,
        y: anchor.y + Math.sin(angle) * radius,
        vx: 0, vy: 0, pinned: false,
      });
    }
  }

  /** Each species owns a fixed direction from the centre, so every
   *  instance of it lands in the same sector. This is what makes a graph
   *  like the circuit example readable: all Nodes together, all Resistors
   *  together, rather than one undifferentiated hairball. */
  _anchorFor(moteId) {
    // A species sits on the same spoke as its instances but further in, so
    // a type and its members read as one radial group. Everything with no
    // species -- rules, loose motes -- stays in the middle.
    if (this.speciesIds.has(moteId)) {
      const angle = (hueFor(moteId) * Math.PI) / 180;
      return { x: Math.cos(angle) * this.spread * 0.45, y: Math.sin(angle) * this.spread * 0.45 };
    }
    const species = this.speciesOf.get(moteId);
    if (!species || species.length === 0) return { x: 0, y: 0 };
    const angle = (hueFor(species[0]) * Math.PI) / 180;
    return { x: Math.cos(angle) * this.spread, y: Math.sin(angle) * this.spread };
  }

  colorFor(moteId) {
    if (this.speciesIds.has(moteId)) return `oklch(0.80 0.13 ${hueFor(moteId)})`;
    const species = this.speciesOf.get(moteId);
    if (!species || species.length === 0) return "oklch(0.62 0.02 250)";
    return `oklch(0.72 0.11 ${hueFor(species[0])})`;
  }

  // ---- visibility ----

  setHiddenSpecies(ids) {
    this.hiddenSpecies = new Set(ids);
    this.alpha = Math.max(this.alpha, 0.4);
  }

  setHiddenRelTypes(types) {
    this.hiddenRelTypes = new Set(types);
    this.alpha = Math.max(this.alpha, 0.4);
  }

  // ---- simulation preview ----

  setSimActive(active) {
    this.simActive = active;
  }

  setSimHighlight(ids) {
    this.simHighlightIds = ids instanceof Set ? ids : new Set(ids);
  }

  isMoteVisible(moteId) {
    if (this.hiddenSpecies.size === 0) return true;
    if (this.speciesIds.has(moteId)) return !this.hiddenSpecies.has(moteId);
    const species = this.speciesOf.get(moteId) || [];
    if (species.length === 0) return !this.hiddenSpecies.has("__none__");
    return species.some((s) => !this.hiddenSpecies.has(s));
  }

  isRelVisible(rel) {
    return (
      !this.hiddenRelTypes.has(rel.type) &&
      this.isMoteVisible(rel.from_id) &&
      this.isMoteVisible(rel.to_id)
    );
  }

  // ---- camera ----

  /** Accepts a single id, an array of ids, or null/empty for "nothing
   *  selected". Panning only ever happens for a single-mote selection --
   *  there's no one sensible place to pan to for several at once, and
   *  box-select callers pass pan:false anyway since everything they just
   *  selected is already on screen by construction. */
  select(ids, { pan = true } = {}) {
    const list = ids == null ? [] : Array.isArray(ids) ? ids : [ids];
    this.selectedIds = new Set(list);
    this.selectedRelIds = new Set();
    if (pan && list.length === 1) {
      const node = this.nodes.get(list[0]);
      if (node) {
        this.panTarget = { x: -node.x * this.camera.scale, y: -node.y * this.camera.scale };
      }
    }
  }

  /** Mutually exclusive with node selection -- selecting a relationship
   *  clears any selected motes and vice versa, since the detail panel
   *  only ever shows one kind of thing at a time. */
  selectRelationship(id) {
    this.selectedIds = new Set();
    this.selectedRelIds = new Set([id]);
  }

  /** Shift+click on an edge, mirroring how shift+drag unions motes via
   *  box-select -- adds/removes just that one relationship rather than
   *  replacing the whole selection. Still mutually exclusive with node
   *  selection. */
  toggleRelationshipSelection(id) {
    this.selectedIds = new Set();
    const next = new Set(this.selectedRelIds);
    next.has(id) ? next.delete(id) : next.add(id);
    this.selectedRelIds = next;
  }

  /** The single selected id, or null if zero or several are selected --
   *  for callers (arrow-key nav) that only make sense for one at a time. */
  get selectedId() {
    return this.selectedIds.size === 1 ? [...this.selectedIds][0] : null;
  }

  resetView() {
    this.fitToContent();
  }

  /** Screen coordinates of a node, relative to the page. Exists so the
   *  smoke test can aim synthetic mouse events at a node the same way a
   *  person would -- there is no DOM element to click on a canvas. */
  screenPositionOf(id) {
    const node = this.nodes.get(id);
    if (!node) return null;
    const rect = this.canvas.getBoundingClientRect();
    return {
      x: rect.left + this.cssWidth / 2 + this.camera.x + node.x * this.camera.scale,
      y: rect.top + this.cssHeight / 2 + this.camera.y + node.y * this.camera.scale,
    };
  }

  pinnedPositions() {
    const out = {};
    for (const [id, node] of this.nodes) {
      if (node.pinned) out[id] = { x: node.x, y: node.y };
    }
    return out;
  }

  // ---- simulation ----

  _tick = () => {
    if (this.alpha > 0.004) this._step();
    if (this._fitUntilAlpha) {
      this.fitToContent();
      if (this.alpha <= this._fitUntilAlpha) this._fitUntilAlpha = 0;
    }
    if (this.panTarget) {
      this.camera.x += (this.panTarget.x - this.camera.x) * 0.16;
      this.camera.y += (this.panTarget.y - this.camera.y) * 0.16;
      const settled =
        Math.abs(this.panTarget.x - this.camera.x) < 0.4 &&
        Math.abs(this.panTarget.y - this.camera.y) < 0.4;
      if (settled) this.panTarget = null;
    }
    this._draw();
    this._scheduleFrame();
  };

  _step() {
    // Nodes hidden by a species/relationship filter are never drawn or
    // interactable, so simulating them is pure waste -- this is an O(n²)
    // loop below, and every hidden node still counted toward that n.
    const ids = [...this.nodes.keys()].filter((id) => this.isMoteVisible(id));

    for (let i = 0; i < ids.length; i++) {
      const a = this.nodes.get(ids[i]);
      for (let j = i + 1; j < ids.length; j++) {
        const b = this.nodes.get(ids[j]);
        let dx = a.x - b.x, dy = a.y - b.y;
        let distSq = dx * dx + dy * dy;
        if (distSq < 1) distSq = 1;
        const dist = Math.sqrt(distSq);
        const force = REPULSION / distSq;
        const fx = (dx / dist) * force, fy = (dy / dist) * force;
        if (!a.pinned) { a.vx += fx; a.vy += fy; }
        if (!b.pinned) { b.vx -= fx; b.vy -= fy; }
      }
    }

    // Edges between motes of different species pull only weakly: species
    // grouping is what carries the layout, and letting every instance_of
    // edge yank its instance toward the species mote would undo it.
    for (const rel of this.relationships) {
      if (!this.isRelVisible(rel)) continue;
      const a = this.nodes.get(rel.from_id), b = this.nodes.get(rel.to_id);
      if (!a || !b) continue;
      const sameSpecies =
        (this.speciesOf.get(rel.from_id) || [])[0] ===
        (this.speciesOf.get(rel.to_id) || [])[0];
      const k = sameSpecies ? SAME_SPECIES_SPRING : CROSS_SPECIES_SPRING;
      let dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const diff = (dist - EDGE_REST_LENGTH) * k;
      const fx = (dx / dist) * diff, fy = (dy / dist) * diff;
      if (!a.pinned) { a.vx += fx; a.vy += fy; }
      if (!b.pinned) { b.vx -= fx; b.vy -= fy; }
    }

    for (const id of ids) {
      const node = this.nodes.get(id);
      if (node.pinned) { node.vx = 0; node.vy = 0; continue; }
      const anchor = this._anchorFor(id);
      node.vx += (anchor.x - node.x) * ANCHOR_PULL;
      node.vy += (anchor.y - node.y) * ANCHOR_PULL;
      node.vx *= DAMPING; node.vy *= DAMPING;
      node.x += node.vx; node.y += node.vy;
    }
    this.alpha *= 0.985;
  }

  // ---- rendering ----

  _resize = () => {
    const rect = this.canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    this.cssWidth = rect.width;
    this.cssHeight = rect.height;
    this.canvas.width = Math.round(rect.width * dpr);
    this.canvas.height = Math.round(rect.height * dpr);
    this.canvas.style.width = rect.width + "px";
    this.canvas.style.height = rect.height + "px";
    this.dpr = dpr;
    this._readColors();
  };

  /** getComputedStyle() forces a style recalc -- cheap once, wasteful if
   *  called every animation frame. Cached here and refreshed on resize
   *  rather than in _draw(); nothing today changes these at runtime (no
   *  theme toggle exists), but if one gets added later it needs to call
   *  this too, not just rely on a resize happening to fire. */
  _readColors() {
    const style = getComputedStyle(document.documentElement);
    this._bg = style.getPropertyValue("--graph-bg").trim() || "#111";
    // Label backplates need their own translucent colour rather than the
    // background with an alpha suffix appended: an unparseable fillStyle
    // is silently ignored by canvas, leaving whatever colour was set last
    // (the node's), which drew every label on a bright coloured slab.
    this._labelBg = style.getPropertyValue("--graph-label-bg").trim() || this._bg;
  }

  _draw() {
    const ctx = this.canvas.getContext("2d");
    const bg = this._bg, labelBg = this._labelBg;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, this.cssWidth, this.cssHeight);

    ctx.save();
    ctx.translate(this.cssWidth / 2 + this.camera.x, this.cssHeight / 2 + this.camera.y);
    ctx.scale(this.camera.scale, this.camera.scale);

    // Neighbour emphasis only makes sense around one mote at a time --
    // hover wins over selection, and a multi-selection doesn't try to
    // union everyone's neighbours together.
    const primaryId = this.hoverId || this.selectedId;
    const neighbours = new Set();
    if (primaryId) {
      for (const rel of this.relationships) {
        if (!this.isRelVisible(rel)) continue;
        if (rel.from_id === primaryId) neighbours.add(rel.to_id);
        if (rel.to_id === primaryId) neighbours.add(rel.from_id);
      }
    }

    this._drawEdges(ctx, bg, neighbours);
    this._drawNodes(ctx, labelBg, neighbours);
    ctx.restore();

    if (this.simActive) {
      ctx.save();
      ctx.strokeStyle = "oklch(0.78 0.13 70 / 0.5)";
      ctx.lineWidth = 2;
      ctx.setLineDash([10, 7]);
      ctx.strokeRect(4, 4, this.cssWidth - 8, this.cssHeight - 8);
      ctx.restore();
    }
  }

  /** Parallel edges between the same pair get bowed apart by an increasing
   *  offset. Straight lines would draw exactly on top of each other and
   *  read as a single relationship -- Cytoscape's bezier curve-style used
   *  to handle this for free. */
  _parallelIndex() {
    const seen = new Map();
    const index = new Map();
    for (const rel of this.relationships) {
      const key = [rel.from_id, rel.to_id].sort().join("|");
      const n = seen.get(key) || 0;
      index.set(rel.id, n);
      seen.set(key, n + 1);
    }
    return index;
  }

  /** The quadratic bezier's control point for one edge, given its
   *  endpoints and how far parallel edges between the same pair have
   *  bowed it apart. Shared by drawing and edge hit-testing so they never
   *  disagree about where the curve actually is. */
  _edgeCurve(a, b, bow) {
    const mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
    const angle = Math.atan2(b.y - a.y, b.x - a.x);
    return {
      mx, my,
      cx: mx + Math.cos(angle - Math.PI / 2) * bow,
      cy: my + Math.sin(angle - Math.PI / 2) * bow,
    };
  }

  _drawEdges(ctx, bg, neighbours) {
    const parallel = this._parallelIndex();
    const anyFocus = this.selectedIds.size > 0 || !!this.hoverId || this.selectedRelIds.size > 0;
    ctx.font = '10px "IBM Plex Sans", sans-serif';

    for (const rel of this.relationships) {
      if (!this.isRelVisible(rel)) continue;
      const a = this.nodes.get(rel.from_id), b = this.nodes.get(rel.to_id);
      if (!a || !b) continue;

      const emphasized =
        this.selectedIds.has(rel.from_id) || this.selectedIds.has(rel.to_id) ||
        rel.from_id === this.hoverId || rel.to_id === this.hoverId ||
        this.selectedRelIds.has(rel.id);
      const dimmed = anyFocus && !emphasized;
      ctx.strokeStyle = emphasized
        ? "oklch(0.80 0.13 70 / 0.95)"
        : dimmed ? "oklch(0.55 0 0 / 0.18)" : "oklch(0.55 0 0 / 0.5)";
      ctx.lineWidth = emphasized ? 1.8 : 1;

      const bow = (parallel.get(rel.id) || 0) * PARALLEL_EDGE_BOW;
      const { mx, my, cx, cy } = this._edgeCurve(a, b, bow);

      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.quadraticCurveTo(cx, cy, b.x, b.y);
      ctx.stroke();

      const tipAngle = Math.atan2(b.y - cy, b.x - cx);
      const tipX = b.x - Math.cos(tipAngle) * 8;
      const tipY = b.y - Math.sin(tipAngle) * 8;
      ctx.save();
      ctx.translate(tipX, tipY);
      ctx.rotate(tipAngle);
      ctx.fillStyle = ctx.strokeStyle;
      ctx.beginPath();
      ctx.moveTo(0, 0); ctx.lineTo(-6, -3); ctx.lineTo(-6, 3);
      ctx.closePath(); ctx.fill();
      ctx.restore();

      if (emphasized || this.camera.scale > 1.5) {
        const lx = (mx + cx) / 2, ly = (my + cy) / 2;
        const width = ctx.measureText(rel.type).width;
        ctx.fillStyle = bg;
        ctx.fillRect(lx - width / 2 - 3, ly - 7, width + 6, 13);
        ctx.fillStyle = emphasized ? "oklch(0.88 0 0)" : "oklch(0.62 0 0)";
        ctx.textAlign = "center"; ctx.textBaseline = "middle";
        ctx.fillText(rel.type, lx, ly);
      }
    }
  }

  _drawNodes(ctx, labelBg, neighbours) {
    const byId = new Map(this.motes.map((m) => [m.id, m]));
    const anyFocus = this.selectedIds.size > 0 || !!this.hoverId || this.selectedRelIds.size > 0;

    for (const [id, node] of this.nodes) {
      const mote = byId.get(id);
      if (!mote || !this.isMoteVisible(id)) continue;

      const isSelected = this.selectedIds.has(id);
      const isHover = id === this.hoverId;
      const isSource = id === this.connectSourceId;
      const related = neighbours.has(id);
      const radius = isSelected ? 9 : isHover ? 8 : 6.5;
      ctx.globalAlpha = anyFocus && !isSelected && !isHover && !related ? 0.32 : 1;

      ctx.beginPath();
      if (this.speciesIds.has(id)) {
        // Species motes are diamonds -- a type is a different kind of
        // thing from an instance and shouldn't have to be read off a label.
        const r = radius * 1.35;
        ctx.moveTo(node.x, node.y - r);
        ctx.lineTo(node.x + r, node.y);
        ctx.lineTo(node.x, node.y + r);
        ctx.lineTo(node.x - r, node.y);
        ctx.closePath();
      } else {
        ctx.arc(node.x, node.y, radius, 0, Math.PI * 2);
      }
      ctx.fillStyle = this.colorFor(id);
      ctx.fill();

      if (isSelected || isHover || isSource) {
        ctx.lineWidth = 2;
        ctx.strokeStyle = isSource ? "oklch(0.80 0.13 70)" : "oklch(0.95 0 0)";
        ctx.stroke();
      }
      if (node.pinned) {
        ctx.lineWidth = 1;
        ctx.strokeStyle = "oklch(0.95 0 0 / 0.45)";
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius + 3.5, 0, Math.PI * 2);
        ctx.stroke();
      }
      if (this.simHighlightIds.has(id)) {
        // A projected tick changed this mote's properties -- dashed so it
        // reads as "provisional" rather than the solid selection/pinned rings.
        ctx.setLineDash([3, 3]);
        ctx.lineWidth = 2;
        ctx.strokeStyle = "oklch(0.80 0.13 70 / 0.9)";
        ctx.beginPath();
        ctx.arc(node.x, node.y, radius + (node.pinned ? 6.5 : 3.5), 0, Math.PI * 2);
        ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.globalAlpha = 1;

      const showLabel =
        isSelected || isHover || related || this.camera.scale > 1.1 || this.nodes.size <= 40;
      if (showLabel && (!anyFocus || isSelected || isHover || related)) {
        this._drawLabel(ctx, labelBg, node, mote, radius, isSelected);
      }
    }
  }

  // ---- input ----
  //
  // Every gesture is resolved here rather than by a library, so there is
  // nothing to fight: plain drag on a node moves it, ctrl+drag connects,
  // drag on empty space box-selects, middle-drag pans, right-click opens a
  // context menu. Previously these competed with Cytoscape's own
  // grab/box-select handlers and needed ungrabify()/boxSelectionEnabled
  // workarounds to behave.

  _bindEvents() {
    this._observer = new ResizeObserver(() => this._resize());
    this._observer.observe(this.canvas.parentElement);
    this.canvas.addEventListener("mousedown", this._onMouseDown);
    this.canvas.addEventListener("wheel", this._onWheel, { passive: false });
    this.canvas.addEventListener("contextmenu", this._onContextMenu);
    window.addEventListener("mousemove", this._onMouseMove);
    window.addEventListener("mouseup", this._onMouseUp);
  }

  _worldAt(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    return {
      x: (clientX - rect.left - this.cssWidth / 2 - this.camera.x) / this.camera.scale,
      y: (clientY - rect.top - this.cssHeight / 2 - this.camera.y) / this.camera.scale,
    };
  }

  _normalizedRect(x1, y1, x2, y2) {
    return {
      left: Math.min(x1, x2), top: Math.min(y1, y2),
      right: Math.max(x1, x2), bottom: Math.max(y1, y2),
    };
  }

  hitTest(clientX, clientY) {
    const world = this._worldAt(clientX, clientY);
    let best = null;
    let bestDist = HIT_RADIUS / this.camera.scale;
    for (const [id, node] of this.nodes) {
      if (!this.isMoteVisible(id)) continue;
      const d = Math.hypot(node.x - world.x, node.y - world.y);
      if (d < bestDist) { bestDist = d; best = id; }
    }
    return best;
  }

  /** Same idea as hitTest() but for edges: only tried when a click hits no
   *  node, and with a tighter radius since edges are thin lines that
   *  shouldn't steal clicks meant for the canvas or a nearby node. The
   *  curve is a quadratic bezier (see _edgeCurve), so this samples it at
   *  fixed steps rather than solving for the nearest point analytically
   *  -- plenty precise for the mild bow these curves ever have. */
  _hitTestEdge(clientX, clientY) {
    const world = this._worldAt(clientX, clientY);
    const parallel = this._parallelIndex();
    let best = null;
    let bestDist = EDGE_HIT_RADIUS / this.camera.scale;
    for (const rel of this.relationships) {
      if (!this.isRelVisible(rel)) continue;
      const a = this.nodes.get(rel.from_id), b = this.nodes.get(rel.to_id);
      if (!a || !b) continue;
      const bow = (parallel.get(rel.id) || 0) * PARALLEL_EDGE_BOW;
      const { cx, cy } = this._edgeCurve(a, b, bow);
      for (let i = 0; i <= 16; i++) {
        const t = i / 16, it = 1 - t;
        const px = it * it * a.x + 2 * it * t * cx + t * t * b.x;
        const py = it * it * a.y + 2 * it * t * cy + t * t * b.y;
        const d = Math.hypot(px - world.x, py - world.y);
        if (d < bestDist) { bestDist = d; best = rel.id; }
      }
    }
    return best;
  }

  /** Every visible node whose screen position falls inside a screen-space
   *  rectangle -- shared by the live box-select preview (every qualifying
   *  mousemove) and its mouseup finalize. */
  _hitsInRect(rect) {
    const hits = [];
    for (const [id] of this.nodes) {
      if (!this.isMoteVisible(id)) continue;
      const pos = this.screenPositionOf(id);
      if (pos.x >= rect.left && pos.x <= rect.right && pos.y >= rect.top && pos.y <= rect.bottom) {
        hits.push(id);
      }
    }
    return hits;
  }

  _onMouseDown = (e) => {
    // Unconditional and first: a click or drag anywhere is the user
    // telling the camera what they want, which always wins over the
    // few-second auto-fit a freshly opened garden runs (fitWhenSettled()).
    // Without this, fitToContent() keeps resetting camera.x/y every frame
    // while the user is mid-drag, fighting their gesture -- not a real
    // unresponsiveness, but indistinguishable from one.
    this._fitUntilAlpha = 0;

    if (e.button === 1) {
      e.preventDefault(); // stop X11/GTK's middle-click autoscroll/paste
      this.panState = {
        startX: e.clientX, startY: e.clientY,
        camX: this.camera.x, camY: this.camera.y,
      };
      this.panTarget = null;
      this.canvas.style.cursor = "grabbing";
      return;
    }
    if (e.button !== 0) return; // right button is handled by contextmenu

    const id = this.hitTest(e.clientX, e.clientY);
    if (id && e.ctrlKey) {
      this.connectState = { sourceId: id };
      this.connectSourceId = id;
      this.canvas.style.cursor = "crosshair";
      return;
    }
    if (id) {
      // Dragging a mote that's part of the current multi-selection moves
      // the whole group together; dragging any other mote (including one
      // outside a smaller/different selection) only moves that one.
      const groupIds = this.selectedIds.has(id) && this.selectedIds.size > 1
        ? [...this.selectedIds]
        : [id];
      const startPositions = new Map(groupIds.map((gid) => {
        const n = this.nodes.get(gid);
        return [gid, { x: n.x, y: n.y }];
      }));
      this.dragState = {
        id, moved: false, startX: e.clientX, startY: e.clientY, groupIds, startPositions,
      };
      return;
    }
    // Empty canvas, plain left button -> box-select. Shift keeps whatever
    // was already selected and adds to it; otherwise a completed box (or a
    // plain click, handled in _onMouseUp) replaces the selection. baseIds
    // snapshots the pre-drag selection so the live preview below has
    // something to union against in additive mode.
    this.boxState = {
      startX: e.clientX, startY: e.clientY, moved: false,
      additive: e.shiftKey, baseIds: [...this.selectedIds],
    };
  };

  _onMouseMove = (e) => {
    if (this.connectState) {
      this.hoverId = this.hitTest(e.clientX, e.clientY);
      return;
    }
    if (this.dragState) {
      const dx = e.clientX - this.dragState.startX;
      const dy = e.clientY - this.dragState.startY;
      if (!this.dragState.moved && Math.hypot(dx, dy) > DRAG_THRESHOLD) {
        this.dragState.moved = true;
      }
      if (this.dragState.moved) {
        // Delta from the primary (grabbed) node's own start position is
        // applied to every group member's own start position, rather than
        // snapping each one to the cursor directly -- that would collapse
        // the whole group onto a single point instead of moving it as one.
        const world = this._worldAt(e.clientX, e.clientY);
        const startPrimary = this.dragState.startPositions.get(this.dragState.id);
        const dx = world.x - startPrimary.x;
        const dy = world.y - startPrimary.y;
        for (const gid of this.dragState.groupIds) {
          const n = this.nodes.get(gid);
          if (!n) continue;
          const start = this.dragState.startPositions.get(gid);
          n.x = start.x + dx; n.y = start.y + dy;
          n.pinned = true; n.vx = 0; n.vy = 0;
        }
        this.alpha = Math.max(this.alpha, 0.3);
      }
      return;
    }
    if (this.panState) {
      this.camera.x = this.panState.camX + (e.clientX - this.panState.startX);
      this.camera.y = this.panState.camY + (e.clientY - this.panState.startY);
      return;
    }
    if (this.boxState) {
      const dx = e.clientX - this.boxState.startX;
      const dy = e.clientY - this.boxState.startY;
      if (!this.boxState.moved && Math.hypot(dx, dy) > DRAG_THRESHOLD) {
        this.boxState.moved = true;
      }
      if (this.boxState.moved) {
        const rect = this._normalizedRect(this.boxState.startX, this.boxState.startY, e.clientX, e.clientY);
        if (this.callbacks.onBoxSelect) this.callbacks.onBoxSelect(rect);

        // Live preview: motes highlight on the canvas as they enter the
        // box, not only once the mouse is released. Deliberately does NOT
        // fire onSelectionChanged on every intermediate step -- for a
        // single hit, app.js's handler goes through the full selectMote()
        // -> refocus() path, a real async round trip to the backend. A
        // fast box-drag can queue several of those, and an earlier one
        // resolving after a later mousemove already moved on would clobber
        // the in-progress selection with stale data. select() alone is
        // synchronous and just touches render state, so it's race-free;
        // the panel catches up once, authoritatively, at mouseup.
        const hits = this._hitsInRect(rect);
        const finalIds = this.boxState.additive
          ? [...new Set([...this.boxState.baseIds, ...hits])]
          : hits;
        if (!_sameIds(finalIds, this.selectedIds)) {
          this.select(finalIds, { pan: false });
        }
      }
      return;
    }
    const id = this.hitTest(e.clientX, e.clientY);
    if (id !== this.hoverId) {
      this.hoverId = id;
      this.canvas.style.cursor = id ? "pointer" : "default";
    }
  };

  _onMouseUp = (e) => {
    if (this.connectState) {
      const { sourceId } = this.connectState;
      this.connectState = null;
      this.connectSourceId = null;
      this.canvas.style.cursor = "default";
      const targetId = this.hitTest(e.clientX, e.clientY);
      if (targetId && targetId !== sourceId && this.callbacks.onConnect) {
        this.callbacks.onConnect(sourceId, targetId);
      }
      return;
    }
    if (this.dragState) {
      const { id, moved } = this.dragState;
      this.dragState = null;
      if (moved) {
        if (this.callbacks.onPositionsChanged) this.callbacks.onPositionsChanged();
      } else if (this.callbacks.onSelect) {
        this.callbacks.onSelect(id);
      }
      return;
    }
    if (this.panState) {
      this.panState = null;
      this.canvas.style.cursor = this.hoverId ? "pointer" : "default";
      return;
    }
    if (this.boxState) {
      const { startX, startY, moved, additive, baseIds } = this.boxState;
      this.boxState = null;
      if (this.callbacks.onBoxSelect) this.callbacks.onBoxSelect(null); // clear the rect overlay

      if (!moved) {
        // A plain click on empty canvas: try an edge first, then fall back
        // to deselect. Shift+click on an edge toggles it into/out of the
        // relationship selection instead of replacing it. Shift-click on
        // truly empty space (no edge under the cursor) is a no-op rather
        // than clearing an existing selection -- shift means "add to", and
        // there's nothing under the cursor to add.
        const relId = this._hitTestEdge(e.clientX, e.clientY);
        if (relId) {
          if (additive) this.toggleRelationshipSelection(relId);
          else this.selectRelationship(relId);
          if (this.callbacks.onRelationshipsSelected) {
            this.callbacks.onRelationshipsSelected([...this.selectedRelIds]);
          }
          return;
        }
        if (!additive) {
          this.select(null);
          if (this.callbacks.onSelectionChanged) this.callbacks.onSelectionChanged([]);
        }
        return;
      }

      // The live preview in _onMouseMove has almost certainly already put
      // the right ids in place; this is the authoritative final call in
      // case no mousemove landed exactly on the release point.
      const rect = this._normalizedRect(startX, startY, e.clientX, e.clientY);
      const hits = this._hitsInRect(rect);
      const finalIds = additive ? [...new Set([...baseIds, ...hits])] : hits;
      this.select(finalIds, { pan: false });
      if (this.callbacks.onSelectionChanged) this.callbacks.onSelectionChanged(finalIds);
      return;
    }
  };

  _onContextMenu = (e) => {
    e.preventDefault();
    const id = this.hitTest(e.clientX, e.clientY);
    // Right-clicking a mote that isn't already part of the selection
    // replaces the selection with just it, so it's visually obvious what
    // the menu is about to act on.
    if (id && !this.selectedIds.has(id)) this.select(id, { pan: false });
    if (this.callbacks.onContextMenu) {
      const world = this._worldAt(e.clientX, e.clientY);
      this.callbacks.onContextMenu(e.clientX, e.clientY, id, world.x, world.y);
    }
  };

  _onWheel = (e) => {
    e.preventDefault();
    const rect = this.canvas.getBoundingClientRect();
    const cx = e.clientX - rect.left, cy = e.clientY - rect.top;
    const world = this._worldAt(e.clientX, e.clientY);
    const scale = Math.max(
      MIN_SCALE, Math.min(MAX_SCALE, this.camera.scale * Math.exp(-e.deltaY * 0.0011))
    );
    this.camera.x = cx - this.cssWidth / 2 - world.x * scale;
    this.camera.y = cy - this.cssHeight / 2 - world.y * scale;
    this.camera.scale = scale;
    this.panTarget = null;
  };

  /** Un-pin a node so it rejoins the force simulation. */
  unpin(id) {
    const node = this.nodes.get(id);
    if (node) { node.pinned = false; this.alpha = Math.max(this.alpha, 0.4); }
  }

  /** Pin a node exactly where it already is, without moving it. */
  pin(id) {
    const node = this.nodes.get(id);
    if (node) { node.pinned = true; node.vx = 0; node.vy = 0; }
  }

  /** Drop a node at a specific world position and pin it there -- used by
   *  paste and "New mote here" so the result lands under the cursor
   *  instead of at its usual hash-seeded starting spot. Only meaningful
   *  after the node already exists (i.e. after a setData() call that
   *  included it), same as pin()/unpin(). */
  placeAt(id, x, y) {
    const node = this.nodes.get(id);
    if (node) { node.x = x; node.y = y; node.pinned = true; node.vx = 0; node.vy = 0; }
  }

  /** The world point currently at the center of the viewport -- always
   *  on-screen regardless of pan, unlike pasting near wherever a mote was
   *  originally copied from (which could be anywhere after panning away). */
  viewCenterWorld() {
    return { x: -this.camera.x / this.camera.scale, y: -this.camera.y / this.camera.scale };
  }

  _drawLabel(ctx, labelBg, node, mote, radius, isSelected) {
    const full = this.callbacks.labelFor ? this.callbacks.labelFor(mote) : mote.id.slice(0, 8);
    // Truncated on the canvas only. A rule called "Kirchhoff's current law"
    // otherwise runs straight through its neighbours' labels; the full text
    // is one click away in the detail panel.
    const label = full.length > 22 ? full.slice(0, 21) + "…" : full;
    ctx.font = isSelected
      ? '600 11.5px "IBM Plex Sans", sans-serif'
      : '11px "IBM Plex Sans", sans-serif';
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    const y = node.y + radius + 5;
    const width = ctx.measureText(label).width;
    ctx.fillStyle = labelBg;
    ctx.fillRect(node.x - width / 2 - 3, y - 1, width + 6, 14);
    ctx.fillStyle = isSelected ? "oklch(0.96 0 0)" : "oklch(0.80 0 0)";
    ctx.fillText(label, node.x, y);
  }
}

window.MonossGraph = MonossGraph;
window.monossHash = hashString;
window.monossHue = hueFor;
