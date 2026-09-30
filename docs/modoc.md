# .modoc -- notes written about a graph

A `.modoc` note is prose that reads its own facts out of the garden. A
small LaTeX subset carries the writing; three MonOSS macros carry the
data. Nothing in a note is a copy of anything: the task count, the due
date and the burndown are looked up when the note is rendered, so a note
cannot go stale the way a number typed into a paragraph does.

    \section{Atlas Migration}

    Owned by \mote{Rina Kovac}, due \val{due}, with
    \val{count(targets('contains'))} tasks in scope.

    \graph{Burndown}

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
| `\graph{...}` | a saved Moxie view, drawn here -- see below |
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
    \val{t}                              0 -- a note is at no step
    \val{turbine.work}                   a role's part (see core/roles.py)

A `\val` never has freedoms. It reads the state the garden is in; there
is no solver behind a note to hand a free variable to -- `core/simulation.py::
expression_context()` clears them for exactly that reason.

### `\graph` -- a saved Moxie view

Block-level, one per line, and it takes one thing: the name (or id) of a
view saved in [Moxie](ui-setup.md#moxie).

    \graph{Burndown}
    \graph{Capacity by team}
    \graph{7f3c1a02-...}              by id, if two views share a name

Everything about what the figure *is* -- which properties, which ranges,
which scenarios, whether it is a line, a surface or a scatter -- lives on
the view, not in this argument. You design a figure once, in the window
built for designing figures, and a note refers to it.

What is saved is the **specification**, never the points: a view is a
mote carrying a `plot` property (`core/sweep.py::PLOT_KEY`), and
rendering a note re-solves it against the garden as it stands
(`core/plotview.py`). Change a rule, and every note that shows that
figure is already showing the new one. That is the same promise `\val`
makes about a number, kept for a curve.

Two consequences worth stating outright:

* **A figure is a sweep, not a projection.** It varies a *property* --
  the x axis -- and holds the clock still. Nothing in a note moves with
  time; see "No clock" below.
* **One broken rule stops every figure in the garden.** A sweep solves
  the whole coupled system at each point, so a rule that cannot solve
  there -- `prev(...)`, which has no previous tick inside a sweep --
  fails the solve, and the figure says so rather than drawing half of
  one.

A name that matches no saved view, a view whose specification is
unreadable, and a range nothing solves across are each a diagnostic and
a visibly empty figure, never a blank page.

## No clock

Everything above resolves against **the garden as it stands**, and that
is the only reading a note has. `\val{t}` is 0, because a note is not at
a step.

This used to be otherwise: notes resolved at `t+N`, Mora carried a
stepper that flipped **Real** to **Projected**, and an `Apply t+N…`
button committed the projection to the graph. All of it is gone, and the
reason is worth keeping written down. A note says what a garden *is*.
What it will be after N steps is a simulation, which is Motion's window
(`preview_simulation` on the MCP surface); how a value
moves as another one changes is a sweep, which is Moxie's. A note editor
that could also run and commit a simulation was holding a lever that
belonged in another room -- and the projection was never quite the same
answer as the one the window built for it gave.

A note therefore writes nothing at all, and there is nothing it could
write. That is a stronger promise than "it asks first", and it needs no
button to keep.

## Nothing raises

A note with a typo in it still renders, with the broken part visibly
broken. Everything that went wrong -- an unknown command, an unresolved
reference, an expression that wouldn't evaluate -- is collected as a
diagnostic (`core/modoc_bind.py::problems`) and reported in one list, the
same list `monoss modoc check` prints and Mora's Problems popover shows.

## Using it

### Mora

The editor: `./run_mora.py [--garden <name>] [--note <mote id>]`. Source
and Rendered panes, a mode switcher, and `@` to insert a link. See
`docs/ui-setup.md`.

### CLI

    monoss modoc render <mote>               the note, resolved, as text
    monoss modoc check  <mote>               only what didn't resolve
    monoss modoc values <mote>               every \val, flat
    monoss modoc export <mote> [--out FILE]
    monoss modoc import <FILE> [--mote ID]

The same five exist in the REPL as `modoc <action> id=<mote>`.

### MCP

`resolve_modoc(mote)` returns the source and everything it resolves to.
Writing a note is ordinary `update_mote` on the `modoc` property; there
is no separate write tool. For what the garden will be rather than what
it is, ask `preview_simulation`.

## Where the code is

| | |
| --- | --- |
| `core/modoc.py` | the grammar. Pure -- no connection, no resolution |
| `core/modoc_bind.py` | what the macros mean, against a garden |
| `core/plotview.py` | a saved Moxie view, re-solved for a `\graph` |
| `core/simulation.py::expression_context` | the evaluation context a `\val` gets |
| `cli/render.py::print_modoc` | the terminal's renderer |
| `mora/` | the window: bridge, payload shaping, frontend |

One parser and one resolver, so `monoss modoc render` and Mora can
disagree about typography and never about meaning.

## Not yet

* No `\include` -- a note is one property.
* No tables, figures-with-captions, references or footnotes.
* `\graph` has two forms. A third waits until these two are used in anger.
