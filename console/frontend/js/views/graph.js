/* `graph`: one verb, two drawings (docs/console.md → Settled for U8,
 * Settled for `graph --flow`).
 *
 *   graph …           the force graph of motes and relationships (graph_force.js)
 *   graph … --flow    the solve, left to right (flow.js)
 *
 * The line decides which, so a layout reopens on the same one. This
 * module only holds the instance the line asks for, and swaps it when a
 * rewrite changes the mode. */

import force from "./graph_force.js";
import flow from "./flow.js";

const FLOW = /(^|\s)--flow(\s|$)/;

function create(ctx) {
  let mode = null, inner = null, el = null;

  function swap(command) {
    const want = FLOW.test(command || "") ? "flow" : "force";
    if (want === mode) return;
    if (inner) inner.unmount();
    el.replaceChildren();
    mode = want;
    inner = (want === "flow" ? flow : force).create(ctx);
    inner.mount(el, command);
    ctx.store.emit("hints");
  }

  return {
    mount(node, command) {
      el = node;
      swap(command);
    },
    run(command) {
      swap(command);
      return inner.run(command);
    },
    unmount() {
      if (inner) inner.unmount();
      inner = el = mode = null;
    },
    hints: () => (inner ? inner.hints() : []),
    onKey: (event) => (inner ? inner.onKey(event) : false),
  };
}

export default {
  id: "graph",
  describe: "a mote's neighbourhood or the garden as a graph; --flow: the solve, left to right (graph)",
  commands: [{ verb: "graph", canonical: "graph @sel --depth 2" }],
  followsSel: false,
  create,
};
