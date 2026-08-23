# core/modoc.py
"""The .modoc note format: a small LaTeX subset plus three MonOSS macros.

A note is a string in the reserved `modoc` property of an ordinary mote
(docs/architecture.md's Notes section). It is not a separate object type,
and `.modoc` files are an import/export shape -- the property is the
canonical copy, which is what puts notes inside mote_history, garden
export and undo for free.

This module is the *language* and nothing else: parsing is pure, takes no
connection, and resolves nothing. What `\\mote{...}` points at and what
`\\val{...}` evaluates to are core/modoc_bind.py's business, because
those are questions about a garden rather than about a note. Keeping them
apart is what lets the whole grammar be tested with strings.

Two rules the grammar leans on, both deliberate:

* A macro argument cannot contain a brace. `\\val{count(targets('x'))}`
  is fine; a macro nested inside a macro is not. The alternative is a
  bracket-matching parser for a format whose entire purpose is to be
  skimmable in a plain textarea.
* Nothing here raises. A note with a typo in it must still render, with
  the broken part visibly broken -- so every failure becomes a
  Diagnostic and parsing carries on.
"""
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from core.relationships import list_relationships

MODOC_KEY = "modoc"
ABOUT = "about"

# Inline macros. `graph` is deliberately absent: it produces a figure, and
# a figure inside a sentence is a different feature from a figure between
# two paragraphs. It is recognized at block level only.
INLINE_COMMANDS = ("mote", "val", "textbf", "emph", "texttt")
BLOCK_COMMANDS = ("section", "subsection", "graph", "item", "begin", "end")

_ESCAPABLE = "\\{}%$"

_INLINE_RE = re.compile(
    r"\\(?P<esc>[\\{}%$])"
    r"|\\(?P<cmd>" + "|".join(INLINE_COMMANDS) + r")"
    r"(?:\[(?P<ref>[^\[\]]*)\])?"
    r"\{(?P<first>[^{}]*)\}"
    r"(?:\{(?P<second>[^{}]*)\})?"
    r"|\$(?P<math>[^$]*)\$"
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
    """`$...$`. The raw source; see math_spans() for the shared reading
    of it, so the CLI and Mora never disagree about what an alpha is."""
    text: str


@dataclass(frozen=True)
class Link:
    """`\\mote{ref}` or `\\mote{ref}{label}`. `ref` is an id or a name;
    which one is core/modoc_bind.py's problem."""
    ref: str
    label: str | None
    line: int


@dataclass(frozen=True)
class Value:
    """`\\val{expr}`, or `\\val[ref]{expr}` to read it off another mote."""
    expression: str
    ref: str | None
    line: int


Inline = Text | Styled | Math | Link | Value


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
    items: list[list[Inline]]
    line: int


@dataclass(frozen=True)
class Equation:
    text: str
    line: int


@dataclass(frozen=True)
class GraphSpec:
    """Either form of `\\graph`. `OVER_T` plots `expression` once per
    simulation step; `BY` counts the motes `expression` selects, grouped
    by each one's `key` property."""
    kind: str
    expression: str
    key: str | None = None


OVER_T = "over_t"
BY = "by"

_OVER_T_RE = re.compile(r"^(?P<expr>.+?)\s+over\s+t$", re.IGNORECASE)
_BY_RE = re.compile(r"^(?P<expr>.+?)\s+by\s+(?P<key>[A-Za-z_][A-Za-z0-9_]*)$", re.IGNORECASE)


@dataclass(frozen=True)
class Graph:
    spec: GraphSpec | None
    ref: str | None
    line: int


Block = Heading | Paragraph | ItemList | Equation | Graph


@dataclass(frozen=True)
class Document:
    blocks: list[Block] = field(default_factory=list)
    diagnostics: list[Diagnostic] = field(default_factory=list)


# --- parsing ---------------------------------------------------------

def parse(source: str) -> Document:
    """Source text to a Document. Pure: no connection, no resolution, no
    exceptions."""
    parser = _Parser()
    for number, raw in enumerate(source.splitlines(), start=1):
        parser.feed(number, strip_comment(raw).strip())
    return parser.finish()


def strip_comment(line: str) -> str:
    """Everything before an unescaped `%`."""
    match = _COMMENT_RE.search(line)
    return line if match is None else line[: match.start()]


class _Parser:
    """A line-at-a-time block parser. A class rather than a closure over
    six locals because `itemize` and `equation` are open/close states,
    and threading those through a fold reads worse than holding them."""

    def __init__(self) -> None:
        self.blocks: list[Block] = []
        self.diagnostics: list[Diagnostic] = []
        self._para: list[str] = []
        self._para_line = 0
        self._items: list[str] | None = None
        self._equation: list[str] | None = None
        self._open_line = 0

    def feed(self, number: int, line: str) -> None:
        if self._equation is not None:
            self._feed_equation(number, line)
        elif self._items is not None:
            self._feed_item(number, line)
        elif not line:
            self._flush()
        elif line.startswith("\\"):
            self._feed_command(number, line)
        else:
            self._push(number, line)

    def finish(self) -> Document:
        for state, name in ((self._items, "itemize"), (self._equation, "equation")):
            if state is not None:
                self.diagnostics.append(Diagnostic(
                    self._open_line, "unterminated",
                    f"\\begin{{{name}}} was never closed with \\end{{{name}}}",
                ))
        self._close_itemize(self._open_line)
        self._close_equation(self._open_line)
        self._flush()
        return Document(self.blocks, self.diagnostics)

    # -- states --

    def _feed_equation(self, number: int, line: str) -> None:
        if line == "\\end{equation}":
            self._close_equation(number)
        else:
            self._equation.append(line)

    def _feed_item(self, number: int, line: str) -> None:
        if line == "\\end{itemize}":
            self._close_itemize(number)
        elif line.startswith("\\item"):
            self._items.append(line[len("\\item"):].strip())
        elif line and self._items:
            self._items[-1] += " " + line
        elif line:
            self.diagnostics.append(Diagnostic(
                number, "stray_text", "text inside itemize before the first \\item",
            ))

    def _feed_command(self, number: int, line: str) -> None:
        if line == "\\begin{itemize}":
            self._flush()
            self._items, self._open_line = [], number
            return
        if line == "\\begin{equation}":
            self._flush()
            self._equation, self._open_line = [], number
            return
        environment = _ENVIRONMENT_RE.match(line)
        if environment is not None:
            self.diagnostics.append(Diagnostic(
                number, "stray_environment",
                f"\\{environment.group(1)}{{{environment.group(2)}}} outside any "
                f"itemize or equation block",
            ))
            return
        match = _BLOCK_RE.match(line)
        if match is None or match.group("cmd") not in ("section", "subsection", "graph"):
            self._push(number, line)
            return
        self._flush()
        command, arg = match.group("cmd"), match.group("arg")
        if command == "graph":
            self.blocks.append(Graph(self._graph_spec(number, arg), match.group("ref"), number))
        else:
            level = 1 if command == "section" else 2
            self.blocks.append(Heading(level, self._inline(number, arg), number))

    def _graph_spec(self, number: int, arg: str) -> GraphSpec | None:
        arg = arg.strip()
        over = _OVER_T_RE.match(arg)
        if over is not None:
            return GraphSpec(OVER_T, over.group("expr").strip())
        grouped = _BY_RE.match(arg)
        if grouped is not None:
            return GraphSpec(BY, grouped.group("expr").strip(), grouped.group("key"))
        self.diagnostics.append(Diagnostic(
            number, "bad_graph",
            f"\\graph{{{arg}}} is neither `<expression> over t` nor `<selector> by <property>`",
        ))
        return None

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

    def _close_itemize(self, number: int) -> None:
        if self._items is None:
            return
        line = self._open_line or number
        items = [self._inline(line, item) for item in self._items]
        if items:
            self.blocks.append(ItemList(items, line))
        self._items = None

    def _close_equation(self, number: int) -> None:
        if self._equation is None:
            return
        self.blocks.append(Equation(" ".join(self._equation).strip(), self._open_line or number))
        self._equation = None

    def _inline(self, number: int, text: str) -> list[Inline]:
        nodes, diagnostics = parse_inline(text, number)
        self.diagnostics.extend(diagnostics)
        return nodes


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
        return Math(match.group("math"))

    command, first, second = match.group("cmd"), match.group("first"), match.group("second")
    if command == "mote":
        return Link(first.strip(), second, line)
    if command == "val":
        ref = match.group("ref")
        return Value(first.strip(), ref.strip() if ref else None, line)
    return Styled({"textbf": "bold", "emph": "emph", "texttt": "code"}[command], first)


def _append_text(
    nodes: list[Inline], diagnostics: list[Diagnostic], text: str, line: int
) -> None:
    for stray in _STRAY_COMMAND_RE.finditer(text):
        name = stray.group(1)
        known = name in INLINE_COMMANDS or name in BLOCK_COMMANDS
        diagnostics.append(Diagnostic(
            line, "unknown_command",
            f"\\{name} takes an argument, e.g. \\{name}{{...}}" if known
            else f"unknown command \\{name}",
        ))
    nodes.append(Text(text))


# --- reading a document back -----------------------------------------

def links(document: Document) -> list[Link]:
    return [n for n in _inlines(document) if isinstance(n, Link)]


def values(document: Document) -> list[Value]:
    return [n for n in _inlines(document) if isinstance(n, Value)]


def graphs(document: Document) -> list[Graph]:
    return [b for b in document.blocks if isinstance(b, Graph)]


# Two macros that say the same thing resolve to the same thing, so
# resolution is keyed by what a macro *asks*, not by where it sits. That
# lets core/modoc_bind.py evaluate `\val{due}` once however often it
# appears, and lets a renderer look a node's answer up without the two
# sides having to agree on a traversal order.
def value_key(node: Value) -> str:
    return f"{node.ref or ''}\u241f{node.expression}"


def graph_key(node: Graph) -> str:
    spec = node.spec
    if spec is None:
        return f"{node.ref or ''}\u241f?{node.line}"
    return f"{node.ref or ''}\u241f{spec.kind}\u241f{spec.expression}\u241f{spec.key or ''}"


def _inlines(document: Document):
    for block in document.blocks:
        if isinstance(block, (Heading, Paragraph)):
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
            total += len(node.text.split())
        elif isinstance(node, Link):
            total += len((node.label or "").split()) or 1
        elif isinstance(node, Value):
            total += 1
    return total


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

_SCRIPT_RE = re.compile(r"([\^_])\{([^{}]*)\}|([\^_])(\S)")


@dataclass(frozen=True)
class MathSpan:
    """kind is "text", "sup" or "sub"."""
    kind: str
    text: str


def math_spans(text: str) -> list[MathSpan]:
    """`$...$` and equation bodies, read once here so every surface
    renders the same alpha and the same superscript."""
    for name, symbol in MATH_SYMBOLS.items():
        text = text.replace(name, symbol)

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
    """The `note` role (core/roles.py): a mote carrying a note."""
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
    is a fact about the graph, and the graph is where it stays."""
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
