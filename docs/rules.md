# Rules for agents

Read `philosophy.md`, `overview.md`, and `roadmap.md` before changing code.
Read the reference doc for whatever you touch (`architecture.md`,
`simulation.md`, `modoc.md`, `mcp.md`, `ui-setup.md`).

## Workflow

- Work in phases. Before any non-trivial phase, enter plan mode, ask the
  user about every architecturally significant decision, and get explicit
  approval of the plan before writing code.
- Never guess requirements. If something is ambiguous, ask.
- Judge every feature against the test in `philosophy.md`: does it shorten
  the path from a problem to finished work, or make that work more
  trustworthy?
- Explain architectural changes. Keep commits focused.
- When a decision changes the direction, update the docs in the same
  change. The docs must describe the app as it is and as it is planned,
  never as it was.

## Never

- Rewrite working code without reason.
- Introduce new dependencies without approval.
- Add a separate object model for something that can be a mote.
- Add a feature that requires the user to file things.
- Add live links between gardens.
- Break a garden on disk. Existing `.db` files must always open, and a
  schema change ships with its migration.

## Breaking changes

The project is pre-1.0. Renaming or removing public APIs, apps, or MCP
tools is fine once the user has approved the phase that does it. Migrate
the data and update every caller. Don't leave compatibility shims behind.

## Always

- Keep functions small (under about 40 lines).
- Add type hints everywhere.
- Write tests.
- Keep the plumbing inspectable. Anything a surface creates must be
  ordinary motes and relationships.

## Code style

- Prefer dataclasses.
- Prefer pathlib.
- No global mutable state.
- Composition first. No inheritance unless it's necessary.
- One class per file unless the classes are tightly related.
- No print debugging. Use logging.
- Match the surrounding code's naming, idioms, and comment density.
