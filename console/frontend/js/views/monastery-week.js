/* `monastery:week` (`week [date] …`, `mo nastery`): the week as an hour
 * grid on the --line grid (docs/console.md → Settled for U7).
 *
 * Above the hours, the all-day strip: bars (multi-day values, a start
 * through a `due`) and all-day items, each spanning the days it covers.
 * Below, each day's timed occurrences, side by side where they overlap.
 *
 * Mouse: click an event to select it; drag it to move (snapped to a
 * quarter hour, across days too) or drag its bottom edge to resize; click
 * an empty slot to point `n` at it, double-click to start a new event
 * there. Every write is a command -- js/calendar.js. */

import { createCalendar, parse, iso, dayIso, hhmm, dayLabel } from "../calendar.js";

const MIN_HOUR_PX = 36;  // --wk-hour: two rows of --row-h; a taller pane stretches the hours to fill it
const SNAP_MIN = 15;
const EDGE_PX = 6;       // the bottom band that resizes rather than moves
const FIRST_HOUR = 7;    // where the grid opens scrolled to

function create(ctx) {
  const { components, store } = ctx;
  const { h } = components;
  // `lastTop`: where the hours were scrolled to. Splitting panes moves a
  // pane's element in the DOM, which resets its scroll; a resize puts it back.
  let root = null, scroller = null, columns = [], drag = null, lastTop = null, resize = null;
  let hourPx = MIN_HOUR_PX;

  const days = () => {
    const p = cal.st.payload;
    const first = parse(p.start);
    return Array.from({ length: 7 }, (_, i) => new Date(first.getFullYear(), first.getMonth(), first.getDate() + i));
  };

  function order(p) {
    const strip = [...p.bars, ...p.rows.filter((r) => r.allDay)];
    const timed = p.rows.filter((r) => !r.allDay);
    return [...strip.sort((a, b) => a.start.localeCompare(b.start)), ...timed.sort((a, b) => a.start.localeCompare(b.start))];
  }

  /* ---- drawing ---- */

  function draw() {
    if (!root) return;
    const { st } = cal;
    if (st.error) return root.replaceChildren(h("div", "pane-error lvl-fault", st.error));
    if (!st.payload) return;
    const p = st.payload;
    const week = days();
    const head = h("div", "wk-head", [h("span", "wk-gutter"), ...week.map((d) => h("span",
      `wk-day-head${dayIso(d) === p.today ? " today" : ""}`, dayLabel(d)))]);
    const strip = drawStrip(week);
    const body = h("div", "wk-body");
    body.style.height = `${24 * hourPx}px`;
    const gutter = h("div", "wk-gutter wk-hours", Array.from({ length: 24 }, (_, i) =>
      h("span", "wk-hour", `${String(i).padStart(2, "0")}:00`)));
    columns = week.map((d) => drawDay(d));
    body.append(gutter, ...columns.map((c) => c.el));
    scroller = h("div", "wk-scroll", body);
    scroller.addEventListener("scroll", () => { lastTop = scroller.scrollTop; });
    const parts = [head, strip, scroller];
    const ask = cal.askText();
    const status = h("div", `plot-status${ask || st.armed ? " lvl-warn" : ""}`,
      ask || (st.armed ? "d again to delete" : statusText()));
    parts.push(status);
    root.replaceChildren(...parts);
    if (fitHours()) return draw();
    scroller.scrollTop = lastTop === null ? FIRST_HOUR * hourPx : lastTop;
    lastTop = scroller.scrollTop;
  }

  /** Stretch an hour so the day fills the pane, never below MIN_HOUR_PX
   * (then it scrolls). True when the hour changed and the grid needs
   * drawing again. */
  function fitHours() {
    if (!scroller || !scroller.clientHeight) return false;
    const next = Math.max(MIN_HOUR_PX, Math.floor(scroller.clientHeight / 24));
    if (next === hourPx) return false;
    hourPx = next;
    root.style.setProperty("--wk-hour", `${hourPx}px`);
    return true;
  }

  function statusText() {
    const item = cal.current();
    if (!item) return cal.st.slot ? `slot ${iso(cal.st.slot.start).replace("T", " ")}` : "";
    const span = item.allDay || item.bar ? `${item.start.slice(0, 10)} → ${item.end.slice(0, 10)}`
      : `${dayLabel(parse(item.start))} ${hhmm(parse(item.start))}–${hhmm(parse(item.end))}`;
    return `${item.name} · ${item.binding} · ${span}${item.recurring ? " · repeats" : ""}`;
  }

  function block(item, className) {
    const primary = store.primary();
    const cursor = cal.current() && cal.current().key === item.key;
    const node = h("div", `${className}${item.moteId === primary ? " selected" : ""}${cursor ? " cursor" : ""}`
      + `${item.recurring ? " recurring" : ""}${item.source === "due" ? " due-bar" : ""}`, item.name);
    node.style.setProperty("--ev", cal.colorOf(item));
    node.title = `${item.name} · ${item.binding}`;
    node.dataset.key = item.key;
    return node;
  }

  function drawStrip(week) {
    const p = cal.st.payload;
    const first = parse(p.start).getTime();
    const items = [...p.bars, ...p.rows.filter((r) => r.allDay)];
    const lanes = [];
    const strip = h("div", "wk-strip");
    strip.appendChild(h("span", "wk-gutter dim", "all day"));
    const grid = h("div", "wk-strip-grid");
    for (const item of items.sort((a, b) => a.start.localeCompare(b.start))) {
      const from = Math.max(0, Math.round((parse(item.start.slice(0, 10)).getTime() - first) / 864e5));
      const to = Math.min(7, Math.round((parse(item.end.slice(0, 10)).getTime() - first) / 864e5) || from + 1);
      let lane = lanes.findIndex((end) => end <= from);
      if (lane < 0) lane = lanes.push(0) - 1;
      lanes[lane] = Math.max(to, from + 1);
      const node = block(item, item.bar ? "wk-bar" : "wk-allday");
      node.style.gridColumn = `${from + 1} / ${Math.max(to, from + 1) + 1}`;
      node.style.gridRow = String(lane + 1);
      node.addEventListener("mousedown", (e) => { e.stopPropagation(); pick(item); });
      grid.appendChild(node);
    }
    grid.addEventListener("dblclick", (e) => {
      const col = Math.floor(((e.clientX - grid.getBoundingClientRect().left) / grid.clientWidth) * 7);
      const day = week[Math.max(0, Math.min(6, col))];
      cal.st.slot = { start: day, end: new Date(day.getTime() + 864e5), allDay: true };
      cal.create();
    });
    grid.style.gridTemplateRows = `repeat(${Math.max(lanes.length, 1)}, var(--row-h))`;
    strip.appendChild(grid);
    return strip;
  }

  function drawDay(day) {
    const p = cal.st.payload;
    const start = day.getTime();
    const end = start + 864e5;
    const col = h("div", `wk-col${dayIso(day) === p.today ? " today" : ""}`);
    const timed = p.rows.filter((r) => !r.allDay && parse(r.start).getTime() < end && parse(r.end).getTime() > start)
      .sort((a, b) => a.start.localeCompare(b.start));
    const lanes = [];
    const placed = timed.map((item) => {
      const s = Math.max(parse(item.start).getTime(), start);
      const e = Math.min(parse(item.end).getTime(), end);
      let lane = lanes.findIndex((free) => free <= s);
      if (lane < 0) lane = lanes.push(0) - 1;
      lanes[lane] = e;
      return { item, s, e, lane };
    });
    for (const { item, s, e, lane } of placed) {
      const node = block(item, "wk-event");
      node.style.top = `${((s - start) / 3600e3) * hourPx}px`;
      node.style.height = `${Math.max(((e - s) / 3600e3) * hourPx, 10)}px`;
      node.style.left = `${(lane / lanes.length) * 100}%`;
      node.style.width = `${100 / lanes.length}%`;
      node.addEventListener("mousedown", (ev) => startDrag(ev, item, node));
      col.appendChild(node);
    }
    const slot = cal.st.slot;
    if (slot && !slot.allDay && dayIso(slot.start) === dayIso(day)) {
      const mark = h("div", "wk-slot");
      mark.style.top = `${((slot.start - start) / 3600e3) * hourPx}px`;
      mark.style.height = `${hourPx}px`;
      col.appendChild(mark);
    }
    col.addEventListener("mousedown", (ev) => {
      if (ev.button !== 0 || ev.target !== col) return;
      const minutes = snap(((ev.clientY - col.getBoundingClientRect().top) / hourPx) * 60, 30);
      const at = new Date(start + minutes * 60e3);
      cal.st.slot = { start: at, end: new Date(at.getTime() + 3600e3), allDay: false };
      draw();
    });
    col.addEventListener("dblclick", (ev) => {
      if (ev.target === col && cal.st.slot) cal.create();
    });
    return { el: col, day };
  }

  /* ---- mouse ---- */

  function pick(item) {
    cal.st.slot = null;
    cal.pointAt(item);
    cal.select(item);
  }

  function snap(minutes, to = SNAP_MIN) {
    return Math.round(minutes / to) * to;
  }

  function startDrag(event, item, node) {
    if (event.button !== 0) return;
    event.preventDefault();
    event.stopPropagation();
    const rect = node.getBoundingClientRect();
    const resize = event.clientY > rect.bottom - EDGE_PX;
    const colWidth = columns[0] ? columns[0].el.getBoundingClientRect().width : 1;
    drag = { item, node, resize, x: event.clientX, y: event.clientY, top: node.offsetTop, height: node.offsetHeight,
      minutes: 0, days: 0, moved: false };
    const move = (e) => {
      const minutes = snap(((e.clientY - drag.y) / hourPx) * 60);
      const dayShift = resize ? 0 : Math.round((e.clientX - drag.x) / colWidth);
      drag.moved = drag.moved || Math.abs(e.clientY - drag.y) > 3 || Math.abs(e.clientX - drag.x) > 3;
      drag.minutes = minutes;
      drag.days = dayShift;
      if (resize) node.style.height = `${Math.max(drag.height + (minutes / 60) * hourPx, 10)}px`;
      else {
        node.style.top = `${drag.top + (minutes / 60) * hourPx}px`;
        node.style.transform = `translateX(${dayShift * colWidth}px)`;
      }
    };
    const up = () => {
      window.removeEventListener("mousemove", move);
      window.removeEventListener("mouseup", up);
      const d = drag;
      drag = null;
      if (!d.moved) return pick(item);
      cal.pointAt(item);
      const start = parse(item.start);
      const end = parse(item.end);
      const shift = d.minutes * 60e3 + d.days * 864e5;
      if (d.resize) cal.place(item, start, new Date(end.getTime() + d.minutes * 60e3));
      else cal.place(item, new Date(start.getTime() + shift), new Date(end.getTime() + shift));
    };
    window.addEventListener("mousemove", move);
    window.addEventListener("mouseup", up);
  }

  const cal = createCalendar(ctx, { draw, order });
  const off = store.subscribe("selection", () => draw());

  return {
    mount(el) {
      root = h("div", "view-week");
      el.appendChild(root);
      resize = new ResizeObserver(() => {
        if (fitHours()) draw();
        else if (scroller && lastTop !== null && scroller.scrollTop !== lastTop) scroller.scrollTop = lastTop;
      });
      resize.observe(root);
    },
    run: cal.run,
    unmount() {
      off();
      if (resize) resize.disconnect();
      cal.st.seq += 1;
      root = scroller = resize = null;
    },
    hints: cal.hints,
    onKey: cal.onKey,
  };
}

export default {
  id: "monastery:week",
  app: "nastery",
  describe: "the week as an hour grid (week)",
  commands: [{ verb: "week" }],
  followsSel: false,
  create,
};
