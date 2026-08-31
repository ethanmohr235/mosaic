// monarch/frontend/graph.js
//
// Canvas graph renderer for Monarch, the MonOSS graph window. Replaces
// Cytoscape, which was dropped for three reasons: its cose layout can't
// cluster motes by species, its fit() on every selection was the jarring
// "snap back" the graph used to do, and owning the mouse pipeline outright
// removes the gesture conflicts that made ctrl+drag-to-connect so painful
// to land on top of a library.
//
// Coordinates: nodes live in world space; the camera maps world -> screen
// as `screen = center + camera.xy + world * scale`.

// The kinds core/kinds.py recognizes. Mirrors that table's names -- the
// frontend never decides for itself what a mote is, it only decides how
// to draw and label what core says it already is.
const KIND = {
  SPECIES: "species",
  TEMPLATE: "template",
  PROPERTY_MODEL: "property_model",
  RULE: "rule",
};

// Human labels for the same, shown in the detail panel and search
// results. Keyed by kind name so an added kind is one line here.
const KIND_LABEL = {
  species: "Species",
  template: "Template",
  property_model: "Property model",
  rule: "Rule",
};

/** The mote ids of `kind`, out of the kinds map monarch/api.py returns.
 *  Lives in graph.js because it loads first and both files need it. */
function idsWithKind(kinds, kind) {
  return Object.keys(kinds || {}).filter((id) => (kinds[id] || []).includes(kind));
}

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
const ATTRACTOR_HIT_RADIUS = 22;
const NO_SPECIES = "__none__";      // the untyped group, in both the legend and the attractor map
const UNKNOWN_COLOR = "oklch(0.62 0.02 250)";

// Every force the layout runs on, as it behaves with no user overrides.
// setViewSettings() layers a garden's saved values on top of this, so an
// untouched garden lays out exactly as it always did.
const DEFAULT_FORCES = {
  repulsion: REPULSION,
  anchorPull: ANCHOR_PULL,
  edgeLength: EDGE_REST_LENGTH,
  relStrength: {},   // relationship type -> spring constant; absent = species-aware default
};

const DEFAULT_COLORS = { mode: "species", propertyKey: "", species: {} };

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

/** Species palette. Hashing a hue straight out of the id gave every species
 *  an independent random hue, which at this lightness and chroma meant a
 *  garden's tenth species routinely looked like its third -- 360 hues read
 *  as about five colours. Instead each species claims one entry of a fixed
 *  palette of deliberately separated colours: hues walk the wheel by the
 *  golden angle so consecutive entries land far apart, and each lap round
 *  the wheel changes lightness and chroma so the second time past a hue is
 *  still visibly a different colour. */
const SPECIES_HUE_STEP = 137.508;    // golden angle
const SPECIES_LAP = 12;              // entries before the tone changes
const SPECIES_TONES = [
  { l: 0.72, c: 0.13 },
  { l: 0.85, c: 0.09 },
  { l: 0.60, c: 0.16 },
  { l: 0.79, c: 0.20 },
];
const SPECIES_PALETTE_SIZE = SPECIES_LAP * SPECIES_TONES.length;

function paletteEntry(slot) {
  const index = ((slot % SPECIES_PALETTE_SIZE) + SPECIES_PALETTE_SIZE) % SPECIES_PALETTE_SIZE;
  return {
    h: (index * SPECIES_HUE_STEP + 25) % 360,
    ...SPECIES_TONES[Math.floor(index / SPECIES_LAP)],
  };
}

function paletteColor(slot, emphatic) {
  const entry = paletteEntry(slot);
  // The species mote itself is the brighter variant of its instances'
  // colour, as it was before -- same hue, one step more presence.
  const l = emphatic ? Math.min(0.9, entry.l + 0.07) : entry.l;
  const c = emphatic ? entry.c + 0.02 : entry.c;
  return `oklch(${l.toFixed(3)} ${c.toFixed(3)} ${entry.h.toFixed(1)})`;
}

// The four other windows carry these constants and these two functions
// byte for byte (mora, monastery, momentum, monolith), so a species is
// the same colour wherever it is drawn. What they cannot share is the
// slot map below: this one hangs off the graph object and theirs is a
// module-level Map, because only this window redraws 60 times a second
// and has a colour cache to invalidate.

/** Sequential ramp for the measured colour modes (connection count, a
 *  numeric property): one hue, dim at the low end and bright at the high,
 *  so the ordering reads as an ordering. Distinct hues per value -- what
 *  hueFor gives -- would say "these are different categories", which is
 *  the wrong thing to say about a number. */
function rampColor(t) {
  const clamped = Math.max(0, Math.min(1, Number.isFinite(t) ? t : 0));
  return `oklch(${(0.45 + clamped * 0.41).toFixed(3)} ${(0.04 + clamped * 0.11).toFixed(3)} 250)`;
}

function numberOr(value, fallback) {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
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
    this.simActive = false;        // a preview or history scrub is open -- draws the dashed viewport border
    this.simHighlightIds = new Set(); // motes with a property changed as of the scrubbed tick
    this.camera = { x: 0, y: 0, scale: INITIAL_SCALE };
    this.alpha = 1;
    this.panTarget = null;
    this.spread = 320;

    // Advanced view settings (monarch/frontend/app.js persists these per
    // garden). Defaults reproduce the layout and colours this graph had
    // before any of it was adjustable.
    this.forces = { ...DEFAULT_FORCES, relStrength: {} };
    this.colors = { ...DEFAULT_COLORS, species: {} };
    this.attractors = {};          // species id (or __none__) -> {x, y}
    this.attractorMode = false;    // arranging attractors: read-only, no editing gestures
    this.hoverAttractorId = null;
    this._moteById = new Map();
    this._colorCache = new Map();  // mote id -> colour, dropped whenever data or settings change
    this._colorStats = null;       // degree/numeric aggregates, computed once per invalidation
    this._speciesSlots = null;     // species id -> palette entry, rebuilt with the colour cache
    this.speciesUniverse = null;   // every species in the *garden*, not just the ones in view

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
    // What a mote is comes from core's kind table (core/kinds.py) as one
    // `kinds` map, rather than a separate id array per kind -- adding a
    // kind upstream doesn't change this payload's shape.
    this.kinds = payload.kinds || {};
    this.speciesIds = new Set(idsWithKind(this.kinds, KIND.SPECIES));
    this._moteById = new Map(this.motes.map((m) => [m.id, m]));
    this._invalidateColors();

    // Species membership is derived from the instance_of edges already in
    // the payload rather than fetched separately. In whole-garden mode the
    // species mote is always present; under a hop limit it may not be, and
    // those instances simply render uncoloured.
    this.speciesOf = new Map();
    for (const rel of this.relationships) {
      if (rel.type === "instance_of") {
        if (!this.speciesOf.has(rel.from_id)) this.speciesOf.set(rel.from_id, []);
        this.speciesOf.get(rel.from_id).push(rel.to_id);
      }
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
    // species -- rules, loose motes -- stays in the middle. A species whose
    // attractor has been dragged somewhere ignores all of that: the whole
    // group, species mote included, goes where it was put.
    if (this.speciesIds.has(moteId)) {
      const moved = this.attractors[moteId];
      if (moved) return moved;
      const angle = (hueFor(moteId) * Math.PI) / 180;
      return { x: Math.cos(angle) * this.spread * 0.45, y: Math.sin(angle) * this.spread * 0.45 };
    }
    const species = this.speciesOf.get(moteId);
    if (!species || species.length === 0) return this.attractors[NO_SPECIES] || { x: 0, y: 0 };
    return this._attractorPosition(species[0]);
  }

  // ---- attractors ----
  //
  // The point each species' cluster is pulled toward. Normally derived
  // from the species id (see _anchorFor), which keeps a garden laying out
  // the same way every time it opens but gives no say in *where* each
  // group lands. Attractor mode makes those points visible and draggable;
  // every other gesture is off while it's on, so the graph can't be
  // edited by accident while rearranging it.

  _attractorPosition(id) {
    const moved = this.attractors[id];
    if (moved) return { x: moved.x, y: moved.y };
    if (id === NO_SPECIES) return { x: 0, y: 0 };
    const angle = (hueFor(id) * Math.PI) / 180;
    return { x: Math.cos(angle) * this.spread, y: Math.sin(angle) * this.spread };
  }

  /** Every attractor worth showing a handle for: one per visible species,
   *  plus the centre group if anything on screen has no species at all.
   *  Hidden species are skipped -- their instances aren't in the layout
   *  loop, so dragging their attractor would visibly do nothing. */
  attractorPoints() {
    const points = [];
    for (const id of this.speciesIds) {
      if (!this.isMoteVisible(id)) continue;
      const mote = this._moteById.get(id);
      points.push({
        id,
        ...this._attractorPosition(id),
        color: this.speciesColorFor(id, { emphatic: true }),
        label: mote && this.callbacks.labelFor ? this.callbacks.labelFor(mote) : id.slice(0, 8),
      });
    }
    const untyped = this.motes.some(
      (m) => !this.speciesIds.has(m.id) && !(this.speciesOf.get(m.id) || []).length,
    );
    if (untyped && this.isMoteVisible(NO_SPECIES)) {
      points.push({
        id: NO_SPECIES,
        ...this._attractorPosition(NO_SPECIES),
        color: UNKNOWN_COLOR,
        label: "No species",
      });
    }
    return points;
  }

  setAttractorMode(active) {
    this.attractorMode = !!active;
    // Any half-finished gesture from before the switch would otherwise
    // finish in the other mode's rules.
    this.dragState = null;
    this.connectState = null;
    this.connectSourceId = null;
    this.boxState = null;
    this.attractorDrag = null;
    this.hoverId = null;
    this.hoverAttractorId = null;
    this.canvas.style.cursor = "default";
    this.alpha = Math.max(this.alpha, 0.4);
  }

  setAttractor(id, x, y) {
    this.attractors = { ...this.attractors, [id]: { x, y } };
    this.alpha = Math.max(this.alpha, 0.5);
  }

  resetAttractors() {
    this.attractors = {};
    this.alpha = Math.max(this.alpha, 0.6);
  }

  /** Release every pinned mote at once, so a layout change can actually
   *  reflow -- a pinned mote ignores its anchor entirely, which makes a
   *  dragged attractor look broken on a graph that's been hand-arranged. */
  unpinAll() {
    for (const node of this.nodes.values()) {
      node.pinned = false;
    }
    this.alpha = Math.max(this.alpha, 0.7);
  }

  _hitTestAttractor(clientX, clientY) {
    const world = this._worldAt(clientX, clientY);
    let best = null;
    let bestDist = ATTRACTOR_HIT_RADIUS / this.camera.scale;
    for (const point of this.attractorPoints()) {
      const d = Math.hypot(point.x - world.x, point.y - world.y);
      if (d < bestDist) { bestDist = d; best = point; }
    }
    return best;
  }

  // ---- view settings ----
  //
  // Everything the "Advanced view settings" panel controls. Only the
  // overrides a garden actually has are stored; anything absent falls
  // back to the DEFAULT_* constants above, so a garden nobody has touched
  // renders identically to before these existed.

  /** Everything setViewSettings falls back to, as a fresh object the
   *  caller can own -- app.js builds a garden's saved settings on top of
   *  this rather than repeating the numbers. */
  static defaults() {
    return {
      forces: { ...DEFAULT_FORCES, relStrength: {} },
      colors: { ...DEFAULT_COLORS, species: {} },
      attractors: {},
    };
  }

  /** The spring constant a relationship type gets when nothing is set for
   *  it. The real default is per-edge (see _step) -- this resolves it to
   *  one number for the settings panel's slider to rest at, reporting
   *  whichever case a mixed type mostly is. */
  defaultRelStrength(type) {
    let same = 0, cross = 0;
    for (const rel of this.relationships) {
      if (rel.type !== type) continue;
      const isSame =
        (this.speciesOf.get(rel.from_id) || [])[0] === (this.speciesOf.get(rel.to_id) || [])[0];
      isSame ? (same += 1) : (cross += 1);
    }
    return same > cross ? SAME_SPECIES_SPRING : CROSS_SPECIES_SPRING;
  }

  setViewSettings(settings = {}) {
    const forces = settings.forces || {};
    const colors = settings.colors || {};
    this.forces = {
      repulsion: numberOr(forces.repulsion, DEFAULT_FORCES.repulsion),
      anchorPull: numberOr(forces.anchorPull, DEFAULT_FORCES.anchorPull),
      edgeLength: numberOr(forces.edgeLength, DEFAULT_FORCES.edgeLength),
      relStrength: { ...(forces.relStrength || {}) },
    };
    this.colors = {
      mode: colors.mode || DEFAULT_COLORS.mode,
      propertyKey: colors.propertyKey || "",
      species: { ...(colors.species || {}) },
    };
    this.attractors = { ...(settings.attractors || {}) };
    this._invalidateColors();
    // Forces only take effect while the layout is running, so a settings
    // change has to wake it back up -- otherwise a slider dragged on a
    // long-settled graph appears to do nothing at all.
    this.alpha = Math.max(this.alpha, 0.5);
  }

  viewSettings() {
    return {
      forces: { ...this.forces, relStrength: { ...this.forces.relStrength } },
      colors: { ...this.colors, species: { ...this.colors.species } },
      attractors: { ...this.attractors },
    };
  }

  // ---- colour ----

  _invalidateColors() {
    this._colorCache.clear();
    this._colorStats = null;
    this._speciesSlots = null;
  }

  /** Every species in the garden, which is what the palette is claimed
   *  from -- see setSpeciesUniverse. Falls back to the ones in view,
   *  which is all this window knows before the first answer lands. */
  setSpeciesUniverse(ids) {
    this.speciesUniverse = ids && ids.length ? new Set(ids) : null;
    this._invalidateColors();
  }

  /** species id -> palette entry. Each species prefers the entry its id
   *  hashes to -- so a colour is stable across sessions and unaffected by
   *  most later additions -- and probes forward from there when that entry
   *  is already claimed, which is what stops two species sharing a colour
   *  until the palette is genuinely full. Sorted ids keep the claiming
   *  order independent of the payload's order.
   *
   *  Claimed from the *garden's* species (api_common.py::species_ids),
   *  not from the ones this view happens to contain: under a hop limit
   *  the canvas shows a subset, and a palette claimed from a subset would
   *  colour a species one way here and another way in the four other
   *  windows, which all ask the same question of the same garden. */
  _speciesSlotMap() {
    if (this._speciesSlots) return this._speciesSlots;
    const slots = new Map();
    const taken = new Set();
    for (const id of [...(this.speciesUniverse || this.speciesIds)].sort()) {
      let slot = hashString(id) % SPECIES_PALETTE_SIZE;
      for (let step = 0; step < SPECIES_PALETTE_SIZE && taken.has(slot); step++) {
        slot = (slot + 1) % SPECIES_PALETTE_SIZE;
      }
      taken.add(slot);
      slots.set(id, slot);
    }
    this._speciesSlots = slots;
    return slots;
  }

  /** The colour a node is actually drawn in -- which is species membership
   *  only in the default mode. Cached per mote because _drawNodes asks for
   *  every visible node's colour on every frame, and the measured modes
   *  below would otherwise re-derive the same answer 60 times a second. */
  colorFor(moteId) {
    let color = this._colorCache.get(moteId);
    if (color === undefined) {
      color = this._computeColor(moteId);
      this._colorCache.set(moteId, color);
    }
    return color;
  }

  _computeColor(moteId) {
    switch (this.colors.mode) {
      case "property": return this._propertyValueColor(moteId);
      case "degree": return this._degreeColor(moteId);
      case "numeric": return this._numericColor(moteId);
      default: return this._speciesModeColor(moteId);
    }
  }

  /** The colour that means "this species", whatever the active colour mode
   *  is. The legend, quick-add chips and search chips are about species
   *  membership even when the canvas is coloured by a property or a count,
   *  so they ask for this rather than colorFor(). `emphatic` is the
   *  brighter variant the species mote itself gets; a custom colour is
   *  used exactly as picked, for the species and its instances alike. */
  speciesColorFor(speciesId, { emphatic = false } = {}) {
    if (!speciesId || speciesId === NO_SPECIES) return UNKNOWN_COLOR;
    const custom = this.colors.species[speciesId];
    if (custom) return custom;
    // A species created since the last refresh has claimed nothing yet;
    // it falls back to its unprobed entry, which still gives its
    // instances a consistent colour.
    const slots = this._speciesSlotMap();
    const slot = slots.has(speciesId)
      ? slots.get(speciesId)
      : hashString(speciesId) % SPECIES_PALETTE_SIZE;
    return paletteColor(slot, emphatic);
  }

  _speciesModeColor(moteId) {
    if (this.speciesIds.has(moteId)) return this.speciesColorFor(moteId, { emphatic: true });
    const species = this.speciesOf.get(moteId);
    if (!species || species.length === 0) return UNKNOWN_COLOR;
    return this.speciesColorFor(species[0]);
  }

  /** Each distinct value of the chosen property gets its own hue, by the
   *  same hash the species colours use. Motes without the property go
   *  grey -- "not in this classification" is itself worth seeing. */
  _propertyValueColor(moteId) {
    const raw = this._propertyOf(moteId, this.colors.propertyKey);
    if (raw === undefined || raw === null || raw === "") return UNKNOWN_COLOR;
    const text = typeof raw === "string" ? raw : JSON.stringify(raw);
    return `oklch(0.72 0.13 ${hueFor(text)})`;
  }

  _degreeColor(moteId) {
    const { degree, maxDegree } = this._stats();
    if (maxDegree === 0) return rampColor(0);
    return rampColor((degree.get(moteId) || 0) / maxDegree);
  }

  _numericColor(moteId) {
    const value = this._numericValueOf(moteId);
    if (value === null) return UNKNOWN_COLOR;
    const { numericMin, numericMax } = this._stats();
    if (numericMin === null) return UNKNOWN_COLOR;
    // A single distinct value has no range to spread over; drawing it at
    // the top of the ramp beats dividing by zero.
    if (numericMax === numericMin) return rampColor(1);
    return rampColor((value - numericMin) / (numericMax - numericMin));
  }

  _propertyOf(moteId, key) {
    if (!key) return undefined;
    const mote = this._moteById.get(moteId);
    return mote && mote.properties ? mote.properties[key] : undefined;
  }

  _numericValueOf(moteId) {
    const raw = this._propertyOf(moteId, this.colors.propertyKey);
    if (raw === undefined || raw === null || raw === "") return null;
    const value = typeof raw === "number" ? raw : Number(raw);
    return Number.isFinite(value) ? value : null;
  }

  /** Aggregates the measured colour modes need: connection counts, and the
   *  numeric property's range. Computed once per data/settings change
   *  rather than per mote, let alone per frame. */
  _stats() {
    if (this._colorStats) return this._colorStats;
    const degree = new Map();
    let maxDegree = 0;
    for (const rel of this.relationships) {
      for (const id of [rel.from_id, rel.to_id]) {
        const next = (degree.get(id) || 0) + 1;
        degree.set(id, next);
        if (next > maxDegree) maxDegree = next;
      }
    }
    let numericMin = null, numericMax = null;
    if (this.colors.mode === "numeric") {
      for (const mote of this.motes) {
        const value = this._numericValueOf(mote.id);
        if (value === null) continue;
        if (numericMin === null || value < numericMin) numericMin = value;
        if (numericMax === null || value > numericMax) numericMax = value;
      }
    }
    this._colorStats = { degree, maxDegree, numericMin, numericMax };
    return this._colorStats;
  }

  /** Distinct property keys across every mote on screen -- the datalist
   *  behind the colour panel's property-key field, so picking one is a
   *  choice rather than a spelling test. */
  propertyKeys() {
    const keys = new Set();
    for (const mote of this.motes) {
      for (const key of Object.keys(mote.properties || {})) keys.add(key);
    }
    return [...keys].sort();
  }

  /** Distinct relationship types on screen, for the per-type force rows. */
  relationshipTypes() {
    return [...new Set(this.relationships.map((r) => r.type))].sort();
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

  // ---- simulation preview / history scrub ----

  setSimActive(active) {
    this.simActive = active;
  }

  setSimHighlight(ids) {
    this.simHighlightIds = ids instanceof Set ? ids : new Set(ids);
  }

  isMoteVisible(moteId) {
    if (this.hiddenSpecies.size > 0) {
      if (this.speciesIds.has(moteId)) {
        if (this.hiddenSpecies.has(moteId)) return false;
      } else {
        const species = this.speciesOf.get(moteId) || [];
        if (species.length === 0) {
          if (this.hiddenSpecies.has("__none__")) return false;
        } else if (!species.some((s) => !this.hiddenSpecies.has(s))) {
          return false;
        }
      }
    }
    return true;
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

  /** Shift+click on an edge -- adds/removes just that one relationship
   *  rather than replacing the whole selection. Still mutually exclusive
   *  with node selection. */
  toggleRelationshipSelection(id) {
    this.selectedIds = new Set();
    const next = new Set(this.selectedRelIds);
    next.has(id) ? next.delete(id) : next.add(id);
    this.selectedRelIds = next;
  }

  /** Bulk analogue of select(), for relationships -- the relation
   *  box-select's live-preview-during-drag calls this directly (no pan,
   *  no callback) for the same race-freedom reason select() already is
   *  mid-drag: see the comment in _onMouseMove's box-select branch. */
  setRelationshipSelection(ids) {
    this.selectedIds = new Set();
    this.selectedRelIds = new Set(ids);
  }

  /** The single selected id, or null if zero or several are selected --
   *  for callers (arrow-key nav) that only make sense for one at a time. */
  get selectedId() {
    return this.selectedIds.size === 1 ? [...this.selectedIds][0] : null;
  }

  resetView() {
    this.fitToContent();
  }

  /** World -> page-relative screen coordinates, the inverse of _worldAt.
   *  Shared by screenPositionOf (a single node) and _edgesInRect (arbitrary
   *  points sampled along an edge's curve). */
  _worldToScreen(x, y) {
    const rect = this.canvas.getBoundingClientRect();
    return {
      x: rect.left + this.cssWidth / 2 + this.camera.x + x * this.camera.scale,
      y: rect.top + this.cssHeight / 2 + this.camera.y + y * this.camera.scale,
    };
  }

  /** Screen coordinates of a node, relative to the page. Exists so the
   *  smoke test can aim synthetic mouse events at a node the same way a
   *  person would -- there is no DOM element to click on a canvas. */
  screenPositionOf(id) {
    const node = this.nodes.get(id);
    if (!node) return null;
    return this._worldToScreen(node.x, node.y);
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
        const force = this.forces.repulsion / distSq;
        const fx = (dx / dist) * force, fy = (dy / dist) * force;
        if (!a.pinned) { a.vx += fx; a.vy += fy; }
        if (!b.pinned) { b.vx -= fx; b.vy -= fy; }
      }
    }

    // Edges between motes of different species pull only weakly: species
    // grouping is what carries the layout, and letting every instance_of
    // edge yank its instance toward the species mote would undo it. A
    // relationship type with its own strength set in the view settings
    // opts out of that rule entirely -- an explicit number means the same
    // thing for every edge of that type, whatever it connects.
    for (const rel of this.relationships) {
      if (!this.isRelVisible(rel)) continue;
      const a = this.nodes.get(rel.from_id), b = this.nodes.get(rel.to_id);
      if (!a || !b) continue;
      const sameSpecies =
        (this.speciesOf.get(rel.from_id) || [])[0] ===
        (this.speciesOf.get(rel.to_id) || [])[0];
      const override = this.forces.relStrength[rel.type];
      const k = typeof override === "number"
        ? override
        : sameSpecies ? SAME_SPECIES_SPRING : CROSS_SPECIES_SPRING;
      let dx = b.x - a.x, dy = b.y - a.y;
      const dist = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const diff = (dist - this.forces.edgeLength) * k;
      const fx = (dx / dist) * diff, fy = (dy / dist) * diff;
      if (!a.pinned) { a.vx += fx; a.vy += fy; }
      if (!b.pinned) { b.vx -= fx; b.vy -= fy; }
    }

    for (const id of ids) {
      const node = this.nodes.get(id);
      if (node.pinned) { node.vx = 0; node.vy = 0; continue; }
      const anchor = this._anchorFor(id);
      node.vx += (anchor.x - node.x) * this.forces.anchorPull;
      node.vy += (anchor.y - node.y) * this.forces.anchorPull;
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
    // Stroke color for a directly-selected relationship (click, shift+
    // click, or relation box-select) -- kept in sync with #select-box.rel's
    // fill/border via the same --select-rel-* tokens so the canvas and the
    // DOM overlay never disagree about what color "relationship selected"
    // means.
    this._edgeSelected = style.getPropertyValue("--select-rel-edge").trim() || "oklch(0.72 0.13 235 / 0.95)";
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
    if (this.attractorMode) this._drawAttractors(ctx, labelBg);
    ctx.restore();

    // Both modes fence off the viewport the same way, in their own colour:
    // amber for a simulation preview, the relationship blue for attractor
    // mode. Either way it means "what you're looking at isn't the ordinary
    // editable graph".
    const border = this.attractorMode ? this._edgeSelected : this.simActive ? "oklch(0.74 0.10 68 / 0.5)" : null;
    if (border) {
      ctx.save();
      ctx.strokeStyle = border;
      ctx.lineWidth = 2;
      ctx.setLineDash([10, 7]);
      ctx.strokeRect(4, 4, this.cssWidth - 8, this.cssHeight - 8);
      ctx.restore();
    }
  }

  /** Attractor handles, drawn on top of the nodes they pull: a ringed
   *  crosshair in the species colour with the species name under it.
   *  Sized in screen pixels (hence the /scale) so a handle stays grabbable
   *  at any zoom, unlike the nodes themselves. */
  _drawAttractors(ctx, labelBg) {
    const s = 1 / this.camera.scale;
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (const point of this.attractorPoints()) {
      const active = point.id === this.hoverAttractorId ||
        (this.attractorDrag && this.attractorDrag.id === point.id);
      const radius = (active ? 15 : 12) * s;

      ctx.strokeStyle = point.color;
      ctx.lineWidth = (active ? 2.6 : 1.8) * s;
      ctx.setLineDash([5 * s, 4 * s]);
      ctx.beginPath();
      ctx.arc(point.x, point.y, radius, 0, Math.PI * 2);
      ctx.stroke();
      ctx.setLineDash([]);

      ctx.beginPath();
      ctx.moveTo(point.x - radius * 0.55, point.y);
      ctx.lineTo(point.x + radius * 0.55, point.y);
      ctx.moveTo(point.x, point.y - radius * 0.55);
      ctx.lineTo(point.x, point.y + radius * 0.55);
      ctx.stroke();

      ctx.font = `${(active ? 600 : 400)} ${11.5 * s}px "IBM Plex Sans", sans-serif`;
      const label = point.label.length > 22 ? point.label.slice(0, 21) + "…" : point.label;
      const width = ctx.measureText(label).width;
      const y = point.y + radius + 5 * s;
      ctx.fillStyle = labelBg;
      ctx.fillRect(point.x - width / 2 - 3 * s, y - 1 * s, width + 6 * s, 15 * s);
      ctx.fillStyle = point.color;
      ctx.fillText(label, point.x, y);
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

      // A relationship the user directly selected (click, shift+click, or
      // relation box-select) gets its own distinct color rather than the
      // amber used for "this edge touches a selected/hovered mote" -- box-
      // selecting several relationships would otherwise look identical to
      // just hovering one node.
      const relSelected = this.selectedRelIds.has(rel.id);
      const moteEmphasized =
        this.selectedIds.has(rel.from_id) || this.selectedIds.has(rel.to_id) ||
        rel.from_id === this.hoverId || rel.to_id === this.hoverId;
      const emphasized = relSelected || moteEmphasized;
      const dimmed = anyFocus && !emphasized;
      ctx.strokeStyle = relSelected
        ? this._edgeSelected
        : moteEmphasized
        ? "oklch(0.78 0.10 68 / 0.95)"
        : dimmed ? "oklch(0.55 0 0 / 0.18)" : "oklch(0.55 0 0 / 0.5)";
      ctx.lineWidth = relSelected ? 2.4 : emphasized ? 1.8 : 1;

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
    const byId = this._moteById;
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
        ctx.strokeStyle = isSource ? "oklch(0.78 0.10 68)" : "oklch(0.95 0 0)";
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
        // A scrubbed tick (a live preview or an entered history run
        // alike) changed this mote's properties -- dashed so it reads as
        // "not the live state" rather than the solid selection/pinned rings.
        ctx.setLineDash([3, 3]);
        ctx.lineWidth = 2;
        ctx.strokeStyle = "oklch(0.78 0.10 68 / 0.9)";
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

  /** Edge counterpart to _hitsInRect, for the relation box-select -- an
   *  edge is in the box if any of the 16 curve samples _hitTestEdge
   *  already takes falls inside it, so a long edge crossing the box
   *  counts even when neither endpoint does. */
  _edgesInRect(rect) {
    const hits = [];
    const parallel = this._parallelIndex();
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
        const { x, y } = this._worldToScreen(px, py);
        if (x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom) {
          hits.push(rel.id);
          break;
        }
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

    // Attractor mode owns the left button outright: you either grab a
    // handle or you do nothing. No node drag, no connect, no box-select --
    // being unable to change the graph from here is the point of the mode.
    if (this.attractorMode) {
      const hit = this._hitTestAttractor(e.clientX, e.clientY);
      if (hit) {
        this.attractorDrag = { id: hit.id };
        this.canvas.style.cursor = "grabbing";
      }
      return;
    }

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
    // Empty canvas, plain left button -> box-select. Plain drag box-selects
    // motes, replacing the mote selection; shift+drag box-selects
    // relationships instead (with a distinct highlight color), replacing
    // the relationship selection. The two are mutually exclusive, same as
    // a single click already is.
    this.boxState = {
      startX: e.clientX, startY: e.clientY, moved: false,
      mode: e.shiftKey ? "rel" : "mote",
    };
  };

  _onMouseMove = (e) => {
    if (this.attractorDrag) {
      const world = this._worldAt(e.clientX, e.clientY);
      this.setAttractor(this.attractorDrag.id, world.x, world.y);
      return;
    }
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
        if (this.callbacks.onBoxSelect) this.callbacks.onBoxSelect(rect, this.boxState.mode);

        // Live preview: motes/edges highlight on the canvas as they enter
        // the box, not only once the mouse is released. Deliberately does
        // NOT fire onSelectionChanged/onRelationshipsSelected on every
        // intermediate step -- for a single hit, app.js's handler goes
        // through a real async round trip (selectMote() -> refocus(), or
        // openRelationshipPanel/renderMultiRelationshipPanel churning the
        // DOM, or closeDetailPanel() clobbering the selection it's still
        // building via its own graph.select(null)). A fast box-drag can
        // queue several of those, and an earlier one resolving after a
        // later mousemove already moved on would stomp the in-progress
        // selection with stale data. select()/setRelationshipSelection()
        // alone are synchronous and just touch render state, so they're
        // race-free; the panel catches up once, authoritatively, at
        // mouseup.
        if (this.boxState.mode === "rel") {
          const hits = this._edgesInRect(rect);
          if (!_sameIds(hits, this.selectedRelIds)) this.setRelationshipSelection(hits);
        } else {
          const hits = this._hitsInRect(rect);
          if (!_sameIds(hits, this.selectedIds)) this.select(hits, { pan: false });
        }
      }
      return;
    }
    if (this.attractorMode) {
      const hit = this._hitTestAttractor(e.clientX, e.clientY);
      const hitId = hit ? hit.id : null;
      if (hitId !== this.hoverAttractorId) {
        this.hoverAttractorId = hitId;
        this.canvas.style.cursor = hitId ? "grab" : "default";
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
    if (this.attractorDrag) {
      const { id } = this.attractorDrag;
      this.attractorDrag = null;
      this.canvas.style.cursor = "grab";
      const point = this.attractors[id];
      if (point && this.callbacks.onAttractorMoved) {
        this.callbacks.onAttractorMoved(id, point.x, point.y);
      }
      return;
    }
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
      const { startX, startY, moved, mode } = this.boxState;
      this.boxState = null;
      if (this.callbacks.onBoxSelect) this.callbacks.onBoxSelect(null, mode); // clear the rect overlay

      if (!moved) {
        // A plain click on empty canvas: try an edge first, then fall back
        // to deselect. A click that started as a shift+drag-relations
        // gesture (mode "rel") but never moved toggles that one edge into/
        // out of the relationship selection instead of replacing it, same
        // as an ordinary shift+click -- see toggleRelationshipSelection.
        // Shift-click on truly empty space (no edge under the cursor) is a
        // no-op rather than clearing an existing selection -- shift means
        // "add to", and there's nothing under the cursor to add.
        const additive = mode === "rel";
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
      if (mode === "rel") {
        const hits = this._edgesInRect(rect);
        this.setRelationshipSelection(hits);
        if (this.callbacks.onRelationshipsSelected) this.callbacks.onRelationshipsSelected(hits);
        return;
      }
      const hits = this._hitsInRect(rect);
      this.select(hits, { pan: false });
      if (this.callbacks.onSelectionChanged) this.callbacks.onSelectionChanged(hits);
      return;
    }
  };

  _onContextMenu = (e) => {
    e.preventDefault();
    if (this.attractorMode) return;   // nothing on that menu is available in a read-only mode
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
