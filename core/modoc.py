# core/modoc.py
"""The .modoc note format: a small LaTeX subset plus three MonOSS macros.

A note is a string in the reserved `modoc` property of an ordinary mote
(docs/architecture.md's Notes section). It is not a separate object type,
and `.modoc` files are an import/export shape -- the property is the
canonical copy, which is what puts notes inside sync, garden
export and undo for free.

This module is the *language* and nothing else: parsing is pure, takes no
connection, and resolves nothing. What `\\mote{...}` points at, what
`\\val{...}` evaluates to and which saved view `\\graph{...}` draws are
core/modoc_bind.py's business, because those are questions about a garden
rather than about a note. Keeping them apart is what lets the whole
grammar be tested with strings.

Two rules the grammar leans on, both deliberate:

* A macro argument cannot contain a brace. `\\val{count(targets('x'))}`
  is fine; a macro nested inside a macro is not. The alternative is a
  bracket-matching parser for a format whose entire purpose is to be
  skimmable in a plain textarea.
* Nothing here raises. A note with a typo in it must still render, with
  the broken part visibly broken -- so every failure becomes a
  Diagnostic and parsing carries on.
"""
import dataclasses
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core.relationships import list_relationships

MODOC_KEY = "modoc"
ABOUT = "about"

# Inline macros. `graph` is deliberately absent: it produces a figure, and
# a figure inside a sentence is a different feature from a figure between
# two paragraphs. It is recognized at block level only. So are the calc
# sections and `\work`, for the same reason -- each is a table or a run of
# display maths, not something that belongs mid-sentence.
INLINE_COMMANDS = ("mote", "val", "ref", "textbf", "emph", "texttt")

# Block commands that take one braced argument.
CAPTION_COMMAND = "caption"
LABEL_COMMAND = "label"
# `\caption` and `\label` are about the environment around them rather than
# part of its body, so they are read by the environment and never appear as
# blocks of their own.
META_COMMANDS = (CAPTION_COMMAND, LABEL_COMMAND)

# The five calc sections. `\work` takes the value to show; the other four
# take nothing, because what goes in them is a property of the garden
# rather than of the note (docs/modoc.md, "The calc sections").
WORK_COMMAND = "work"
SECTION_COMMANDS = ("given", "answers", "assumptions", "checks", "verification")
CALC_COMMANDS = (WORK_COMMAND, *SECTION_COMMANDS)

# Block commands with one braced argument, and what each makes.
_BLOCK_HANDLERS = ("section", "subsection", "graph", "rule", "include")
IMAGE_COMMAND = "image"
BLOCK_COMMANDS = (
    *_BLOCK_HANDLERS, IMAGE_COMMAND, "item", "begin", "end", *META_COMMANDS,
    *CALC_COMMANDS,
)

_ESCAPABLE = "\\{}%$"

_INLINE_RE = re.compile(
    r"\\(?P<esc>[\\{}%$])"
    r"|\\(?P<cmd>" + "|".join(INLINE_COMMANDS) + r")"
    r"(?:\[(?P<ref>[^\[\]]*)\])?"
    r"\{(?P<first>[^{}]*)\}"
    r"(?:\{(?P<second>[^{}]*)\})?"
    r"|\$(?P<math>[^$]*)\$"
)
# A `\val` inside maths, the one macro a formula is read for. `\\` is
# matched first so a TeX line break before `val` is never taken for one.
_MATH_VALUE_RE = re.compile(
    r"\\\\"
    r"|\\val(?:\[(?P<ref>[^\[\]]*)\])?"
    r"\{(?P<first>[^{}]*)\}"
    r"(?:\{(?P<second>[^{}]*)\})?"
)
# A backslash-word left over after the pass above: a command we don't
# know, or a known one missing its argument. Either way it is worth
# saying so rather than printing the backslash and moving on.
_STRAY_COMMAND_RE = re.compile(r"\\([a-zA-Z]+)")
_COMMENT_RE = re.compile(r"(?<!\\)%")
# Greedy to the line's last brace: unlike an inline macro, a block
# command's argument may hold inline macros of its own, so
# `\section{Status of \mote{x}{Atlas}}` has to reach the outer close.
_BLOCK_RE = re.compile(r"^\\(?P<cmd>[a-zA-Z]+)(?:\[(?P<ref>[^\[\]]*)\])?\{(?P<arg>.*)\}$")
_ENVIRONMENT_RE = re.compile(r"^\\(begin|end)\{([a-zA-Z]*)\}$")
# `\caption{...}` / `\label{...}` on their own line inside an environment.
_META_RE = re.compile(
    r"^\\(?P<cmd>" + "|".join(META_COMMANDS) + r")\{(?P<arg>.*)\}$"
)
# A `\graph{...}` line, wherever it is -- used both as a block of its own
# and to find the one inside a `figure` environment.
_FIGURE_RE = re.compile(r"^\\graph\{(?P<arg>[^{}]*)\}$")
# `\image{shot-001}` or `\image{shot-001}{width=60}`. Non-greedy braces for
# the same reason as _CALC_RE: the second brace is options, not more name.
_IMAGE_RE = re.compile(
    r"^\\image\{(?P<arg>[^{}]*)\}(?:\{(?P<opts>[^{}]*)\})?$"
)
_WIDTH_RE = re.compile(r"^width\s*=\s*(\d{1,3})$")
# A calc block: `\work{bd.l}`, `\work{bd.l}{in, 3}`, `\given`, `\checks{3}`.
# Braces are non-greedy here, unlike _BLOCK_RE's, because a calc argument
# holds a reference or a format spec rather than inline macros -- so the
# second brace is a second argument and not part of the first.
_CALC_RE = re.compile(
    r"^\\(?P<cmd>" + "|".join(CALC_COMMANDS) + r")"
    r"(?:\{(?P<arg>[^{}]*)\})?"
    r"(?:\{(?P<fmt>[^{}]*)\})?$"
)


# --- diagnostics -----------------------------------------------------

@dataclass(frozen=True)
class Diagnostic:
    """Something wrong with a note, reported rather than raised. `line` is
    1-based, or 0 for a diagnostic about the note as a whole."""
    line: int
    kind: str
    message: str


# --- inline nodes ----------------------------------------------------

@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Styled:
    """`\\textbf`, `\\emph`, `\\texttt`. Flat on purpose -- these do not
    nest, so style is a name rather than a tree."""
    style: str
    text: str


@dataclass(frozen=True)
class Math:
    """`$...$`. The raw source, kept raw: apart from a `\\val` (see
    math_values()), math is the one place this grammar does not look
    inside. Mora hands the string to KaTeX and the CLI reduces it with
    math_spans(); neither reading belongs in a parse tree the other has to
    agree with."""
    text: str
    line: int = 0


@dataclass(frozen=True)
class Link:
    """`\\mote{ref}` or `\\mote{ref}{label}`. `ref` is an id or a name;
    which one is core/modoc_bind.py's problem."""
    ref: str
    label: str | None
    line: int


@dataclass(frozen=True)
class Format:
    """How a quantity should be written: a unit to convert into, and how
    many significant figures to quote.

    Both optional, and both read from one comma-separated argument --
    `{in}`, `{3}`, `{in, 3}`. Grammar rather than meaning, which is why it
    lives here: whether `in` is a unit this garden can convert to is
    core/units.py's answer, and this only says which half of the argument
    was asking."""
    unit: str | None = None
    figures: int | None = None

    @property
    def empty(self) -> bool:
        return self.unit is None and self.figures is None


def parse_format(text: str | None) -> Format:
    """`"in, 3"` -> Format("in", 3). A bare integer is significant figures
    and anything else is a unit name, so neither has to be labelled --
    there is no unit named `3`, and no figure count spelled `in`."""
    if not text:
        return Format()
    unit: str | None = None
    figures: int | None = None
    for part in (p.strip() for p in text.split(",")):
        if not part:
            continue
        if part.isdigit():
            figures = int(part)
        elif unit is None:
            unit = part
    return Format(unit, figures)


@dataclass(frozen=True)
class Value:
    """`\\val{expr}`, `\\val[ref]{expr}` to read it off another mote, or
    `\\val{expr}{in, 3}` to say how to write it."""
    expression: str
    ref: str | None
    line: int
    format: Format = field(default_factory=Format)


@dataclass(frozen=True)
class Ref:
    """`\\ref{eq:bar}` -- the number of whatever carries that `\\label`.

    Resolved against the *document* rather than against the garden, which
    is why it needs no binder: a cross-reference is a fact about the text,
    and numbering it is `numbered()` below."""
    name: str
    line: int


Inline = Text | Styled | Math | Link | Value | Ref


# --- block nodes -----------------------------------------------------

@dataclass(frozen=True)
class Heading:
    level: int
    content: list[Inline]
    line: int


@dataclass(frozen=True)
class Paragraph:
    content: list[Inline]
    line: int


@dataclass(frozen=True)
class ItemList:
    """`itemize` or, when `ordered`, `enumerate`. One node for both, because
    a list is a list and the only difference is what the marker says."""
    items: list[list[Inline]]
    line: int
    ordered: bool = False


@dataclass(frozen=True)
class Equation:
    """Display maths, numbered in document order.

    `label` is a `\\label{...}` line inside the environment, and is what a
    `\\ref{...}` elsewhere finds. Numbered whether or not it is labelled, so
    the numbers a reader sees do not shift when somebody adds a label."""
    text: str
    line: int
    label: str = ""
    number: int = 0


@dataclass(frozen=True)
class Graph:
    """`\\graph{Load curve}`: a figure, named rather than described.

    `view` is the name or id of a mote carrying a saved Moxie view
    (core/sweep.py::PLOT_KEY), and everything about what the figure
    *is* -- which properties, which ranges, which scenarios, a line or a
    surface or a scatter -- lives on that mote rather than in this
    argument. Which mote it names is core/plotview.py's business, for the
    same reason `\\mote{...}` leaves resolution to core/modoc_bind.py.

    A bare `\\graph` has no caption and **no number**: it is an illustration
    dropped between two paragraphs. Wrapped in `\\begin{figure}` it becomes
    a numbered, captioned figure a reader can be sent to by `\\ref`. Those
    are two different things to want, and one node carries both because the
    figure itself is identical either way."""
    view: str
    line: int
    caption: list[Inline] = field(default_factory=list)
    label: str = ""
    number: int = 0


@dataclass(frozen=True)
class Image:
    """`\\image{shot-001}`: a stored picture (core/images.py), named.

    `ref` is an image mote's name or id; which one it finds is
    core/modoc_bind.py's business, as with `\\graph`. `width` is a percentage
    of the text width (written without a `%`, which starts a comment), 0
    meaning the picture's own size up to the column.
    Numbered exactly as a `\\graph` is: bare it is an unnumbered
    illustration, inside `\\begin{figure}` it takes a caption and a number
    from the same counter."""
    ref: str
    line: int
    width: int = 0
    caption: list[Inline] = field(default_factory=list)
    label: str = ""
    number: int = 0


@dataclass(frozen=True)
class Table:
    """`\\begin{tabular}`: rows of cells, `&` between them.

    The first row is the header, which is a convention rather than a
    syntax -- `\\hline` and a column spec would be two more things to type
    for a format whose point is to stay skimmable in a plain textarea. A
    table is numbered and captioned like a figure."""
    header: list[list[Inline]]
    rows: list[list[list[Inline]]]
    line: int
    caption: list[Inline] = field(default_factory=list)
    label: str = ""
    number: int = 0


@dataclass(frozen=True)
class Cited:
    """`\\rule{ΣFx}`: a rule's equation, quoted as a numbered equation.

    The equation is read from the graph rather than copied into the note,
    which is the whole point -- a document citing a rule shows what that
    rule says *now*, and cannot drift from it the way a retyped equation
    does. Which mote the name finds is core/modoc_bind.py's business.

    The rule's own name is its label, so `\\ref{ΣFx}` works without a
    separate `\\label` line: a rule already has a name, and asking for a
    second one would be asking twice."""
    rule: str
    line: int
    number: int = 0

    @property
    def label(self) -> str:
        return self.rule


@dataclass(frozen=True)
class Include:
    """`\\include{Method}`: another note's blocks, spliced in here.

    The one macro that changes the *document* rather than resolving to
    something inside it, which is why splicing is
    core/modoc_bind.py::assemble() and not part of the grammar: it needs the
    garden to find the note, and the numbering has to run over the whole
    assembled thing rather than over each piece."""
    ref: str
    line: int


@dataclass(frozen=True)
class Work:
    """`\\work{bd.l}`, or `\\work{bd.l}{in, 4}`: the shown work for one
    value -- the equation, the equation with numbers in it, the result.

    `target` is a `mote.property` reference, or a bare property read off the
    note's anchor. Which mote that names and where the three lines come from
    are core/calc.py's business, for the same reason `\\graph{...}` leaves
    its view to core/plotview.py."""
    target: str
    line: int
    format: Format = field(default_factory=Format)


@dataclass(frozen=True)
class Section:
    """`\\given`, `\\answers`, `\\assumptions` or `\\checks`: one of a calc
    package's standing sections, as a table.

    It takes no argument, and that is the design. What belongs in a
    document's given list is a property of the *garden* -- the numbers its
    equations read and are not free to determine -- not something the note
    restates. A note that had to list them would be the copy this format
    exists to avoid."""
    kind: str
    line: int
    format: Format = field(default_factory=Format)


Block = (
    Heading | Paragraph | ItemList | Equation | Graph | Image | Table | Cited
    | Include | Work | Section
)

# What a `\label` can point at, and which counter numbers it.
EQUATION_LABEL = "equation"
FIGURE_LABEL = "figure"
TABLE_LABEL = "table"


@dataclass(frozen=True)
class Label:
    """Where a `\\ref` lands: what kind of thing was labelled, and its
    number. The kind is carried so a renderer can write "Figure 2" rather
    than a bare 2 -- which is the difference between a cross-reference and
    a number sitting in a sentence."""
    kind: str
    number: int
    line: int = 0


@dataclass(frozen=True)
class Document:
    blocks: list[Block] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    # Every `\label` in the document, and what it points at. Filled in by
    # numbered() rather than by the parser, because an `\include` splices
    # blocks in and the numbers have to be counted over the whole assembled
    # document rather than over each piece of it.
    labels: dict[str, Label] = field(default_factory=dict)


# --- parsing ---------------------------------------------------------

def parse(source: str) -> Document:
    """Source text to a Document. Pure: no connection, no resolution, no
    exceptions."""
    parser = _Parser()
    for number, raw in enumerate(source.splitlines(), start=1):
        parser.feed(number, strip_comment(raw).strip())
    return numbered(parser.finish())


# Which counter each numbered block draws from. Three counters, as in
# LaTeX: an equation, a figure and a table are numbered independently,
# because a reader looking for "Figure 2" should not have to know how many
# equations came before it.
# The diagnostics numbered() works out for itself, and so must not inherit.
_RECOMPUTED = frozenset({"unresolved_ref", "duplicate_label"})

_COUNTERS: dict[type, str] = {
    Equation: EQUATION_LABEL,
    Cited: EQUATION_LABEL,
    Graph: FIGURE_LABEL,
    Image: FIGURE_LABEL,
    Table: TABLE_LABEL,
}


# The prefix `\ref{eq:3}` / `\ref{fig:2}` / `\ref{tab:1}` uses to reach a
# block by its number when it has no `\label`.
_NUMBER_PREFIX = {EQUATION_LABEL: "eq", FIGURE_LABEL: "fig", TABLE_LABEL: "tab"}


def numbered(document: Document) -> Document:
    """The document with every numbered block numbered and every `\\label`
    collected, plus a diagnostic for a duplicate label or a `\\ref` to one
    that does not exist.

    Pure and recomputed from scratch, so it can be run again over a
    document that has had `\\include`s spliced into it
    (core/modoc_bind.py::assemble) without the first pass' numbers
    surviving to confuse the second. A bare `\\graph` -- an illustration
    between two paragraphs rather than a figure a reader is sent to -- has
    no caption and no label, and is left unnumbered."""
    counters: dict[str, int] = {}
    blocks: list[Block] = []
    labels: dict[str, Label] = {}
    positional: dict[str, Label] = {}
    # Anything this pass is about to work out again is dropped first, which
    # is what makes running it twice safe. A `\ref` in an included note may
    # point at a `\label` in the note that included it, so the first pass
    # over each piece on its own is *bound* to call some of them dangling;
    # only the assembled document knows.
    diagnostics = [d for d in document.diagnostics if d.kind not in _RECOMPUTED]

    for block in document.blocks:
        kind = _COUNTERS.get(type(block))
        if kind is None or not _is_numbered(block):
            blocks.append(block)
            continue
        counters[kind] = counters.get(kind, 0) + 1
        block = dataclasses.replace(block, number=counters[kind])
        blocks.append(block)
        positional[f"{_NUMBER_PREFIX[kind]}:{counters[kind]}"] = Label(
            kind, counters[kind], block.line)
        if block.label:
            if block.label in labels:
                diagnostics.append(Diagnostic(
                    block.line, "duplicate_label",
                    f"\\label{{{block.label}}} is used twice -- a \\ref to it "
                    f"cannot say which one was meant",
                ))
            labels.setdefault(block.label, Label(kind, counters[kind], block.line))

    # `\ref{eq:3}` finds the third equation by position; a `\label` of the
    # same name wins, so a name the author chose is never shadowed.
    for name, label in positional.items():
        labels.setdefault(name, label)

    document = Document(blocks, diagnostics, labels)
    document.diagnostics.extend(_dangling_refs(document))
    return document


def _is_numbered(block: Block) -> bool:
    """A `\\graph` or `\\image` earns a number by being a figure -- captioned or labelled
    -- rather than by being a graph. Everything else in the counter table is
    numbered always."""
    if isinstance(block, (Graph, Image)):
        return bool(block.caption or block.label)
    return True


def _dangling_refs(document: Document) -> list[Diagnostic]:
    return [
        Diagnostic(
            node.line, "unresolved_ref",
            f"\\ref{{{node.name}}} names no \\label in this document",
        )
        for node in _inlines(document)
        if isinstance(node, Ref) and node.name not in document.labels
    ]


LABEL_WORDS = {
    EQUATION_LABEL: "Equation",
    FIGURE_LABEL: "Figure",
    TABLE_LABEL: "Table",
}


def reference_text(document: Document, node: Ref) -> str:
    """How a `\\ref` reads: `Figure 2`, or a visibly broken `?name` for one
    that points nowhere. Here rather than in each renderer, because what a
    cross-reference *says* is part of the document and only its typography
    belongs to a surface."""
    label = document.labels.get(node.name)
    if label is None:
        return f"?{node.name}"
    return f"{LABEL_WORDS.get(label.kind, label.kind)} {label.number}"


def strip_comment(line: str) -> str:
    """Everything before an unescaped `%`."""
    match = _COMMENT_RE.search(line)
    return line if match is None else line[: match.start()]


class _Parser:
    """A line-at-a-time block parser. A class rather than a closure over
    six locals because an environment is an open/close state, and threading
    that through a fold reads worse than holding it.

    One state covers every environment. It used to be a field per
    environment -- `_items` and `_equation`, each with its own feed and
    close -- and adding `enumerate`, `tabular` and `figure` to that shape
    would have meant five of everything. What differs between them is only
    how the collected body is read at `\\end`, so that is the only thing
    kept per environment (`_CLOSERS`); `\\caption` and `\\label` are the
    same two lines inside all of them and are read once, here."""

    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.diagnostics: list[Diagnostic] = []
        self._para: list[str] = []
        self._para_line = 0
        self._env: str | None = None
        # [line, text] pairs, not bare strings: a bad \val on the third item
        # should be reported at the third item, not at the \begin the whole
        # list opened on.
        self._body: list[list] = []
        self._caption = ""
        self._label = ""
        self._open_line = 0

    def feed(self, number: int, line: str) -> None:
        if self._env is not None:
            self._feed_environment(number, line)
        elif not line:
            self._flush()
        elif line.startswith("\\"):
            self._feed_command(number, line)
        else:
            self._push(number, line)

    def finish(self) -> Document:
        if self._env is not None:
            self.diagnostics.append(Diagnostic(
                self._open_line, "unterminated",
                f"\\begin{{{self._env}}} was never closed with \\end{{{self._env}}}",
            ))
            self._close(self._open_line)
        self._flush()
        return Document(self.blocks, self.diagnostics)

    # -- environments --

    def _open(self, number: int, name: str) -> None:
        self._flush()
        self._env, self._open_line = name, number
        self._body, self._caption, self._label = [], "", ""

    def _feed_environment(self, number: int, line: str) -> None:
        if line == f"\\end{{{self._env}}}":
            self._close(number)
            return
        meta = _META_RE.match(line)
        if meta is not None:
            # `\caption` and `\label` are about the environment rather than
            # part of its body, in every environment that has one.
            if meta.group("cmd") == CAPTION_COMMAND:
                self._caption = meta.group("arg").strip()
            else:
                self._label = meta.group("arg").strip()
            return
        self._body.append([number, line])

    def _close(self, number: int) -> None:
        name, body = self._env, self._body
        self._env, self._body = None, []
        if name is None:
            return
        _CLOSERS[name](self, name, body, self._open_line or number)

    def _close_list(self, name: str, body: list[list], line: int) -> None:
        """`\\item` starts an item and a bare line continues the one
        before, so an item that wraps in the editor is still one item."""
        items: list[list] = []
        for at, text in body:
            if text.startswith("\\item"):
                items.append([at, text[len("\\item"):].strip()])
            elif text and items:
                items[-1][1] += " " + text
            elif text:
                self.diagnostics.append(Diagnostic(
                    at, "stray_text", f"text inside {name} before the first \\item",
                ))
        if items:
            self.blocks.append(ItemList(
                [self._inline(at, text) for at, text in items],
                line,
                ordered=name == "enumerate",
            ))

    def _close_equation(self, _name: str, body: list[list], line: int) -> None:
        self.blocks.append(Equation(
            " ".join(text for _at, text in body).strip(), line, self._label,
        ))

    def _close_tabular(self, _name: str, body: list[list], line: int) -> None:
        """Rows of cells, `&` between them, the first row the header. A
        trailing `\\\\` is stripped rather than required: a LaTeX habit
        should not be a syntax error, and its absence should not be one
        either."""
        rows = [
            [self._inline(at, cell.strip()) for cell in _row_cells(text)]
            for at, text in body if text.strip(" \\")
        ]
        if not rows:
            self.diagnostics.append(Diagnostic(
                line, "empty_table", "\\begin{tabular} has no rows in it",
            ))
            return
        self.blocks.append(Table(
            rows[0], rows[1:], line, self._inline(line, self._caption), self._label,
        ))

    def _close_figure(self, _name: str, body: list[list], line: int) -> None:
        """A figure is a `\\graph` or an `\\image` with a caption and a number
        around it, so the environment only has to find the one inside
        itself."""
        found = []
        for at, text in body:
            if (match := _FIGURE_RE.match(text)) is not None:
                found.append(Graph(
                    match.group("arg").strip(), line,
                    self._inline(line, self._caption), self._label,
                ))
            elif (match := _IMAGE_RE.match(text)) is not None:
                found.append(self._image(
                    at, match, self._inline(line, self._caption), self._label,
                ))
        if len(found) != 1:
            self.diagnostics.append(Diagnostic(
                line, "bad_figure",
                f"\\begin{{figure}} holds {len(found)} \\graph or \\image lines -- "
                f"it needs exactly one, since a figure has one caption and one number",
            ))
            if not found:
                return
        self.blocks.append(found[0])

    def _image(
        self, number: int, match: re.Match, caption: list[Inline] | None = None,
        label: str = "",
    ) -> Image:
        ref = match.group("arg").strip()
        if not ref:
            self.diagnostics.append(Diagnostic(
                number, "bad_image",
                "\\image{} needs the name of a stored image (`monoss image add`)",
            ))
        return Image(
            ref, number, self._width(number, match.group("opts")), caption or [], label,
        )

    def _width(self, number: int, opts: str | None) -> int:
        if not opts or not opts.strip():
            return 0
        found = _WIDTH_RE.match(opts.strip())
        if found is None or not 0 < int(found.group(1)) <= 100:
            self.diagnostics.append(Diagnostic(
                number, "bad_image_option",
                f"\\image option {opts.strip()!r} -- the only one is width=N, "
                f"N being a percent of the text width from 1 to 100",
            ))
            return 0
        return int(found.group(1))

    def _feed_command(self, number: int, line: str) -> None:
        environment = _ENVIRONMENT_RE.match(line)
        if environment is not None:
            self._feed_environment_command(number, *environment.groups())
            return
        # Before _BLOCK_RE, whose greedy argument would swallow a calc
        # command's second brace into its first.
        calc = _CALC_RE.match(line)
        if calc is not None:
            self._flush()
            self._feed_calc(number, calc)
            return
        image = _IMAGE_RE.match(line)
        if image is not None:
            self._flush()
            self.blocks.append(self._image(number, image))
            return
        meta = _META_RE.match(line)
        if meta is not None:
            self.diagnostics.append(Diagnostic(
                number, "stray_meta",
                f"\\{meta.group('cmd')}{{...}} belongs inside an environment -- "
                f"put it between \\begin{{...}} and \\end{{...}}",
            ))
            return
        match = _BLOCK_RE.match(line)
        if match is None or match.group("cmd") not in _BLOCK_HANDLERS:
            self._push(number, line)
            return
        self._flush()
        command, arg = match.group("cmd"), match.group("arg")
        if command == "graph":
            self._feed_graph(number, arg, match.group("ref"))
        elif command == "rule":
            self._feed_cited(number, arg)
        elif command == "include":
            self._feed_include(number, arg)
        else:
            level = 1 if command == "section" else 2
            self.blocks.append(Heading(level, self._inline(number, arg), number))

    def _feed_environment_command(self, number: int, verb: str, name: str) -> None:
        if verb == "begin" and name in ENVIRONMENTS:
            self._open(number, name)
            return
        self.diagnostics.append(Diagnostic(
            number, "stray_environment",
            f"\\{verb}{{{name}}} outside any environment -- the ones this format "
            f"knows are {', '.join(ENVIRONMENTS)}",
        ))

    def _feed_cited(self, number: int, arg: str) -> None:
        rule = arg.strip()
        if not rule:
            self.diagnostics.append(Diagnostic(
                number, "bad_rule",
                "\\rule{} needs the name or id of a rule, e.g. \\rule{ΣFx}",
            ))
            return
        self.blocks.append(Cited(rule, number))

    def _feed_include(self, number: int, arg: str) -> None:
        ref = arg.strip()
        if not ref:
            self.diagnostics.append(Diagnostic(
                number, "bad_include",
                "\\include{} needs the name or id of the note to include",
            ))
            return
        self.blocks.append(Include(ref, number))

    def _feed_calc(self, number: int, match: re.Match) -> None:
        """`\\work` needs to know which value; the four section commands
        need nothing, and are reported if handed something anyway rather
        than having the argument quietly ignored."""
        command, arg = match.group("cmd"), (match.group("arg") or "").strip()
        if command == WORK_COMMAND:
            if not arg:
                self.diagnostics.append(Diagnostic(
                    number, "bad_work",
                    "\\work{} needs the value to show, e.g. \\work{bd.l}",
                ))
                return
            self.blocks.append(Work(arg, number, parse_format(match.group("fmt"))))
            return

        # A section command has no argument but its format, so its *first*
        # brace is the format -- `\answers{3}`, not `\answers{}{3}`. A
        # second brace has nothing left to mean.
        if match.group("fmt") is not None:
            self.diagnostics.append(Diagnostic(
                number, "unexpected_argument",
                f"\\{command} takes only a unit and figures -- write "
                f"\\{command}{{{arg}}}, not two braces",
            ))
        self.blocks.append(Section(command, number, parse_format(arg)))

    def _feed_graph(self, number: int, arg: str, ref: str | None) -> None:
        """`\\graph` takes one thing: the saved view to draw.

        It used to take an expression and a shape (`... over t`,
        `... by status`), and `[ref]` said which mote to evaluate that
        expression against. A saved view names its own motes, so there is
        nothing left for an anchor to anchor -- a `[ref]` here is a note
        written against the old grammar, and saying so is more use than
        ignoring it."""
        if ref is not None:
            self.diagnostics.append(Diagnostic(
                number, "graph_anchor",
                f"\\graph[{ref}]{{...}} has no meaning any more -- a saved view "
                f"names its own motes, so drop the [{ref}]",
            ))
        view = arg.strip()
        if not view:
            self.diagnostics.append(Diagnostic(
                number, "bad_graph",
                "\\graph{} needs the name of a saved plot (`plot save ‹name›`)",
            ))
        self.blocks.append(Graph(view, number))

    # -- accumulation --

    def _push(self, number: int, line: str) -> None:
        if not self._para:
            self._para_line = number
        self._para.append(line)

    def _flush(self) -> None:
        if not self._para:
            return
        content = self._inline(self._para_line, " ".join(self._para))
        self.blocks.append(Paragraph(content, self._para_line))
        self._para = []

    def _inline(self, number: int, text: str) -> list[Inline]:
        nodes, diagnostics = parse_inline(text, number)
        self.diagnostics.extend(diagnostics)
        return nodes


def _row_cells(text: str) -> list[str]:
    return text.rstrip().removesuffix("\\\\").split("&")


_CLOSERS = {
    "itemize": _Parser._close_list,
    "enumerate": _Parser._close_list,
    "equation": _Parser._close_equation,
    "tabular": _Parser._close_tabular,
    "figure": _Parser._close_figure,
}
ENVIRONMENTS = tuple(_CLOSERS)


def parse_inline(text: str, line: int = 1) -> tuple[list[Inline], list[Diagnostic]]:
    """One run of text into inline nodes. Exposed for tests and for
    anything that wants to read a single line without a whole Document."""
    nodes: list[Inline] = []
    diagnostics: list[Diagnostic] = []
    position = 0

    for match in _INLINE_RE.finditer(text):
        if match.start() > position:
            _append_text(nodes, diagnostics, text[position:match.start()], line)
        nodes.append(_inline_node(match, line))
        position = match.end()
    if position < len(text):
        _append_text(nodes, diagnostics, text[position:], line)
    return _merge_text(nodes), diagnostics


def _merge_text(nodes: list[Inline]) -> list[Inline]:
    """An escape splits a run of prose in three; the tree should not
    remember that. Renderers get one Text node per actual run."""
    merged: list[Inline] = []
    for node in nodes:
        if isinstance(node, Text) and merged and isinstance(merged[-1], Text):
            merged[-1] = Text(merged[-1].text + node.text)
        else:
            merged.append(node)
    return [n for n in merged if not (isinstance(n, Text) and not n.text)]


def _inline_node(match: re.Match, line: int) -> Inline:
    if match.group("esc") is not None:
        return Text(match.group("esc"))
    if match.group("math") is not None:
        return Math(match.group("math"), line)

    command, first, second = match.group("cmd"), match.group("first"), match.group("second")
    if command == "mote":
        return Link(first.strip(), second, line)
    if command == "ref":
        return Ref(first.strip(), line)
    if command == "val":
        return _value_node(match, line)
    return Styled({"textbf": "bold", "emph": "emph", "texttt": "code"}[command], first)


def _value_node(match: re.Match, line: int) -> Value:
    ref = match.group("ref")
    return Value(
        match.group("first").strip(), ref.strip() if ref else None, line,
        parse_format(match.group("second")),
    )


def _append_text(
    nodes: list[Inline], diagnostics: list[Diagnostic], text: str, line: int
) -> None:
    for stray in _STRAY_COMMAND_RE.finditer(text):
        name = stray.group(1)
        diagnostics.append(Diagnostic(line, "unknown_command", _stray_message(name)))
    nodes.append(Text(text))


def _stray_message(name: str) -> str:
    """Why a backslash-word is left over: an unknown command, a known one
    missing its argument, or -- the confusing case worth naming -- a block
    command used mid-sentence, where it cannot be seen at all.

    `\\rule{ΣFx}` inside a paragraph reads as "takes an argument" otherwise,
    which is the one thing it plainly does have."""
    if name in BLOCK_COMMANDS and name not in INLINE_COMMANDS:
        return (
            f"\\{name} is a block command -- put it on a line of its own. To "
            f"mention one inside a sentence, label it and use \\ref{{...}}"
        )
    if name in INLINE_COMMANDS:
        return f"\\{name} takes an argument, e.g. \\{name}{{...}}"
    return f"unknown command \\{name}"


# --- reading a document back -----------------------------------------

def links(document: Document) -> list[Link]:
    return [n for n in _inlines(document) if isinstance(n, Link)]


def values(document: Document) -> list[Value]:
    """Every `\\val` in the note, prose and maths alike."""
    found = []
    for node in _inlines(document):
        if isinstance(node, Value):
            found.append(node)
        elif isinstance(node, Math):
            found.extend(math_values(node.text, node.line))
    for block in document.blocks:
        if isinstance(block, Equation):
            found.extend(math_values(block.text, block.line))
    return found


def formulas(document: Document) -> list[tuple[str, int]]:
    """Every `$...$` and equation body, with its line: the TeX the read
    pane hands KaTeX and the export hands LaTeX."""
    found = [(n.text, n.line) for n in _inlines(document) if isinstance(n, Math)]
    found += [(b.text, b.line) for b in document.blocks if isinstance(b, Equation)]
    return sorted(found, key=lambda f: f[1])


def graphs(document: Document) -> list[Graph]:
    return [b for b in document.blocks if isinstance(b, Graph)]


def images(document: Document) -> list[Image]:
    return [b for b in document.blocks if isinstance(b, Image)]


def works(document: Document) -> list[Work]:
    return [b for b in document.blocks if isinstance(b, Work)]


def includes(document: Document) -> list[Include]:
    return [b for b in document.blocks if isinstance(b, Include)]


def cited(document: Document) -> list[Cited]:
    return [b for b in document.blocks if isinstance(b, Cited)]


def sections(document: Document) -> list[Section]:
    return [b for b in document.blocks if isinstance(b, Section)]


# Two macros that say the same thing resolve to the same thing, so
# resolution is keyed by what a macro *asks*, not by where it sits. That
# lets core/modoc_bind.py evaluate `\val{due}` once however often it
# appears, and lets a renderer look a node's answer up without the two
# sides having to agree on a traversal order.
def value_key(node: Value) -> str:
    return f"{node.ref or ''}\u241f{node.expression}\u241f{_format_key(node.format)}"


def graph_key(node: Graph) -> str:
    return node.view


def image_key(node: Image) -> str:
    return node.ref


def work_key(node: Work) -> str:
    return f"{node.target}\u241f{_format_key(node.format)}"


def section_key(node: Section) -> str:
    return f"{node.kind}\u241f{_format_key(node.format)}"


def _format_key(spec: Format) -> str:
    """The format is part of what a macro asks, not decoration on top of
    it: `\\val{bd.l}{in}` and `\\val{bd.l}{m}` want different answers, so
    they must not share a key."""
    return f"{spec.unit or ''}\u241f{spec.figures or ''}"


def _inlines(document: Document):
    for block in document.blocks:
        if isinstance(block, Table):
            for row in (block.header, *block.rows):
                for cell in row:
                    yield from cell
            yield from block.caption
        elif isinstance(block, (Graph, Image)):
            yield from block.caption
        elif isinstance(block, (Heading, Paragraph)):
            yield from block.content
        elif isinstance(block, ItemList):
            for item in block.items:
                yield from item


def word_count(document: Document) -> int:
    """Words as *rendered*, counted off the tree rather than off the
    source. Regexing the raw text was the obvious way and the wrong one:
    it either kept `\\textbf`'s braces as prose or threw the word inside
    them away with the markup. A `\\val` counts as the one token it
    prints; math and macro plumbing count as nothing."""
    total = 0
    for node in _inlines(document):
        if isinstance(node, (Text, Styled)):
            total += _words(node.text)
        elif isinstance(node, Link):
            total += _words(node.label or "") or 1
        elif isinstance(node, Value):
            total += 1
    return total


def _words(text: str) -> int:
    """Whitespace-separated runs holding at least one letter or digit --
    so stranded punctuation left behind by a macro ("." after
    `\\mote{...}`) isn't counted as a word of prose."""
    return len([w for w in text.split() if any(c.isalnum() for c in w)])


def plain_text(document: Document) -> str:
    """The note's prose, flattened to one searchable string -- what a
    reader sees, not what they typed. Backs Mora's notes search: a
    query matches a `\\textbf{plan}` or a `\\mote{id}{Atlas}`'s label the
    same way it matches ordinary text, and never matches a `\\val`
    expression or a graph spec, neither of which is prose. Block
    boundaries become newlines so a snippet built from an index into this
    string never straddles two unrelated blocks."""
    lines = []
    for block in document.blocks:
        if isinstance(block, (Heading, Paragraph)):
            lines.append(_inline_text(block.content))
        elif isinstance(block, ItemList):
            lines.extend(_inline_text(item) for item in block.items)
        elif isinstance(block, Equation):
            lines.append(block.text)
    return "\n".join(line for line in lines if line)


def _inline_text(nodes: list[Inline]) -> str:
    parts = []
    for node in nodes:
        if isinstance(node, (Text, Styled)):
            parts.append(node.text)
        elif isinstance(node, Link):
            parts.append(node.label or node.ref)
    return " ".join(p for p in parts if p).strip()


# --- math ------------------------------------------------------------

MATH_SYMBOLS = {
    "\\alpha": "\u03b1", "\\beta": "\u03b2", "\\gamma": "\u03b3", "\\delta": "\u03b4",
    "\\theta": "\u03b8", "\\lambda": "\u03bb", "\\mu": "\u03bc", "\\pi": "\u03c0",
    "\\rho": "\u03c1", "\\sigma": "\u03c3", "\\phi": "\u03c6", "\\omega": "\u03c9",
    "\\Delta": "\u0394", "\\Sigma": "\u03a3", "\\Omega": "\u03a9",
    "\\times": "\u00d7", "\\cdot": "\u00b7", "\\le": "\u2264", "\\ge": "\u2265",
    "\\ne": "\u2260", "\\approx": "\u2248", "\\pm": "\u00b1", "\\infty": "\u221e",
    "\\sum": "\u03a3", "\\int": "\u222b", "\\sqrt": "\u221a", "\\to": "\u2192",
    "\\partial": "\u2202",
}

# Whole control sequences only. Replacing these as plain substrings --
# which is what this did -- turns `\left(` into `≤ft(`, because `\le` is
# a prefix of it. The trailing lookahead is the fix, and it matters more
# now than it used to: KaTeX typesets `\left`, `\theta`, `\ne`-prefixed
# names and the rest happily, so a formula that renders correctly in
# Mora reaches this reducer as a matter of course.
_SYMBOL_RE = re.compile(
    "(?:" + "|".join(re.escape(name) for name in sorted(MATH_SYMBOLS, key=len, reverse=True))
    + r")(?![a-zA-Z])"
)

_SCRIPT_RE = re.compile(r"([\^_])\{([^{}]*)\}|([\^_])(\S)")


def math_values(text: str, line: int) -> list[Value]:
    """The `\\val`s in a `$...$` or equation body. Same grammar as in
    prose, so a format brace still means `{unit, figures}`."""
    return [_value_node(m, line) for m in _MATH_VALUE_RE.finditer(text)
            if m.group("first") is not None]


def math_value_spans(text: str) -> list[tuple[int, int]]:
    """Where each `\\val` sits in a maths body, so a reader of the rest of
    the TeX (core/latex_vocab.py) can step over them."""
    return [m.span() for m in _MATH_VALUE_RE.finditer(text) if m.group("first") is not None]


def fill_math(text: str, line: int, write: Callable[[Value], str]) -> str:
    """A maths body with each `\\val` replaced by what `write` makes of
    it. Everything else is left exactly as typed."""
    def one(match: re.Match) -> str:
        if match.group("first") is None:
            return match.group(0)
        return write(_value_node(match, line))
    return _MATH_VALUE_RE.sub(one, text)


@dataclass(frozen=True)
class MathSpan:
    """kind is "text", "sup" or "sub"."""
    kind: str
    text: str


def math_spans(text: str) -> list[MathSpan]:
    """`$...$` and equation bodies, reduced to what a terminal can show:
    Greek for the handful of names below, and one level of super/
    subscript. cli/render.py is the only caller.

    Mora renders the same source with KaTeX instead. That is not the two
    surfaces disagreeing about the note -- the note is the TeX, and this
    is a lossy view of it for a device with no glyphs for a fraction bar.
    A `\\frac` reads here as the literal `\\frac`, which is worse than a
    typeset one and better than a silent lie about the formula."""
    text = _SYMBOL_RE.sub(lambda m: MATH_SYMBOLS[m.group(0)], text)

    spans: list[MathSpan] = []
    position = 0
    for match in _SCRIPT_RE.finditer(text):
        if match.start() > position:
            spans.append(MathSpan("text", text[position:match.start()]))
        marker = match.group(1) or match.group(3)
        spans.append(MathSpan("sup" if marker == "^" else "sub", match.group(2) or match.group(4)))
        position = match.end()
    if position < len(text):
        spans.append(MathSpan("text", text[position:]))
    return spans


# --- the graph side --------------------------------------------------

def has_modoc(properties: dict) -> bool:
    """Whether a mote carries a note. Deliberately not a kind
    (core/kinds.py) -- see docs/architecture.md's Notes section."""
    return isinstance(properties.get(MODOC_KEY), str)


def read_source(conn: sqlite3.Connection, note_id: str) -> str:
    """A mote's note source, or "" if it has none. A missing mote and an
    unannotated one read the same on purpose: opening either in Mora
    should give you an empty page to type into, not an error."""
    from core.motes import get_mote

    mote = get_mote(conn, note_id)
    if mote is None:
        return ""
    source = mote.properties.get(MODOC_KEY)
    return source if isinstance(source, str) else ""


def write_source(conn: sqlite3.Connection, note_id: str, source: str) -> None:
    """Write the note back, touching `modoc` and nothing else."""
    from core.motes import get_mote, update_mote

    if get_mote(conn, note_id) is None:
        raise LookupError(f"No such mote: {note_id!r}")
    update_mote(conn, note_id, {MODOC_KEY: source})


def export_file(conn: sqlite3.Connection, note_id: str, path: Path) -> Path:
    """Write a note out as a `.modoc` file -- the property's string and
    nothing else.

    No header naming the garden it came from, which was the first design
    and the wrong one: it would have made export/import stop round-
    tripping, and given the format a line that means something to exactly
    one program. A `.modoc` file is a note's source. Where a note belongs
    is a fact about the graph, and the graph is where it stays.

    Unlike read_source(), this refuses a mote that does not exist. An
    empty page is the right answer to "open this note"; an empty file is
    not the right answer to "export it"."""
    from core.motes import get_mote

    if get_mote(conn, note_id) is None:
        raise LookupError(f"No such mote: {note_id!r}")
    path.write_text(read_source(conn, note_id), encoding="utf-8")
    return path


def import_file(
    conn: sqlite3.Connection, path: Path, note_id: str | None = None
) -> str:
    """Read a `.modoc` file onto a mote, and return that mote's id. With
    no note_id, a new mote is created for it, named after the file --
    importing a note you were handed should not first require deciding
    what it is about."""
    from core.motes import create_mote

    source = path.read_text(encoding="utf-8")
    if note_id is None:
        return create_mote(conn, {"name": path.stem, MODOC_KEY: source})
    write_source(conn, note_id, source)
    return note_id


def anchor(conn: sqlite3.Connection, note_id: str) -> tuple[str, list[Diagnostic]]:
    """Which mote `\\val{key}` reads from: the note's own mote, unless
    exactly one `about` relationship points out of it.

    More than one is not an error worth refusing to render over -- it is
    a note that has not decided what it is about -- so it falls back to
    the note itself and says so."""
    about = [
        r for r in list_relationships(conn)
        if r.from_id == note_id and r.type == ABOUT
    ]
    if len(about) == 1:
        return about[0].to_id, []
    if not about:
        return note_id, []
    return note_id, [Diagnostic(
        0, "ambiguous_anchor",
        f"{len(about)} `about` relationships point out of this note; "
        f"values are read from the note's own mote instead",
    )]
