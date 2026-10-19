/* The console's tooltip. Every `title` in the window shows here rather
 * than as the platform's native tooltip, so a hover reads in the
 * console's own type, colors and square corners.
 *
 * Views and components keep setting plain `title` attributes (or an
 * SVG `<title>` child, as plot's dots do). On hover the text moves to
 * `data-tip` (so the native one never shows) and
 * the text appears under the element after a short delay; a later
 * `title` write (a header re-run) wins over the stashed copy. Any key,
 * click, scroll or leaving the element hides it. */

const DELAY_MS = 450;

export function createTooltip({ root = document.body } = {}) {
  const tip = document.createElement("div");
  tip.className = "c-tip";
  tip.hidden = true;
  root.appendChild(tip);

  let target = null;
  let timer = 0;

  function svgTitle(el) {
    return [...el.children].find((c) => c.tagName === "title") || null;
  }

  // The nearest element with something to show: a title attribute, a
  // stashed tip, or an SVG element carrying a <title> child.
  function owner(el) {
    for (let at = el; at && at !== document.documentElement; at = at.parentElement) {
      if (at.hasAttribute("title") || "tip" in at.dataset) return at;
      if (at instanceof SVGElement && svgTitle(at)) return at;
    }
    return null;
  }

  function textOf(el) {
    const inner = el instanceof SVGElement ? svgTitle(el) : null;
    if (inner) {
      el.dataset.tip = inner.textContent;
      inner.remove();
    }
    if (el.hasAttribute("title")) {
      el.dataset.tip = el.getAttribute("title");
      el.removeAttribute("title");
    }
    return el.dataset.tip || "";
  }

  function hide() {
    clearTimeout(timer);
    timer = 0;
    target = null;
    tip.hidden = true;
  }

  function show() {
    if (!target || !target.isConnected) return hide();
    const text = textOf(target);
    if (!text) return hide();
    tip.textContent = text;
    tip.hidden = false;
    const at = target.getBoundingClientRect();
    const size = tip.getBoundingClientRect();
    const left = Math.max(0, Math.min(at.left, window.innerWidth - size.width));
    const below = at.bottom + size.height <= window.innerHeight;
    tip.style.left = `${left}px`;
    tip.style.top = `${below ? at.bottom : Math.max(0, at.top - size.height)}px`;
  }

  document.addEventListener("mouseover", (event) => {
    const el = event.target instanceof Element ? owner(event.target) : null;
    if (el === target) return;
    hide();
    if (!el || !textOf(el)) return;
    target = el;
    timer = setTimeout(show, DELAY_MS);
  });
  document.addEventListener("mouseout", (event) => {
    if (target && !target.contains(event.relatedTarget)) hide();
  });
  ["mousedown", "keydown", "wheel"].forEach((type) => document.addEventListener(type, hide, true));
  window.addEventListener("blur", hide);
  return { hide };
}
