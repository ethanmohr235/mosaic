/* Sticky-note annotations layered over a hand-written canvas graph
 * (monarch/frontend/graph.js today; other apps' graphs can adopt this
 * later -- see docs/architecture.md's Annotations section).
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
 *   block that draws nodes (both world- and mote-anchored notes are world
 *   coordinates):
 *     if (graph.annotateMode || Annotations.hasNotes()) Annotations.draw(ctx);
 *
 *   While annotate mode is on, the host hands its left-button
 *   mousedown/mousemove/mouseup and its dblclick to onMouseDown/
 *   onMouseMove/onMouseUp/onDoubleClick, mirroring the existing
 *   attractorMode short-circuit, and gives onKeyDown(e) first refusal on
 *   keys (Delete/Backspace, Enter, Escape act on the note selection).
 *
 * Gestures inside annotate mode deliberately match the graph editor's
 * own: click selects, shift+click adds to the selection, drag on empty
 * canvas box-selects, dragging a selected note moves the whole selection,
 * and ctrl+drag from a note to a mote attaches it there (or detaches it,
 * if it's already on that mote) the way ctrl+drag between motes relates
 * them. Double-click is what creates a note (or edits one); the bottom-
 * right corner resizes one.
 *
 * `graph` must expose: _worldAt(clientX, clientY), _worldToScreen(x, y),
 * camera.scale, hitTest(clientX, clientY), hoverId, canvas, and nodes (Map
 * of mote id -> {x, y}) for resolving a mote-anchored note's live
 * position.
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

  let api = null;
  let graph = null;
  let onChanged = () => {};

  let notes = [];            // last list_annotations() payload, camelCase
  let active = false;        // annotate mode on/off
  let selected = new Set();  // note ids
  // The one gesture in progress, if any -- the same one-at-a-time shape
  // as graph.js's dragState/boxState/connectState, folded together:
  //   {kind: "drag", id, moved, startClientX, startClientY, starts: Map id -> {x, y}}
  //   {kind: "box", moved, additive, base: Set, startClientX, startClientY, rect}
  //   {kind: "connect", id, cursor: {x, y}, targetId}
  //   {kind: "resize", id, moved, startClientX, startClientY, startX, startY,
  //    startBaseW, startBaseH, startW, startH}
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
    leader: "oklch(0.68 0 0 / 0.55)",
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
  }

  function isActive() {
    return active;
  }

  function hasNotes() {
    return notes.length > 0;
  }

  function selectedIds() {
    return [...selected];
  }

  async function enter() {
    active = true;
    await refresh();
  }

  function exit() {
    active = false;
    _stopEditing({ save: true });
    _endGesture();
    selected.clear();
  }

  async function refresh() {
    notes = await api.list_annotations();
    const ids = new Set(notes.map((n) => n.id));
    for (const id of selected) if (!ids.has(id)) selected.delete(id);
    if (editingId != null && !ids.has(editingId)) _stopEditing({ save: false });
    onChanged();
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

  function draw(ctx) {
    // Leaders first, so a note overlapping another note's line covers it.
    for (const note of notes) _drawLeader(ctx, note);
    const collapsed = _isCollapsed();
    for (const note of notes) {
      if (note.id === editingId) continue; // the textarea overlay covers it
      if (collapsed) _drawGlyph(ctx, note);
      else _drawNote(ctx, note);
    }
    if (gesture && gesture.kind === "connect") _drawConnectPreview(ctx);
    if (gesture && gesture.kind === "box" && gesture.moved) _drawBox(ctx, gesture.rect);
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

    const closeHit = _hitTestCloseBadge(world.x, world.y);
    if (closeHit) {
      _deleteNotes([closeHit.id]);
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

    const hit = _hitTest(world.x, world.y);
    // A click outside the note being edited only finishes that edit --
    // the same "click away to unfocus" every text field gets from
    // frontend_shared/focus.js -- rather than also starting a box-select.
    // Grabbing another note still works in one click.
    if ((editingId != null || editJustEnded) && !hit) {
      _stopEditing({ save: true });
      return true;
    }

    if (hit) {
      if (e.ctrlKey || e.metaKey) {
        gesture = { kind: "connect", id: hit.id, cursor: world, targetId: null };
        graph.canvas.style.cursor = "crosshair";
        return true;
      }
      if (e.shiftKey) {
        if (selected.has(hit.id)) {
          selected.delete(hit.id);
          return true;
        }
        selected.add(hit.id);
      } else if (!selected.has(hit.id)) {
        selected = new Set([hit.id]);
      }
      // Dragging a note in the selection moves the whole selection, same
      // as dragging a mote in graph.js's multi-selection.
      gesture = {
        kind: "drag", id: hit.id, moved: false,
        startClientX: e.clientX, startClientY: e.clientY,
        starts: new Map(notes.filter((n) => selected.has(n.id)).map((n) => [n.id, { x: n.x, y: n.y }])),
      };
      return true;
    }

    gesture = {
      kind: "box", moved: false, additive: e.shiftKey,
      base: e.shiftKey ? new Set(selected) : new Set(),
      startClientX: e.clientX, startClientY: e.clientY, rect: null,
    };
    return true;
  }

  function onMouseMove(e) {
    if (!gesture) {
      _updateHoverCursor(e);
      return active;
    }
    const pastThreshold = Math.hypot(
      e.clientX - (gesture.startClientX ?? e.clientX),
      e.clientY - (gesture.startClientY ?? e.clientY),
    ) > DRAG_THRESHOLD;
    const world = graph._worldAt(e.clientX, e.clientY);

    if (gesture.kind === "drag") {
      if (!gesture.moved && pastThreshold) gesture.moved = true;
      if (!gesture.moved) return true;
      const startWorld = graph._worldAt(gesture.startClientX, gesture.startClientY);
      const dx = world.x - startWorld.x, dy = world.y - startWorld.y;
      for (const note of notes) {
        const start = gesture.starts.get(note.id);
        if (!start) continue;
        note.x = start.x + dx;
        note.y = start.y + dy;
      }
    } else if (gesture.kind === "box") {
      if (!gesture.moved && pastThreshold) gesture.moved = true;
      if (!gesture.moved) return true;
      const start = graph._worldAt(gesture.startClientX, gesture.startClientY);
      gesture.rect = {
        left: Math.min(start.x, world.x), top: Math.min(start.y, world.y),
        w: Math.abs(world.x - start.x), h: Math.abs(world.y - start.y),
      };
      // Live, like graph.js's box-select: notes light up as the box
      // reaches them, not only on release.
      selected = new Set(gesture.base);
      for (const note of notes) {
        if (_rectsOverlap(_shownRectOf(note), gesture.rect)) selected.add(note.id);
      }
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
    }
    return true;
  }

  async function onMouseUp(e) {
    if (!gesture) return active;
    const g = gesture;
    _endGesture();

    if (g.kind === "drag") {
      if (g.moved) {
        const moves = notes.filter((n) => g.starts.has(n.id)).map((n) => ({ id: n.id, x: n.x, y: n.y }));
        await api.move_annotations(moves);
      } else if (!e.shiftKey && selected.size > 1) {
        // A plain click on one note of a multi-selection narrows to it,
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
    }
    onChanged();
    return true;
  }

  /** Double-click edits a note, or adds one: on a mote, attached to it;
   *  anywhere else, on the canvas. */
  function onDoubleClick(e) {
    const world = graph._worldAt(e.clientX, e.clientY);
    const hit = _hitTest(world.x, world.y);
    if (hit) {
      selected = new Set([hit.id]);
      _startEditing(hit);
      return true;
    }
    const moteId = graph.hitTest(e.clientX, e.clientY);
    if (moteId) {
      _create({
        anchorMode: "mote", moteId,
        x: MOTE_NOTE_OFFSET + DEFAULT_WIDTH / 2,
        y: -MOTE_NOTE_OFFSET - DEFAULT_HEIGHT / 2,
      });
    } else {
      _create({ anchorMode: "world", x: world.x, y: world.y });
    }
    return true;
  }

  /** Keys that act on the note selection. The host calls this first
   *  while annotate mode is on and skips its own handling when it returns
   *  true. The editor textarea stops its own keys from reaching here. */
  function onKeyDown(e) {
    if (!active || editingId != null) return false;
    if ((e.key === "Delete" || e.key === "Backspace") && selected.size > 0) {
      e.preventDefault();
      _deleteNotes([...selected]);
      return true;
    }
    if (e.key === "Enter" && selected.size === 1) {
      const note = notes.find((n) => selected.has(n.id));
      if (note) {
        e.preventDefault();
        _startEditing(note);
        return true;
      }
    }
    if (e.key === "Escape" && selected.size > 0) {
      selected.clear();
      onChanged();
      return true;
    }
    return false;
  }

  function _updateHoverCursor(e) {
    if (!active) return;
    const world = graph._worldAt(e.clientX, e.clientY);
    let cursor = "default";
    if (_hitTestCloseBadge(world.x, world.y)) cursor = "pointer";
    else if (_hitTestResizeGrip(world.x, world.y)) cursor = "nwse-resize";
    else if (_hitTest(world.x, world.y)) cursor = e.ctrlKey || e.metaKey ? "crosshair" : "move";
    graph.canvas.style.cursor = cursor;
  }

  function _endGesture() {
    if (gesture && gesture.kind === "connect") graph.hoverId = null;
    gesture = null;
    if (graph) graph.canvas.style.cursor = "default";
  }

  // ---- CRUD ---------------------------------------------------------------

  async function _create({ anchorMode, x, y, moteId }) {
    const note = await api.create_sticky_note(anchorMode, x, y, moteId || null, "");
    notes.push(note);
    selected = new Set([note.id]);
    onChanged();
    _startEditing(note);
  }

  async function _deleteNotes(ids) {
    if (editingId != null && ids.includes(editingId)) _stopEditing({ save: false });
    const gone = new Set(ids);
    notes = notes.filter((n) => !gone.has(n.id));
    for (const id of ids) { selected.delete(id); heightCache.delete(id); }
    onChanged();
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
        _deleteNotes([editingId]);
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
    if (editingId == null) return;
    const note = notes.find((n) => n.id === editingId);
    if (note) _positionEditor(note);
  }

  return {
    install, isActive, hasNotes, selectedIds, enter, exit, refresh,
    draw, tick, onMouseDown, onMouseMove, onMouseUp, onDoubleClick, onKeyDown,
  };
})();
