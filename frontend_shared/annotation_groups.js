/* Group boxes for annotate mode: a titled, colored rectangle that owns a
 * set of motes. frontend_shared/annotations.js owns the gestures and the
 * list of annotations; this module draws groups, hit-tests them, edits
 * their titles, and -- the part no other annotation kind has -- keeps
 * their members inside them as a constraint on the host's force layout.
 *
 * A group is a row with type "group": x/y its centre, width/height its
 * size, text its title, color its color (null = automatic, the host
 * palette's data.slot entry) and data.members the mote ids it owns.
 *
 * Membership is a layout constraint, never a pin: the host calls
 * applyForces(graph) inside its simulation step (a gentle pull back
 * toward the box for members nearing its edge) and constrain(graph)
 * every frame, settled or not (a hard wall both ways -- members are
 * clamped inside their box, everyone else is pushed out of every box).
 * A mote being dragged is exempt until it's dropped; where it's dropped
 * decides its group (membershipAfterDrop).
 *
 * The host graph must expose, beyond annotations.js's contract,
 * isMoteVisible(id) and draggingIds() (a Set), and may define a global
 * paletteColor(slot) (monarch/frontend/graph.js's species palette) for
 * automatic colors.
 *
 * Kept in frontend_shared/ and symlinked into each app's frontend/, like
 * annotations.js. */

const AnnotationGroups = (() => {
  "use strict";

  const PAD = 16;              // world units members keep from the wall
  const PULL_MARGIN = 24;      // world units in from PAD where the pull starts
  const PULL = 0.03;           // fraction of the gap closed per tick
  const CORNER_RADIUS = 10;
  const FILL_ALPHA = 0.08;
  const TAB_HEIGHT = 20;       // world units, like a note's text metrics
  const TAB_FONT = 12;
  const TAB_PAD_X = 8;
  const TAB_MIN_WIDTH = 44;
  const TAB_INSET = 10;        // tab's left edge, in from the box's corner
  const MIN_LEGIBLE_FONT = 6;  // screen px, as for notes
  const MIN_SIZE = 40;         // world units, smallest box a resize allows
  const BORDER_HIT = 6;        // screen px either side of the edge
  const RESIZE_GRIP = 10;      // screen px either side of the corner
  const PLACEHOLDER = "Group";
  const FONT_FAMILY = '"IBM Plex Sans", sans-serif';

  let shared = null;
  let editor = null;           // the one reusable title <input>
  let editingId = null;
  let editJustEnded = false;   // see annotations.js's own editJustEnded
  let measureCtx = null;

  function install(context) {
    shared = context;
    measureCtx = document.createElement("canvas").getContext("2d");
    _ensureEditor();
  }

  function groups() {
    return shared.items.filter((a) => a.type === "group");
  }

  function membersOf(group) {
    return group.data.members || [];
  }

  function autoColor(slot) {
    // eslint-disable-next-line no-undef
    if (typeof paletteColor === "function") return paletteColor(slot, false);
    return `oklch(0.72 0.13 ${((slot * 137.508 + 25) % 360).toFixed(1)})`;
  }

  function colorOf(group) {
    return group.color || autoColor(group.data.slot || 0);
  }

  // ---- geometry --------------------------------------------------------

  function rectOf(group) {
    return {
      left: group.x - group.width / 2, top: group.y - group.height / 2,
      w: group.width, h: group.height,
    };
  }

  function _titleLegible() {
    return TAB_FONT * shared.graph.camera.scale >= MIN_LEGIBLE_FONT;
  }

  /** The title tab on the box's top edge, or null when there's nothing
   *  to show: an untitled group only gets a (placeholder) tab while
   *  annotating. */
  function tabRectOf(group) {
    const title = group.id === editingId ? editor.value : group.text;
    if (!title && !shared.active) return null;
    const rect = rectOf(group);
    measureCtx.font = `600 ${TAB_FONT}px ${FONT_FAMILY}`;
    const textW = _titleLegible() ? measureCtx.measureText(title || PLACEHOLDER).width : 0;
    const w = Math.min(Math.max(TAB_MIN_WIDTH, textW + 2 * TAB_PAD_X), Math.max(TAB_MIN_WIDTH, rect.w - 2 * TAB_INSET));
    return { left: rect.left + TAB_INSET, top: rect.top - TAB_HEIGHT, w, h: TAB_HEIGHT };
  }

  function _inside(rect, x, y, grow = 0) {
    return x >= rect.left - grow && x <= rect.left + rect.w + grow
      && y >= rect.top - grow && y <= rect.top + rect.h + grow;
  }

  /** The group whose tab or border is at (worldX, worldY). The interior
   *  is deliberately not a hit, so box-selecting or drawing inside a
   *  group still works. */
  function hitTest(worldX, worldY) {
    const band = BORDER_HIT / shared.graph.camera.scale;
    const list = groups();
    for (let i = list.length - 1; i >= 0; i--) {
      const group = list[i];
      const tab = tabRectOf(group);
      if (tab && _inside(tab, worldX, worldY)) return group;
      const rect = rectOf(group);
      if (_inside(rect, worldX, worldY, band) && !_inside(rect, worldX, worldY, -band)) return group;
    }
    return null;
  }

  function hitTestTab(worldX, worldY) {
    const list = groups();
    for (let i = list.length - 1; i >= 0; i--) {
      const tab = tabRectOf(list[i]);
      if (tab && _inside(tab, worldX, worldY)) return list[i];
    }
    return null;
  }

  function hitTestResizeGrip(worldX, worldY) {
    const g = RESIZE_GRIP / shared.graph.camera.scale;
    const list = groups();
    for (let i = list.length - 1; i >= 0; i--) {
      const rect = rectOf(list[i]);
      const cx = rect.left + rect.w, cy = rect.top + rect.h;
      if (Math.abs(worldX - cx) <= g && Math.abs(worldY - cy) <= g) return list[i];
    }
    return null;
  }

  /** Visible motes whose centres lie inside a world rect -- a new
   *  group's members. */
  function motesIn(rect) {
    const graph = shared.graph;
    const out = [];
    for (const [id, node] of graph.nodes) {
      if (graph.isMoteVisible && !graph.isMoteVisible(id)) continue;
      if (_inside(rect, node.x, node.y)) out.push(id);
    }
    return out;
  }

  // ---- layout constraint -------------------------------------------------

  function _ownerMap(list) {
    const owner = new Map();
    for (const group of list) for (const id of membersOf(group)) owner.set(id, group);
    return owner;
  }

  /** Clamp bounds for a member: the box shrunk by `inset`, collapsing to
   *  the centre line on an axis the box is too small for. */
  function _innerBounds(rect, inset) {
    const cx = rect.left + rect.w / 2, cy = rect.top + rect.h / 2;
    return {
      minX: Math.min(rect.left + inset, cx), maxX: Math.max(rect.left + rect.w - inset, cx),
      minY: Math.min(rect.top + inset, cy), maxY: Math.max(rect.top + rect.h - inset, cy),
    };
  }

  /** Called from the host's simulation step, before it integrates: a
   *  member drifting toward its wall is nudged back, so the hard clamp in
   *  constrain() is a backstop rather than what visibly holds the group
   *  together. Pinned members don't take forces, as everywhere else. */
  function applyForces(graph) {
    for (const group of groups()) {
      const b = _innerBounds(rectOf(group), PAD + PULL_MARGIN);
      for (const id of membersOf(group)) {
        const node = graph.nodes.get(id);
        if (!node || node.pinned || (graph.isMoteVisible && !graph.isMoteVisible(id))) continue;
        const tx = Math.max(b.minX, Math.min(node.x, b.maxX));
        const ty = Math.max(b.minY, Math.min(node.y, b.maxY));
        node.vx += (tx - node.x) * PULL;
        node.vy += (ty - node.y) * PULL;
      }
    }
  }

  /** Called every frame, whether or not the layout is still running --
   *  a box moved or resized after the graph settled must still hold its
   *  members and keep everyone else out. Non-members are pushed out
   *  first, members clamped in last, so where two boxes overlap a mote's
   *  own box wins. Applies to pinned motes too (it's a wall, not a
   *  force); skips whatever is being dragged right now. */
  function constrain(graph) {
    const list = groups();
    if (list.length === 0) return;
    const dragging = graph.draggingIds ? graph.draggingIds() : new Set();
    const owner = _ownerMap(list);
    const rects = new Map(list.map((g) => [g.id, rectOf(g)]));

    for (const [id, node] of graph.nodes) {
      if (dragging.has(id)) continue;
      if (graph.isMoteVisible && !graph.isMoteVisible(id)) continue;
      const own = owner.get(id);
      for (const group of list) {
        if (group === own) continue;
        _pushOut(node, rects.get(group.id));
      }
      if (own) _clampIn(node, rects.get(own.id));
    }
  }

  function _pushOut(node, rect) {
    if (!_inside(rect, node.x, node.y, PAD)) return;
    const exits = [
      { d: node.x - (rect.left - PAD), axis: "x", to: rect.left - PAD },
      { d: rect.left + rect.w + PAD - node.x, axis: "x", to: rect.left + rect.w + PAD },
      { d: node.y - (rect.top - PAD), axis: "y", to: rect.top - PAD },
      { d: rect.top + rect.h + PAD - node.y, axis: "y", to: rect.top + rect.h + PAD },
    ];
    exits.sort((a, b) => a.d - b.d);
    const exit = exits[0];
    if (exit.axis === "x") { node.x = exit.to; node.vx = 0; }
    else { node.y = exit.to; node.vy = 0; }
  }

  function _clampIn(node, rect) {
    const b = _innerBounds(rect, PAD);
    if (node.x < b.minX || node.x > b.maxX) {
      node.x = Math.max(b.minX, Math.min(node.x, b.maxX));
      node.vx = 0;
    }
    if (node.y < b.minY || node.y > b.maxY) {
      node.y = Math.max(b.minY, Math.min(node.y, b.maxY));
      node.vy = 0;
    }
  }

  /** After the host drops dragged motes: each belongs to whichever box
   *  it landed in (the topmost, if boxes overlap), or to none. Updates
   *  the local rows at once -- the next frame's constrain() would
   *  otherwise shove a mote dropped into a box straight back out before
   *  the save came back -- and returns the [{groupId, members}] changes
   *  for set_group_members(), or [] if nothing changed. */
  function membershipAfterDrop(ids) {
    const list = groups();
    if (list.length === 0) return [];
    const owner = _ownerMap(list);
    const touched = new Map();
    for (const id of ids) {
      const node = shared.graph.nodes.get(id);
      if (!node) continue;
      let target = null;
      for (let i = list.length - 1; i >= 0; i--) {
        if (_inside(rectOf(list[i]), node.x, node.y)) { target = list[i]; break; }
      }
      const current = owner.get(id) || null;
      if (target === current) continue;
      if (current) {
        current.data = { ...current.data, members: membersOf(current).filter((m) => m !== id) };
        touched.set(current.id, current);
      }
      if (target) {
        target.data = { ...target.data, members: [...membersOf(target), id] };
        touched.set(target.id, target);
      }
    }
    return [...touched.values()].map((g) => ({ groupId: g.id, members: membersOf(g) }));
  }

  // ---- drawing -------------------------------------------------------------

  /** Drawn under the edges and nodes -- a group is a region motes sit in,
   *  not something laid over them. */
  function draw(ctx, selected) {
    const s = 1 / shared.graph.camera.scale;
    const legible = _titleLegible();
    for (const group of groups()) {
      const rect = rectOf(group);
      const color = colorOf(group);
      const isSelected = selected.has(group.id);
      ctx.save();
      _roundRect(ctx, rect.left, rect.top, rect.w, rect.h, CORNER_RADIUS);
      ctx.fillStyle = color;
      ctx.globalAlpha = FILL_ALPHA;
      ctx.fill();
      ctx.globalAlpha = isSelected ? 1 : 0.75;
      ctx.strokeStyle = color;
      ctx.lineWidth = (isSelected ? 2.5 : 1.5) * s;
      ctx.stroke();
      ctx.globalAlpha = 1;

      const tab = tabRectOf(group);
      if (tab) {
        _tabPath(ctx, tab);
        ctx.fillStyle = color;
        ctx.globalAlpha = group.text || group.id === editingId ? 0.9 : 0.45;
        ctx.fill();
        ctx.globalAlpha = 1;
        if (legible && group.id !== editingId) {
          ctx.save();
          ctx.clip();
          ctx.fillStyle = group.text ? shared.theme.onColor : shared.theme.text;
          ctx.font = `600 ${TAB_FONT}px ${FONT_FAMILY}`;
          ctx.textBaseline = "middle";
          ctx.fillText(group.text || PLACEHOLDER, tab.left + TAB_PAD_X, tab.top + tab.h / 2 + 0.5);
          ctx.restore();
        }
      }

      if (shared.active) {
        const gx = rect.left + rect.w - 4 * s, gy = rect.top + rect.h - 4 * s;
        ctx.strokeStyle = color;
        ctx.lineWidth = s;
        ctx.beginPath();
        ctx.moveTo(gx - 7 * s, gy); ctx.lineTo(gx, gy - 7 * s);
        ctx.moveTo(gx - 3 * s, gy); ctx.lineTo(gx, gy - 3 * s);
        ctx.stroke();
      }
      ctx.restore();
    }
  }

  /** The Group tool's rubber band, before there's a group to draw. */
  function drawDraft(ctx, rect) {
    const s = 1 / shared.graph.camera.scale;
    ctx.save();
    _roundRect(ctx, rect.left, rect.top, rect.w, rect.h, CORNER_RADIUS);
    ctx.fillStyle = shared.theme.accentSoft;
    ctx.fill();
    ctx.strokeStyle = shared.theme.accent;
    ctx.lineWidth = 1.5 * s;
    ctx.setLineDash([5 * s, 4 * s]);
    ctx.stroke();
    ctx.restore();
  }

  function _tabPath(ctx, tab) {
    const r = Math.min(6, tab.h / 2);
    ctx.beginPath();
    ctx.moveTo(tab.left, tab.top + tab.h);
    ctx.lineTo(tab.left, tab.top + r);
    ctx.arcTo(tab.left, tab.top, tab.left + r, tab.top, r);
    ctx.lineTo(tab.left + tab.w - r, tab.top);
    ctx.arcTo(tab.left + tab.w, tab.top, tab.left + tab.w, tab.top + r, r);
    ctx.lineTo(tab.left + tab.w, tab.top + tab.h);
    ctx.closePath();
  }

  function _roundRect(ctx, x, y, w, h, r) {
    r = Math.min(r, w / 2, h / 2);
    ctx.beginPath();
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  // ---- title editing -------------------------------------------------------

  function _ensureEditor() {
    editor = document.createElement("input");
    editor.type = "text";
    editor.className = "annotation-group-title hidden";
    editor.placeholder = PLACEHOLDER;
    editor.addEventListener("blur", () => stopEditing({ save: true }));
    editor.addEventListener("keydown", (e) => {
      // Escape keeps the edit, as it does in a note.
      if (e.key === "Enter" || e.key === "Escape") {
        e.preventDefault();
        stopEditing({ save: true });
      }
      e.stopPropagation(); // keep graph.js's and annotate mode's keys out of typing
    });
    document.body.appendChild(editor);
  }

  function isEditing() {
    return editingId != null;
  }

  function editJustEndedNow() {
    return editJustEnded;
  }

  function startEditing(group) {
    editingId = group.id;
    editor.value = group.text || "";
    editor.classList.remove("hidden");
    _positionEditor(group);
    editor.focus();
    editor.select();
  }

  function stopEditing({ save }) {
    if (editingId == null) return;
    const id = editingId;
    const text = editor.value.trim();
    editingId = null;
    editJustEnded = true;
    setTimeout(() => { editJustEnded = false; }, 0);
    editor.classList.add("hidden");
    // Hiding a focused input doesn't blur it in WebKit, and the host
    // would go on treating every key as typing into it.
    if (document.activeElement === editor) editor.blur();
    if (!save) return;
    const group = shared.items.find((a) => a.id === id);
    if (group && group.text !== text) {
      group.text = text;
      shared.api.update_annotation_text(id, text).then(shared.changed);
    }
  }

  function _positionEditor(group) {
    const tab = tabRectOf(group) || { left: rectOf(group).left, top: rectOf(group).top - TAB_HEIGHT, w: TAB_MIN_WIDTH, h: TAB_HEIGHT };
    const scale = shared.graph.camera.scale;
    const topLeft = shared.graph._worldToScreen(tab.left, tab.top);
    editor.style.left = `${topLeft.x}px`;
    editor.style.top = `${topLeft.y}px`;
    editor.style.width = `${Math.max(tab.w, 140) * scale}px`;
    editor.style.height = `${tab.h * scale}px`;
    editor.style.padding = `0 ${TAB_PAD_X * scale}px`;
    editor.style.fontSize = `${TAB_FONT * scale}px`;
    editor.style.borderRadius = `${6 * scale}px ${6 * scale}px 0 0`;
    editor.style.background = colorOf(group);
  }

  /** Every frame while the title is being edited, so it tracks pan/zoom. */
  function tick() {
    if (editingId == null) return;
    const group = shared.items.find((a) => a.id === editingId);
    if (group) _positionEditor(group);
    else stopEditing({ save: false });
  }

  return {
    install, groups, membersOf, colorOf, autoColor, rectOf, motesIn, hitTest, hitTestTab,
    hitTestResizeGrip, applyForces, constrain, membershipAfterDrop, draw, drawDraft,
    isEditing, editJustEndedNow, startEditing, stopEditing, tick, MIN_SIZE,
  };
})();
