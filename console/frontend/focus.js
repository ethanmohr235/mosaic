/* Two window-wide focus rules the console gets just by loading this
 * file -- no install() call, since neither needs the bridge or any
 * per-app configuration:
 *
 * 1. Middle-click never pastes. X11/GTK's primary-selection paste isn't
 *    tied to the element under the cursor: WebKitGTK runs it on the
 *    middle button's *mouseup*, into whatever currently has focus, unless
 *    that mouseup was preventDefault()ed. So a middle-drag pan across a
 *    canvas while a text field still had focus
 *    dropped the primary selection into it, and guarding only mousedown
 *    (what each app and shared module used to do separately, each over
 *    its own subtree) left exactly that hole. Both events are blocked
 *    here, in the capture phase on window, so no subtree can miss it.
 *    Middle-drag panning is unaffected -- the graphs read button === 1
 *    themselves and never relied on the default action.
 *
 * 2. Clicking outside a text field unfocuses it. Browsers do this on
 *    their own, but only when the mousedown's default action runs, and
 *    every canvas / resize handle / drag surface that preventDefault()s
 *    its mousedown (motion's surface, the panel resizers, ...) quietly
 *    kept a stale field focused -- still catching keystrokes, still not
 *    committed. Run in the capture phase so a stopPropagation() further
 *    down can't opt out by accident either. A click on another focusable
 *    control is left alone (focus moves there natively), as is anything
 *    inside an element marked `data-keep-focus` -- the explicit opt-out
 *    for a popup that must leave focus in the field it belongs to.
 *    Middle-clicks are exempt: panning isn't "clicking elsewhere".
 *
 * Once shared by every window through frontend_shared/; the console is
 * the only window now (docs/console.md, U9). Loaded before main.js. */

const Focus = (() => {
  "use strict";

  const FOCUSABLE = "input, textarea, select, button, a[href], label, "
    + "[contenteditable]:not([contenteditable='false']), [tabindex]:not([tabindex='-1'])";

  function isTextField(el) {
    if (!(el instanceof Element)) return false;
    const tag = el.tagName.toLowerCase();
    return tag === "input" || tag === "textarea" || tag === "select" || el.isContentEditable;
  }

  function blockMiddleClick(event) {
    if (event.button === 1) event.preventDefault();
  }

  function blurOnOutsideClick(event) {
    if (event.button === 1) return;
    const active = document.activeElement;
    if (!isTextField(active)) return;
    const target = event.target;
    if (!(target instanceof Element)) return;
    if (active.contains(target)) return;
    if (target.closest(FOCUSABLE)) return;
    if (target.closest("[data-keep-focus]")) return;
    active.blur();
  }

  window.addEventListener("mousedown", blockMiddleClick, true);
  window.addEventListener("mouseup", blockMiddleClick, true);
  window.addEventListener("mousedown", blurOnOutsideClick, true);

  return { isTextField };
})();
