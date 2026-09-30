/* `motion:rule` (`rule @sel|‹rule›`, `why ‹m.k›`): a rule's equation and
 * the values it binds, or how the last solve determined one value
 * (docs/console.md → Settled for U5).
 *
 * `rule @sel` follows the selection: a rule shows itself, any other mote
 * shows every rule that touches it. While a sim pane owns the playhead,
 * api.motion.rule() is asked with that run ({ticks, scenario, solver})
 * and every binding carries its series, so the values follow the tick
 * without asking again. Names the equation can't settle on one mote come
 * back as hints (wire one of the candidates to fix it).
 *
 * `w` on a binding explains it (`why C1.current` in this pane); `esc`
 * goes back to the line before. Editing an equation is a `set` like any
 * other: `i` loads `set ‹rule› equation ‹it›` on the command line. */

import { fmt } from "../plot.js";
import { PLAYHEAD_HINTS, playheadKey, at } from "../playhead.js";

const RULE_HINTS = [["j k", "binding"], ["⏎", "select mote"], ["w", "why"], ["i", "edit equation"],
  ...PLAYHEAD_HINTS];
const WHY_HINTS = [["esc", "back"]];

function message(err) {
  return String(err && err.message ? err.message : err);
}

/** One command-line argument: bare when safe, else double-quoted. */
function arg(text) {
  return /^[^"'\\\s-][^"'\\\s]*$/.test(text) ? text : JSON.stringify(text);
}

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const st = { command: "", seq: 0, payload: null, error: null, empty: null, cursor: 0, back: null, runKey: "" };
  let body = null;
  const offs = [];

  const bindings = () => (st.payload && st.payload.kind === "rule"
    ? st.payload.cards.flatMap((card) => card.bindings.map((b) => ({ ...b, card }))) : []);

  function value(binding) {
    const p = store.state.playhead;
    return binding.series ? at(binding.series, p.tick) : binding.value;
  }

  /* ---- drawing ---- */

  function render() {
    if (!body) return;
    if (st.error) return body.replaceChildren(h("div", "pane-error lvl-fault", st.error));
    if (st.empty) return body.replaceChildren(h("div", "plot-empty", st.empty));
    if (!st.payload) return;
    if (st.payload.kind === "why") return body.replaceChildren(...whyLines(st.payload.explanation, 0));
    let index = 0;
    const parts = [];
    for (const card of st.payload.cards) {
      const meta = `${card.check ? "check" : "rule"} · ${card.scope} ${card.target}`;
      parts.push(h("div", "motion-rule-head", [h("span", "motion-rule-name", card.name), h("span", "dim", meta)]));
      parts.push(h("div", "motion-equation", card.equation));
      const rows = card.bindings.map((b) => ({ ...b, index: index++ }));
      parts.push(components.table({
        columns: [
          { key: "role", label: "", width: "10ch", render: (r) => h("span", "dim", r.role) },
          { key: "ref", label: "", width: "minmax(10ch, 1fr)" },
          { key: "value", label: "", width: "10ch", align: "right", render: (r) => fmt(value(r)) },
          { key: "unit", label: "", width: "max-content", render: (r) => h("span", "dim", r.unit || "") },
        ],
        rows, rowKey: (r) => String(r.index), header: false,
        cursor: rows.some((r) => r.index === st.cursor) ? { index: st.cursor - rows[0].index, col: null } : null,
        onRow: (row) => {
          st.cursor = row.index;
          render();
        },
      }));
      for (const hint of card.hints) {
        parts.push(h("div", "motion-hint lvl-warn",
          `${hint.name} could be ${hint.candidates.join(", ")} -- link the one you mean`));
      }
      if (card.anchors.length > 1) parts.push(h("div", "motion-hint dim", `on ${card.anchors.join(", ")}`));
    }
    const run = st.payload.run;
    if (run && run.ticks) {
      parts.push(h("div", "motion-hint dim", `values at tick ${store.state.playhead.tick}${run.scenario ? ` under ${run.scenario}` : ""}`));
    }
    body.replaceChildren(...parts);
  }

  function whyLines(e, depth) {
    if (!e) return [];
    const pad = (n) => ({ paddingLeft: `calc(${n} * 2ch + var(--pad-x))` });
    const line = (className, content, n = depth) => {
      const node = h("div", `motion-why ${className}`, content);
      Object.assign(node.style, pad(n));
      return node;
    };
    const head = [h("span", "motion-rule-name", e.ref), ` = ${fmt(e.value)}${e.unit ? ` ${e.unit}` : ""}`];
    if (e.method) head.push(h("span", "dim", `  ${e.method}${e.detail ? ` · ${e.detail}` : ""}`));
    const out = [line("", head)];
    if (e.note) out.push(line("dim", e.note, depth + 1));
    for (const q of e.equations) out.push(line("", [h("span", "dim", `by ${q.rule} on ${q.anchor}  `), q.equation], depth + 1));
    if (e.alsoDetermined.length) out.push(line("dim", `with ${e.alsoDetermined.join(", ")}`, depth + 1));
    for (const input of e.inputs) {
      if (input.derivation) out.push(...whyLines(input.derivation, depth + 1));
      else out.push(line("", [`${input.ref} = ${fmt(input.value)}`, h("span", "dim", `  ${input.kind}`)], depth + 1));
    }
    return out;
  }

  /* ---- gestures ---- */

  function current() {
    return bindings().find((_b, i) => i === st.cursor) || null;
  }

  function moveTo(index) {
    const n = bindings().length;
    if (!n) return;
    st.cursor = Math.max(0, Math.min(n - 1, index));
    render();
  }

  function why() {
    const b = current();
    if (!b) return;
    st.back = st.command;
    ctx.setCommand(`why ${b.ref}`);
  }

  function editEquation() {
    const b = current();
    const card = b ? b.card : st.payload.cards[0];
    if (card) ctx.prefill(`set #${card.moteId} equation ${arg(card.equation)}`);
  }

  const KEYS = {
    j: () => moveTo(st.cursor + 1), ArrowDown: () => moveTo(st.cursor + 1),
    k: () => moveTo(st.cursor - 1), ArrowUp: () => moveTo(st.cursor - 1),
    Enter: () => { const b = current(); if (b) ctx.navigate(b.moteId); },
    w: why,
    i: editEquation,
  };

  function onKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    if (event.key === "Escape" && st.back) {
      const back = st.back;
      st.back = null;
      ctx.setCommand(back);
      return true;
    }
    if (!st.payload || st.payload.kind !== "rule") return false;
    if (playheadKey(store, event)) return true;
    const action = KEYS[event.key];
    if (!action) return false;
    action();
    return true;
  }

  /* ---- the playhead ---- */

  offs.push(store.subscribe("playhead", (what) => {
    if (!body || !st.payload || st.payload.kind !== "rule") return;
    if (what && what.run) {
      const key = JSON.stringify(store.state.playhead.run);
      if (key !== st.runKey) run(st.command);
    } else if (what && what.tick) render();
  }));

  /* ---- the contract ---- */

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    const playRun = store.state.playhead.run;
    let p;
    try {
      p = api.motion && typeof api.motion.rule === "function"
        ? await api.motion.rule(command, store.state.selection, playRun) : { error: "this window has no motion api" };
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq || !body) return { summary: "", level: "idle" };
    st.runKey = JSON.stringify(playRun);
    if (p && p.empty) {
      Object.assign(st, { payload: null, error: null, empty: p.usage });
      render();
      return { summary: "", level: "idle" };
    }
    if (!p || p.error) {
      Object.assign(st, { payload: null, empty: null, error: (p && p.error) || "no answer" });
      render();
      return { summary: st.error, level: "fault" };
    }
    Object.assign(st, { payload: p, error: null, empty: null });
    st.cursor = Math.min(st.cursor, Math.max(bindings().length - 1, 0));
    render();
    if (p.kind === "why") {
      const e = p.explanation;
      return { summary: e && e.method ? e.method : "not solved", level: e && e.method ? "ok" : "warn" };
    }
    const hints = p.cards.reduce((n, c) => n + c.hints.length, 0);
    const summary = `${p.cards.length} rule${p.cards.length === 1 ? "" : "s"}${hints ? ` · ${hints} hint${hints === 1 ? "" : "s"}` : ""}`;
    return { summary, level: hints ? "warn" : "ok" };
  }

  return {
    mount(el, command) {
      st.command = command;
      body = h("div", "view-props scroll");
      el.appendChild(body);
    },
    run,
    unmount() {
      offs.splice(0).forEach((off) => off());
      st.seq += 1;
      body = null;
    },
    hints: () => (st.payload && st.payload.kind === "why" ? (st.back ? WHY_HINTS : []) : st.payload ? RULE_HINTS : []),
    onKey,
  };
}

export default {
  id: "motion:rule",
  app: "tion",
  describe: "a rule and the values it binds, or why a value is what it is (rule, why)",
  commands: [{ verb: "rule", canonical: "rule @sel" }, { verb: "why" }],
  followsSel: false,
  create,
};
