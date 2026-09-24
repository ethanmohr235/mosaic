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
 *   In the host's _onMouseDown/_onMouseMove/_onMouseUp, before its own
 *   annotateMode-aware logic, mirroring the existing attractorMode
 *   short-circuit:
 *     if (this.annotateMode) { if (Annotations.onMouseDown(e)) return; }
 *     (same for onMouseMove/onMouseUp)
 *
 * `graph` must expose: _worldAt(clientX, clientY), _worldToScreen(x, y),
 * camera.scale, and nodes (Map of mote id -> {x, y}) for resolving a
 * mote-anchored note's live position.
 *
 * Kept in frontend_shared/ and symlinked into each app's frontend/, like
 * undo.js and inspector.js. */

const Annotations = (() => {
  "use strict";

  const DEFAULT_WIDTH = 160;
  const DEFAULT_HEIGHT = 90;
  const DEFAULT_COLOR = "#fff3a0";
  const CORNER_RADIUS = 8;
  const CLOSE_BADGE_RADIUS = 9;
  const DRAG_THRESHOLD = 4;

  let api = null;
  let graph = null;
  let onChanged = () => {};

  let notes = [];          // last list_annotations() payload, camelCase
  let active = false;       // annotate mode on/off
  let dragState = null;     // {id, startX, startY, startWorldX, startWorldY, moved}
  let editingId = null;
  let editor = null;        // the one reusable <textarea> overlay

  function install(options) {
    api = options.api;
    graph = options.graph;
    onChanged = options.onChanged || (() => {});
    _ensureEditor();
  }

  function isActive() {
    return active;
  }

  function hasNotes() {
    return notes.length > 0;
  }

  async function enter() {
    active = true;
    await refresh();
  }

  function exit() {
    active = false;
    _stopEditing({ save: true });
    dragState = null;
  }

  async function refresh() {
    notes = await api.list_annotations();
    onChanged();
  }

  // ---- geometry --------------------------------------------------------

  /** A note's live world position. Mote-anchored notes store an offset
   *  from their mote and follow it as it moves; once the mote is gone
   *  (deleted -- see core/db.py's annotations table docstring for why the
   *  row survives that), there is no way to recover where the mote last
   *  was -- core/annotations.py deliberately never observes mote deletes
   *  -- so the stored offset is reused as an absolute position instead.
   *  That keeps the note from vanishing or crashing; it's an
   *  approximation, not a true "last known spot". */
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

  function _rectOf(note) {
    const pos = _resolve(note);
    const w = note.width || DEFAULT_WIDTH;
    const h = note.height || DEFAULT_HEIGHT;
    return { left: pos.x - w / 2, top: pos.y - h / 2, w, h, orphaned: pos.orphaned };
  }

  function _hitTest(worldX, worldY) {
    // Later notes (higher z / more recently created) sit on top.
    for (let i = notes.length - 1; i >= 0; i--) {
      const note = notes[i];
      const rect = _rectOf(note);
      if (worldX >= rect.left && worldX <= rect.left + rect.w
        && worldY >= rect.top && worldY <= rect.top + rect.h) {
        return note;
      }
    }
    return null;
  }

  function _hitTestCloseBadge(worldX, worldY) {
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

  // ---- drawing -----------------------------------------------------------

  function draw(ctx) {
    for (const note of notes) {
      if (note.id === editingId) continue; // the textarea overlay covers it
      _drawNote(ctx, note);
    }
  }

  function _drawNote(ctx, note) {
    const rect = _rectOf(note);
    const s = 1 / graph.camera.scale;

    ctx.save();
    ctx.fillStyle = note.color || DEFAULT_COLOR;
    ctx.strokeStyle = "rgba(0,0,0,0.35)";
    ctx.lineWidth = s;
    if (rect.orphaned) ctx.setLineDash([6 * s, 4 * s]);
    _roundRect(ctx, rect.left, rect.top, rect.w, rect.h, CORNER_RADIUS);
    ctx.fill();
    ctx.stroke();
    ctx.setLineDash([]);

    ctx.fillStyle = "#1a1a1a";
    ctx.textAlign = "left";
    ctx.textBaseline = "top";
    ctx.font = "13px sans-serif";
    _wrapText(ctx, note.text || "", rect.left + 8, rect.top + 8, rect.w - 16, 16);

    if (active) {
      const cx = rect.left + rect.w, cy = rect.top;
      ctx.beginPath();
      ctx.arc(cx, cy, CLOSE_BADGE_RADIUS * s, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(0,0,0,0.55)";
      ctx.fill();
      ctx.strokeStyle = "#fff";
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

  function _wrapText(ctx, text, x, y, maxWidth, lineHeight) {
    const words = text.split(/\s+/).filter(Boolean);
    let line = "";
    let cy = y;
    for (const word of words) {
      const attempt = line ? `${line} ${word}` : word;
      if (ctx.measureText(attempt).width > maxWidth && line) {
        ctx.fillText(line, x, cy);
        line = word;
        cy += lineHeight;
      } else {
        line = attempt;
      }
    }
    if (line) ctx.fillText(line, x, cy);
  }

  // ---- pointer handling ---------------------------------------------------
  // Each returns true when it consumed the event -- the host short-circuits
  // its own handling in that case, same contract as graph.js's own
  // attractorMode branches.

  function onMouseDown(e) {
    if (e.button !== 0) return false;
    const world = graph._worldAt(e.clientX, e.clientY);

    const closeHit = _hitTestCloseBadge(world.x, world.y);
    if (closeHit) {
      _delete(closeHit.id);
      return true;
    }

    const hit = _hitTest(world.x, world.y);
    if (hit) {
      dragState = {
        id: hit.id, moved: false,
        startClientX: e.clientX, startClientY: e.clientY,
        startX: hit.x, startY: hit.y,
      };
      return true;
    }

    const moteId = graph.hitTest(e.clientX, e.clientY);
    if (moteId) {
      _create({ anchorMode: "mote", moteId, x: 36, y: -36 });
    } else {
      _create({ anchorMode: "world", x: world.x, y: world.y });
    }
    return true;
  }

  function onMouseMove(e) {
    if (!dragState) return active;
    const dx = e.clientX - dragState.startClientX;
    const dy = e.clientY - dragState.startClientY;
    if (!dragState.moved && Math.hypot(dx, dy) > DRAG_THRESHOLD) dragState.moved = true;
    if (dragState.moved) {
      const world = graph._worldAt(e.clientX, e.clientY);
      const startWorld = graph._worldAt(dragState.startClientX, dragState.startClientY);
      const note = notes.find((n) => n.id === dragState.id);
      if (note) {
        note.x = dragState.startX + (world.x - startWorld.x);
        note.y = dragState.startY + (world.y - startWorld.y);
      }
    }
    return true;
  }

  async function onMouseUp(e) {
    if (!dragState) return active;
    const { id, moved } = dragState;
    dragState = null;
    const note = notes.find((n) => n.id === id);
    if (!note) return true;
    if (moved) {
      await api.move_annotation(id, note.x, note.y);
      onChanged();
    } else {
      _startEditing(note);
    }
    return true;
  }

  // ---- CRUD ---------------------------------------------------------------

  async function _create({ anchorMode, x, y, moteId }) {
    const note = await api.create_sticky_note(anchorMode, x, y, moteId || null, "");
    notes.push(note);
    onChanged();
    _startEditing(note);
  }

  async function _delete(id) {
    if (editingId === id) _stopEditing({ save: false });
    await api.delete_annotation(id);
    notes = notes.filter((n) => n.id !== id);
    onChanged();
  }

  // ---- text editing overlay ------------------------------------------------
  // The one place a real DOM element is justified: canvas text can't
  // support IME composition, native selection, or multiline editing.

  function _ensureEditor() {
    editor = document.createElement("textarea");
    editor.className = "annotation-editor hidden";
    editor.addEventListener("blur", () => _stopEditing({ save: true }));
    editor.addEventListener("keydown", (e) => {
      if (e.key === "Escape") { e.preventDefault(); _stopEditing({ save: true }); }
      e.stopPropagation(); // don't let graph.js's Delete/Ctrl+Z shortcuts fire while typing
    });
    document.body.appendChild(editor);
  }

  function _startEditing(note) {
    editingId = note.id;
    editor.value = note.text || "";
    editor.classList.remove("hidden");
    _positionEditor(note);
    editor.focus();
    editor.select();
  }

  function _positionEditor(note) {
    const rect = _rectOf(note);
    const scale = graph.camera.scale;
    const topLeft = graph._worldToScreen(rect.left, rect.top);
    editor.style.left = `${topLeft.x}px`;
    editor.style.top = `${topLeft.y}px`;
    editor.style.width = `${rect.w * scale}px`;
    editor.style.height = `${rect.h * scale}px`;
    editor.style.background = note.color || DEFAULT_COLOR;
  }

  function _stopEditing({ save }) {
    if (editingId == null) return;
    const id = editingId;
    const text = editor.value;
    editingId = null;
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
    install, isActive, hasNotes, enter, exit, refresh,
    draw, tick, onMouseDown, onMouseMove, onMouseUp,
  };
})();
