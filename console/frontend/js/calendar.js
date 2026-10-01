/* What the three calendar panes (views/monastery-*.js) share: fetching a
 * line's answer, the occurrence cursor, and every edit -- docs/console.md
 * → Settled for U7.
 *
 * Every write is a command the pane runs (ctx.runCommand), so it is in
 * the history, the log and undo: api.monastery.edit() hands back the
 * `set` line a move or resize means, skip() the one that drops a single
 * occurrence, and lifting one occurrence out of a series is `detach`. A
 * recurring event asks first -- `t` this one, `s` the series -- and the
 * pane's status line says so while it waits.
 *
 * Keys every calendar pane has:
 *   [ ]  page          t  today          j k  occurrence
 *   ⏎    select        o  follow (a note → read, a project → cd, else open)
 *   J K  ± an hour (a day when all-day)   H L  ± a day
 *   < >  shorter / longer                 n    new event here (prefills the line)
 *   d d  delete (a recurring one asks)    b / B  hide this key / show every key */

import { createPalette } from "./palette.js";

export const HINTS = [["[ ]", "page"], ["t", "today"], ["j k", "event"], ["⏎", "select"], ["o", "follow"],
  ["J K H L", "move"], ["< >", "length"], ["n", "new"], ["d d", "delete"], ["b B", "keys"]];
const ASK_HINTS = [["t", "this one"], ["s", "the series"], ["esc", "cancel"]];

const HOUR = 3600e3;
const DAY = 24 * HOUR;
const STEP = 30 * 60e3; // < > change a timed event's length by half an hour

export function message(err) {
  return String(err && err.message ? err.message : err);
}

/** "2026-10-07T09:00" -- local, minutes, the shape a garden stores. */
export function iso(when) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())}T${pad(when.getHours())}:${pad(when.getMinutes())}`;
}

export function dayIso(when) {
  return iso(when).slice(0, 10);
}

export function parse(text) {
  return new Date(text.length === 10 ? `${text}T00:00` : text);
}

export function hhmm(when) {
  return iso(when).slice(11, 16);
}

const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

export function dayLabel(when) {
  return `${WEEKDAYS[when.getDay()]} ${when.getDate()} ${MONTHS[when.getMonth()]}`;
}

/**
 * The shared half of a calendar pane: state, fetching, the cursor and
 * every key. `draw()` is the pane's own renderer; `order(payload)` lists
 * the items (rows and bars) the cursor walks, in drawing order.
 */
export function createCalendar(ctx, { draw, order }) {
  const { api, store } = ctx;
  const st = { command: "", seq: 0, payload: null, error: null, items: [], cursor: 0, cursorKey: null,
    palette: createPalette([]), asking: null, armed: null, slot: null };

  function colorOf(item) {
    return item ? st.palette.colorOf({ id: item.moteId, speciesId: item.speciesId, kinds: [] }) : "var(--dim)";
  }

  const current = () => st.items[st.cursor] || null;

  function moveTo(index) {
    if (!st.items.length) return;
    st.cursor = Math.max(0, Math.min(st.items.length - 1, index));
    st.cursorKey = st.items[st.cursor].key;
    st.armed = null;
    draw();
  }

  function pointAt(item) {
    const at = st.items.findIndex((i) => i.key === item.key);
    if (at >= 0) moveTo(at);
  }

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    let p;
    try {
      p = api.monastery && typeof api.monastery.view === "function" ? await api.monastery.view(command)
        : { error: "this window has no monastery api" };
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq) return { summary: "", level: "idle" };
    if (!p || p.error) {
      Object.assign(st, { payload: null, error: (p && p.error) || "no answer", items: [] });
      draw();
      return { summary: st.error, level: "fault" };
    }
    st.payload = p;
    st.error = null;
    st.palette = createPalette(p.speciesIds || []);
    p.bars.forEach((b) => { b.bar = true; });
    st.items = order(p);
    const at = st.items.findIndex((i) => i.key === st.cursorKey);
    const sameMote = at < 0 && st.cursorKey ? st.items.findIndex((i) => st.cursorKey.startsWith(`${i.moteId}:${i.binding}:`)) : -1;
    st.cursor = at >= 0 ? at : sameMote >= 0 ? sameMote : Math.min(st.cursor, Math.max(st.items.length - 1, 0));
    st.cursorKey = st.items[st.cursor] ? st.items[st.cursor].key : null;
    draw();
    const n = p.rows.length + p.bars.length;
    return { summary: `${p.title} · ${n} item${n === 1 ? "" : "s"}${p.problems.length ? ` · ${p.problems[0]}` : ""}`,
      level: p.problems.length ? "warn" : "ok" };
  }

  async function rewrite(change) {
    let r;
    try {
      r = await api.monastery.rewrite(st.command, change);
    } catch (err) {
      r = { error: message(err) };
    }
    if (r.error) store.log("result", r.error, { level: "fault" });
    else await ctx.setCommand(r.command);
  }

  /* ---- edits ---- */

  /** Where an occurrence is to come to rest: [start, end) as Dates. */
  async function place(item, start, end) {
    if (!item || item.source === "due") return;
    if (end <= start) return;
    if (item.recurring) {
      st.asking = { kind: "place", item, start, end };
      store.emit("hints");
      draw();
      return;
    }
    await placeSeries(item, start, end);
  }

  async function placeSeries(item, start, end) {
    let r;
    try {
      r = await api.monastery.edit(item.moteId, item.binding, item.start, iso(start), iso(end));
    } catch (err) {
      r = { error: message(err) };
    }
    if (r.error) store.log("result", r.error, { level: "fault" });
    else if (r.line) {
      st.cursorKey = `${item.moteId}:${item.binding}:`;
      await ctx.runCommand(r.line);
    }
  }

  async function answer(scope) {
    const asking = st.asking;
    st.asking = null;
    store.emit("hints");
    if (!asking) return;
    const { item } = asking;
    if (asking.kind === "place") {
      if (scope === "series") await placeSeries(item, asking.start, asking.end);
      else await ctx.runCommand(`detach #${item.moteId} ${item.binding} ${item.start.slice(0, 16)} ${iso(asking.start)} ${iso(asking.end)}`);
    } else if (scope === "series") {
      ctx.prefill(`delete #${item.moteId}`);
    } else {
      const r = await api.monastery.skip(item.moteId, item.binding, item.start);
      if (r.error) store.log("result", r.error, { level: "fault" });
      else if (r.line) await ctx.runCommand(r.line);
    }
    draw();
  }

  function nudge(item, { move = 0, grow = 0 }) {
    if (!item) return;
    const start = parse(item.start);
    const end = parse(item.end);
    const newStart = new Date(start.getTime() + move);
    const newEnd = new Date(end.getTime() + move + grow);
    place(item, newStart, newEnd);
  }

  function step(item, hours) {
    return item && item.allDay ? DAY * Math.sign(hours) : HOUR * hours;
  }

  async function remove() {
    const item = current();
    if (!item || item.source === "due") return;
    if (st.armed !== item.key) {
      st.armed = item.key;
      store.log("result", "d again to delete", { level: "warn" });
      draw();
      return;
    }
    st.armed = null;
    if (item.recurring) {
      st.asking = { kind: "delete", item };
      store.emit("hints");
      draw();
      return;
    }
    ctx.prefill(`delete #${item.moteId}`);
  }

  async function follow() {
    const item = current();
    if (!item) return;
    const r = await api.monastery.follow(item.moteId);
    if (r.error) store.log("result", r.error, { level: "fault" });
    else await ctx.runCommand(r.command, { newPane: r.newPane });
  }

  function select(item = current()) {
    if (item) ctx.navigate(item.moteId, item.name);
  }

  /** `n`: the slot the pane points at (a clicked one, else the cursor's
   * day at 09:00), written the way a new event's `date` is. */
  async function create() {
    let start, end, allDay = false;
    if (st.slot) ({ start, end, allDay } = st.slot);
    else {
      const item = current();
      const day = item ? parse((item.day || item.start).slice(0, 10)) : parse(st.payload.today);
      start = new Date(day.getTime() + 9 * HOUR);
      end = new Date(start.getTime() + HOUR);
    }
    const r = await api.monastery.slot(iso(start), iso(end), allDay);
    if (r.error) store.log("result", r.error, { level: "fault" });
    else ctx.prefill(`.date = ${r.value}`, 0);
  }

  function toggleKey(all) {
    const item = current();
    if (all) rewrite({ keys: null });
    else if (item) rewrite({ key: item.binding });
  }

  const KEYS = {
    "[": () => rewrite({ page: -1 }), "]": () => rewrite({ page: 1 }), t: () => rewrite({ today: true }),
    j: () => moveTo(st.cursor + 1), ArrowDown: () => moveTo(st.cursor + 1),
    k: () => moveTo(st.cursor - 1), ArrowUp: () => moveTo(st.cursor - 1),
    Enter: () => select(), o: follow,
    J: () => nudge(current(), { move: step(current(), 1) }), K: () => nudge(current(), { move: step(current(), -1) }),
    L: () => nudge(current(), { move: DAY }), H: () => nudge(current(), { move: -DAY }),
    ">": () => nudge(current(), { grow: current() && current().allDay ? DAY : STEP }),
    "<": () => nudge(current(), { grow: current() && current().allDay ? -DAY : -STEP }),
    n: create, d: remove, b: () => toggleKey(false), B: () => toggleKey(true),
  };

  function onKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    if (st.asking) {
      if (event.key === "t") answer("one");
      else if (event.key === "s") answer("series");
      else if (event.key === "Escape") {
        st.asking = null;
        store.emit("hints");
        draw();
      } else return true;
      return true;
    }
    if (event.key === "Escape" && (st.armed || st.slot)) {
      st.armed = null;
      st.slot = null;
      draw();
      return true;
    }
    if (!st.payload) return false;
    const action = KEYS[event.key];
    if (!action) return false;
    if (event.key !== "d") st.armed = null;
    action();
    return true;
  }

  function askText() {
    if (!st.asking) return null;
    const what = st.asking.kind === "place" ? "move" : "delete";
    return `${st.asking.item.name} repeats -- ${what} t this one · s the series · esc cancel`;
  }

  return {
    st, run, rewrite, onKey, place, pointAt, select, colorOf, current, askText, moveTo, create,
    hints: () => (st.asking ? ASK_HINTS : st.payload ? HINTS : []),
  };
}
