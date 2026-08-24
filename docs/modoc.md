# .modoc -- notes written about a graph

A `.modoc` note is prose that reads its own facts out of the garden. A
small LaTeX subset carries the writing; three MonOSS macros carry the
data. Nothing in a note is a copy of anything: the task count, the due
date and the burndown are looked up when the note is rendered, so a note
cannot go stale the way a number typed into a paragraph does.

    \section{Atlas Migration}

    Owned by \mote{Rina Kovac}, due \val{due}, with
    \val{count(targets('contains'))} tasks in scope.

    \graph{targets('contains') by status}

## Where a note lives

In the reserved `modoc` property of an ordinary mote. That is the whole
storage design: a note is not a new object type, and carrying one is not
a role either -- it is data, like `url` or `date`. A project with a note
on it is still a project. It follows that a note is inside mote history,
garden export, undo and MCP's `update_mote` without any of them being
taught about notes.

`.modoc` **files** are import and export only (`monoss modoc
export|import`). A file is exactly the property's string -- no header,
no origin comment -- so the round trip is byte-identical and a file you
were handed is just a note.

### What a note is about

By default, the mote carrying it: `\val{due}` on the Atlas Migration
project reads *that project's* `due`. This is the common case and needs
no setup.

A note that should live on its own mote instead points at its subject
with an `about` relationship:

    note --about--> subject

and every macro reads from the subject. Exactly one `about` moves the
anchor; several is ambiguous, and falls back to the note's own mote with
a diagnostic rather than picking one.

## The language

### Blocks

Each on its own line.

| Syntax | |
| --- | --- |
| `% comment` | dropped; `\%` is a literal percent |
| `\section{...}` | title |
| `\subsection{...}` | section rule |
| `\begin{itemize}` / `\item ...` / `\end{itemize}` | bulleted list |
| `\begin{equation}` ... `\end{equation}` | display math |
| `\graph{...}` | a figure -- see below |
| blank line | paragraph break |

A wrapped `\item` continues on the next line. A block command's argument
may contain inline macros (`\section{Status of \mote{atlas}{Atlas}}`).

### Inline

`\textbf{...}`, `\emph{...}`, `\texttt{...}`, and `$...$` for maths.

Maths is the one place this grammar does not look inside. A `$...$` body
and an `\begin{equation}` body are kept as the TeX you typed and are
typeset by [KaTeX](https://katex.org) (vendored under
`mora/frontend/vendor/katex/`), so `\frac`, `\sum` with limits, `\left(`,
and environments KaTeX knows -- `aligned`, `matrix`, `cases` -- all work:

    \begin{equation}
    \begin{aligned}
    G &= \frac{E}{2(1+\nu)} \\
    \lambda &= \frac{4G^2 \nu}{E - 6G\nu}
    \end{aligned}
    \end{equation}

Nothing outside a formula changes: the macros above are still flat, and
a `\frac` in a formula is never mistaken for an unknown command because
the parser never reads into `$...$` at all. Broken TeX renders in red
where it stands rather than taking the note down with it -- the same
bargain the rest of the format strikes.

`monoss modoc render` has no typesetter to hand, so in the terminal a
formula falls back to symbol and script substitution: `\alpha` becomes
α, `r^{2}` raises the 2, and a `\frac` prints as itself.

The two limits worth knowing: a `$...$` body cannot contain a `$`, and
`\[...\]` is not display-math syntax here -- use `\begin{equation}`.

Escapes: `\\`, `\{`, `\}`, `\%`, `\$`.

An **inline** macro's argument may not contain a brace. `\val{count(targets('x'))}`
is fine; a macro nested inside a macro is not. The alternative is a
brace-matching parser for a format whose point is to stay skimmable in a
plain textarea.

### `\mote` -- link a mote

    \mote{ref}
    \mote{ref}{label}

`ref` is resolved as an exact mote id first, then as a mote whose `name`
matches uniquely (exactly, then case-insensitively). Ids win outright, so
a hand-typed name can never shadow one.

Write names while drafting; let Mora's `@` picker insert ids for anything
you'd rather survive a rename. A reference that matches nothing, or
matches two motes, renders visibly broken and shows up under Problems --
it is never silently blank.

### `\val` -- a value from the graph

    \val{expression}
    \val[ref]{expression}

`[ref]` reads from another mote instead of the anchor.

An expression that is a bare property name the mote actually has is
returned as-is, whatever its type -- this is what makes `\val{due}` a
date. Anything else goes to the same expression language rules use
(`docs/simulation.md`), so all of this works:

    \val{due}                            2026-09-30
    \val{count(targets('contains'))}     12
    \val{sum(targets('contains').cost)}  4820
    \val{budget * 1.2}                   48000
    \val{t}                              the current step
    \val{turbine.work}                   a role's part (see core/roles.py)

A `\val` never has freedoms. It reads the state the garden is in; there
is no solver behind a note to hand a free variable to -- `core/simulation.py::
expression_context()` clears them for exactly that reason.

### `\graph` -- a figure

Block-level, one per line, in two forms:

    \graph{<expression> over t}          a line, one point per step
    \graph{<selection> by <property>}    bars, motes grouped and counted
    \graph[ref]{...}                     either form, anchored elsewhere

`over t` evaluates the expression once per step of a simulation preview,
out to whichever is further: the current `t+N`, or a default eight-step
horizon (a projection of one point is not a projection). `by` takes
something that *selects* motes -- `targets('contains')`, `sources('works_on')` --
and counts them by the value of one property.

## Time

Everything above resolves at a step. `t+0` is the garden as it stands and
reads as **Real**. Any higher step resolves the note against
`run_time_series(commit=False)` that far ahead and reads as
**Projected**: every `\val` moves, both graph forms move with it, and
nothing is written or logged.

Turning a projection into fact is a separate, explicit act -- Mora's
`Apply t+N…`, which asks first. Saving a note and applying a run are
deliberately two buttons: a save must never be a way to write simulation
results.

## Nothing raises

A note with a typo in it still renders, with the broken part visibly
broken. Everything that went wrong -- an unknown command, an unresolved
reference, an expression that wouldn't evaluate -- is collected as a
diagnostic (`core/modoc_bind.py::problems`) and reported in one list, the
same list `monoss modoc check` prints and Mora's Problems panel shows.

## Using it

### Mora

The editor: `./run_mora.py [--garden <name>] [--note <mote id>]`. Source,
Rendered and Data panes, a mode switcher, the `t+N` stepper, and `@` to
insert a link. See `docs/ui-setup.md`.

### CLI

    monoss modoc render <mote> [--step N]    the note, resolved, as text
    monoss modoc check  <mote>               only what didn't resolve
    monoss modoc values <mote> [--step N]    every \val, flat
    monoss modoc export <mote> [--out FILE]
    monoss modoc import <FILE> [--mote ID]

The same five exist in the REPL as `modoc <action> id=<mote>`.

### MCP

`resolve_modoc(mote, step)` returns the source and everything it resolves
to. Writing a note is ordinary `update_mote` on the `modoc` property;
there is no separate write tool, and applying a simulation step stays off
the MCP surface by charter (`docs/architecture.md`).

## Where the code is

| | |
| --- | --- |
| `core/modoc.py` | the grammar. Pure -- no connection, no resolution |
| `core/modoc_bind.py` | what the macros mean, against a garden |
| `core/simulation.py::expression_context` | the evaluation context a `\val` gets |
| `cli/output.py::print_modoc` | the terminal's renderer |
| `mora/` | the window: bridge, payload shaping, frontend |

One parser and one resolver, so `monoss modoc render` and Mora can
disagree about typography and never about meaning.

## Not yet

* No `\include` -- a note is one property.
* No tables, figures-with-captions, references or footnotes.
* `\graph` has two forms. A third waits until these two are used in anger.
* A note cannot read across a portal, for the same reason a rule cannot
  (`docs/architecture.md`, Portals).
