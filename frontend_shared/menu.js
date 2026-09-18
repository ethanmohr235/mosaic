/* The right-click menu -- one dropdown, seven windows.
 *
 * Monarch had one, driven off its canvas. Nothing else did: right-click
 * anywhere in the other five either did nothing or fell through to
 * WebKit's own menu. That was tolerable while each window's chrome was
 * its own business, and stopped being tolerable the moment a gesture had
 * to mean the same thing everywhere -- "inspect this in a new tab" is a
 * property of a mote, not of the window you happen to be looking at it
 * in.
 *
 * This is the dropdown itself and nothing else. What goes *in* it is
 * each window's own business, passed in as items: Monolith offers bulk
 * operations a calendar has no use for, Monastery offers occurrence
 * actions a table has no concept of. The shared part is that they all
 * look, position and dismiss the same way.
 *
 * Symlinked into each app's frontend/; see docs/architecture.md. */

const ContextMenu = (() => {
  "use strict";

  let root = null;

  function ensure() {
    if (root) return root;
    root = document.createElement("div");
    root.className = "ctx-menu hidden";
    document.body.appendChild(root);
    // Any click that isn't on the menu dismisses it, including one that
    // opens a different menu -- open() closes first for that reason.
    document.addEventListener("mousedown", (event) => {
      if (root.classList.contains("hidden")) return;
      if (!root.contains(event.target)) close();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && !root.classList.contains("hidden")) {
        event.stopPropagation();
        close();
      }
    }, true);
    window.addEventListener("resize", close);
    return root;
  }

  function close() {
    if (root) root.classList.add("hidden");
  }

  const isOpen = () => !!root && !root.classList.contains("hidden");

  /** Open at the click point with `items`.
   *
   *  An item is `{ label, run }`, optionally `danger` (destructive, drawn
   *  red) or `disabled` (shown so the menu's shape stays stable, but
   *  inert). A literal `"-"` is a separator. Falsy entries are skipped,
   *  so a caller can inline `condition && {...}` rather than building the
   *  array in two steps. */
  function open(clientX, clientY, items) {
    const menu = ensure();
    close();
    menu.innerHTML = "";

    for (const item of items) {
      if (!item) continue;
      if (item === "-") {
        const sep = document.createElement("div");
        sep.className = "ctx-sep";
        menu.appendChild(sep);
        continue;
      }
      const el = document.createElement("div");
      el.className = "ctx-item"
        + (item.danger ? " danger" : "")
        + (item.disabled ? " disabled" : "");
      el.textContent = item.label;
      if (!item.disabled) {
        el.addEventListener("click", async () => {
          close();
          await item.run();
        });
      }
      menu.appendChild(el);
    }

    // Placed, then measured, then nudged: a display:none element has no
    // dimensions, so the overflow check has to happen with it visible.
    menu.style.left = "0px";
    menu.style.top = "0px";
    menu.classList.remove("hidden");
    const rect = menu.getBoundingClientRect();
    const margin = 6;
    const left = Math.min(clientX, window.innerWidth - rect.width - margin);
    const top = Math.min(clientY, window.innerHeight - rect.height - margin);
    menu.style.left = Math.max(margin, left) + "px";
    menu.style.top = Math.max(margin, top) + "px";
  }

  /** The two items every window's mote menu starts with, so the gesture
   *  reads the same in all seven. `open` and `openNewTab` are separate
   *  entries rather than one plus a modifier: a menu is where you go when
   *  you do *not* remember the modifier. */
  function inspectItems(moteId, anchor) {
    return [
      { label: "Inspect", run: () => Inspector.open(moteId, { anchor }) },
      { label: "Inspect in new tab", run: () => Inspector.open(moteId, { anchor, newTab: true }) },
    ];
  }

  return { open, close, isOpen, inspectItems };
})();
