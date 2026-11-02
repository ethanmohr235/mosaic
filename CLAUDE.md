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
- **Run the tests.** Use `.venv/bin/python -m pytest -q`. The sync tests
  (`test_sync_server.py`, `test_sync.py`, `test_home.py`'s sync case) need
  Docker and skip without it.
- **Commit style.** Write focused commits, and end each message with the
  `Co-Authored-By` line your harness gives you.

## Where things stand (2026-10-06)

Every roadmap phase (1–9) and every console step (U1–U9) is built. The
console is the only window; there are no apps, only views and built-in
layouts (`layout calc|docs|plan|graph|gardens`). What each part is, and
what is still to do, lives in the docs; the short map:

- **Model:** garden-scope names, the worksheet, solve health, checks
  (`?=`), uncertainty (`±`), the materials provider, scenarios, sweeps and
  `fit`. `why` and `\work` explain the garden as it stands from a live solve
  (`core/derivations.py`); nothing about a solve is stored.
- **Functions:** a function mote (`function`, `parameters`) is called by
  name from any formula; a call at 0/0 takes a reported limit
  (`core/functions.py`, `core/limits.py`, `core/function_text.py`); `sinc`.
  `integrate(phi, a, b)` and `deriv(phi, x0)` take one numerically
  (`core/calculus.py`), recorded in provenance.
- **Knobs:** `--set m.k=value [unit]` on `plot`, `sim`, `fit` and
  `graph --flow` tries a value and writes nothing; `--keep` (key `W`)
  writes it (`core/knobs.py`).
- **Plots:** `plot … vs t` draws the run `sim` walks; publication export,
  `--data` overlays.
- **Documents:** the calc macros, LaTeX basics, images, `.tex`/PDF export
  (`core/texexport.py`, tectonic), `submit` and revisions. The read pane's
  KaTeX and the PDF share one maths vocabulary (`core/latex_vocab.py`;
  regenerate `core/data/latex_math.json` with `scripts/latex_vocab.py`
  after a KaTeX or preamble change).
- **Projects:** a mote may own a garden; Home (`gardens`, `garden`) shows
  the tree with solve and check marks; archive is read-only in the console
  too; cross-garden search.
- **Time:** agenda, week, month, plan-my-week, `worked`, `budget`.
- **Sync (2026-10-22):** one account per machine syncs every garden
  through the self-hosted server (`sync_server/`). The parts are the merge
  (`core/merge.py`, stamps in `core/stamps.py`), the all-gardens pass with
  renames, trash and name clashes (`core/sync_all.py`), the worker
  (`core/sync_worker.py`: `monoss sync daemon` or inside the console),
  and the `sync` verb and pane. Login is CLI-only (`monoss sync login`).
- **Still to do** (`roadmap.md`): the project variables pane; `fit` and
  `--data` over a time axis; document health and the submitted revision on
  Home rows; SVG images and surface plots in `.tex` export; symbolic
  derivatives; the Mosaic rename of code (an open question).

## Environment notes

- `gardens/` (the user's real data) is gitignored and not in the repo.
  Seed test gardens in tests (`tests/conftest.py` points `GARDENS_DIR`
  at a temp dir).
- pywebview, GTK and WebKit windows need a display. After touching
  `console/`, run `.venv/bin/python scripts/console_smoke_test.py`.
- Locally, `python3` on PATH is conda. The app runs from `.venv`, which
  is built from `/usr/bin/python3`.
