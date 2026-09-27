/* The annotate-mode toolbar: a small bar along the bottom of the graph,
 * shown only while annotating, for switching tools (Select, Note, Group,
 * Pen, Eraser) and picking colors and pen widths.
 *
 * Pure DOM and no state of its own beyond what it last rendered:
 * frontend_shared/annotations.js owns the tool, the pen, and the
 * selection, calls sync() whenever any of them changes, and gets told
 * what was clicked through the handlers passed to install().
 *
 * The color row works like the species color rows in Monarch's view
 * settings: preset swatches from the same palette group boxes pick their
 * automatic colors from, a native color input for anything else, and ↺
 * back to automatic. It colors the selected groups and strokes when there
 * are any, and the pen otherwise.
 *
 * Kept in frontend_shared/ and symlinked into each app's frontend/, like
 * annotations.js. */

const AnnotationToolbar = (() => {
  "use strict";

  const SWATCH_COUNT = 8;

  // 16px line icons, stroked in currentColor so they take the button's
  // own text color (and the accent, when active).
  const ICONS = {
    select: '<path d="M4 2.5l9 5.2-4 1.1-1.9 3.9z" stroke-linejoin="round"/>',
    note: '<rect x="2.5" y="3" width="11" height="10" rx="1.5"/><path d="M5 6.5h6M5 9.5h4"/>',
    group: '<rect x="2" y="4.5" width="12" height="9" rx="1.5"/><path d="M3.5 4.5V2.5h5v2"/>',
    pen: '<path d="M3 13l1-3.5 7-7 2.5 2.5-7 7z" stroke-linejoin="round"/><path d="M9.5 4l2.5 2.5"/>',
    eraser: '<path d="M6.5 13.5l-4-4 6.5-6.5 4 4-4.5 4.5z" stroke-linejoin="round"/><path d="M6.5 13.5h7M5 7.5l4 4"/>',
  };
  const TOOLS = [
    { id: "select", label: "Select", key: "V" },
    { id: "note", label: "Note", key: "N" },
    { id: "group", label: "Group", key: "G" },
    { id: "pen", label: "Pen", key: "P" },
    { id: "eraser", label: "Eraser", key: "E" },
  ];

  let root = null;
  let handlers = {};
  let colorInput = null;
  let resetBtn = null;
  let swatches = [];
  let widthRow = null;
  let colorRow = null;
  const toolButtons = new Map();
  const widthButtons = new Map();

  /** mount: the element the bar is positioned inside (the graph's own
   *  container). handlers: onTool(id), onColor(color, {final}),
   *  onWidth(px), with onColor(null, {final: true}) meaning "automatic". */
  function install(mount, palette, widths, options) {
    handlers = options;
    root = document.createElement("div");
    root.className = "annotation-toolbar hidden";
    root.setAttribute("role", "toolbar");
    root.setAttribute("aria-label", "Annotation tools");

    const tools = _group("annotation-toolbar-tools");
    for (const tool of TOOLS) {
      const btn = _button(`${tool.label} (${tool.key})`, _icon(ICONS[tool.id]));
      btn.addEventListener("click", () => handlers.onTool(tool.id));
      toolButtons.set(tool.id, btn);
      tools.appendChild(btn);
    }
    root.appendChild(tools);

    colorRow = _group("annotation-toolbar-colors");
    for (let i = 0; i < SWATCH_COUNT; i++) {
      const color = palette(i);
      const btn = _button("", "");
      btn.className = "annotation-swatch";
      btn.style.setProperty("--swatch", color);
      btn.dataset.color = color;
      btn.addEventListener("click", () => handlers.onColor(color, { final: true }));
      swatches.push(btn);
      colorRow.appendChild(btn);
    }
    colorInput = document.createElement("input");
    colorInput.type = "color";
    colorInput.title = "Custom color";
    colorInput.addEventListener("input", () => handlers.onColor(colorInput.value, { final: false }));
    colorInput.addEventListener("change", () => handlers.onColor(colorInput.value, { final: true }));
    colorRow.appendChild(colorInput);
    resetBtn = _button("Back to the automatic color", "↺");
    resetBtn.classList.add("annotation-reset");
    resetBtn.addEventListener("click", () => handlers.onColor(null, { final: true }));
    colorRow.appendChild(resetBtn);
    root.appendChild(colorRow);

    widthRow = _group("annotation-toolbar-widths");
    for (const px of widths) {
      const dot = `<span class="annotation-width-dot" style="width:${px + 2}px;height:${px + 2}px"></span>`;
      const btn = _button(`${px}px pen`, dot);
      btn.addEventListener("click", () => handlers.onWidth(px));
      widthButtons.set(px, btn);
      widthRow.appendChild(btn);
    }
    root.appendChild(widthRow);

    // Clicks on the bar are the bar's -- never a canvas gesture.
    root.addEventListener("mousedown", (e) => e.stopPropagation());
    mount.appendChild(root);
  }

  function _group(className) {
    const el = document.createElement("div");
    el.className = `annotation-toolbar-group ${className}`;
    return el;
  }

  function _button(title, html) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "annotation-tool";
    if (title) { btn.title = title; btn.setAttribute("aria-label", title); }
    btn.innerHTML = html;
    return btn;
  }

  function _icon(paths) {
    return `<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round">${paths}</svg>`;
  }

  function show() {
    root.classList.remove("hidden");
    document.body.classList.add("annotation-toolbar-open");
  }

  function hide() {
    root.classList.add("hidden");
    document.body.classList.remove("annotation-toolbar-open");
  }

  /** state: {tool, widthPx, color (the one being shown, or null for
   *  automatic), target: "pen" | "selection", showColors, showWidths}. */
  function sync(state) {
    for (const [id, btn] of toolButtons) {
      btn.classList.toggle("active", id === state.tool);
      btn.setAttribute("aria-pressed", String(id === state.tool));
    }
    colorRow.classList.toggle("hidden", !state.showColors);
    widthRow.classList.toggle("hidden", !state.showWidths);
    for (const [px, btn] of widthButtons) btn.classList.toggle("active", px === state.widthPx);
    for (const btn of swatches) btn.classList.toggle("active", btn.dataset.color === state.color);
    resetBtn.disabled = state.color == null;
    colorInput.value = _toHex(state.color || state.autoColor);
    const what = state.target === "selection" ? "the selection" : "the pen";
    colorInput.title = `Custom color for ${what}`;
    root.dataset.target = state.target;
  }

  /** <input type="color"> only takes #rrggbb; the palette is oklch.
   *  Canvas serializes any color it can parse to hex. */
  function _toHex(color) {
    const ctx = _toHex.ctx || (_toHex.ctx = document.createElement("canvas").getContext("2d"));
    ctx.fillStyle = "#888888";
    ctx.fillStyle = color || "#888888";
    const value = ctx.fillStyle;
    return typeof value === "string" && value.startsWith("#") ? value : "#888888";
  }

  return { install, show, hide, sync, TOOLS };
})();
