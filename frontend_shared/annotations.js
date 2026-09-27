/* Annotations layered over a hand-written canvas graph -- sticky notes,
 * group boxes and pen strokes (monarch/frontend/graph.js today; other
 * apps' graphs can adopt this later -- see docs/architecture.md's
 * Annotations section).
 *
 * This file is the public face and the owner of shared state: the list
 * of annotations of every kind, the selection, the current tool, and
 * every gesture. Sticky notes live here too; group boxes and pen strokes
 * are drawn, hit-tested and (for groups) constrained by their own modules,
 * which read this module's state through the `shared` object handed to
 * their install():
 *   annotation_groups.js   AnnotationGroups   boxes that hold motes
 *   annotation_ink.js      AnnotationInk      pen strokes and the eraser
 *   annotation_toolbar.js  AnnotationToolbar  the tool bar's DOM
 * Load those three before this one.
 *
 * Unlike Undo (a pure keybinding module -- the stack lives in Python),
 * annotations render *inside* the host graph's own canvas loop, so the
 * contract runs the other way: the host graph owns the canvas element and
 * every pointer listener, and calls into this module at the right points,
 * mirroring how graph.js's own `attractorMode` is a flag/behavior on the
 * Graph instance rather than an independent module stealing events.
 *
 *   Annotations.install({ api, graph, onChanged })   once, after the
 *                                                     bridge and graph exist
 *   graph.annotateMode = true; Annotations.enter()    toggle the mode
 *   Annotations.exit()
 *
 *   In the host's _draw(), inside the same ctx.save()/translate/scale
 *   block that draws nodes (every annotation is in world coordinates):
 *     Annotations.drawUnderlay(ctx);   before edges -- group boxes
 *     Annotations.draw(ctx);           after nodes -- ink, notes, previews
 *   and, after the transform is restored, Annotations.tick().
 *
 *   In the host's layout: Annotations.applyLayoutForces(graph) in its
 *   simulation step before integrating, Annotations.constrainLayout(graph)
 *   every frame, and Annotations.onMotesDropped(ids) when a mote drag ends
 *   (see annotation_groups.js).
 *
 *   While annotate mode is on, the host hands its left-button
 *   mousedown/mousemove/mouseup and its dblclick to onMouseDown/
 *   onMouseMove/onMouseUp/onDoubleClick, mirroring the existing
 *   attractorMode short-circuit, and gives onKeyDown(e) first refusal on
 *   keys (V/N/G/P/E pick a tool; Delete/Backspace, Enter, Escape act on
 *   the selection).
 *
 * The Select tool's gestures deliberately match the graph editor's
 * own: click selects, shift+click adds to the selection, drag on empty
 * canvas box-selects, dragging a selected annotation moves the whole
 * selection, and ctrl+drag from a note to a mote attaches it there (or
 * detaches it, if it's already on that mote) the way ctrl+drag between
 * motes relates them. Double-click creates a note or edits one (or a
 * group's title); a bottom-right corner resizes a note or group. A group
 * is grabbed by its title tab or border -- its interior stays clear for
 * box-selecting -- and moving it carries its member motes and any
 * canvas-anchored notes and strokes lying wholly inside it.
 *
 * `graph` must expose: _worldAt(clientX, clientY), _worldToScreen(x, y),
 * camera.scale, hitTest(clientX, clientY), hoverId, canvas, alpha,
 * callbacks, and nodes (Map of mote id -> {x, y, vx, vy, pinned}) for
 * resolving a mote-anchored note's live position and moving a group's
 * members -- plus what annotation_groups.js asks for.
 *
 * Kept in frontend_shared/ and symlinked into each app's frontend/, like
 * undo.js and inspector.js. */

const Annotations = (() => {
  "use strict";

  const DEFAULT_WIDTH = 160;
  const DEFAULT_HEIGHT = 90;
  const CORNER_RADIUS = 8;
  // Text metrics in world units, shared by the canvas renderer and the
  // textarea overlay (scaled by camera.scale there) so a note reads the
  // same size, face, and wrap whether it's being edited or not.
  const FONT_SIZE = 13;
  const LINE_HEIGHT = 18;
  const PADDING = 8;
  const FONT_FAMILY = '"IBM Plex Sans", sans-serif';
  const MIN_WIDTH = 60;
  const MIN_HEIGHT = LINE_HEIGHT + 2 * PADDING;
  // Leader line from a mote-anchored note back to its mote starts this
  // far out from the mote's centre -- just past the largest node shape
  // (a selected species diamond, 9 * 1.35) so it never draws over it.
  const LEADER_GAP = 13;
  // Where a note double-clicked onto a mote lands: its top-left corner
  // this far up-right of the mote, so the note doesn't cover the mote and
  // its leader line has room to show.
  const MOTE_NOTE_OFFSET = 24;
  // Frosted glass rather than an opaque card: the fill lets the graph
  // show through, and the pixels under a note are blurred first so what
  // shows through doesn't fight the note's own text.
  const FILL_ALPHA = 0.72;
  const BACKDROP_BLUR = 5;        // screen px
  // Below this on-screen font size a note's text is unreadable anyway,
  // so the note collapses to a small glyph marking where it is.
  const MIN_LEGIBLE_FONT = 6;     // screen px
  const GLYPH_SIZE = 11;          // screen px, the collapsed marker's width
  // Where double-click/Enter zooms to when asked to edit a collapsed
  // note -- editing text you can't read would be pointless.
  const EDIT_SCALE = 1;
  const CLOSE_BADGE_RADIUS = 9;   // screen px
  const RESIZE_GRIP = 10;         // screen px, either side of the corner
  const DRAG_THRESHOLD = 4;       // screen px
  const TOOL_KEYS = { v: "select", n: "note", g: "group", p: "pen", e: "eraser" };
  // Note and Group place one thing and hand back to Select; Pen and
  // Eraser stay picked until you choose another tool.
  const ONE_SHOT_TOOLS = new Set(["note", "group"]);
  const PEN_STORAGE_KEY = "monoss:annotation-pen";

  let api = null;
  let graph = null;
  let onChanged = () => {};

  // What annotation_groups.js and annotation_ink.js see of this module.
  // `items` is every annotation (last list_annotations() payload,
  // camelCase, updated in place as gestures move things); `notes` and
  // `strokes` below are views of it, rebuilt by _reindex().
  const shared = {
    api: null, graph: null, theme: null, items: [], active: false,
    changed: () => onChanged(),
  };
  let notes = [];
  let strokes = [];
  let active = false;        // annotate mode on/off
  let selected = new Set();  // annotation ids, any kind
  let tool = "select";
  let pen = { color: null, widthPx: 4 };  // color null = the theme's text color
  let pointerWorld = null;   // last pointer position, for the eraser's ring
  // The one gesture in progress, if any -- the same one-at-a-time shape
  // as graph.js's dragState/boxState/connectState, folded together:
  //   {kind: "drag", id, moved, startClientX, startClientY, starts: Map id -> {x, y},
  //    members: [mote id], lastDx, lastDy}     (members: of the groups being moved)
  //   {kind: "box", moved, additive, base: Set, startClientX, startClientY, rect}
  //   {kind: "connect", id, cursor: {x, y}, targetId}
  //   {kind: "resize", id, moved, startClientX, startClientY, startX, startY,
  //    startBaseW, startBaseH, startW, startH}
  //   {kind: "groupResize", id, moved, startClientX, startClientY, left, top, startW, startH}
  //   {kind: "groupDraw", moved, startClientX, startClientY, rect}
  //   {kind: "pen", points}                    (annotation_ink.js)
  //   {kind: "erase", ids: Set}
  let gesture = null;
  let editingId = null;
  // True for the rest of the current event dispatch after an edit ends.
  // frontend_shared/focus.js blurs the editor in mousedown's capture
  // phase -- before the canvas's own listener, and so onMouseDown(), ever
  // runs -- so by then editingId is already null, and this is how
  // onMouseDown still knows the click was the one that ended the edit.
  let editJustEnded = false;
  let editor = null;         // the one reusable <textarea> overlay
  // The editor's own content height (world units), measured on every
  // input -- a backstop so the note grows to whatever the textarea itself
  // needs even where its line-breaking differs from _wrapLines' by a word.
  let editorContentHeight = 0;
  let measureCtx = null;     // offscreen 2d context for text measurement
  let canBlur = false;       // this WebKit supports CanvasRenderingContext2D.filter
  const heightCache = new Map(); // note id -> {text, w, h}
  // A dark note matching the host window's chrome rather than a yellow
  // sticky: read from the shared palette tokens (see monarch/frontend/
  // style.css) once at install, with fallbacks for a host without them.
  let theme = {
    fill: "oklch(0.24 0 0)", border: "oklch(0.32 0 0)",
    text: "oklch(0.93 0 0)", faint: "oklch(0.48 0 0)",
    accent: "oklch(0.74 0.10 68)", accentSoft: "oklch(0.74 0.10 68 / 0.12)",
    leader: "oklch(0.68 0 0 / 0.55)", onColor: "oklch(0.18 0 0)",
  };

  function install(options) {
    api = options.api;
    graph = options.graph;
    onChanged = options.onChanged || (() => {});
    measureCtx = document.createElement("canvas").getContext("2d");
    measureCtx.filter = "blur(1px)";
    canBlur = measureCtx.filter === "blur(1px)";
    measureCtx.filter = "none";
    _readTheme();
    _ensureEditor();
    _loadPen();
    Object.assign(shared, { api, graph, theme });
    AnnotationInk.install(shared);
    AnnotationGroups.install(shared);
    AnnotationToolbar.install(graph.canvas.parentElement, AnnotationGroups.autoColor, AnnotationInk.WIDTHS, {
      onTool: setTool, onColor: _applyColor, onWidth: _setPenWidth,
    });
  }

  function isActive() {
    return active;
  }

  function hasNotes() {
    return notes.length > 0;
  }

  function hasContent() {
    return shared.items.length > 0;
  }

  function selectedIds() {
    return [...selected];
  }

  function currentTool() {
    return tool;
  }

  async function enter() {
    active = shared.active = true;
    tool = "select";
    AnnotationToolbar.show();
    await refresh();
  }

  function exit() {
    active = shared.active = false;
    _stopEditing({ save: true });
    AnnotationGroups.stopEditing({ save: true });
    _endGesture();
    selected.clear();
    tool = "select";
    AnnotationToolbar.hide();
  }

  async function refresh() {
    shared.items = await api.list_annotations();
    _reindex();
    const ids = new Set(shared.items.map((n) => n.id));
    for (const id of selected) if (!ids.has(id)) selected.delete(id);
    if (editingId != null && !ids.has(editingId)) _stopEditing({ save: false });
    _changed();
  }

  function _reindex() {
    notes = shared.items.filter((a) => a.type === "sticky_note");
    strokes = shared.items.filter((a) => a.type === "stroke");
  }

  function _byId(id) {
    return shared.items.find((a) => a.id === id);
  }

  /** Anything visible changed: tell the host, and bring the tool bar's
   *  color row in line with the new selection. */
  function _changed() {
    if (active) _syncToolbar();
    onChanged();
  }

  // ---- tools -------------------------------------------------------------

  function setTool(next) {
    if (!AnnotationToolbar.TOOLS.some((t) => t.id === next)) return;
    _endGesture();
    tool = next;
    // A tool that makes something starts from a clean slate -- otherwise
    // the color row would be coloring an old selection, not the new ink.
    if (next !== "select") selected.clear();
    graph.canvas.style.cursor = _toolCursor();
    _changed();
  }

  function _toolCursor() {
    if (tool === "pen" || tool === "group") return "crosshair";
    if (tool === "note") return "cell";
    if (tool === "eraser") return "none"; // draw() rings the pointer instead
    return "default";
  }

  function _loadPen() {
    try {
      const saved = JSON.parse(localStorage.getItem(PEN_STORAGE_KEY) || "null");
      if (saved && AnnotationInk.WIDTHS.includes(saved.widthPx)) pen = { color: saved.color || null, widthPx: saved.widthPx };
    } catch (_) { /* the default pen, then */ }
  }

  function _savePen() {
    try { localStorage.setItem(PEN_STORAGE_KEY, JSON.stringify(pen)); } catch (_) { /* per-viewer nicety only */ }
  }

  function _setPenWidth(px) {
    pen.widthPx = px;
    _savePen();
    _syncToolbar();
  }

  /** Groups and strokes in the selection -- what the color row recolors
   *  when there are any. */
  function _colorTargets() {
    return shared.items.filter((a) => selected.has(a.id) && (a.type === "group" || a.type === "stroke"));
  }

  function _applyColor(color, { final }) {
    const targets = _colorTargets();
    if (targets.length === 0) {
      pen.color = color;
      if (final) _savePen();
      _syncToolbar();
      return;
    }
    for (const item of targets) item.color = color;
    if (final) api.recolor_annotations(targets.map((t) => t.id), color).then(() => _changed());
    else onChanged();
  }

  function _syncToolbar() {
    const targets = _colorTargets();
    const first = targets[0];
    let color = pen.color, auto = theme.text;
    if (first) {
      color = first.color;
      auto = first.type === "group" ? AnnotationGroups.colorOf(first) : theme.text;
    }
    AnnotationToolbar.sync({
      tool, widthPx: pen.widthPx, color, autoColor: auto,
      target: first ? "selection" : "pen",
      showColors: tool === "pen" || targets.length > 0,
      showWidths: tool === "pen",
    });
  }

  function _readTheme() {
    const style = getComputedStyle(document.documentElement);
    const token = (name) => style.getPropertyValue(name).trim();
    theme = {
      fill: token("--raised") || theme.fill,
      border: token("--border") || theme.border,
      text: token("--fg") || theme.text,
      faint: token("--faint") || theme.faint,
      accent: token("--accent") || theme.accent,
      accentSoft: token("--accent-soft") || theme.accentSoft,
      leader: theme.leader,
      onColor: theme.onColor,
    };
  }

  // ---- geometry --------------------------------------------------------

  /** A note's live world position (its centre, as stored). Mote-anchored
   *  notes store an offset from their mote and follow it as it moves;
   *  once the mote is gone (deleted -- see core/db.py's annotations table
   *  docstring for why the row survives that), there is no way to recover
   *  where the mote last was -- core/annotations.py deliberately never
   *  observes mote deletes -- so the stored offset is reused as an
   *  absolute position instead. That keeps the note from vanishing or
   *  crashing; it's an approximation, not a true "last known spot". */
  function _resolve(note) {
    if (note.anchorMode === "mote") {
      const moteNode = graph.nodes.get(note.moteId);
      if (moteNode) {
        return { x: moteNode.x + note.x, y: moteNode.y + note.y, orphaned: false };
      }
      return { x: note.x, y: note.y, orphaned: true };
    }
    return { x: note.x, y: note.y, orphaned: false };
  }

  function _baseSize(note) {
    return { w: note.width || DEFAULT_WIDTH, h: note.height || DEFAULT_HEIGHT };
  }

  /** Height the note's text needs at width w -- the live editor text
   *  while it's being edited, so the note grows as you type. Cached per
   *  note on (text, width) since draw() asks every frame. */
  function _contentHeight(note, w) {
    const editing = note.id === editingId;
    const text = editing ? editor.value : (note.text || "");
    const cached = heightCache.get(note.id);
    let h;
    if (cached && cached.text === text && cached.w === w) {
      h = cached.h;
    } else {
      measureCtx.font = `${FONT_SIZE}px ${FONT_FAMILY}`;
      h = _wrapLines(measureCtx, text, w - 2 * PADDING).length * LINE_HEIGHT + 2 * PADDING;
      heightCache.set(note.id, { text, w, h });
    }
    return editing ? Math.max(h, editorContentHeight) : h;
  }

  /** The drawn box. A note's stored width/height is the size it was
   *  given (default or resized); it's drawn taller whenever its text needs
   *  more, growing downward from a fixed top edge, so a note never
   *  scrolls or clips. */
  function _rectOf(note) {
    const pos = _resolve(note);
    const base = _baseSize(note);
    const left = pos.x - base.w / 2;
    const top = pos.y - base.h / 2;
    const h = Math.max(base.h, _contentHeight(note, base.w));
    return { left, top, w: base.w, h, orphaned: pos.orphaned };
  }

  function _isCollapsed() {
    return FONT_SIZE * graph.camera.scale < MIN_LEGIBLE_FONT;
  }

  /** The collapsed marker's box: a fixed on-screen size, centred where
   *  the full note would be. */
  function _glyphRectOf(note) {
    const rect = _rectOf(note);
    const s = 1 / graph.camera.scale;
    const w = GLYPH_SIZE * s, h = GLYPH_SIZE * 0.8 * s;
    return {
      left: rect.left + rect.w / 2 - w / 2, top: rect.top + rect.h / 2 - h / 2,
      w, h, orphaned: rect.orphaned,
    };
  }

  /** What's on screen for a note right now: the note, or its marker. */
  function _shownRectOf(note) {
    return _isCollapsed() && note.id !== editingId ? _glyphRectOf(note) : _rectOf(note);
  }

  function _hitTest(worldX, worldY) {
    // Later notes (higher z / more recently created) sit on top.
    for (let i = notes.length - 1; i >= 0; i--) {
      const note = notes[i];
      const rect = _shownRectOf(note);
      if (worldX >= rect.left && worldX <= rect.left + rect.w
        && worldY >= rect.top && worldY <= rect.top + rect.h) {
        return note;
      }
    }
    return null;
  }

  function _hitTestCloseBadge(worldX, worldY) {
    if (_isCollapsed()) return null;
    const s = 1 / graph.camera.scale;
    for (let i = notes.length - 1; i >= 0; i--) {
      const note = notes[i];
      const rect = _rectOf(note);
      const cx = rect.left + rect.w, cy = rect.top;
      if (Math.hypot(worldX - cx, worldY - cy) <= CLOSE_BADGE_RADIUS * s * 1.4) {
        return note;
      }
    }
    return null;
  }

  function _hitTestResizeGrip(worldX, worldY) {
    if (_isCollapsed()) return null;
    const g = RESIZE_GRIP / graph.camera.scale;
    for (let i = notes.length - 1; i >= 0; i--) {
      const note = notes[i];
      const rect = _rectOf(note);
      const cx = rect.left + rect.w, cy = rect.top + rect.h;
      if (Math.abs(worldX - cx) <= g && Math.abs(worldY - cy) <= g) return note;
    }
    return null;
  }

  function _rectsOverlap(a, b) {
    return a.left <= b.left + b.w && b.left <= a.left + a.w
      && a.top <= b.top + b.h && b.top <= a.top + a.h;
  }

  /** The point on a note's edge closest to (x, y) -- where a leader or a
   *  ctrl+drag preview line meets the note. */
  function _nearestEdgePoint(rect, x, y) {
    return {
      x: Math.max(rect.left, Math.min(x, rect.left + rect.w)),
      y: Math.max(rect.top, Math.min(y, rect.top + rect.h)),
    };
  }

  // ---- drawing -----------------------------------------------------------

  /** Group boxes, under the host's edges and nodes. */
  function drawUnderlay(ctx) {
    if (!graph) return; // the host's frame loop can start before install()
    AnnotationGroups.draw(ctx, selected);
    if (gesture && gesture.kind === "groupDraw" && gesture.moved) AnnotationGroups.drawDraft(ctx, gesture.rect);
  }

  function draw(ctx) {
    if (!graph) return;
    // Ink under the notes, so a note always reads over a scribble.
    for (const stroke of strokes) AnnotationInk.drawStroke(ctx, stroke, selected.has(stroke.id));
    // Leaders first, so a note overlapping another note's line covers it.
    for (const note of notes) _drawLeader(ctx, note);
    const collapsed = _isCollapsed();
    for (const note of notes) {
      if (note.id === editingId) continue; // the textarea overlay covers it
      if (collapsed) _drawGlyph(ctx, note);
      else _drawNote(ctx, note);
    }
    if (!gesture) {
      if (active && tool === "eraser" && pointerWorld) AnnotationInk.drawEraserCursor(ctx, pointerWorld);
      return;
    }
    if (gesture.kind === "connect") _drawConnectPreview(ctx);
    if (gesture.kind === "box" && gesture.moved) _drawBox(ctx, gesture.rect);
    if (gesture.kind === "pen") AnnotationInk.drawLive(ctx, gesture, pen.color, pen.widthPx);
    if (gesture.kind === "erase" && pointerWorld) AnnotationInk.drawEraserCursor(ctx, pointerWorld);
  }

  /** The one visual difference between a mote-anchored note and a
   *  canvas-anchored one: a faint dashed line from the note's nearest
   *  edge back to the mote it follows. Still drawn while the note is being
   *  edited, and skipped when the mote is gone (orphaned -- the dashed
   *  border says that instead) or filtered out of view. */
  function _drawLeader(ctx, note) {
    if (note.anchorMode !== "mote") return;
    const moteNode = graph.nodes.get(note.moteId);
    if (!moteNode) return;
    if (graph.isMoteVisible && !graph.isMoteVisible(note.moteId)) return;
    const edge = _nearestEdgePoint(_shownRectOf(note), moteNode.x, moteNode.y);
    const dist = Math.hypot(edge.x - moteNode.x, edge.y - moteNode.y);
    if (dist <= LEADER_GAP) return; // the note already sits on its mote
    const ux = (edge.x - moteNode.x) / dist, uy = (edge.y - moteNode.y) / dist;
    const s = 1 / graph.camera.scale;

    ctx.save();
    ctx.strokeStyle = theme.leader;
    ctx.lineWidth = s;
    ctx.setLineDash([3 * s, 3 * s]);
    ctx.beginPath();
    ctx.moveTo(moteNode.x + ux * LEADER_GAP, moteNode.y + uy * LEADER_GAP);
    ctx.lineTo(edge.x, edge.y);
    ctx.stroke();
    ctx.restore();
  }

  function _drawConnectPreview(ctx) {
    const note = notes.find((n) => n.id === gesture.id);
    if (!note) return;
    const targetNode = gesture.targetId ? graph.nodes.get(gesture.targetId) : null;
    const to = targetNode || gesture.cursor;
    if (!to) return;
    const from = _nearestEdgePoint(_shownRectOf(note), to.x, to.y);
    const s = 1 / graph.camera.scale;
    ctx.save();
    ctx.strokeStyle = theme.accent;
    ctx.lineWidth = 1.5 * s;
    ctx.setLineDash([4 * s, 3 * s]);
    ctx.beginPath();
    ctx.moveTo(from.x, from.y);
    ctx.lineTo(to.x, to.y);
    ctx.stroke();
    ctx.restore();
  }

  function _drawBox(ctx, rect) {
    const s = 1 / graph.camera.scale;
    ctx.save();
    ctx.fillStyle = theme.accentSoft;
    ctx.strokeStyle = theme.accent;
    ctx.lineWidth = s;
    ctx.setLineDash([4 * s, 3 * s]);
    ctx.fillRect(rect.left, rect.top, rect.w, rect.h);
    ctx.strokeRect(rect.left, rect.top, rect.w, rect.h);
    ctx.restore();
  }

  /** A zoomed-out note: a small page-with-lines marker at a fixed screen
   *  size, quiet enough not to compete with the motes. */
  function _drawGlyph(ctx, note) {
    const g = _glyphRectOf(note);
    const s = 1 / graph.camera.scale;
    const isSelected = selected.has(note.id);
    ctx.save();
    ctx.globalAlpha = isSelected ? 1 : 0.75;
    ctx.fillStyle = theme.fill;
    ctx.strokeStyle = isSelected ? theme.accent : theme.faint;
    ctx.lineWidth = s;
    if (g.orphaned) ctx.setLineDash([2 * s, 2 * s]);
    _roundRect(ctx, g.left, g.top, g.w, g.h, 2 * s);
    ctx.fill();
    ctx.stroke();
    ctx.setLineDash([]);
    ctx.beginPath();
    for (const frac of [0.35, 0.65]) {
      const y = g.top + g.h * frac;
      ctx.moveTo(g.left + 2.5 * s, y);
      ctx.lineTo(g.left + g.w - (frac > 0.5 ? 4.5 : 2.5) * s, y);
    }
    ctx.stroke();
    ctx.restore();
  }

  /** Blur whatever the canvas has already drawn under the note's shape,
   *  in place -- the canvas-side equivalent of CSS backdrop-filter (the
   *  editor overlay uses the real thing). Works in device pixels with the
   *  transform reset, since drawImage from the canvas itself reads raw
   *  pixels; the clip set in world space still applies. */
  function _blurBackdrop(ctx, rect) {
    const m = ctx.getTransform();
    const dpr = window.devicePixelRatio || 1;
    const pad = BACKDROP_BLUR * dpr * 2;
    const canvas = ctx.canvas;
    const x0 = Math.max(0, Math.floor(m.a * rect.left + m.e - pad));
    const y0 = Math.max(0, Math.floor(m.d * rect.top + m.f - pad));
    const x1 = Math.min(canvas.width, Math.ceil(m.a * (rect.left + rect.w) + m.e + pad));
    const y1 = Math.min(canvas.height, Math.ceil(m.d * (rect.top + rect.h) + m.f + pad));
    if (x1 <= x0 || y1 <= y0) return;
    ctx.save();
    _roundRect(ctx, rect.left, rect.top, rect.w, rect.h, CORNER_RADIUS);
    ctx.clip();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.filter = `blur(${BACKDROP_BLUR * dpr}px)`;
    ctx.drawImage(canvas, x0, y0, x1 - x0, y1 - y0, x0, y0, x1 - x0, y1 - y0);
    ctx.restore();
  }

  function _drawNote(ctx, note) {
    const rect = _rectOf(note);
    const s = 1 / graph.camera.scale;
    const isSelected = selected.has(note.id);

    if (canBlur) _blurBackdrop(ctx, rect);
    ctx.save();
    ctx.fillStyle = note.color || theme.fill;
    ctx.strokeStyle = isSelected ? theme.accent : theme.border;
    ctx.lineWidth = (isSelected ? 1.5 : 1) * s;
    if (rect.orphaned) ctx.setLineDash([6 * s, 4 * s]);
    _roundRect(ctx, rect.left, rect.top, rect.w, rect.h, CORNER_RADIUS);
    ctx.globalAlpha = FILL_ALPHA;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.save();
    ctx.clip(); // belt and braces: the box already grows to fit its text
    ctx.fillStyle = theme.text;
    ctx.textAlign = "left";
    // Centred in an 18px line box, the way CSS lays out the textarea's
    // lines -- "top" would sit each line a couple of pixels higher than
    // it does while editing.
    ctx.textBaseline = "middle";
    ctx.font = `${FONT_SIZE}px ${FONT_FAMILY}`;
    const lines = _wrapLines(ctx, note.text || "", rect.w - 2 * PADDING);
    lines.forEach((line, i) => {
      ctx.fillText(line, rect.left + PADDING, rect.top + PADDING + LINE_HEIGHT * (i + 0.5));
    });
    ctx.restore();

    if (active) {
      // Resize grip: two short diagonals tucked into the bottom-right corner.
      const gx = rect.left + rect.w - 4 * s, gy = rect.top + rect.h - 4 * s;
      ctx.strokeStyle = theme.faint;
      ctx.lineWidth = s;
      ctx.beginPath();
      ctx.moveTo(gx - 7 * s, gy); ctx.lineTo(gx, gy - 7 * s);
      ctx.moveTo(gx - 3 * s, gy); ctx.lineTo(gx, gy - 3 * s);
      ctx.stroke();

      const cx = rect.left + rect.w, cy = rect.top;
      ctx.beginPath();
      ctx.arc(cx, cy, CLOSE_BADGE_RADIUS * s, 0, Math.PI * 2);
      ctx.fillStyle = theme.border;
      ctx.fill();
      ctx.strokeStyle = theme.text;
      ctx.lineWidth = s;
      ctx.beginPath();
      ctx.moveTo(cx - 3 * s, cy - 3 * s);
      ctx.lineTo(cx + 3 * s, cy + 3 * s);
      ctx.moveTo(cx + 3 * s, cy - 3 * s);
      ctx.lineTo(cx - 3 * s, cy + 3 * s);
      ctx.stroke();
    }
    ctx.restore();
  }

  function _roundRect(ctx, x, y, w, h, r) {
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  /** Wraps the way the textarea does: explicit newlines are kept
   *  (blank lines included), lines break between words, and a single word
   *  wider than the note breaks mid-word rather than overflowing. */
  function _wrapLines(ctx, text, maxWidth) {
    const out = [];
    for (const paragraph of text.split("\n")) {
      let line = "";
      for (const word of paragraph.split(" ")) {
        const attempt = line ? `${line} ${word}` : word;
        if (ctx.measureText(attempt).width <= maxWidth) { line = attempt; continue; }
        if (line) out.push(line);
        line = word;
        while (line.length > 1 && ctx.measureText(line).width > maxWidth) {
          let cut = line.length - 1;
          while (cut > 1 && ctx.measureText(line.slice(0, cut)).width > maxWidth) cut--;
          out.push(line.slice(0, cut));
          line = line.slice(cut);
        }
      }
      out.push(line);
    }
    return out;
  }

  // ---- pointer handling ---------------------------------------------------
  // The host only calls these while annotate mode is on, and only with
  // the left button (middle-drag pan and wheel zoom stay the host's).

  function onMouseDown(e) {
    if (e.button !== 0) return false;
    const world = graph._worldAt(e.clientX, e.clientY);
    pointerWorld = world;

    // A click away from a note or group title being edited only finishes
    // that edit -- the same "click away to unfocus" every text field gets
    // from frontend_shared/focus.js -- rather than also starting whatever
    // the tool would do. Grabbing a note still works in one click.
    const wasEditing = editingId != null || editJustEnded
      || AnnotationGroups.isEditing() || AnnotationGroups.editJustEndedNow();
    if (wasEditing && !(tool === "select" && _hitTest(world.x, world.y))) {
      _stopEditing({ save: true });
      AnnotationGroups.stopEditing({ save: true });
      return true;
    }

    if (tool === "pen") {
      gesture = AnnotationInk.begin(world);
      return true;
    }
    if (tool === "eraser") {
      gesture = { kind: "erase", ids: new Set() };
      _eraseAt(world);
      return true;
    }
    if (tool === "group") {
      gesture = { kind: "groupDraw", moved: false, startClientX: e.clientX, startClientY: e.clientY, rect: null };
      return true;
    }
    if (tool === "note") {
      e.preventDefault(); // keep the new note's editor focused once it opens
      _createNoteAt(e.clientX, e.clientY, world);
      setTool("select");
      return true;
    }
    return _selectMouseDown(e, world);
  }

  function _selectMouseDown(e, world) {
    const closeHit = _hitTestCloseBadge(world.x, world.y);
    if (closeHit) {
      _deleteItems([closeHit.id]);
      return true;
    }

    const gripHit = _hitTestResizeGrip(world.x, world.y);
    if (gripHit) {
      const rect = _rectOf(gripHit);
      const base = _baseSize(gripHit);
      selected = new Set([gripHit.id]);
      gesture = {
        kind: "resize", id: gripHit.id, moved: false,
        startClientX: e.clientX, startClientY: e.clientY,
        startX: gripHit.x, startY: gripHit.y,
        startBaseW: base.w, startBaseH: base.h,
        // Resizing starts from the size you can see -- a note already
        // grown to fit its text resizes from that height, not the smaller
        // stored one, or the grip would do nothing until you passed it.
        startW: rect.w, startH: rect.h,
      };
      return true;
    }

    const noteHit = _hitTest(world.x, world.y);
    if (noteHit && (e.ctrlKey || e.metaKey)) {
      gesture = { kind: "connect", id: noteHit.id, cursor: world, targetId: null };
      graph.canvas.style.cursor = "crosshair";
      return true;
    }

    const groupGrip = noteHit ? null : AnnotationGroups.hitTestResizeGrip(world.x, world.y);
    if (groupGrip) {
      const rect = AnnotationGroups.rectOf(groupGrip);
      selected = new Set([groupGrip.id]);
      gesture = {
        kind: "groupResize", id: groupGrip.id, moved: false,
        startClientX: e.clientX, startClientY: e.clientY,
        left: rect.left, top: rect.top, startW: rect.w, startH: rect.h,
      };
      _changed();
      return true;
    }

    const hit = noteHit
      || AnnotationInk.hitTest(strokes, world.x, world.y)
      || AnnotationGroups.hitTest(world.x, world.y);
    if (hit) {
      if (e.shiftKey) {
        if (selected.has(hit.id)) {
          selected.delete(hit.id);
          _changed();
          return true;
        }
        selected.add(hit.id);
      } else if (!selected.has(hit.id)) {
        selected = new Set([hit.id]);
      }
      gesture = _dragGesture(hit.id, e);
      _changed();
      return true;
    }

    gesture = {
      kind: "box", moved: false, additive: e.shiftKey,
      base: e.shiftKey ? new Set(selected) : new Set(),
      startClientX: e.clientX, startClientY: e.clientY, rect: null,
    };
    return true;
  }

  /** Dragging an annotation in the selection moves the whole selection,
   *  same as dragging a mote in graph.js's multi-selection. A group in it
   *  brings its member motes along, and any canvas-anchored note or
   *  stroke lying wholly inside the box. */
  function _dragGesture(id, e) {
    const moving = shared.items.filter((a) => selected.has(a.id));
    const carried = new Set(moving);
    const members = [];
    for (const group of moving.filter((a) => a.type === "group")) {
      members.push(...AnnotationGroups.membersOf(group));
      const box = AnnotationGroups.rectOf(group);
      for (const item of shared.items) {
        if (carried.has(item) || !_isLoose(item)) continue;
        if (_contains(box, _boundsOf(item))) carried.add(item);
      }
    }
    return {
      kind: "drag", id, moved: false,
      startClientX: e.clientX, startClientY: e.clientY,
      starts: new Map([...carried].map((a) => [a.id, { x: a.x, y: a.y }])),
      members, lastDx: 0, lastDy: 0,
    };
  }

  /** A note or stroke pinned to the canvas rather than to a mote -- what
   *  a moving group can carry. */
  function _isLoose(item) {
    return item.type === "stroke" || (item.type === "sticky_note" && item.anchorMode === "world");
  }

  function _boundsOf(item) {
    if (item.type === "stroke") return AnnotationInk.boundsOf(item);
    if (item.type === "group") return AnnotationGroups.rectOf(item);
    return _shownRectOf(item);
  }

  function _contains(outer, inner) {
    return inner.left >= outer.left && inner.top >= outer.top
      && inner.left + inner.w <= outer.left + outer.w && inner.top + inner.h <= outer.top + outer.h;
  }

  function onMouseMove(e) {
    const world = graph._worldAt(e.clientX, e.clientY);
    pointerWorld = world;
    if (!gesture) {
      _updateHoverCursor(e);
      return active;
    }
    const pastThreshold = Math.hypot(
      e.clientX - (gesture.startClientX ?? e.clientX),
      e.clientY - (gesture.startClientY ?? e.clientY),
    ) > DRAG_THRESHOLD;

    if (gesture.kind === "drag") {
      if (!gesture.moved && pastThreshold) gesture.moved = true;
      if (!gesture.moved) return true;
      const startWorld = graph._worldAt(gesture.startClientX, gesture.startClientY);
      const dx = world.x - startWorld.x, dy = world.y - startWorld.y;
      for (const item of shared.items) {
        const start = gesture.starts.get(item.id);
        if (!start) continue;
        item.x = start.x + dx;
        item.y = start.y + dy;
      }
      // Members move by this step's delta rather than to start + total:
      // the layout keeps nudging them between mouse events, and pinning
      // them to a fixed offset would fight it.
      const stepX = dx - gesture.lastDx, stepY = dy - gesture.lastDy;
      for (const moteId of gesture.members) {
        const node = graph.nodes.get(moteId);
        if (node) { node.x += stepX; node.y += stepY; }
      }
      gesture.lastDx = dx;
      gesture.lastDy = dy;
      if (gesture.members.length) graph.alpha = Math.max(graph.alpha, 0.3);
    } else if (gesture.kind === "box") {
      if (!gesture.moved && pastThreshold) gesture.moved = true;
      if (!gesture.moved) return true;
      gesture.rect = _dragRect(gesture, world);
      // Live, like graph.js's box-select: annotations light up as the box
      // reaches them, not only on release. Notes and strokes need only
      // touch the box; a group, being big, must be wholly inside it.
      selected = new Set(gesture.base);
      for (const item of shared.items) {
        const bounds = _boundsOf(item);
        const hit = item.type === "group" ? _contains(gesture.rect, bounds) : _rectsOverlap(bounds, gesture.rect);
        if (hit) selected.add(item.id);
      }
      _changed();
    } else if (gesture.kind === "connect") {
      gesture.cursor = world;
      gesture.targetId = graph.hitTest(e.clientX, e.clientY);
      graph.hoverId = gesture.targetId; // the target mote swells, as in a mote connect
    } else if (gesture.kind === "resize") {
      if (!gesture.moved && pastThreshold) gesture.moved = true;
      if (!gesture.moved) return true;
      const note = notes.find((n) => n.id === gesture.id);
      if (!note) return true;
      const scale = graph.camera.scale;
      const w = Math.max(MIN_WIDTH, gesture.startW + (e.clientX - gesture.startClientX) / scale);
      const h = Math.max(MIN_HEIGHT, gesture.startH + (e.clientY - gesture.startClientY) / scale);
      // Stored by centre, so keeping the top-left corner put means the
      // centre moves by half the size change.
      note.width = w;
      note.height = h;
      note.x = gesture.startX + (w - gesture.startBaseW) / 2;
      note.y = gesture.startY + (h - gesture.startBaseH) / 2;
    } else if (gesture.kind === "groupResize") {
      if (!gesture.moved && pastThreshold) gesture.moved = true;
      if (!gesture.moved) return true;
      const group = _byId(gesture.id);
      if (!group) return true;
      const scale = graph.camera.scale;
      const min = AnnotationGroups.MIN_SIZE;
      group.width = Math.max(min, gesture.startW + (e.clientX - gesture.startClientX) / scale);
      group.height = Math.max(min, gesture.startH + (e.clientY - gesture.startClientY) / scale);
      group.x = gesture.left + group.width / 2;
      group.y = gesture.top + group.height / 2;
      graph.alpha = Math.max(graph.alpha, 0.3);
    } else if (gesture.kind === "groupDraw") {
      if (!gesture.moved && pastThreshold) gesture.moved = true;
      if (gesture.moved) gesture.rect = _dragRect(gesture, world);
    } else if (gesture.kind === "pen") {
      AnnotationInk.extend(gesture, world);
    } else if (gesture.kind === "erase") {
      _eraseAt(world);
    }
    return true;
  }

  function _dragRect(g, world) {
    const start = graph._worldAt(g.startClientX, g.startClientY);
    return {
      left: Math.min(start.x, world.x), top: Math.min(start.y, world.y),
      w: Math.abs(world.x - start.x), h: Math.abs(world.y - start.y),
    };
  }

  /** The eraser takes strokes off the canvas as it touches them; the one
   *  save (and undo step) waits for mouseup. */
  function _eraseAt(world) {
    const hits = AnnotationInk.erasedAt(strokes, world.x, world.y);
    if (hits.length === 0) return;
    for (const stroke of hits) gesture.ids.add(stroke.id);
    shared.items = shared.items.filter((a) => !gesture.ids.has(a.id));
    _reindex();
  }

  async function onMouseUp(e) {
    if (!gesture) return active;
    const g = gesture;
    _endGesture();

    if (g.kind === "drag") {
      if (g.moved) {
        const moves = shared.items.filter((a) => g.starts.has(a.id)).map((a) => ({ id: a.id, x: a.x, y: a.y }));
        // Pinned members moved too, and pinned positions are the host's
        // to save.
        if (g.members.length && graph.callbacks.onPositionsChanged) graph.callbacks.onPositionsChanged();
        await api.move_annotations(moves);
      } else if (!e.shiftKey && selected.size > 1) {
        // A plain click on one item of a multi-selection narrows to it,
        // same as clicking a mote in graph.js.
        selected = new Set([g.id]);
      }
    } else if (g.kind === "box") {
      if (!g.moved && !g.additive) selected.clear();
    } else if (g.kind === "connect") {
      const targetId = graph.hitTest(e.clientX, e.clientY);
      const note = notes.find((n) => n.id === g.id);
      if (note && targetId) await _toggleAnchor(note, targetId);
    } else if (g.kind === "resize" && g.moved) {
      const note = notes.find((n) => n.id === g.id);
      if (note) await api.resize_annotation(note.id, note.x, note.y, note.width, note.height);
    } else if (g.kind === "groupResize" && g.moved) {
      const group = _byId(g.id);
      if (group) await api.resize_annotation(group.id, group.x, group.y, group.width, group.height);
    } else if (g.kind === "groupDraw") {
      setTool("select");
      if (g.moved && g.rect.w >= AnnotationGroups.MIN_SIZE / 2 && g.rect.h >= AnnotationGroups.MIN_SIZE / 2) {
        await _createGroup(g.rect);
      }
    } else if (g.kind === "pen") {
      const stroke = AnnotationInk.finish(g, pen.widthPx);
      const created = await api.create_stroke(stroke.x, stroke.y, stroke.points, stroke.width, pen.color);
      shared.items.push(created);
      _reindex();
    } else if (g.kind === "erase" && g.ids.size) {
      await api.delete_annotations([...g.ids]);
    }
    _changed();
    return true;
  }

  /** Double-click (Select tool) edits a note or a group's title, or adds
   *  a note: on a mote, attached to it; anywhere else, on the canvas. */
  function onDoubleClick(e) {
    if (tool !== "select") return true;
    const world = graph._worldAt(e.clientX, e.clientY);
    const hit = _hitTest(world.x, world.y);
    if (hit) {
      selected = new Set([hit.id]);
      _startEditing(hit);
      return true;
    }
    const group = AnnotationGroups.hitTest(world.x, world.y);
    if (group) {
      selected = new Set([group.id]);
      AnnotationGroups.startEditing(group);
      return true;
    }
    if (AnnotationInk.hitTest(strokes, world.x, world.y)) return true;
    _createNoteAt(e.clientX, e.clientY, world);
    return true;
  }

  function _createNoteAt(clientX, clientY, world) {
    const moteId = graph.hitTest(clientX, clientY);
    if (moteId) {
      _create({
        anchorMode: "mote", moteId,
        x: MOTE_NOTE_OFFSET + DEFAULT_WIDTH / 2,
        y: -MOTE_NOTE_OFFSET - DEFAULT_HEIGHT / 2,
      });
    } else {
      _create({ anchorMode: "world", x: world.x, y: world.y });
    }
  }

  /** Keys for the tools and the selection. The host calls this first
   *  while annotate mode is on and skips its own handling when it returns
   *  true. The note and title editors stop their own keys from reaching
   *  here. */
  function onKeyDown(e) {
    if (!active || editingId != null || AnnotationGroups.isEditing()) return false;
    const toolKey = TOOL_KEYS[e.key.toLowerCase()];
    if (toolKey && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault();
      setTool(toolKey);
      return true;
    }
    if ((e.key === "Delete" || e.key === "Backspace") && selected.size > 0) {
      e.preventDefault();
      _deleteItems([...selected]);
      return true;
    }
    if (e.key === "Enter" && selected.size === 1) {
      const item = _byId([...selected][0]);
      if (item && item.type === "sticky_note") {
        e.preventDefault();
        _startEditing(item);
        return true;
      }
      if (item && item.type === "group") {
        e.preventDefault();
        AnnotationGroups.startEditing(item);
        return true;
      }
    }
    if (e.key === "Escape") {
      // Back to Select first, then clear the selection; only with neither
      // left does Escape fall through to the host (which leaves the mode).
      if (tool !== "select") {
        setTool("select");
        return true;
      }
      if (selected.size > 0) {
        selected.clear();
        _changed();
        return true;
      }
    }
    return false;
  }

  function _updateHoverCursor(e) {
    if (!active) return;
    if (tool !== "select") {
      graph.canvas.style.cursor = _toolCursor();
      return;
    }
    const world = graph._worldAt(e.clientX, e.clientY);
    let cursor = "default";
    if (_hitTestCloseBadge(world.x, world.y)) cursor = "pointer";
    else if (_hitTestResizeGrip(world.x, world.y)) cursor = "nwse-resize";
    else if (_hitTest(world.x, world.y)) cursor = e.ctrlKey || e.metaKey ? "crosshair" : "move";
    else if (AnnotationGroups.hitTestResizeGrip(world.x, world.y)) cursor = "nwse-resize";
    else if (AnnotationInk.hitTest(strokes, world.x, world.y)) cursor = "move";
    else if (AnnotationGroups.hitTest(world.x, world.y)) cursor = "move";
    graph.canvas.style.cursor = cursor;
  }

  function _endGesture() {
    if (gesture && gesture.kind === "connect") graph.hoverId = null;
    gesture = null;
    if (graph) graph.canvas.style.cursor = active ? _toolCursor() : "default";
  }

  // ---- host layout hooks ----------------------------------------------------

  function applyLayoutForces(host) {
    if (graph) AnnotationGroups.applyForces(host);
  }

  function constrainLayout(host) {
    if (graph) AnnotationGroups.constrain(host);
  }

  /** The host finished dragging motes (outside annotate mode): each joins
   *  the group box it was dropped in, or leaves its group if it was
   *  dropped outside it. */
  function onMotesDropped(ids) {
    if (!graph) return;
    const changes = AnnotationGroups.membershipAfterDrop(ids);
    if (changes.length === 0) return;
    onChanged();
    api.set_group_members(changes).then(() => onChanged());
  }

  // ---- CRUD ---------------------------------------------------------------

  async function _create({ anchorMode, x, y, moteId }) {
    const note = await api.create_sticky_note(anchorMode, x, y, moteId || null, "");
    shared.items.push(note);
    _reindex();
    selected = new Set([note.id]);
    _changed();
    _startEditing(note);
  }

  /** A group around whatever visible motes the drawn rectangle holds.
   *  Refreshes afterwards: taking motes from another group edited that
   *  group's row too. Opens the title for typing, as a new note opens
   *  its text. */
  async function _createGroup(rect) {
    const members = AnnotationGroups.motesIn(rect);
    const created = await api.create_group(
      rect.left + rect.w / 2, rect.top + rect.h / 2, rect.w, rect.h, members, null,
    );
    await refresh();
    selected = new Set([created.id]);
    _changed();
    const group = _byId(created.id);
    if (group) AnnotationGroups.startEditing(group);
  }

  async function _deleteItems(ids) {
    if (editingId != null && ids.includes(editingId)) _stopEditing({ save: false });
    const gone = new Set(ids);
    shared.items = shared.items.filter((a) => !gone.has(a.id));
    _reindex();
    for (const id of ids) { selected.delete(id); heightCache.delete(id); }
    _changed();
    await api.delete_annotations(ids);
  }

  /** Ctrl+drag from a note onto a mote: attach it to that mote, or, if
   *  it's already attached there, detach it back to the canvas. Either
   *  way the note stays exactly where it's drawn -- only what it's
   *  measured from changes. */
  async function _toggleAnchor(note, moteId) {
    const pos = _resolve(note);
    let change;
    if (note.anchorMode === "mote" && note.moteId === moteId) {
      change = { anchorMode: "world", moteId: null, x: pos.x, y: pos.y };
    } else {
      const moteNode = graph.nodes.get(moteId);
      if (!moteNode) return;
      change = { anchorMode: "mote", moteId, x: pos.x - moteNode.x, y: pos.y - moteNode.y };
    }
    Object.assign(note, change);
    await api.anchor_annotation(note.id, change.anchorMode, change.moteId, change.x, change.y);
  }

  // ---- text editing overlay ------------------------------------------------
  // The one place a real DOM element is justified: canvas text can't
  // support IME composition, native selection, or multiline editing.

  function _ensureEditor() {
    editor = document.createElement("textarea");
    editor.className = "annotation-editor hidden";
    editor.addEventListener("blur", () => _stopEditing({ save: true }));
    editor.addEventListener("input", _measureEditor);
    editor.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); _stopEditing({ save: true }); }
      // Backspace in an empty note throws the note away -- quicker than
      // reaching for its × badge.
      if (e.key === "Backspace" && editor.value === "" && editingId != null) {
        e.preventDefault();
        _deleteItems([editingId]);
      }
      e.stopPropagation(); // don't let graph.js's Delete/Ctrl+Z shortcuts fire while typing
    });
    document.body.appendChild(editor);
  }

  /** Centre the camera on a note at a zoom where its text is readable. */
  function _zoomTo(note) {
    const rect = _rectOf(note);
    const cx = rect.left + rect.w / 2, cy = rect.top + rect.h / 2;
    const scale = Math.max(graph.camera.scale, EDIT_SCALE);
    graph.camera.scale = scale;
    graph.camera.x = -cx * scale;
    graph.camera.y = -cy * scale;
    graph.panTarget = null;
    graph._fitUntilAlpha = 0; // a freshly opened garden's auto-fit would undo this
  }

  function _startEditing(note) {
    if (_isCollapsed()) _zoomTo(note);
    editingId = note.id;
    editor.value = note.text || "";
    editor.classList.remove("hidden");
    _measureEditor();
    editor.focus();
    const end = editor.value.length;
    editor.setSelectionRange(end, end);
  }

  /** Collapse the textarea and read back how tall its content really is,
   *  then lay it out again at the note's (now possibly taller) size. */
  function _measureEditor() {
    const note = notes.find((n) => n.id === editingId);
    if (!note) return;
    editorContentHeight = 0;
    _positionEditor(note);
    editor.style.height = "0px";
    editorContentHeight = editor.scrollHeight / graph.camera.scale;
    _positionEditor(note);
  }

  function _positionEditor(note) {
    const rect = _rectOf(note);
    const scale = graph.camera.scale;
    const topLeft = graph._worldToScreen(rect.left, rect.top);
    editor.style.left = `${topLeft.x}px`;
    editor.style.top = `${topLeft.y}px`;
    editor.style.width = `${rect.w * scale}px`;
    editor.style.height = `${rect.h * scale}px`;
    editor.style.padding = `${PADDING * scale}px`;
    editor.style.fontSize = `${FONT_SIZE * scale}px`;
    editor.style.lineHeight = `${LINE_HEIGHT * scale}px`;
    editor.style.borderRadius = `${CORNER_RADIUS * scale}px`;
    editor.style.background = `color-mix(in oklch, ${note.color || theme.fill} ${FILL_ALPHA * 100}%, transparent)`;
  }

  function _stopEditing({ save }) {
    if (editingId == null) return;
    const id = editingId;
    const text = editor.value;
    editingId = null;
    editorContentHeight = 0;
    editJustEnded = true;
    setTimeout(() => { editJustEnded = false; }, 0);
    editor.classList.add("hidden");
    if (save) {
      const note = notes.find((n) => n.id === id);
      if (note && note.text !== text) {
        note.text = text;
        api.update_annotation_text(id, text).then(onChanged);
      }
    }
  }

  /** Called every frame while an editor is open, so it tracks pan/zoom --
   *  the host calls this from the same place it calls draw(). */
  function tick() {
    if (!graph) return;
    AnnotationGroups.tick();
    if (editingId == null) return;
    const note = notes.find((n) => n.id === editingId);
    if (note) _positionEditor(note);
  }

  return {
    install, isActive, hasNotes, hasContent, selectedIds, currentTool, setTool,
    enter, exit, refresh, drawUnderlay, draw, tick,
    onMouseDown, onMouseMove, onMouseUp, onDoubleClick, onKeyDown,
    applyLayoutForces, constrainLayout, onMotesDropped,
  };
})();
