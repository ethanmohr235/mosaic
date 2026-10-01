/* `mosaic:garden` (`garden [‹name›]`, `mo saic`): one garden's details
 * (docs/console.md → Settled for U9) -- its ownership path, its cover and
 * owner, what it owns, counts and size, when it last changed, how it
 * syncs, why it is archived, and what is wrong with its ownership.
 *
 * `garden` alone is the open garden. The `gardens` pane's cursor hands
 * its garden here (instance.action), and the pane rewrites its own line
 * to `garden ‹name›`, so a saved layout reopens on it. ⏎ (or a click on a
 * garden named here) is `cd`. */

import { arg } from "./mosaic-gardens.js";

const HINTS = [["⏎", "open (cd)"], ["click", "a garden named here: cd"]];

function message(err) {
  return String(err && err.message ? err.message : err);
}

function size(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function create(ctx) {
  const { api, components } = ctx;
  const { h } = components;
  const st = { command: "garden", seq: 0, payload: null, error: null };
  let body = null;

  /** A garden name that `cd`s there when clicked. */
  function link(garden) {
    const node = h("span", "home-link", garden);
    node.addEventListener("click", () => ctx.runCommand(`cd ${arg(garden)}`));
    return node;
  }

  function render() {
    if (!body) return;
    if (st.error) return body.replaceChildren(h("div", "pane-error lvl-fault", st.error));
    const p = st.payload;
    if (!p) return;
    const crumbs = [];
    p.crumbs.forEach((g, i) => crumbs.push(i ? " / " : "/ ", i === p.crumbs.length - 1 ? h("span", "bright", g) : link(g)));
    const owner = p.owner
      ? [p.owner.name, h("span", "dim", " in "), link(p.owner.garden),
        ...["due", "status", "author", "revision"].filter((k) => p.owner[k]).map((k) => h("span", "dim", ` · ${k} ${p.owner[k]}`))]
      : h("span", "dim", "none -- loose");
    const owns = p.owns.length ? p.owns.flatMap((g, i) => (i ? [", ", link(g)] : [link(g)])) : h("span", "dim", "nothing");
    const sync = p.sync ? [p.sync.url, h("span", "dim", ` · auto ${p.sync.auto ? "on" : "off"}`)]
      : h("span", "dim", "off -- `garden sync ‹name› ‹url›`");
    const pairs = [
      ["path", crumbs],
      ["owner", owner],
      ["owns", owns],
      ["motes", p.motes === null ? h("span", "lvl-warn", "unreadable -- not a garden?") : `${p.motes} · ${p.relationships} relationships`],
      ["on disk", `${size(p.bytes)} · ${p.path}`],
      ["changed", p.modified.replace("T", " ")],
      ["sync", sync],
    ];
    if (p.archivedBy) pairs.push(["archived", h("span", "lvl-warn", p.archivedBy)]);
    const parts = [h("div", "props-title", [p.garden, p.current ? h("span", "dim", "  · open here") : ""]),
      components.kvGrid(pairs)];
    if (p.problems.length) parts.push(h("div", "home-problems lvl-warn", p.problems.join("\n")));
    body.replaceChildren(...parts);
  }

  async function run(command) {
    st.command = command;
    const seq = ++st.seq;
    let p;
    try {
      p = await api.home.garden(command);
    } catch (err) {
      p = { error: message(err) };
    }
    if (seq !== st.seq || !body) return { summary: "", level: "idle" };
    if (!p || p.error) {
      Object.assign(st, { payload: null, error: (p && p.error) || "no answer" });
      render();
      return { summary: st.error, level: "fault" };
    }
    Object.assign(st, { payload: p, error: null });
    render();
    return { summary: p.problems.length ? `${p.problems.length} problem${p.problems.length === 1 ? "" : "s"}`
      : p.archivedBy ? "archived" : p.current ? "open here" : p.garden,
    level: p.problems.length ? "warn" : "ok" };
  }

  return {
    mount(el) {
      body = h("div", "view-props view-garden scroll");
      el.appendChild(body);
    },
    run,
    /** From the `gardens` pane: show this garden. */
    action({ garden }) {
      const command = `garden ${arg(garden)}`;
      if (garden && command !== st.command) ctx.setCommand(command);
      return true;
    },
    unmount() {
      st.seq += 1;
      body = null;
    },
    hints: () => (st.payload ? HINTS : []),
    onKey(event) {
      if (event.key !== "Enter" || !st.payload || event.ctrlKey || event.metaKey || event.altKey) return false;
      ctx.runCommand(`cd ${arg(st.payload.garden)}`);
      return true;
    },
  };
}

export default {
  id: "mosaic:garden",
  app: "saic",
  describe: "one garden's details: owner, size, sync, problems (garden)",
  commands: [{ verb: "garden" }],
  followsSel: false,
  create,
};
