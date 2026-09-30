/* The app switcher (⌘/Ctrl+J) -- every sibling app but this one, in a
 * fixed order so "Motion is ⌘5" holds regardless of what's typed into
 * the filter box. See api_common.py::APPS for the id each maps to on
 * the Python side.
 *
 * Host apps drive it with one call, same shape as CommandBar's:
 *
 *   Launcher.install({ api, thisApp, getGardenName, getFocus, onError })
 *
 * `getFocus()` is optional and returns the mote id (if any) the sibling
 * app should open focused on -- Motion's selected node, Moxie's plotted
 * x-mote, or nothing for an app with no such notion. `onError(message)`
 * is how a failed launch reaches whatever this window uses for a toast.
 * The overlay and its ⌘J / ⌘1-9 shortcuts are wired here, once; a host
 * only needs its own toolbar button to call Launcher.open().
 *
 * Kept in frontend_shared/ and symlinked into each app's frontend/, like
 * commandbar.js and inspector.js; see docs/architecture.md. */

const Launcher = (() => {
  "use strict";

  const COLUMNS = 2; // matches #launcher-results' CSS grid-template-columns

  const APPS = [
    {
      id: "monarch", name: "Monarch", kind: "graph",
      desc: "Motes and relationships in a live force field.",
      icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
        <circle cx="20" cy="20" r="12" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></circle>
        <ellipse cx="20" cy="20" rx="12" ry="4.5" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6" transform="rotate(-28 20 20)"></ellipse>
        <circle cx="20" cy="20" r="4" fill="var(--accent)"></circle>
      </svg>`,
    },
    {
      id: "mora", name: "Mora", kind: "notes",
      desc: "Write and link long-form .modoc notes.",
      icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
        <rect x="9" y="6" width="22" height="28" rx="3" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></rect>
        <rect x="14" y="14" width="12" height="1.8" rx="0.9" fill="var(--fg)" opacity="0.7"></rect>
        <rect x="14" y="20" width="12" height="1.8" rx="0.9" fill="var(--fg)" opacity="0.7"></rect>
        <rect x="14" y="26" width="7" height="1.8" rx="0.9" fill="var(--accent)"></rect>
      </svg>`,
    },
    {
      id: "monastery", name: "Monastery", kind: "calendar",
      desc: "See and schedule a garden's dated motes.",
      icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
        <rect x="7" y="9" width="26" height="24" rx="3" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.6"></rect>
        <rect x="7" y="15" width="26" height="1.6" fill="var(--fg)" opacity="0.5"></rect>
        <rect x="12" y="20" width="5" height="5" rx="1" fill="var(--fg)" opacity="0.55"></rect>
        <rect x="21" y="20" width="5" height="5" rx="1" fill="var(--accent)"></rect>
      </svg>`,
    },
    {
      id: "motion", name: "Motion", kind: "simulation",
      desc: "Run the garden's rules and scrub the result tick by tick.",
      icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
        <circle cx="20" cy="20" r="10.5" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.55"></circle>
        <ellipse cx="20" cy="20" rx="10.5" ry="4" fill="none" stroke="var(--fg)" stroke-width="1.6" opacity="0.55" transform="rotate(-28 20 20)"></ellipse>
        <circle cx="20" cy="20" r="3.2" fill="var(--accent)"></circle>
        <circle cx="29.4" cy="15.3" r="2" fill="var(--accent)"></circle>
      </svg>`,
    },
    {
      id: "moxie", name: "Moxie", kind: "graphs",
      desc: "Sweep one property and graph another against it.",
      icon: `<svg width="20" height="20" viewBox="0 0 40 40" aria-hidden="true">
        <path d="M7 30 C14.5 30 13.5 11 20 11 C26.5 11 25.5 30 33 30" fill="none"
              stroke="var(--fg)" stroke-width="2.2" stroke-linecap="round" opacity="0.6"></path>
        <circle cx="20" cy="11" r="3" fill="var(--accent)"></circle>
      </svg>`,
    },
  ];

  let api = null;
  let thisApp = null;
  let getGardenName = () => "";
  let getFocus = () => null;
  let onError = () => {};

  let root, input, results, empty, gardenLabel;
  let highlight = 0;

  function install(options) {
    api = options.api;
    thisApp = options.thisApp;
    getGardenName = options.getGardenName || (() => "");
    getFocus = options.getFocus || (() => null);
    onError = options.onError || (() => {});
    build();
    document.addEventListener("keydown", onGlobalKeydown);
  }

  function choices() { return APPS.filter((a) => a.id !== thisApp); }

  function filtered(query) {
    const q = query.trim().toLowerCase();
    const list = choices();
    if (!q) return list;
    return list.filter((a) => (a.name + " " + a.kind + " " + a.desc).toLowerCase().includes(q));
  }

  function build() {
    root = document.createElement("div");
    root.id = "launcher-overlay";
    root.className = "hidden";
    root.innerHTML = `
      <div id="launcher-modal">
        <div id="launcher-input-row">
          <span class="launcher-glyph"></span>
          <input type="text" id="launcher-input" placeholder="Open an app…" autocomplete="off">
          <kbd>esc</kbd>
        </div>
        <div id="launcher-results"></div>
        <div id="launcher-empty" class="hidden"></div>
        <div id="launcher-footer">
          <span>↑↓ select</span><span>↵ open</span><span>⌘1–${choices().length} jump</span>
          <div class="spacer"></div>
          <span id="launcher-garden"></span>
        </div>
      </div>`;
    document.body.appendChild(root);

    input = root.querySelector("#launcher-input");
    results = root.querySelector("#launcher-results");
    empty = root.querySelector("#launcher-empty");
    gardenLabel = root.querySelector("#launcher-garden");

    root.addEventListener("mousedown", (event) => {
      if (event.target === root) close();
    });
    input.addEventListener("input", (event) => render(event.target.value));
    input.addEventListener("keydown", onInputKeydown);
  }

  function render(query) {
    const list = filtered(query);
    highlight = Math.min(highlight, Math.max(list.length - 1, 0));
    results.innerHTML = "";
    list.forEach((app, i) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "launcher-card" + (i === highlight ? " highlighted" : "");
      card.innerHTML =
        `<div class="launcher-icon">${app.icon}</div>` +
        `<div class="launcher-text">` +
        `<div class="launcher-name-row"><span class="launcher-name">${app.name}</span>` +
        `<span class="launcher-kind">${app.kind}</span></div>` +
        `<div class="launcher-desc">${app.desc}</div></div>` +
        `<span class="launcher-key">⌘${i + 1}</span>`;
      card.addEventListener("mouseenter", () => { highlight = i; updateHighlight(); });
      card.addEventListener("mousedown", (event) => { event.preventDefault(); launch(app.id); });
      results.appendChild(card);
    });
    empty.classList.toggle("hidden", list.length > 0);
    empty.textContent = list.length ? "" : `no app matches "${query}"`;
    return list;
  }

  function updateHighlight() {
    results.querySelectorAll(".launcher-card").forEach((card, i) => {
      card.classList.toggle("highlighted", i === highlight);
    });
  }

  const isOpen = () => !!root && !root.classList.contains("hidden");

  function open() {
    highlight = 0;
    root.classList.remove("hidden");
    gardenLabel.textContent = "garden · " + getGardenName();
    input.value = "";
    render("");
    input.focus();
  }

  function close() { if (root) root.classList.add("hidden"); }

  async function launch(appId) {
    close();
    try {
      await api.open_app(appId, getFocus());
    } catch (err) {
      onError(String((err && err.message) || err));
    }
  }

  function onInputKeydown(event) {
    const list = filtered(input.value);
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (highlight + COLUMNS < list.length) highlight += COLUMNS;
      updateHighlight();
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (highlight - COLUMNS >= 0) highlight -= COLUMNS;
      updateHighlight();
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      if (highlight % COLUMNS < COLUMNS - 1 && highlight + 1 < list.length) highlight++;
      updateHighlight();
    } else if (event.key === "ArrowLeft") {
      event.preventDefault();
      if (highlight % COLUMNS > 0) highlight--;
      updateHighlight();
    } else if (event.key === "Enter" && list[highlight]) {
      launch(list[highlight].id);
    }
  }

  function onGlobalKeydown(event) {
    if (event.key === "Escape" && isOpen()) {
      event.preventDefault();
      close();
      return;
    }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "j") {
      event.preventDefault();
      isOpen() ? close() : open();
      return;
    }
    if ((event.metaKey || event.ctrlKey) && /^[1-9]$/.test(event.key)) {
      const app = choices()[Number(event.key) - 1];
      if (app) {
        event.preventDefault();
        launch(app.id);
      }
    }
  }

  return { install, open, close, isOpen };
})();
