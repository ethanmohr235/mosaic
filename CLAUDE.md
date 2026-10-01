# Mosaic (code name: monoss)

Read before changing anything:
- `docs/rules.md`, which holds the working rules and code style
- `docs/philosophy.md`, `docs/overview.md` and `docs/roadmap.md`, which
  cover direction and phases
- `docs/console.md` before any UI work

The docs are the source of truth. Update them in the same change as the
code.

**`docs/manual.md` is the user's quick reference** for every console
command, flag and key. Any change that adds, removes or changes one
updates it in the same commit. Keep it compact: tables, no prose.

## How we work

- **Plan each phase first.** Plan every non-trivial phase before
  writing code. Ask the user about each architecturally significant
  decision and get the plan approved (`rules.md` → *Workflow*).
- **Don't decide open questions yourself.** Anything under *Open
  questions* in `roadmap.md` or `console.md` is the user's call; ask.
- **No new dependencies** without approval.
- **Run the tests.** Use `python -m pytest -q`. `tests/test_sync_server.py`
  needs Docker.
- **Commit style.** Write focused commits, and end each message with the
  `Co-Authored-By` line your harness gives you.

## Status (2026-09-30)

- Roadmap **phase 1 (cleanup) is done**:
  - Momentum is removed
  - Monarch is frozen
  - commit-tick and run history are cut
  - `why` explains the last solve only
- Console **U1 (the shell) is done**:
  - the package is `console/`, launched by `run_console.py`
  - the grammar lives in `core/commands.py`
- Roadmap **phase 2's backend is done**:
  - garden-scope names (`core/formula.py`), which deleted the
    `related_to`-for-resolution edge
  - the worksheet (`core/worksheet.py`), a projection of the graph
  - solve health (`core/health.py`, `core/incidence.py::blocks`)
  - `monoss worksheet read|apply` and `monoss health`; MCP
    `read_worksheet`, `write_worksheet`, `solve_health`
  - it needs a UI: `console.md` step U2+ — see the draft PR's *Handoff to
    frontend*
- Console **U2's core is done** (`core/table.py`, `core/findtext.py`,
  `core/batch.py`, `core/views.py`), built in the cloud session at the
  user's direction; `monolith/payload.py` re-exports from `core/table.py`
  so the old window still works until U2 deletes it.
- Roadmap **phase 3's backend is done**:
  - the calc macros `\work`, `\given`, `\answers`, `\assumptions`,
    `\checks`, and `\val{bd.l}{in, 4}` (`core/calc.py`)
  - LaTeX basics: numbered equations, `\label`/`\ref`, `\rule` citing a
    rule, `figure` captions, `tabular`, `enumerate`, and `\include`
    (`core/modoc.py`, `core/modoc_bind.py::assemble`)
  - new reserved property `source` (`assumed`, `external`)
  - `monoss calc ...`; MCP `calc_sections`, `shown_work`
  - its UI is the console's read pane (U6)
- Roadmap **phase 4's backend is done** (`core/projects.py`): the reserved
  `garden` property, `start_project`, the one-owner check, the ownership
  tree and the cover (`name`, `due`, `status`, `author`, `revision`);
  `monoss project start|stop|tree|check`; MCP `project_tree`,
  `start_project`.
- Roadmap **phase 7's backend is done** (Moxie's publication export is
  UI and still to do; `plot export csv|png` exists):
  - checks: reserved `check: true` / `tolerance` on a rule, `?=` in the
    worksheet (`core/rules.py`, `core/simulation.py`)
  - the V&V table: `source: {..., checks: <key>}`, `calc.verification`,
    `\verification`, `monoss calc verification`
  - CSV import (`core/csv_import.py`, `monoss import csv`, MCP `import_csv`)
- Roadmap **phase 8's backend is done** (`core/uncertainty.py`): the
  reserved `uncertainty` map, propagation on read, ± in `\val`/`\given`/
  `\answers` and the worksheet (`bd.x = 80 ± 0.5 in`); `monoss
  uncertainty`; MCP `propagate_uncertainty`.
- Roadmap **phase 9's backend is done**: archive (reserved `archived`,
  `projects.set_archived`/`ensure_writable`, CLI/MCP writes refuse an
  archived garden), cross-garden search (`core/search.py`, `monoss
  search`, MCP `search_gardens`), and the `materials` property provider
  (`core/materials_provider.py` over `core/data/materials.json`).
- Every roadmap phase's backend through 9 is done except phases 5 and 6,
  which the user's brief did not assign to the cloud session.
- Console **U2 is done**: the `find` table (`monolith:table`,
  `console/monolith.py` over `core/table.py`/`findtext.py`), the `find`
  and `set` verbs (`core/commands.py`; `set @sel` writes the whole
  selection as one undo step), and Monolith's old window is deleted.
  At the user's direction the cloud session now does frontend work too,
  since the local session stopped.
- Console **U3 is done**: the `mote:props` pane (`open @sel|#id|name`,
  `mo te`) and the `p` float over the same view; `core/inspect.py` is the
  payload both it and the old floating panel draw; `rename`, `unlink`
  and `template` verbs; every typed statement is undoable
  (`core/command_bar.py`'s write journal).
- Console **U4 is done**: the `moxie:plot` pane (`plot …`, `mo xie`) over
  `core/plotcmd.py` (the grammar, rewrites, line → saved-plot spec) and
  `core/plotview.py` (`figure_from_spec`, `sensitivity`, `save_plot`);
  `console/moxie.py` is `api.moxie`; `plot save`/`plot export` without a
  line are a `pane_action` for the focused plot pane. Moxie's old window
  is deleted.
- **`fit` is done** (`core/fit.py`: Levenberg–Marquardt over the sweep's
  solve, numpy only; `fit … --apply` writes value, σ and `source:
  fitted`): the `moxie:fit` pane, `monoss fit`, MCP `fit_model`. The plot
  pane's follow-up fixes are in too (narrow rail, solves off the lock,
  magnitude-split axes).
- Console **U5 is done**: Motion is `mo tion` (`scenarios | sim | rule
  @sel`) over `core/motion.py` (the `sim` grammar and run rows, rule
  cards, `why`, the undoable `solve`, the live solve); `console/motion.py`
  is `api.motion`; `set --scenario` writes a scenario's override; the
  shared playhead is store state. Motion's old window is deleted.
- Console **U6 is done**: Mora is `mo ra` (`notes | edit @sel | read
  @sel`) plus the `worksheet` pane; `core/document.py` renders a note's
  draft (every macro), `console/mora.py` is `api.mora`, `js/editor.js` is
  the modal editor with find/replace, and `core/worksheet.py` applies as
  one undo step. Mora's old window is deleted.
- Console **U7 is done**: Monastery is `mo nastery` (`agenda | week`,
  and `month`) over `core/agenda.py` (the grammar, rows, bars, edits as
  `set` lines, the `detach` verb); `console/monastery.py` is
  `api.monastery`. Roadmap phase 5's bars and click-to-open are in;
  plan-my-week and planned vs. actual are not. Monastery's old window is
  deleted. The next UI step is U8 (Monarch).

## Split of work: cloud backend, local frontend

Two sessions work in parallel:
- A **cloud session** does backend work that pytest can verify:
  `core/`, `cli/`, `mcp_server/`, their tests and their docs.
- A **local session** does the console frontend and app ports (U2–U9),
  because those need a display for the pywebview smoke tests and
  screenshots.

**The cloud session does not touch:**
- `console/`, any `*/frontend/`, `frontend_shared/`, `window_launcher.py`,
  `scripts/*_smoke_test.py` or the per-app `*/api.py` bridges
- `core/commands.py` and `RESERVED_VERBS` in `core/reserved.py`, which
  belong to the console grammar

When a roadmap phase needs UI, build its `core/` part with tests and a
CLI/MCP surface. Then write down the UI it needs under *Handoff to
frontend* in the PR description.

**Branch protocol for the cloud session:**
- Work on `cloud/backend`, rebased or merged on `master` often.
- Keep one draft PR open for it (`cloud/backend` → `master`).
- Keep its description current as the status board:
  - phases done, in progress and next
  - decisions the user made
  - questions waiting on the user
  - *Handoff to frontend*
- Don't merge to `master` yourself. The user or the local session
  merges.

## Environment notes

- `gardens/` (the user's real data) is gitignored and not in the repo.
  Seed test gardens in tests (`tests/conftest.py` points `GARDENS_DIR`
  at a temp dir).
- pywebview, GTK and WebKit windows can't run without a display. Don't
  try to run `scripts/*_smoke_test.py` in the cloud.
- Locally, `python3` on PATH is conda. The app runs from `.venv`, which
  is built from `/usr/bin/python3`.
