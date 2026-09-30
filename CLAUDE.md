# Mosaic (code name: monoss)

Read before changing anything:
- `docs/rules.md`, which holds the working rules and code style
- `docs/philosophy.md`, `docs/overview.md` and `docs/roadmap.md`, which
  cover direction and phases
- `docs/console.md` before any UI work

The docs are the source of truth. Update them in the same change as the
code.

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

## Status (2026-09-29)

- Roadmap **phase 1 (cleanup) is done**:
  - Momentum is removed
  - Monarch is frozen
  - commit-tick and run history are cut
  - `why` explains the last solve only
- Console **U1 (the shell) is done**:
  - the package is `console/`, launched by `run_console.py`
  - the grammar lives in `core/commands.py`
- The next UI step is U2. The next roadmap step is phase 2.

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
