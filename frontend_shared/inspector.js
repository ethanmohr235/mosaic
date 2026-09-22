/* The floating mote inspector -- one surface, seven windows.
 *
 * Every window here shows motes some particular way: Monarch as a graph,
 * Monolith as a table, Monastery as a calendar, Momentum as a board,
 * Mora as prose, Motion as a solver. What they used to disagree about
 * was what happens once you pick one. Monarch could add a property and
 * set a species; Motion could overwrite the values a mote already had
 * and nothing else; Mora could do neither. The same mote therefore had
 * six different sets of things you were allowed to do to it, decided by
 * which window you happened to be standing in.
 *
 * This is that surface, extracted. It is floating rather than docked for
 * the same reason it is shared: a docked panel belongs to the window it
 * is docked to, and six docked panels read as six panels however alike
 * they look. One slab of glass that hovers over a graph, a table and a
 * calendar reads as one instrument you carried there.
 *
 * Host apps drive it with three calls:
 *
 *   Inspector.install({ api, onChanged, onOpenMora })   once, after the bridge is up
 *   Inspector.open(moteId, { anchor, newTab })
 *   Inspector.close()
 *   Inspector.updateAnchor(anchor)          re-anchor the leader after a pan/zoom
 *
 * and everything else -- typed editors, the tab stack, species, the
 * leader line, autosave -- is in here, identical wherever it runs.
 *
 * Kept in frontend_shared/ and symlinked into each app's frontend/,
 * because pywebview serves each frontend directory as its own HTTP root
 * and a file above that root is simply not fetchable. The symlink is
 * what makes six copies one file; see docs/architecture.md. */

const Inspector = (() => {
  "use strict";

  // ---- the species palette --------------------------------------------
  //
  // Byte for byte what monarch/frontend/graph.js and the four other
  // windows carry, and it has to be: a species that is teal in Monarch
  // must be teal in the inspector floating over Monarch, and in the
  // inspector floating over Monolith. Same constants, same claiming
  // order, same universe (api_common.species_ids), same colour.

  const SPECIES_HUE_STEP = 137.508;    // golden angle
  const SPECIES_LAP = 12;              // entries before the tone changes
  const SPECIES_TONES = [
    { l: 0.72, c: 0.13 },
    { l: 0.85, c: 0.09 },
    { l: 0.60, c: 0.16 },
    { l: 0.79, c: 0.20 },
  ];
  const SPECIES_PALETTE_SIZE = SPECIES_LAP * SPECIES_TONES.length;
  const UNKNOWN_COLOR = "var(--unknown)";

  function hashString(text) {
    let hash = 0;
    for (let i = 0; i < text.length; i++) {
      hash = (hash * 31 + text.charCodeAt(i)) | 0;
    }
    return Math.abs(hash);
  }

  function paletteEntry(slot) {
    const index = ((slot % SPECIES_PALETTE_SIZE) + SPECIES_PALETTE_SIZE) % SPECIES_PALETTE_SIZE;
    return {
      h: (index * SPECIES_HUE_STEP + 25) % 360,
      ...SPECIES_TONES[Math.floor(index / SPECIES_LAP)],
    };
  }

  function paletteColor(slot, emphatic) {
    const entry = paletteEntry(slot);
    const l = emphatic ? Math.min(0.9, entry.l + 0.07) : entry.l;
    const c = emphatic ? entry.c + 0.02 : entry.c;
    return `oklch(${l.toFixed(3)} ${c.toFixed(3)} ${entry.h.toFixed(1)})`;
  }

  let speciesUniverse = [];
  let slotCache = null;

  function setUniverse(ids) {
    const next = (ids || []).slice().sort();
    if (next.join(" ") === speciesUniverse.join(" ")) return;
    speciesUniverse = next;
    slotCache = null;
  }

  /** species id -> palette entry, each species preferring the entry its
   *  id hashes to and probing forward when that one is claimed. Claimed
   *  from the garden's whole species list rather than the ones on
   *  screen, for the reason graph.js's copy spells out: a palette
   *  claimed from a subset colours a species one way here and another
   *  way in the window next to it. */
  function slots() {
    if (slotCache) return slotCache;
    const map = new Map();
    const taken = new Set();
    for (const id of speciesUniverse) {
      let slot = hashString(id) % SPECIES_PALETTE_SIZE;
      for (let step = 0; step < SPECIES_PALETTE_SIZE && taken.has(slot); step++) {
        slot = (slot + 1) % SPECIES_PALETTE_SIZE;
      }
      taken.add(slot);
      map.set(id, slot);
    }
    slotCache = map;
    return map;
  }

  function speciesColor(speciesId, emphatic) {
    if (!speciesId) return UNKNOWN_COLOR;
    const map = slots();
    // A species made since the last payload has claimed nothing yet; its
    // unprobed entry still gives its instances a consistent colour.
    const slot = map.has(speciesId) ? map.get(speciesId)
      : hashString(speciesId) % SPECIES_PALETTE_SIZE;
    return paletteColor(slot, !!emphatic);
  }

  // ---- state ----------------------------------------------------------

  const GEOMETRY_KEY = "monoss:inspector:geometry";
  const ADVANCED_KEY = "monoss:inspector:advanced";
  const MAX_TABS = 6;
  const SAVE_DEBOUNCE_MS = 400;
  const SAVED_FLASH_MS = 1400;

  let api = null;
  let onChanged = () => {};
  let extraSections = [];
  let hostEditor = null;
  let notesDoor = true;
  let onOpenMora = () => {};

  let root = null;
  let leader = null;
  let els = {};

  const state = {
    stack: [],          // [{ id, payload, anchor }] -- the open tabs
    active: -1,
    open: false,
    advanced: false,
    picking: false,     // the add-relationship picker
    relType: "",
    candidates: [],
    why: {},            // key -> the rendered provenance answer, if asked
    pos: { x: 0, y: 0 },
    size: { w: 392, h: 560 },
    note: "autosaves as you type",
  };

  let savedTimer = null;
  const pending = new Map();   // key -> debounce timer, one per property

  // ---- install --------------------------------------------------------

  /** Called once by the host app after window.pywebview.api exists.
   *  `onChanged` is how the host learns a write happened -- it redraws
   *  its own graph/table/board, since the inspector edits the same motes
   *  that view is showing and has no idea how to repaint them. */
  function install(options) {
    api = options.api;
    onChanged = options.onChanged || (() => {});
    extraSections = options.advancedSections || [];
    hostEditor = options.valueEditor || null;
    // Mora is the room, not a door to it.
    notesDoor = options.notesDoor !== false;
    onOpenMora = options.onOpenMora || (() => {});
    build();
    restoreGeometry();
    state.advanced = localStorage.getItem(ADVANCED_KEY) === "1";

    window.addEventListener("resize", clampToViewport);
    window.addEventListener("mousemove", onPointerMove);
    window.addEventListener("mouseup", onPointerUp);
    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && state.open && !isTypingInside(event.target)) close();
    });
  }

  /** Escape closes the panel, except while you are typing in it -- there,
   *  Escape means "abandon what I'm typing in this field", which is what
   *  blurring does. Closing the whole panel on the first Escape after a
   *  mistyped value is how you lose the other four you had just set. */
  function isTypingInside(target) {
    // A keydown's target is not always an element -- it is `window` when
    // nothing is focused, and Node.contains() throws on anything that
    // isn't a Node rather than returning false.
    if (!root || !(target instanceof Node) || !root.contains(target)) return false;
    const tag = (target.tagName || "").toLowerCase();
    if (tag !== "input" && tag !== "textarea" && tag !== "select") return false;
    target.blur();
    return true;
  }

  // ---- opening --------------------------------------------------------

  /** Show `moteId`.
   *
   *  `newTab` pushes it onto the stack instead of replacing the active
   *  tab -- ctrl/cmd-click in the host, and how following a relationship
   *  keeps where you came from. `anchor` is the element (or a point) the
   *  mote is drawn at, which the leader line draws back to. */
  async function open(moteId, options = {}) {
    if (!api || !moteId) return;
    const anchor = anchorPoint(options.anchor);
    const existing = state.stack.findIndex((tab) => tab.id === moteId);

    if (existing !== -1) {
      state.stack[existing].anchor = anchor || state.stack[existing].anchor;
      state.active = existing;
    } else if (options.newTab || state.active === -1 || !state.stack.length) {
      state.stack.push({ id: moteId, payload: null, anchor });
      if (state.stack.length > MAX_TABS) state.stack.shift();
      state.active = state.stack.length - 1;
    } else {
      state.stack[state.active] = { id: moteId, payload: null, anchor };
    }

    state.open = true;
    state.picking = false;
    state.why = {};
    if (anchor && !hasStoredGeometry()) positionBeside(anchor);
    render();
    await load();
  }

  function anchorPoint(anchor) {
    if (!anchor) return null;
    if (anchor.getBoundingClientRect) {
      const box = anchor.getBoundingClientRect();
      return { left: box.left, top: box.top, right: box.right, bottom: box.bottom };
    }
    if (typeof anchor.x === "number" && typeof anchor.y === "number") {
      return { x: anchor.x, y: anchor.y };
    }
    return null;
  }

  /** A single point from an anchor -- for a box, the corner nearest
   *  `toward` (the panel), so the leader lands on whichever corner is
   *  closest instead of always the bottom-right, and never has to cross
   *  over the box to get there. For a plain point, itself. */
  function nearestPoint(anchor, toward) {
    if (anchor.left === undefined) return anchor;
    const x = toward.x < (anchor.left + anchor.right) / 2 ? anchor.left : anchor.right;
    const y = toward.y < (anchor.top + anchor.bottom) / 2 ? anchor.top : anchor.bottom;
    return { x, y };
  }

  async function load() {
    const tab = state.stack[state.active];
    if (!tab) return;
    const fresh = await bridge(() => api.inspect_mote(tab.id));
    if (!fresh) {
      // The mote went away under us -- another window deleted it, or a
      // sync pulled its deletion in. Drop the tab rather than showing a
      // panel about nothing.
      closeTab(state.stack.indexOf(tab));
      return;
    }
    tab.payload = fresh;
    setUniverse(fresh.speciesUniverse);
    render();
  }

  async function bridge(thunk) {
    try {
      return await thunk();
    } catch (err) {
      note(String((err && err.message) || err));
      return null;
    }
  }

  function close() {
    IntervalPicker.closeOpen();
    state.open = false;
    state.picking = false;
    render();
  }

  function closeTab(index) {
    IntervalPicker.closeOpen();
    if (index < 0 || index >= state.stack.length) return;
    state.stack.splice(index, 1);
    if (!state.stack.length) {
      state.active = -1;
      state.open = false;
    } else if (state.active > index) {
      state.active -= 1;
    } else if (state.active === index) {
      state.active = Math.min(index, state.stack.length - 1);
    }
    state.why = {};
    render();
    if (state.open) load();
  }

  const current = () => (state.active === -1 ? null : state.stack[state.active]) || null;
  const payload = () => (current() ? current().payload : null);

  // ---- writing --------------------------------------------------------

  /** Debounced so that typing a word is one write rather than eight.
   *  Per key, not one shared timer: tabbing from a half-typed `status`
   *  into `priority` must not let the second edit cancel the first.
   *
   *  The panel is never repainted from a debounced write's answer. Doing
   *  so would rebuild the input you are still typing in, moving the
   *  caret to the end mid-word -- so the fresh payload is stored and the
   *  next deliberate redraw picks it up. */
  function writeProperty(key, value, { immediate = false } = {}) {
    const tab = current();
    if (!tab) return;
    const moteId = tab.id;
    clearTimeout(pending.get(key));

    const commit = async () => {
      pending.delete(key);
      const next = await bridge(() => api.inspector_set_property(moteId, key, value));
      if (next && tab.id === moteId) tab.payload = next;
      flashSaved(key);
      onChanged();
    };

    if (immediate) commit();
    else pending.set(key, setTimeout(commit, SAVE_DEBOUNCE_MS));
  }

  /** Structural edits -- add, remove, rename, link, unlink, species -- go
   *  through here instead: they change what the panel *contains* rather
   *  than what one field holds, so they write at once and redraw. */
  async function structural(thunk, label) {
    const tab = current();
    if (!tab) return;
    const next = await bridge(thunk);
    if (next) tab.payload = next;
    flashSaved(label);
    render();
    onChanged();
  }

  function flashSaved(label) {
    state.note = label ? `saved - ${label}` : "saved";
    els.saved.classList.remove("hidden");
    els.note.textContent = state.note;
    clearTimeout(savedTimer);
    savedTimer = setTimeout(() => {
      els.saved.classList.add("hidden");
      state.note = "autosaves as you type";
      els.note.textContent = state.note;
    }, SAVED_FLASH_MS);
  }

  function note(text) {
    state.note = text;
    els.note.textContent = text;
  }

  // ---- chrome ---------------------------------------------------------

  function build() {
    leader = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    leader.setAttribute("class", "ins-leader hidden");
    leader.innerHTML =
      `<line stroke="oklch(0.74 0.1 68 / 0.5)" stroke-width="1" stroke-dasharray="3 3"></line>` +
      `<circle r="3.5" fill="var(--accent)"></circle>`;
    document.body.appendChild(leader);

    root = document.createElement("div");
    root.className = "ins hidden";
    root.innerHTML = `
      <div class="ins-tabs"></div>
      <div class="ins-head">
        <div class="ins-eyebrow">
          <span class="ins-dot"></span>
          <span class="ins-species"></span>
          <span class="ins-saved hidden">saved</span>
          <button class="ins-x ins-head-close" type="button" title="Close inspector (Esc)">×</button>
        </div>
        <input class="ins-name" spellcheck="false">
        <div class="ins-name-note hidden">name</div>
        <div class="ins-idline hidden">
          <span class="ins-id"></span>
          <button class="ins-ghost ins-copy" type="button">copy</button>
        </div>
      </div>
      <div class="ins-body"></div>
      <div class="ins-foot">
        <button class="ins-toggle" type="button">
          <span class="ins-track"><span class="ins-knob"></span></span>
          <span>Advanced</span>
        </button>
        <span class="ins-note">autosaves as you type</span>
      </div>
      <div class="ins-grip" title="Resize"></div>`;
    document.body.appendChild(root);

    // X11/GTK's middle-click-paste fires on any focused <input>/<textarea>
    // under the cursor, including the panel's key/value fields - guard the
    // whole panel the same way commandbar.js and graph.js already do.
    root.addEventListener("mousedown", (event) => {
      if (event.button === 1) event.preventDefault();
    });

    els = {
      tabs: root.querySelector(".ins-tabs"),
      head: root.querySelector(".ins-head"),
      dot: root.querySelector(".ins-head .ins-dot"),
      species: root.querySelector(".ins-species"),
      saved: root.querySelector(".ins-saved"),
      headClose: root.querySelector(".ins-head-close"),
      nameNote: root.querySelector(".ins-name-note"),
      name: root.querySelector(".ins-name"),
      idline: root.querySelector(".ins-idline"),
      id: root.querySelector(".ins-id"),
      copy: root.querySelector(".ins-copy"),
      body: root.querySelector(".ins-body"),
      toggle: root.querySelector(".ins-toggle"),
      note: root.querySelector(".ins-note"),
      grip: root.querySelector(".ins-grip"),
      leaderLine: leader.querySelector("line"),
      leaderDot: leader.querySelector("circle"),
    };

    els.name.addEventListener("input", () => writeProperty("name", els.name.value));
    els.name.addEventListener("keydown", (e) => { if (e.key === "Enter") els.name.blur(); });
    els.copy.addEventListener("click", () => {
      const tab = current();
      if (tab && navigator.clipboard) navigator.clipboard.writeText(tab.id);
      note("id copied");
    });
    els.toggle.addEventListener("click", () => {
      state.advanced = !state.advanced;
      if (!state.advanced) state.why = {};
      localStorage.setItem(ADVANCED_KEY, state.advanced ? "1" : "0");
      render();
    });
    els.headClose.addEventListener("mousedown", (event) => {
      event.stopPropagation();
      close();
    });
    els.tabs.addEventListener("mousedown", startDrag);
    els.head.addEventListener("mousedown", startDrag);
    els.grip.addEventListener("mousedown", startResize);
  }

  // ---- render ---------------------------------------------------------

  function render() {
    const tab = current();
    const showing = state.open && !!tab;
    root.classList.toggle("hidden", !showing);
    leader.classList.toggle("hidden", !showing || !tab || !tab.anchor);
    if (!showing) return;

    root.style.left = `${state.pos.x}px`;
    root.style.top = `${state.pos.y}px`;
    root.style.width = `${state.size.w}px`;
    root.style.height = `${state.size.h}px`;
    drawLeader(tab.anchor);

    renderTabs();
    els.toggle.classList.toggle("on", state.advanced);
    els.idline.classList.toggle("hidden", !state.advanced);
    // The title is not a special field -- it is the `name` property in a
    // bigger font, sorted out of the list below so it isn't shown twice.
    // Advanced is where that stops being a convenient fiction.
    els.nameNote.classList.toggle("hidden", !state.advanced);

    const data = tab.payload;
    if (!data) {
      els.species.textContent = "loading";
      els.body.innerHTML = "";
      return;
    }

    els.dot.style.background = speciesColor(data.species.id, data.species.isSpecies);
    els.species.textContent = data.species.label
      || (data.species.isSpecies ? "species" : "no species");
    els.id.textContent = data.id;
    // Only when it actually differs: assigning to a focused input moves
    // the caret to the end, and this redraws while you are typing.
    if (els.name.value !== (data.properties.name ?? "")) {
      els.name.value = data.properties.name ?? "";
    }

    // A host's onChanged fires right after a debounced write commits --
    // Motion's in particular re-fetches and calls this on every commit
    // (see motion/frontend/app.js's refresh -> Inspector.reload). If that
    // lands while you're still in the very field that just committed,
    // rebuilding the body wholesale (innerHTML = "") replaces the input
    // you're typing in with a fresh, unfocused one and the next keystroke
    // goes nowhere. Skip the rebuild while focus is inside it; the stored
    // payload is picked up by the next redraw that isn't mid-edit.
    if (!bodyHasFocus() && !IntervalPicker.isOpen()) {
      els.body.innerHTML = "";
      renderProperties(data);
      renderRelationships(data);
      if (state.advanced) renderAdvanced(data);
    }
  }

  function bodyHasFocus() {
    const active = document.activeElement;
    if (!(active instanceof Node) || !els.body.contains(active)) return false;
    const tag = (active.tagName || "").toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select";
  }

  /** The ribbon earns its space only once there is a choice to make.
   *  With one mote open it is a row containing one tab that is already
   *  active and a close button -- so it hides, the close button moves up
   *  into the title row, and the panel is that much shorter. */
  function renderTabs() {
    const many = state.stack.length > 1;
    els.tabs.classList.toggle("hidden", !many);
    els.headClose.classList.toggle("hidden", many);
    if (!many) { els.tabs.innerHTML = ""; return; }
    els.tabs.innerHTML = "";
    state.stack.forEach((tab, index) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `ins-tab${index === state.active ? " active" : ""}`;

      const dot = document.createElement("span");
      dot.className = "ins-dot";
      const species = tab.payload ? tab.payload.species : null;
      dot.style.background = speciesColor(species && species.id, species && species.isSpecies);

      const label = document.createElement("span");
      label.className = "ins-tab-label";
      label.textContent = tab.payload ? tab.payload.label : tab.id;

      const shut = document.createElement("span");
      shut.className = "ins-x";
      shut.textContent = "×";
      shut.title = "Close tab";
      shut.addEventListener("mousedown", (event) => {
        event.stopPropagation();
        event.preventDefault();
        closeTab(index);
      });

      button.append(dot, label, shut);
      button.addEventListener("mousedown", (event) => {
        event.stopPropagation();
        if (index === state.active) return;
        IntervalPicker.closeOpen();
        state.active = index;
        state.picking = false;
        state.why = {};
        render();
        load();
      });
      els.tabs.appendChild(button);
    });

    const spacer = document.createElement("span");
    spacer.style.cssText = "flex:1;min-width:8px";
    const shut = document.createElement("button");
    shut.type = "button";
    shut.className = "ins-x ins-close";
    shut.textContent = "×";
    shut.title = "Close inspector (Esc)";
    shut.addEventListener("mousedown", (event) => { event.stopPropagation(); close(); });
    els.tabs.append(spacer, shut);
  }

  // ---- properties -----------------------------------------------------

  function renderProperties(data) {
    els.body.appendChild(heading("Properties"));

    const rows = document.createElement("div");
    rows.className = "ins-rows";
    if (!data.keys.length) {
      rows.appendChild(empty("No properties yet -- add one below, or take a suggestion."));
    }
    for (const key of data.keys) {
      rows.appendChild(propertyRow(data, key));
      if (state.why[key]) {
        const out = document.createElement("div");
        out.className = "ins-why-out";
        out.textContent = state.why[key];
        rows.appendChild(out);
      }
    }
    els.body.appendChild(rows);

    els.body.appendChild(addPropertyRow(data));
    if (data.hints.length) els.body.appendChild(hintRow(data));
  }

  function propertyRow(data, key) {
    const meta = data.kinds[key] || { kind: "text" };
    const value = data.properties[key];
    const reserved = data.reserved.includes(key);

    const row = document.createElement("div");
    row.className = "ins-row";

    // The key is an input, not a label: renaming a property in place is
    // otherwise delete-then-add, which loses the value if you stop
    // halfway. Reserved keys (a rule's `equation`, a mote's `units`) are
    // spelled by core/reserved.py and mean something to the solver, so
    // those are shown accented and not renameable.
    const keyEl = document.createElement("input");
    keyEl.className = `ins-key${reserved ? " reserved" : ""}`;
    keyEl.value = key;
    keyEl.readOnly = reserved;
    keyEl.spellcheck = false;
    keyEl.title = reserved
      ? `${key} -- reserved, meaningful to MonOSS itself`
      : `${key} -- click to rename`;
    if (!reserved) {
      keyEl.addEventListener("keydown", (e) => { if (e.key === "Enter") keyEl.blur(); });
      keyEl.addEventListener("blur", () => {
        const next = keyEl.value.trim();
        if (!next || next === key) { keyEl.value = key; return; }
        structural(() => api.inspector_rename_property(data.id, key, next), `${key} -> ${next}`);
      });
    }

    const holder = document.createElement("div");
    holder.className = "ins-val";
    // A host may know a better editor for a particular value than the
    // generic ones below -- Monarch's interval picker is a calendar, and
    // a text box holding an ISO range is not the same instrument. It
    // gets first refusal; anything it declines falls through to the
    // shared editors, so a host that supplies nothing loses nothing.
    const custom = hostEditor
      ? hostEditor(data, key, meta, value, (next) => writeProperty(key, next, { immediate: true }))
      : null;
    holder.appendChild(custom || valueEditor(data, key, meta, value));
    if (!custom) wireValueTypeMenu(data, key, meta, value, holder);

    row.append(keyEl, holder);
    for (const control of trailingControls(data, key, value)) row.appendChild(control);

    // "Why is this value what it is?" -- only Motion can answer, and only
    // in Advanced. Everywhere else inspector_why returns null and the
    // button is never drawn.
    if (state.advanced && api.inspector_why) {
      const why = document.createElement("button");
      why.type = "button";
      why.className = "ins-why";
      why.textContent = "why";
      why.title = "Where this value came from";
      why.addEventListener("click", () => explain(data.id, key));
      row.appendChild(why);
    }

    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "ins-del";
    remove.textContent = "×";
    remove.title = `Remove ${key}`;
    remove.addEventListener("click", () =>
      structural(() => api.inspector_delete_property(data.id, key), `${key} removed`));
    row.appendChild(remove);
    return row;
  }

  /** A property's value has no stored type -- valueEditor (below) picks
   *  an editor by shape, which only works once a value already exists to
   *  sniff. This is the other direction: right-click an empty or
   *  plain-text value to say up front what it should hold. One entry
   *  today (the interval picker, moved here from Monarch's own inspector
   *  when the panel was shared -- docs/architecture.md); a future
   *  property type is another item here, not a redesign. Skipped for
   *  values that already have a dedicated editor (modoc, enum, ref, map,
   *  boolean, number) or are already interval-shaped, since those either
   *  have no business becoming a date or already are one. */
  function wireValueTypeMenu(data, key, meta, value, holder) {
    if (key === "modoc") return;
    if (["enum", "ref", "map", "boolean", "number"].includes(meta.kind)) return;
    const asText = typeof value === "string" ? value : "";
    if (asText && IntervalPicker.parse(asText)) return;

    holder.addEventListener("contextmenu", (event) => {
      event.preventDefault();
      ContextMenu.open(event.clientX, event.clientY, [
        {
          label: "Date & time…",
          run: () => {
            const seed = (asText && IntervalPicker.parse(asText)) ? asText : IntervalPicker.isoOf(new Date());
            holder.innerHTML = "";
            const picker = IntervalPicker.field(seed, (next) => writeProperty(key, next, { immediate: true }), {
              onClear: () => structural(
                () => api.inspector_set_property(data.id, key, ""), `${key} cleared`),
            });
            holder.appendChild(picker);
            picker.querySelector(".interval-summary-btn")?.click();
          },
        },
      ]);
    });
  }

  /** The one-click follow buttons some reserved keys earn.
   *
   *  `url` and `modoc` are reserved MonOSS property names (see
   *  core/reserved.py) and mean the same thing in every window, so
   *  following one belongs here rather than in whichever app's panel
   *  happened to grow it -- which, until the inspector was shared, was
   *  Monarch's and only Monarch's. */
  function trailingControls(data, key, value) {
    const controls = [];
    if (typeof value !== "string" || !value.trim()) return controls;

    if (key === "url") {
      controls.push(followButton("Open", () => api.inspector_open_link(value)));
    }
    if (key === "modoc") {
      // A .modoc note has live bindings in it (docs/modoc.md) and Mora is
      // the window that renders them. The inspector offers the door
      // rather than pretending a property box is the room.
      controls.push(followButton("Open this note in Mora",
        () => openMora(data.id)));
    }
    return controls;
  }

  /** The one door out of the panel to Mora, from both the ↗ follow
   *  button and double-clicking the note summary. `onOpenMora` is the
   *  host's launcher toast ("Opening Mora…") -- this is the same act of
   *  opening the app, just triggered from inside a mote instead of the
   *  launcher grid, and should read the same way. */
  function openMora(moteId) {
    api.open_app("mora", moteId);
    onOpenMora();
  }

  function followButton(title, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ins-follow";
    button.textContent = "↗";
    button.title = title;
    button.addEventListener("click", () => bridge(action));
    return button;
  }

  /** The typed editor for one value. Which one is decided by the bridge
   *  (api_common.InspectorApi._inspector_kinds), not here: the enum case
   *  needs every sibling instance's properties to work out the closed set
   *  of values, and shipping those to the frontend would make the payload
   *  the size of the garden. */
  function valueEditor(data, key, meta, value) {
    if (key === "modoc" && typeof value === "string") {
      return noteSummary(value, () => bridge(() => openMora(data.id)));
    }

    // By shape, not by key name: `date` stopped being a key the engine
    // reads when a start became an ISO 8601 interval (core/interval.py),
    // so any property holding one gets the calendar -- `when`, `due`,
    // `starts`, or whatever this garden calls it, repeating or not.
    const asText = typeof value === "string" ? value : "";
    if (asText && IntervalPicker.parse(asText)) {
      return IntervalPicker.field(asText, (next) => writeProperty(key, next, { immediate: true }), {
        // Clearing a date stops the value being interval-shaped, so the
        // row needs rebuilding as a plain one.
        onClear: () => structural(
          () => api.inspector_set_property(data.id, key, ""), `${key} cleared`),
      });
    }

    if (meta.kind === "enum") {
      const select = document.createElement("select");
      select.className = "ins-select";
      // The current value may not be in the vocabulary -- the set is
      // inferred from what siblings use, and this mote is allowed to be
      // the first to say something new.
      const options = meta.options.includes(String(value))
        ? meta.options : meta.options.concat([String(value)]);
      for (const option of options) {
        const el = document.createElement("option");
        el.value = option;
        el.textContent = option;
        select.appendChild(el);
      }
      select.value = String(value);
      select.addEventListener("change", () =>
        writeProperty(key, select.value, { immediate: true }));
      return select;
    }

    if (meta.kind === "ref") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ins-ref";
      button.title = "Open this mote -- ctrl-click for a new tab";
      const dot = document.createElement("span");
      dot.className = "ins-dot";
      dot.style.background = speciesColor(meta.refSpeciesId);
      const label = document.createElement("span");
      label.className = "ins-ref-label";
      label.textContent = meta.refLabel;
      const go = document.createElement("span");
      go.className = "ins-ref-go";
      go.textContent = "↗";
      button.append(dot, label, go);
      button.addEventListener("click", (event) =>
        open(String(value), { newTab: event.ctrlKey || event.metaKey }));
      return button;
    }

    if (meta.kind === "map" && value && typeof value === "object") {
      // A dict-valued property (a mote's `units` map is the only one
      // today) has no single scalar to edit -- rendering it through the
      // generic text box below would just print "[object Object]". It's
      // set structurally (the "/" command bar, `Mote.prop = 1 ft`), so
      // the inspector shows it read-only as `key: value` lines rather
      // than pretending it's a text field.
      const list = document.createElement("div");
      list.className = "ins-map";
      const entries = Object.entries(value);
      if (!entries.length) {
        list.textContent = "(empty)";
      } else {
        for (const [k, v] of entries) {
          const line = document.createElement("div");
          line.className = "ins-map-row";
          line.textContent = `${k}: ${v}`;
          list.appendChild(line);
        }
      }
      return list;
    }

    if (meta.kind === "boolean") {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ins-bool";
      const track = document.createElement("span");
      track.className = "ins-track";
      const knob = document.createElement("span");
      knob.className = "ins-knob";
      track.appendChild(knob);
      const label = document.createElement("span");
      const paint = (on) => {
        track.style.background = on ? "oklch(0.4 0.07 68)" : "";
        knob.style.left = on ? "12px" : "1px";
        knob.style.background = on ? "var(--accent-bright)" : "";
        label.textContent = on ? "true" : "false";
      };
      button.append(track, label);
      paint(value === true);
      button.addEventListener("click", () => {
        const next = label.textContent !== "true";
        paint(next);
        writeProperty(key, next, { immediate: true });
      });
      return button;
    }


    if (meta.kind === "number") {
      const input = document.createElement("input");
      input.type = "number";
      input.className = "ins-input mono";
      input.value = String(value);
      input.addEventListener("input", () => {
        // An empty box is mid-edit, not "make this zero" -- wait for a
        // number rather than writing NaN into the garden on backspace.
        if (input.value === "" || Number.isNaN(Number(input.value))) return;
        writeProperty(key, Number(input.value));
      });
      if (!meta.unit) return input;

      // A declared unit (`Mote.prop = 8 ft`, or the "/" bar's `units`
      // form) is silently honored by the solver -- it converts to a
      // common base before combining values across motes -- but until
      // now nothing in this shared panel showed that a unit was even
      // attached. The badge names it; the tooltip gives the base-SI
      // equivalent so a conversion that already happens under the hood
      // is at least visible somewhere.
      const wrap = document.createElement("div");
      wrap.className = "ins-unit-wrap";
      const badge = document.createElement("span");
      badge.className = "ins-unit-badge";
      badge.textContent = meta.unit;
      if (meta.unitBase) badge.title = `= ${meta.unitBase}`;
      wrap.append(input, badge);
      return wrap;
    }

    // Long strings get a textarea: a rule's equation and a note's body
    // are properties like any other, and a 12px single-line box is the
    // wrong instrument for either.
    const text = String(value ?? "");
    if (text.length > 60 || text.includes("\n")) {
      const area = document.createElement("textarea");
      area.className = "ins-input text-area";
      area.spellcheck = false;
      area.value = text;
      area.addEventListener("input", () => writeProperty(key, area.value));
      return area;
    }

    const input = document.createElement("input");
    input.className = "ins-input";
    input.spellcheck = false;
    input.value = text;
    input.addEventListener("input", () => writeProperty(key, input.value));
    return input;
  }

  /** A note, summarized and not editable. Mora renders .modoc; a 12px
   *  property box can only mangle it, so the panel says how much note
   *  there is and leaves the editing to the window that can do it.
   *  Double-clicking it is the same door as the ↗ follow button next to
   *  it (trailingControls) -- this is just the bigger target, since the
   *  box is what most of the row's width actually is. */
  function noteSummary(text, onOpen) {
    const lines = text.split("\n").length;
    const field = document.createElement("input");
    field.className = "ins-input";
    field.readOnly = true;
    field.value = `${lines} line${lines === 1 ? "" : "s"}`;
    field.title = "Notes are edited in Mora -- double-click to open";
    if (onOpen) field.addEventListener("dblclick", onOpen);
    return field;
  }

  function addPropertyRow(data) {
    const row = document.createElement("div");
    row.className = "ins-add";

    const key = document.createElement("input");
    key.className = "ins-input ins-add-key";
    key.placeholder = "key";
    key.spellcheck = false;

    const value = document.createElement("input");
    value.className = "ins-input";
    value.placeholder = "value";
    value.spellcheck = false;

    const add = document.createElement("button");
    add.type = "button";
    add.className = "ins-btn";
    add.textContent = "Add";

    const submit = () => {
      const name = key.value.trim();
      if (!name) { key.focus(); return; }
      // Sent as the typed string: api_common.coerce is what decides "6"
      // is a number and "six" is not, and it is the same function every
      // other text-entry path in MonOSS already goes through.
      structural(() => api.inspector_set_property(data.id, name, value.value), `${name} added`);
      key.value = "";
      value.value = "";
    };
    add.addEventListener("click", submit);
    for (const field of [key, value]) {
      field.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    }

    row.append(key, value, add);
    return row;
  }

  /** The design's "Person usually has role, email, team" chips, sourced
   *  from the species' template rather than a hardcoded table -- see
   *  api_common._template_hints. A species with no template offers none,
   *  which is honest: nothing in the garden has said what this kind of
   *  mote usually carries. */
  function hintRow(data) {
    const row = document.createElement("div");
    row.className = "ins-hints";

    const label = document.createElement("span");
    label.className = "ins-hint-label";
    label.textContent = `${data.species.label || "this"} usually has`;
    row.appendChild(label);

    for (const { key, value } of data.hints) {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "ins-chip";
      chip.textContent = `+ ${key}`;
      chip.title = `Add ${key}: ${typeof value === "string" ? value : JSON.stringify(value)}`;
      chip.addEventListener("click", () =>
        structural(() => api.inspector_set_property(data.id, key, value), `${key} added`));
      row.appendChild(chip);
    }
    return row;
  }

  // ---- relationships --------------------------------------------------

  function renderRelationships(data) {
    els.body.appendChild(heading("Relationships"));

    const rows = document.createElement("div");
    rows.className = "ins-rows";
    if (!data.relationships.length) {
      rows.appendChild(empty("Nothing points at this mote, and it points at nothing."));
    }
    for (const rel of data.relationships) {
      const row = document.createElement("div");
      row.className = "ins-rel";

      const arrow = document.createElement("span");
      arrow.className = "ins-arrow";
      arrow.textContent = rel.direction === "out" ? "→" : "←";

      const type = document.createElement("span");
      // Reserved types mean something to MonOSS itself (core/reserved.py)
      // -- `applies_to` wires a rule to what it governs. Accented for the
      // same reason a reserved property key is: so a type you are about
      // to unlink reads as load-bearing rather than as one you coined.
      type.className = `ins-rel-type${rel.reserved ? " reserved" : ""}`;
      type.textContent = rel.type;
      type.title = rel.reserved
        ? `${rel.type} -- reserved, meaningful to MonOSS itself`
        : rel.type;

      const other = document.createElement("button");
      other.type = "button";
      other.className = "ins-rel-other";
      other.title = "Open this mote -- ctrl-click for a new tab";
      const dot = document.createElement("span");
      dot.className = "ins-dot";
      dot.style.background = speciesColor(rel.otherSpeciesId);
      const label = document.createElement("span");
      label.textContent = rel.otherLabel;
      other.append(dot, label);
      other.addEventListener("click", (event) =>
        open(rel.otherId, { newTab: event.ctrlKey || event.metaKey }));

      const cut = document.createElement("button");
      cut.type = "button";
      cut.className = "ins-del";
      cut.textContent = "×";
      cut.title = "Remove this relationship";
      cut.addEventListener("click", () =>
        structural(() => api.inspector_unrelate(data.id, rel.id), "unlinked"));

      row.append(arrow, type, other, cut);
      rows.appendChild(row);
    }
    els.body.appendChild(rows);

    els.body.appendChild(state.picking ? relationshipPicker(data) : addRelationshipButton());
  }

  function addRelationshipButton() {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ins-ghost";
    button.style.marginTop = "7px";
    button.textContent = "+ Add relationship";
    button.addEventListener("click", () => {
      state.picking = true;
      render();
      searchCandidates("");
    });
    return button;
  }

  function relationshipPicker(data) {
    const box = document.createElement("div");
    box.className = "ins-picker";

    const top = document.createElement("div");
    top.className = "ins-picker-top";

    // A combo rather than a plain select: the list is the types this
    // garden already uses (api_common._relationship_types), but a new
    // relationship is often exactly where a new type gets coined, so
    // typing one that isn't listed has to work.
    const type = document.createElement("input");
    type.className = "ins-input ins-picker-type";
    type.setAttribute("list", "ins-rel-types");
    type.placeholder = "type";
    type.spellcheck = false;
    type.value = state.relType || data.relTypes[0] || "related_to";
    type.addEventListener("input", () => { state.relType = type.value; });

    const list = document.createElement("datalist");
    list.id = "ins-rel-types";
    for (const name of data.relTypes) {
      const option = document.createElement("option");
      option.value = name;
      list.appendChild(option);
    }

    const query = document.createElement("input");
    query.className = "ins-input";
    query.placeholder = "find a mote...";
    query.spellcheck = false;
    let debounce = null;
    query.addEventListener("input", () => {
      clearTimeout(debounce);
      debounce = setTimeout(() => searchCandidates(query.value), 180);
    });

    const shut = document.createElement("button");
    shut.type = "button";
    shut.className = "ins-x";
    shut.textContent = "×";
    shut.addEventListener("click", () => { state.picking = false; render(); });

    top.append(type, list, query, shut);
    box.appendChild(top);
    box.appendChild(candidateList(data, type));

    setTimeout(() => query.focus(), 0);
    return box;
  }

  function candidateList(data, typeField) {
    const candidates = document.createElement("div");
    candidates.className = "ins-candidates";
    const usable = state.candidates.filter((c) => c.id !== data.id);
    if (!usable.length) {
      candidates.appendChild(empty("No matching motes."));
    }
    for (const candidate of usable) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "ins-candidate";
      const dot = document.createElement("span");
      dot.className = "ins-dot";
      dot.style.background = speciesColor(candidate.speciesId);
      const label = document.createElement("span");
      label.className = "ins-candidate-label";
      label.textContent = candidate.label;
      const species = document.createElement("span");
      species.className = "ins-candidate-species";
      species.textContent = candidate.speciesLabel || "";
      button.append(dot, label, species);
      button.addEventListener("click", () => {
        const relType = (typeField.value || "").trim() || "related_to";
        state.picking = false;
        structural(() => api.inspector_relate(data.id, candidate.id, relType), "linked");
      });
      candidates.appendChild(button);
    }
    return candidates;
  }

  async function searchCandidates(text) {
    const found = await bridge(() => api.inspector_search(text, 8));
    state.candidates = found || [];
    const data = payload();
    if (!state.picking || !data) return;
    // Swap only the candidate list, so the query box keeps focus and
    // whatever has been typed into it.
    const existing = els.body.querySelector(".ins-candidates");
    const typeField = els.body.querySelector(".ins-picker-type");
    if (existing && typeField) existing.replaceWith(candidateList(data, typeField));
  }

  // ---- advanced -------------------------------------------------------

  function renderAdvanced(data) {
    // No species dropdown here any more. A species *is* an `instance_of`
    // relationship, and the relationship list shows it -- so a second
    // control saying the same thing was one more place for the two to
    // disagree. Setting a species is adding that relationship, in the
    // picker, like any other.
    if (data.species.id) {
      els.body.appendChild(heading("Template"));
      if (data.species.isTemplate) {
        els.body.appendChild(empty(`This mote is the template for ${data.species.label}.`));
      } else {
        const promote = document.createElement("button");
        promote.type = "button";
        promote.className = "ins-ghost";
        promote.textContent = data.species.hasTemplate
          ? `Replace the template for ${data.species.label}`
          : `Make this the template for ${data.species.label}`;
        promote.title = "New instances of this species will start from this mote's properties";
        promote.addEventListener("click", () =>
          structural(() => api.inspector_set_template(data.id, data.species.id), "template set"));
        els.body.appendChild(promote);
      }
    }

    for (const section of extraSections) {
      const node = section(data, { structural, speciesColor, heading, empty });
      if (node) els.body.appendChild(node);
    }

    els.body.appendChild(heading("Raw"));
    const raw = document.createElement("pre");
    raw.className = "ins-raw";
    raw.textContent = JSON.stringify(
      { id: data.id, species: data.species.id, properties: data.properties }, null, 2);
    els.body.appendChild(raw);
  }

  async function explain(moteId, key) {
    if (state.why[key]) {
      delete state.why[key];
      render();
      return;
    }
    const answer = await bridge(() => api.inspector_why(moteId, key));
    state.why[key] = answer && answer.lines && answer.lines.length
      ? answer.lines.join("\n")
      : "No logged run determined this value.";
    render();
  }

  // ---- small builders -------------------------------------------------

  function heading(text) {
    const el = document.createElement("div");
    el.className = "ins-section";
    el.textContent = text;
    return el;
  }

  function empty(text) {
    const el = document.createElement("div");
    el.className = "ins-empty";
    el.textContent = text;
    return el;
  }

  // ---- geometry -------------------------------------------------------

  function hasStoredGeometry() {
    return localStorage.getItem(GEOMETRY_KEY) !== null;
  }

  /** The default is the upper right of whatever window this is, and it
   *  is the same default in all seven -- that sameness is the point, since
   *  it is what makes the panel feel like one surface you keep meeting
   *  rather than six panels that resemble each other. Where you then
   *  drag it is remembered per app: each window is its own origin, so
   *  localStorage cannot literally be shared, and a panel that suits a
   *  wide graph canvas may not suit a dense table anyway. */
  function restoreGeometry() {
    let stored = null;
    try { stored = JSON.parse(localStorage.getItem(GEOMETRY_KEY) || "null"); } catch { /* ignore */ }
    if (stored && typeof stored.w === "number") {
      state.size = { w: stored.w, h: stored.h };
      state.pos = { x: stored.x, y: stored.y };
    } else {
      state.size = { w: 392, h: Math.min(560, window.innerHeight - 90) };
      state.pos = { x: window.innerWidth - state.size.w - 24, y: 58 };
    }
    clampToViewport();
  }

  function saveGeometry() {
    localStorage.setItem(GEOMETRY_KEY, JSON.stringify({
      x: state.pos.x, y: state.pos.y, w: state.size.w, h: state.size.h,
    }));
  }

  /** Opens beside the mote rather than over it, and only before the
   *  panel has a remembered home -- once you have put it somewhere, it
   *  staying put is worth more than it following each click. */
  function positionBeside(anchor) {
    const p = anchor.left === undefined ? anchor : { x: anchor.right, y: anchor.bottom };
    state.pos = clamp(p.x + 34, p.y - 90);
  }

  function clamp(x, y) {
    return {
      x: Math.max(10, Math.min(x, window.innerWidth - state.size.w - 14)),
      y: Math.max(10, Math.min(y, window.innerHeight - state.size.h - 16)),
    };
  }

  function clampToViewport() {
    state.size = {
      w: Math.max(310, Math.min(state.size.w, window.innerWidth - 28)),
      h: Math.max(300, Math.min(state.size.h, window.innerHeight - 76)),
    };
    state.pos = clamp(state.pos.x, state.pos.y);
    if (state.open) render();
  }

  /** Re-anchor the leader line without changing which mote or which
   *  panel tab is showing -- for a host whose canvas can pan or zoom
   *  out from under an open panel (Motion's board). `anchor` is the
   *  same shape `open()` takes: an element or a point. */
  function updateAnchor(anchor) {
    const tab = current();
    if (!tab) return;
    const point = anchorPoint(anchor);
    if (!point) return;
    tab.anchor = point;
    if (state.open) drawLeader(tab.anchor);
  }

  function drawLeader(anchor) {
    if (!anchor) { leader.classList.add("hidden"); return; }
    leader.classList.remove("hidden");
    const target = { x: state.pos.x + 6, y: state.pos.y + 46 };
    const point = nearestPoint(anchor, target);
    els.leaderLine.setAttribute("x1", point.x);
    els.leaderLine.setAttribute("y1", point.y);
    els.leaderLine.setAttribute("x2", target.x);
    els.leaderLine.setAttribute("y2", target.y);
    els.leaderDot.setAttribute("cx", point.x);
    els.leaderDot.setAttribute("cy", point.y);
  }

  let drag = null;
  let resize = null;

  function startDrag(event) {
    // Left button only, and never from a control inside the header --
    // dragging must not start because you aimed at the name field.
    if (event.button !== 0) return;
    const tag = (event.target.tagName || "").toLowerCase();
    if (["input", "textarea", "select", "button"].includes(tag)) return;
    drag = { sx: event.clientX, sy: event.clientY, ox: state.pos.x, oy: state.pos.y };
    event.preventDefault();
  }

  function startResize(event) {
    event.stopPropagation();
    event.preventDefault();
    resize = { sx: event.clientX, sy: event.clientY, ow: state.size.w, oh: state.size.h };
  }

  function onPointerMove(event) {
    if (drag) {
      state.pos = clamp(drag.ox + (event.clientX - drag.sx), drag.oy + (event.clientY - drag.sy));
    } else if (resize) {
      state.size = {
        w: Math.max(310, Math.min(620, resize.ow + (event.clientX - resize.sx))),
        h: Math.max(300, Math.min(window.innerHeight - 90, resize.oh + (event.clientY - resize.sy))),
      };
    } else {
      return;
    }
    root.style.left = `${state.pos.x}px`;
    root.style.top = `${state.pos.y}px`;
    root.style.width = `${state.size.w}px`;
    root.style.height = `${state.size.h}px`;
    const tab = current();
    if (tab) drawLeader(tab.anchor);
  }

  function onPointerUp() {
    if (drag || resize) saveGeometry();
    drag = null;
    resize = null;
  }

  return {
    install, open, close, updateAnchor,
    isOpen: () => state.open,
    currentId: () => (current() ? current().id : null),
    /** Re-read the open mote from the garden -- for a host whose own
     *  refresh may have changed it (a sync pull, an edit made elsewhere
     *  in the same window). */
    reload: () => (state.open ? load() : Promise.resolve()),
    speciesColor,
  };
})();
