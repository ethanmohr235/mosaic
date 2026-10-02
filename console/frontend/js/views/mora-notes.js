/* `mora:notes` (`notes [‹text›]`, `layout docs`): every note in the garden, or
 * those whose name or prose contains the text (docs/console.md → Settled
 * for U6). ⏎ or a click selects the note's mote, so `edit @sel` and
 * `read @sel` follow; `n` puts `edit ` on the command line to start one. */

const HINTS = [["j k", "move"], ["⏎", "select"], ["n", "new note"]];

function message(err) {
  return String(err && err.message ? err.message : err);
}

function create(ctx) {
  const { api, store, components } = ctx;
  const { h } = components;
  const st = { seq: 0, notes: [], error: null, cursor: 0, query: "" };
  let body = null;

  function render() {
    if (!body) return;
    if (st.error) return body.replaceChildren(h("div", "pane-error lvl-fault", st.error));
    const primary = store.primary();
    const grid = components.table({
      columns: [
        { key: "name", label: "note", width: "minmax(10ch, 1fr)" },
        { key: "words", label: "words", width: "7ch", align: "right" },
        { key: "problems", label: "!", width: "3ch", align: "right",
          render: (n) => (n.problems ? String(n.problems) : ""), level: (n) => (n.problems ? "warn" : null) },
      ],
      rows: st.notes, rowKey: (n) => n.id,
      selected: primary ? [primary] : [], primary,
      cursor: { index: st.cursor, col: null },
      onRow: (n, _e, index) => {
        st.cursor = index;
        ctx.navigate(n.id, n.name);
      },
    });
    const parts = [grid];
    const at = st.notes[st.cursor];
    if (at && at.first) parts.push(h("div", "c-table-empty", at.first));
    if (!st.notes.length) {
      parts.push(h("div", "c-table-empty", st.query ? `no note contains "${st.query}"`
        : "no notes"));
    }
    body.replaceChildren(...parts);
  }

  function moveTo(index) {
    st.cursor = Math.max(0, Math.min(st.notes.length - 1, index));
    render();
  }

  const KEYS = {
    j: () => moveTo(st.cursor + 1), ArrowDown: () => moveTo(st.cursor + 1),
    k: () => moveTo(st.cursor - 1), ArrowUp: () => moveTo(st.cursor - 1),
    g: () => moveTo(0), G: () => moveTo(st.notes.length - 1),
    Enter: () => { const n = st.notes[st.cursor]; if (n) ctx.navigate(n.id, n.name); },
    n: () => ctx.prefill("edit "),
  };

  function onKey(event) {
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    const action = KEYS[event.key];
    if (!action) return false;
    action();
    return true;
  }

  const off = store.subscribe("selection", () => render());

  async function run(command) {
    const seq = ++st.seq;
    let r;
    try {
      r = await api.mora.notes(command);
    } catch (err) {
      r = { error: message(err) };
    }
    if (seq !== st.seq || !body) return { summary: "", level: "idle" };
    if (r.error) {
      st.error = r.error;
      render();
      return { summary: r.error, level: "fault" };
    }
    st.error = null;
    st.query = command.replace(/^notes\s*/, "");
    st.notes = r.notes;
    const primary = store.primary();
    const at = st.notes.findIndex((n) => n.id === primary);
    st.cursor = at >= 0 ? at : Math.min(st.cursor, Math.max(st.notes.length - 1, 0));
    render();
    return { summary: `${st.notes.length} note${st.notes.length === 1 ? "" : "s"}`, level: "ok" };
  }

  return {
    mount(el) {
      body = h("div", "view-table scroll");
      el.appendChild(body);
    },
    run,
    unmount() {
      off();
      st.seq += 1;
      body = null;
    },
    hints: () => HINTS,
    onKey,
  };
}

export default {
  id: "mora:notes",
  app: "ra",
  describe: "every note in the garden (notes)",
  commands: [{ verb: "notes" }],
  followsSel: false,
  create,
};
