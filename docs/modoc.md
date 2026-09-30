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
| `\begin{enumerate}` ... | the same, numbered |
| `\begin{equation}` ... `\end{equation}` | display math, numbered |
| `\begin{tabular}` ... `\end{tabular}` | a table -- see below |
| `\begin{figure}` ... `\end{figure}` | a captioned, numbered figure |
| `\caption{...}` / `\label{...}` | inside an environment -- see below |
| `\rule{ΣFx}` | a rule's equation, cited -- see below |
| `\include{Method}` | another note's blocks, spliced in |
| `\graph{...}` | a saved Moxie view, drawn here -- see below |
| `\work{bd.l}` | the shown work for one value -- see below |
| `\given` `\answers` `\assumptions` `\checks` `\verification` | a calc section -- see below |
| blank line | paragraph break |

A wrapped `\item` continues on the next line. A block command's argument
may contain inline macros (`\section{Status of \mote{atlas}{Atlas}}`).

### Inline

`\textbf{...}`, `\emph{...}`, `\texttt{...}`, `\ref{...}`, and `$...$` for
maths.

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
    \val{expression}{unit, figures}

`[ref]` reads from another mote instead of the anchor. The second brace
says how to *write* the value: a unit to convert into, a count of
significant figures, or both in either order.

    \val{bd.l}                  2.54 m      -- the unit it is stored in
    \val{bd.l}{in}              100 in
    \val{bd.l}{in, 4}           100.0 in
    \val{bd.l}{3}               2.54 m

Neither half has to be labelled: there is no unit named `3` and no figure
count spelled `in`. The unit is a **display** unit -- nothing about the
value or its `units` entry changes, which is the whole reason a stored unit
and a shown one are separate things (`docs/simulation.md` → *Display units
versus stored units*).

Significant figures keep their trailing zeros, because that is the claim:
`2.50` says the hundredths place was determined and `2.5` says it was not.
An expression rather than a plain property is evaluated in base SI, so a
unit asked for there is the unit to present it in and there is nothing to
convert from; the figure count still applies.

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

### `\work` -- how a value came out

Block-level. It takes the value to show, and optionally the same
`{unit, figures}` `\val` takes.

    \work{bd.l}
    \work{bd.l}{in, 4}
    \work{l}                   -- a bare name reads off the note's anchor

Three lines: the equation, the equation with the values written into it,
and the result.

    bd.l == sqrt(bd.x^2 + bd.y^2)
    bd.l == sqrt((80 in)^2 + (60 in)^2)
    bd.l = 100.0 in

Read from **the last solve's record** (`core/derivations.py`) -- the same
record `why` reads, so a document and a debugger explain one value one way.
It follows that a garden nobody has solved has no work to show: the macro
says so where it stands rather than inventing a derivation.

Three things about the middle line are worth knowing:

- **Values are in the unit each mote declares**, not the base SI the solver
  worked in. Somebody who wrote `80 in` has to see 80.
- **Where one line mixes two units of a dimension they are converted to
  one, and the line says which.** `4.7 kohm + 300 ohm` is shown as
  `(4700 ohm) + (300 ohm)`, noted *converted to ohm*, because declared units
  read well but do not multiply out and a line nobody can check by eye is
  worse than a labelled one.
- **A quantity is bracketed** where it needs to be: `sqrt(80 in^2)` would
  read as eighty square inches, so it is written `(80 in)^2`.

The figure count applies to the **result**, not to the givens. Padding
`80 in` to `80.00 in` would claim precision the given never stated.

A value several equations determined **together** gets one symbolic line
per equation and no substituted line -- there is no single expression to
put numbers into, and inventing one would imply an attribution the solve
never made. The values found with it are named instead.

### The calc sections

Five block macros, each taking nothing but an optional `{unit, figures}`:

| | |
| --- | --- |
| `\given` | every number the equations read and are not free to determine |
| `\answers` | every value the solver is free to determine, and what it holds |
| `\assumptions` | every value marked `source: assumed` |
| `\checks` | every check (a comparison, or an equality marked `check: true`), and whether the values satisfy it |
| `\verification` | each external value beside the hand value it verifies, with the % error |

**They take no argument, and that is the design.** What belongs in a given
list is a property of the garden, not something the note restates -- a note
that had to list them would be the copy this format exists to avoid. The
contents come from `core/calc.py`.

Givens and answers are scoped to the motes the equations actually mention,
so a homework mote's `estimate: 3` is not an input to anything.
Assumptions are not scoped that way: an assumption may be about something
no equation touches ("neglect self-weight"), and leaving it out because
nothing computed with it would defeat the point of writing it down.

An answer nobody has computed yet appears with an empty value rather than
being hidden. A check whose value has not been determined reads *not
checked*, never as a pass.

`\checks` takes no unit or figures -- a relation is not a quantity -- and
says so if handed one.

### `source` -- where a value came from

A given may carry its provenance in the reserved `source` property, a
per-property map in exactly the shape `units` has:

    source:  {"x": "assumed", "sigma_fea": "external"}
    source:  {"x": {"kind": "assumed", "note": "neglect self-weight"}}

`assumed` is what `\assumptions` lists, and the note is the sentence it
prints beside the value. `external` marks a value that came from outside
the garden -- an FEA probe, a measurement -- which is what a verification
table compares against a hand calc. The solver never reads it: where a
value came from changes nothing about how it is used.

### `\verification` -- hand vs. external

An external value names the hand value it verifies with `checks`, in its
own `source` entry:

    bd.sigma:      210  (MPa, solved)
    bd.sigma_fea:  30000  (psi)
    source:  {"sigma_fea": {"kind": "external", "note": "FEA probe", "checks": "sigma"}}

`\verification` then prints one row per such pair: the hand value, the
external value converted into the hand value's unit, and the error
`(hand - external) / |external|` as a percentage -- relative to the
external value, which is the reference the hand calc is being verified
against. `measured` works the same as `external`. A row that cannot be
computed (the hand value not solved yet, an external of zero, units of two
dimensions) says why instead of printing a number. An external value that
names nothing is not a comparison and is left out.

### ±

Where a given carries a ± (the reserved `uncertainty` map, see
`simulation.md` -> *Uncertainty*), `\val`, `\given` and `\answers` print
it: `\val{bd.l}{in, 4}` reads `100.0 ± 0.54 in`. A result's ± is
propagated from the givens' when the note is resolved, at most once per
note, and only in a garden where some given carries one; a garden with
none prints exactly what it did before. The ± is quoted to two
significant figures whatever the value is, and converts with it (by scale,
never offset: ± 1 °C is ± 1 K).

## Numbers and cross-references

Equations, figures and tables are numbered, and a `\label` inside one is
what a `\ref` elsewhere finds.

    \begin{equation}
    \label{eq:pyth}
    l = \sqrt{x^2 + y^2}
    \end{equation}

    By \ref{eq:pyth}, the bar is \val{bd.l}{in} long.

`\ref{eq:pyth}` reads as **Equation 1** -- the kind and the number, not a
bare digit, so a cross-reference reads as one in a sentence. It may point
forward as well as back. A `\ref` naming no label renders visibly broken
(`?eq:pyth`) and shows up under Problems, and a `\label` used twice is
reported rather than one of them silently winning.

**Three counters, as in LaTeX.** An equation, a figure and a table are
numbered independently, because a reader looking for Figure 2 should not
have to know how many equations came before it. `\rule` shares the equation
counter, since what it prints *is* a numbered equation.

Every `\begin{equation}` is numbered whether or not it is labelled, so the
numbers a reader sees do not shift the day somebody adds a label.

`\caption{...}` and `\label{...}` go **inside** the environment they are
about, on their own lines. They are the same two lines in every
environment, which is why they are read once rather than being each
environment's own syntax. Outside one, they are a diagnostic.

### `tabular` -- a table

    \begin{tabular}
    Quantity & Symbol & Value
    Run      & x      & \val{bd.x}
    Rise     & y      & \val{bd.y}
    \caption{Inputs to the bar}
    \label{tab:in}
    \end{tabular}

Cells are separated by `&`, rows by lines, and **the first row is the
header** -- a convention rather than a syntax, because `\hline` and a column
spec would be two more things to type in a format whose point is to stay
skimmable in a plain textarea. A trailing `\\` is stripped if you type one
out of LaTeX habit, and is not required if you don't.

A cell may hold inline macros, so a table of values is a table of lookups
rather than a table of copies.

### `figure` -- a captioned graph

    \begin{figure}
    \graph{Load curve}
    \caption{Load against deflection}
    \label{fig:load}
    \end{figure}

A bare `\graph{...}` is unchanged and stays **unnumbered**: it is an
illustration dropped between two paragraphs. Wrapped in a `figure` it
becomes something a reader can be sent to. Those are two different things
to want, and the figure itself is identical either way.

A `figure` needs exactly one `\graph` in it, since it has one caption and
one number.

### `\rule` -- cite a rule

    \rule{ΣFx}

Prints that rule's equation as a numbered equation, **read from the graph**
rather than copied into the note. That is the whole point: a document citing
a rule shows what the rule says *now*, and cannot drift from it the way a
retyped equation does -- which is exactly the drift `FEA-Homework-4`'s rule
names had already suffered.

The rule's own name is its label, so `\ref{ΣFx}` works with no separate
`\label`: a rule already has a name, and asking for a second one would be
asking twice. A rule a worksheet left unnamed is labelled by its equation
(`core/rules.py::label_of`), so `\rule{bd.l == bd.x * 2}` cites it too --
though naming it with a trailing `! ΣFx` on the worksheet line reads better.

Only a mote that really is a rule is citable: an `equation` **and** an
`applies_to`. A mote with an equation and no `applies_to` is a half-built
rule the solver skips, and quoting it as though it governed something would
be the document saying more than the garden does.

### `\include` -- one document across several notes

    \include{Method}

Splices that note's blocks in where the line stands. It is the one macro
that changes the *document* rather than resolving to something inside it,
so it is not grammar: `core/modoc_bind.py::assemble()` needs the garden to
find the note, and the numbering runs over the assembled result -- an
equation in an included section is Equation 3 of the document, not Equation
1 of its own note. A `\ref` reaches a `\label` in either direction across
the splice.

A note that includes itself, a cycle between two notes, and a chain more
than eight deep are each refused with a diagnostic rather than followed.

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

The calc sections are readable on their own, without a note around
them -- the same content the macros print:

    monoss calc given [--unit U] [--figures N]
    monoss calc answers | assumptions | external
    monoss calc checks
    monoss calc work <mote> <property> [--unit U] [--figures N]

### MCP

`resolve_modoc(mote)` returns the source and everything it resolves to,
the calc sections and shown work included. `calc_sections()` and
`shown_work(mote, key)` answer the same questions without a note to
render, which is what a client building a calculation wants.
Writing a note is ordinary `update_mote` on the `modoc` property; there
is no separate write tool. For what the garden will be rather than what
it is, ask `preview_simulation`.

## Where the code is

| | |
| --- | --- |
| `core/modoc.py` | the grammar, and the numbering. Pure -- no connection |
| `core/modoc_bind.py::assemble` | `\include` spliced in, then numbered as one |
| `core/modoc_bind.py` | what the macros mean, against a garden |
| `core/calc.py` | what goes in the five calc sections, and the shown work |
| `core/plotview.py` | a saved Moxie view, re-solved for a `\graph` |
| `core/simulation.py::expression_context` | the evaluation context a `\val` gets |
| `cli/render.py::print_modoc` | the terminal's renderer |
| `mora/` | the window: bridge, payload shaping, frontend |

One parser and one resolver, so `monoss modoc render` and Mora can
disagree about typography and never about meaning.

## Not yet

* No footnotes, no bibliography, no `\cite` (`roadmap.md` → *Not
  planned*).
* A table's columns cannot be aligned or spanned -- no column spec, no
  `\multicolumn`.
* `\ref` reads as "Equation 1", never as a page or a section number:
  a note has no pages, and sections are not numbered.
* `\graph` has two forms. A third waits until these two are used in anger.
