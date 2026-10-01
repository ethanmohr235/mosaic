/* `monastery:month` (`month [date] …`): six weeks of day cells on the
 * --line grid (docs/console.md → Settled for U7). Each day lists up to
 * three items (bars first, then all-day, then timed by start) and
 * `+N more`, which opens that week in a new pane. A bar shows on every
 * day it covers. Click an item to select it; double-click a day to start
 * a new event on it. Keys and edits are js/calendar.js's. */

import { createCalendar, parse, dayIso, hhmm } from "../calendar.js";

const SHOWN = 3;
const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function create(ctx) {
  const { components, store } = ctx;
  const { h } = components;
  let root = null;

  function itemsOn(p, day) {
    const bars = p.bars.filter((b) => b.start <= day && b.end > day);
    const rows = p.rows.filter((r) => r.start.slice(0, 10) === day);
    return [...bars, ...rows.filter((r) => r.allDay), ...rows.filter((r) => !r.allDay)];
  }

  function cells(p) {
    const first = parse(p.start);
    return Array.from({ length: 42 }, (_, i) => dayIso(new Date(first.getFullYear(), first.getMonth(), first.getDate() + i)));
  }

  /** The cursor walks each item once, on the first day it shows. */
  function order(p) {
    const seen = new Set();
    const out = [];
    for (const day of cells(p)) {
      for (const item of itemsOn(p, day)) {
        if (seen.has(item.key)) continue;
        seen.add(item.key);
        out.push(item);
      }
    }
    return out;
  }

  function draw() {
    if (!root) return;
    const { st } = cal;
    if (st.error) return root.replaceChildren(h("div", "pane-error lvl-fault", st.error));
    if (!st.payload) return;
    const p = st.payload;
    const middle = parse(cells(p)[14]).getMonth();
    const primary = store.primary();
    const cursorKey = cal.current() ? cal.current().key : null;
    const grid = h("div", "mo-grid", WEEKDAYS.map((d) => h("span", "mo-weekday dim", d)));
    for (const day of cells(p)) {
      const when = parse(day);
      const items = itemsOn(p, day);
      const cell = h("div", `mo-cell${when.getMonth() !== middle ? " outside" : ""}${day === p.today ? " today" : ""}`,
        h("span", "mo-date", String(when.getDate())));
      for (const item of items.slice(0, SHOWN)) {
        const label = item.bar || item.allDay ? item.name : `${hhmm(parse(item.start))} ${item.name}`;
        const chip = h("div", `mo-chip${item.bar ? " bar" : ""}${item.moteId === primary ? " selected" : ""}`
          + `${item.key === cursorKey ? " cursor" : ""}`, label);
        chip.style.setProperty("--ev", cal.colorOf(item));
        chip.title = `${item.name} · ${item.binding}`;
        chip.addEventListener("mousedown", (e) => {
          e.stopPropagation();
          cal.st.slot = null;
          cal.pointAt(item);
          cal.select(item);
        });
        cell.appendChild(chip);
      }
      if (items.length > SHOWN) {
        const more = h("div", "mo-more dim", `+${items.length - SHOWN} more`);
        more.addEventListener("mousedown", (e) => {
          e.stopPropagation();
          ctx.runCommand(`week ${day}`, { newPane: true });
        });
        cell.appendChild(more);
      }
      cell.addEventListener("dblclick", () => {
        cal.st.slot = { start: when, end: new Date(when.getTime() + 864e5), allDay: true };
        cal.create();
      });
      grid.appendChild(cell);
    }
    const ask = cal.askText();
    const status = h("div", `plot-status${ask || st.armed ? " lvl-warn" : ""}`,
      ask || (st.armed ? "d again to delete" : p.title));
    root.replaceChildren(grid, status);
  }

  const cal = createCalendar(ctx, { draw, order });
  const off = store.subscribe("selection", () => draw());

  return {
    mount(el) {
      root = h("div", "view-month");
      el.appendChild(root);
    },
    run: cal.run,
    unmount() {
      off();
      cal.st.seq += 1;
      root = null;
    },
    hints: cal.hints,
    onKey: cal.onKey,
  };
}

export default {
  id: "monastery:month",
  app: "nastery",
  describe: "the month as a six-week grid (month)",
  commands: [{ verb: "month" }],
  followsSel: false,
  create,
};
