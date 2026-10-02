/* `agenda` (`agenda [from] [to] …`, `layout plan`): every dated
 * mote in the range, a row per occurrence, grouped by day -- the calendar's
 * keyboard home (docs/console.md → Settled for U7).
 *
 * A bar (a multi-day value, or a start through a `due`) is one row on the
 * first day it shows, reading `→ Fri 9 Oct`. Keys and edits are
 * js/calendar.js's. */

import { createCalendar, parse, hhmm, dayLabel } from "../calendar.js";

function create(ctx) {
  const { components, store } = ctx;
  const { h } = components;
  let body = null;

  /** Items in drawing order: each day's bars, then its rows. */
  function order(p) {
    const days = groups(p);
    return days.flatMap((d) => d.items);
  }

  function groups(p) {
    const byDay = new Map();
    const add = (day, item) => {
      if (!byDay.has(day)) byDay.set(day, []);
      byDay.get(day).push(item);
    };
    for (const b of p.bars) add(b.start < p.start ? p.start : b.start, b);
    for (const r of p.rows) add(r.day, r);
    return [...byDay.keys()].sort().map((day) => ({
      day, items: byDay.get(day).sort((a, b) => Number(!a.bar) - Number(!b.bar)
        || Number(!a.allDay) - Number(!b.allDay) || a.start.localeCompare(b.start)),
    }));
  }

  function when(item) {
    if (item.bar) {
      const end = new Date(parse(item.end).getTime() - 24 * 3600e3);
      return `→ ${dayLabel(end)}`;
    }
    if (item.allDay) return "all day";
    return `${hhmm(parse(item.start))}–${hhmm(parse(item.end))}`;
  }

  function marks(item) {
    return `${item.recurring ? "↻" : ""}${item.note ? "✎" : ""}${item.garden ? "▣" : ""}`;
  }

  function draw() {
    if (!body) return;
    const { st } = cal;
    if (st.error) return body.replaceChildren(h("div", "pane-error lvl-fault", st.error));
    if (!st.payload) return;
    const p = st.payload;
    const primary = store.primary();
    const grid = components.table({
      columns: [
        { key: "when", label: "", width: "13ch", render: (i) => h("span", i.bar ? "cal-bar-when" : "dim", when(i)) },
        { key: "name", label: "", width: "minmax(12ch, 1fr)",
          render: (i) => [components.swatch(cal.colorOf(i)), " ", i.name] },
        { key: "binding", label: "", width: "max-content", render: (i) => h("span", "dim", i.binding) },
        { key: "marks", label: "", width: "3ch", render: (i) => h("span", "dim", marks(i)) },
      ],
      groups: groups(p).map((g) => ({
        key: g.day, labelled: true, rows: g.items, count: g.items.length,
        label: `${dayLabel(parse(g.day))}${g.day === p.today ? " · today" : ""}`,
      })),
      rowKey: (i) => i.key, header: false,
      selected: st.items.filter((i) => i.moteId === primary).map((i) => i.key),
      primary: st.items.find((i) => i.moteId === primary)?.key ?? null,
      cursor: { index: st.cursor, col: null },
      onRow: (item, event) => {
        cal.pointAt(item);
        cal.select(item);
      },
    });
    grid.querySelectorAll(".c-group-all").forEach((n) => n.remove());
    const parts = [grid];
    if (!st.items.length) parts.push(h("div", "c-table-empty", `nothing dated ${p.title}`));
    const ask = cal.askText();
    if (ask) parts.unshift(h("div", "cal-ask lvl-warn", ask));
    if (st.armed) parts.unshift(h("div", "cal-ask lvl-warn", "d again to delete"));
    body.replaceChildren(...parts);
    const line = grid.querySelector(`.c-table-row[data-index="${st.cursor}"]`);
    if (line) line.scrollIntoView({ block: "nearest" });
  }

  const cal = createCalendar(ctx, { draw, order });
  const off = store.subscribe("selection", () => draw());

  return {
    mount(el) {
      body = h("div", "view-table view-agenda scroll");
      el.appendChild(body);
    },
    run: cal.run,
    unmount() {
      off();
      cal.st.seq += 1;
      body = null;
    },
    hints: cal.hints,
    onKey: cal.onKey,
  };
}

export default {
  id: "agenda",
  describe: "dated motes by day, a row per occurrence (agenda)",
  commands: [{ verb: "agenda" }],
  followsSel: false,
  create,
};
