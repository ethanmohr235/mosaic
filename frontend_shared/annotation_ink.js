/* Freehand pen strokes for annotate mode -- the geometry half of the Pen
 * and Eraser tools. frontend_shared/annotations.js owns the gestures and
 * the list of annotations; this module turns captured pointer points into
 * a stored stroke, draws strokes, and hit-tests them.
 *
 * Strokes are vectors, not pixels: a stroke is an origin (x, y) plus
 * data.points, [dx, dy] offsets from it (so moving one is a plain
 * move_annotation), simplified with Ramer-Douglas-Peucker when the pen
 * lifts and drawn as quadratic curves through the points' midpoints.
 * data.width is in world units -- the pen's screen width divided by the
 * zoom it was drawn at -- so a stroke looks as thick as it did while
 * drawing, and scales with the graph after.
 *
 * Kept in frontend_shared/ and symlinked into each app's frontend/, like
 * annotations.js. */

const AnnotationInk = (() => {
  "use strict";

  const WIDTHS = [2, 4, 8];         // screen px, the toolbar's three pens
  const SIMPLIFY_EPSILON = 0.75;    // screen px an RDP-dropped point may stray
  const MIN_STEP = 1.5;             // screen px between captured points
  const ERASER_RADIUS = 8;          // screen px
  const HIT_SLOP = 4;               // screen px of grace when clicking a stroke

  let shared = null;
  // A stroke's own bounds, relative to its origin -- keyed on its data
  // object, which a move leaves alone, so it's computed once per stroke.
  const boundsCache = new WeakMap();

  function install(context) {
    shared = context;
  }

  function colorOf(stroke) {
    return stroke.color || shared.theme.text;
  }

  // ---- capture -------------------------------------------------------------

  function begin(world) {
    return { kind: "pen", points: [[world.x, world.y]] };
  }

  function extend(gesture, world) {
    const last = gesture.points[gesture.points.length - 1];
    const minStep = MIN_STEP / shared.graph.camera.scale;
    if (Math.hypot(world.x - last[0], world.y - last[1]) >= minStep) {
      gesture.points.push([world.x, world.y]);
    }
  }

  /** The stroke as create_stroke() takes it: simplified, re-based on its
   *  first point, rounded to a tenth of a world unit (plenty, at any zoom
   *  a stroke is legible at, and it keeps the JSON small). */
  function finish(gesture, widthPx) {
    const scale = shared.graph.camera.scale;
    const points = simplify(gesture.points, SIMPLIFY_EPSILON / scale);
    const [ox, oy] = points[0];
    const round = (v) => Math.round(v * 10) / 10;
    return {
      x: ox, y: oy,
      points: points.map(([px, py]) => [round(px - ox), round(py - oy)]),
      width: widthPx / scale,
    };
  }

  /** Ramer-Douglas-Peucker, iterative so a long scribble can't blow the
   *  stack: keep the point farthest from each chord while it strays more
   *  than epsilon, drop the rest. */
  function simplify(points, epsilon) {
    if (points.length <= 2) return points.slice();
    const keep = new Uint8Array(points.length);
    keep[0] = keep[points.length - 1] = 1;
    const stack = [[0, points.length - 1]];
    while (stack.length) {
      const [start, end] = stack.pop();
      let farthest = -1, maxDist = epsilon;
      for (let i = start + 1; i < end; i++) {
        const d = _segmentDistance(points[i], points[start], points[end]);
        if (d > maxDist) { maxDist = d; farthest = i; }
      }
      if (farthest !== -1) {
        keep[farthest] = 1;
        stack.push([start, farthest], [farthest, end]);
      }
    }
    return points.filter((_, i) => keep[i]);
  }

  function _segmentDistance(p, a, b) {
    const dx = b[0] - a[0], dy = b[1] - a[1];
    const lengthSq = dx * dx + dy * dy;
    if (lengthSq === 0) return Math.hypot(p[0] - a[0], p[1] - a[1]);
    const t = Math.max(0, Math.min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / lengthSq));
    return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
  }

  // ---- geometry ------------------------------------------------------------

  /** World-space bounding box, stroke width included. */
  function boundsOf(stroke) {
    let rel = boundsCache.get(stroke.data);
    if (!rel) {
      const xs = stroke.data.points.map((p) => p[0]);
      const ys = stroke.data.points.map((p) => p[1]);
      const half = (stroke.data.width || 1) / 2;
      rel = {
        left: Math.min(...xs) - half, top: Math.min(...ys) - half,
        right: Math.max(...xs) + half, bottom: Math.max(...ys) + half,
      };
      boundsCache.set(stroke.data, rel);
    }
    return {
      left: stroke.x + rel.left, top: stroke.y + rel.top,
      w: rel.right - rel.left, h: rel.bottom - rel.top,
    };
  }

  /** Does a circle of radiusPx screen px at (worldX, worldY) touch the
   *  stroke? The eraser and a Select-tool click both ask this. */
  function touches(stroke, worldX, worldY, radiusPx) {
    const scale = shared.graph.camera.scale;
    const reach = radiusPx / scale + (stroke.data.width || 1) / 2;
    const b = boundsOf(stroke);
    if (worldX < b.left - reach || worldX > b.left + b.w + reach
      || worldY < b.top - reach || worldY > b.top + b.h + reach) return false;
    const pts = stroke.data.points;
    const p = [worldX - stroke.x, worldY - stroke.y];
    if (pts.length === 1) return Math.hypot(p[0] - pts[0][0], p[1] - pts[0][1]) <= reach;
    for (let i = 1; i < pts.length; i++) {
      if (_segmentDistance(p, pts[i - 1], pts[i]) <= reach) return true;
    }
    return false;
  }

  function hitTest(strokes, worldX, worldY) {
    for (let i = strokes.length - 1; i >= 0; i--) {
      if (touches(strokes[i], worldX, worldY, HIT_SLOP)) return strokes[i];
    }
    return null;
  }

  function erasedAt(strokes, worldX, worldY) {
    return strokes.filter((s) => touches(s, worldX, worldY, ERASER_RADIUS));
  }

  // ---- drawing -------------------------------------------------------------

  /** Quadratic curves through the midpoints of consecutive points, with
   *  each captured point as the control -- smooth, and it still passes
   *  through the first and last points exactly. */
  function _tracePath(ctx, points, ox, oy) {
    ctx.beginPath();
    const [x0, y0] = points[0];
    ctx.moveTo(ox + x0, oy + y0);
    if (points.length === 1) {
      ctx.lineTo(ox + x0 + 0.01, oy + y0); // a dot: round caps draw it
      return;
    }
    for (let i = 1; i < points.length - 1; i++) {
      const [cx, cy] = points[i];
      const [nx, ny] = points[i + 1];
      ctx.quadraticCurveTo(ox + cx, oy + cy, ox + (cx + nx) / 2, oy + (cy + ny) / 2);
    }
    const [lx, ly] = points[points.length - 1];
    ctx.lineTo(ox + lx, oy + ly);
  }

  function drawStroke(ctx, stroke, isSelected) {
    const width = stroke.data.width || 1;
    ctx.save();
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    if (isSelected) {
      // A soft halo under the ink rather than recoloring it -- the
      // stroke's own color is what you're selecting it to change.
      _tracePath(ctx, stroke.data.points, stroke.x, stroke.y);
      ctx.strokeStyle = shared.theme.accentSoft;
      ctx.lineWidth = width + 8 / shared.graph.camera.scale;
      ctx.stroke();
    }
    _tracePath(ctx, stroke.data.points, stroke.x, stroke.y);
    ctx.strokeStyle = colorOf(stroke);
    ctx.lineWidth = width;
    ctx.stroke();
    ctx.restore();
  }

  function drawLive(ctx, gesture, color, widthPx) {
    ctx.save();
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    _tracePath(ctx, gesture.points, 0, 0);
    ctx.strokeStyle = color || shared.theme.text;
    ctx.lineWidth = widthPx / shared.graph.camera.scale;
    ctx.stroke();
    ctx.restore();
  }

  function drawEraserCursor(ctx, world) {
    const s = 1 / shared.graph.camera.scale;
    ctx.save();
    ctx.strokeStyle = shared.theme.text;
    ctx.globalAlpha = 0.7;
    ctx.lineWidth = s;
    ctx.beginPath();
    ctx.arc(world.x, world.y, ERASER_RADIUS * s, 0, Math.PI * 2);
    ctx.stroke();
    ctx.restore();
  }

  return {
    WIDTHS, install, colorOf, begin, extend, finish, simplify, boundsOf,
    hitTest, erasedAt, drawStroke, drawLive, drawEraserCursor,
  };
})();
