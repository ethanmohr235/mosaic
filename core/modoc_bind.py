# core/modoc_bind.py
"""Resolving a parsed .modoc against a garden.

core/modoc.py knows what a note *says*; this knows what it *means* right
now -- which mote a `\\mote{...}` points at, what a `\\val{...}` comes to,
which saved view a `\\graph{...}` draws. The split is the reason the
grammar can be tested with strings and this can be tested with a garden.

A `\\graph` is the one macro this module hands straight on: it names a
view saved in Moxie, and re-solving that view is core/plotview.py's job
rather than a second, weaker plotter living here. That is the whole
point of the macro -- there is one place a figure is specified (Moxie),
one place it is computed (core/sweep.py), and a note only ever refers to
one.

Two things it deliberately does not do. It never writes, and it never
runs the clock: a note says what the garden *is*, not what it will be.
That boundary is what keeps a note honest -- `sim` is where a garden
moves through time, `plot` is where a property is swept, and a note
reads the state those produce. And it never raises: every failure
lands in the returned diagnostics, because a note with one bad
expression in it must still render the other nineteen.
"""
import dataclasses
import re
import sqlite3
from dataclasses import dataclass, field

from core import calc
from core import derivations
from core import images as images_repo
from core.calc import format_value  # noqa: F401 -- re-exported
from core import modoc
from core import kinds as kinds_repo
from core import plotview
from core import rules as rules_repo
from core import uncertainty
from core.entities import Mote, Relationship
from core.formula import Context, FormulaError, evaluate_expression
from core.motes import list_motes
from core.relationships import list_relationships
from core.simulation import expression_context, garden_freedoms
from core.units import UNITS_KEY, format_unit, parse_units_property

NAME_KEY = "name"

_PLAIN_PROPERTY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# `\val{n.E}`: a bare "mote.property" reference, one dot, no arithmetic.
# Caught before the formula evaluator sees it for the same reason the
# plain-property fast path exists -- core/formula.py normalizes every
# property it reads to its dimension's base SI unit (so the solver can
# combine values from different motes), which is exactly wrong for
# display: `n.E` declared in eV must read back as eV, not as its tiny
# value in joules.
_DOTTED_REF_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)$"
)

@dataclass(frozen=True)
class LinkTarget:
    """What a `\\mote{ref}` came to. `mote_id` is None when nothing
    matched, which the renderers show as a broken link rather than
    silently dropping."""
    ref: str
    mote_id: str | None
    name: str
    kinds: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass(frozen=True)
class ValueResult:
    """`text` is always safe to print -- an error message renders in its
    own colour rather than leaving a hole in the sentence."""
    text: str
    value: object = None
    error: str | None = None


@dataclass(frozen=True)
class CitedRule:
    """What a `\\rule{...}` came to: the rule's equation, read from the
    graph rather than copied into the note.

    That is the whole point of citing one. A document showing what a rule
    says *now* cannot drift from it the way a retyped equation does --
    which is exactly the drift `FEA-Homework-4`'s rule names had already
    suffered (core/rules.py::label_of)."""
    ref: str
    mote_id: str | None = None
    name: str = ""
    equation: str = ""
    error: str | None = None


@dataclass(frozen=True)
class SectionResult:
    """What one of the four calc sections came to: its rows (or, for
    `\\checks`, its constraint results) and a heading for it.

    Rows come through as `core/calc.py` shapes rather than as text, so a
    renderer can draw a real table and the terminal can draw columns --
    formatting a table into a string here would leave both with the same
    string and neither able to align it."""
    kind: str
    heading: str
    rows: tuple = ()
    checks: tuple = ()
    # `\verification`: `core/calc.py::Comparison`, hand beside external.
    comparisons: tuple = ()
    # The section's `{unit, figures}`, already applied to each row's unit
    # and carried here for the figures a renderer passes to `row.text()`.
    # Converting once here rather than in each renderer is what keeps the
    # terminal and the window quoting a table to the same precision.
    figures: int | None = None
    error: str | None = None

    @property
    def empty(self) -> bool:
        return not self.rows and not self.checks and not self.comparisons


@dataclass(frozen=True)
class Resolution:
    anchor_id: str
    links: dict[str, LinkTarget] = field(default_factory=dict)
    values: dict[str, ValueResult] = field(default_factory=dict)
    graphs: dict[str, plotview.Figure] = field(default_factory=dict)
    images: dict[str, images_repo.Picture | str] = field(default_factory=dict)
    works: dict[str, calc.Work] = field(default_factory=dict)
    sections: dict[str, SectionResult] = field(default_factory=dict)
    rules: dict[str, CitedRule] = field(default_factory=dict)
    diagnostics: list[modoc.Diagnostic] = field(default_factory=list)


class _Garden:
    """The garden read once, as every macro in the note will see it.

    One read rather than one per macro: a note with forty `\\val`s in it
    asks about the same forty motes, and nothing between the first and
    the last can have changed it. This was a `_Timeline` of one
    nondestructive simulation preview per step; there are no steps any
    more, so it is what it always was underneath -- properties,
    relationships, and a Context built from them."""

    def __init__(self, conn: sqlite3.Connection, motes: list[Mote]) -> None:
        self.relationships: list[Relationship] = list_relationships(conn)
        self.properties: dict[str, dict] = {m.id: dict(m.properties) for m in motes}
        self._conn = conn
        self._freedoms = None
        self._spreads = None

    def sigma(self, mote_id: str, key: str) -> float | None:
        """A value's ±: a given's own, or a result's as propagated. The
        propagation is two solves per uncertain given, so it is run at most
        once per note, and only when a `\val` asks about a result in a
        garden where some given carries a ±."""
        if self._freedoms is None:
            self._freedoms = garden_freedoms(self._conn)
        if key not in self._freedoms.get(mote_id, set()):
            return uncertainty.sigma_of(self.at(mote_id), key)
        if self._spreads is None:
            self._spreads = (
                uncertainty.propagate(self._conn)
                if uncertainty.has_uncertainty(self._conn) else uncertainty.Report()
            )
        found = self._spreads.of(mote_id, key)
        return None if found is None else found.sigma

    def at(self, mote_id: str) -> dict:
        return self.properties.get(mote_id, {})

    def context(self, mote_id: str) -> Context:
        return expression_context(self.properties, self.relationships, mote_id)


def assemble(
    conn: sqlite3.Connection, note_id: str, depth: int = 0, seen: tuple = (),
    source: str | None = None,
) -> modoc.Document:
    """A note's document with every `\\include` spliced in, numbered over the
    whole of it.

    `\\include` is the one macro that changes the *document* rather than
    resolving to something inside it, so it cannot be grammar: it needs the
    garden to find the note, and the numbering has to count over the
    assembled result -- an equation in an included section is "Equation 3"
    of the document, not "Equation 1" of its own file.

    A cycle is refused with a diagnostic rather than followed: a note that
    includes itself is a mistake, and recursing until Python's stack gives
    out is a worse way to say so. So is depth: a chain long enough to hit
    the limit is a structure nobody meant.

    `source` is the note's text when it isn't the stored one -- an editor's
    unsaved draft, which a read pane shows as it is typed."""
    if source is None:
        source = modoc.read_source(conn, note_id)
    document = modoc.parse(source)
    if not modoc.includes(document):
        return document
    return modoc.numbered(modoc.Document(
        _spliced(conn, document, note_id, depth, seen),
        list(document.diagnostics),
    ))


MAX_INCLUDE_DEPTH = 8


def _spliced(
    conn: sqlite3.Connection,
    document: modoc.Document,
    note_id: str,
    depth: int,
    seen: tuple,
) -> list[modoc.Block]:
    blocks: list[modoc.Block] = []
    chain = (*seen, note_id)
    for block in document.blocks:
        if not isinstance(block, modoc.Include):
            blocks.append(block)
            continue
        included, problem = _included(conn, block, chain, depth)
        if problem is not None:
            document.diagnostics.append(
                modoc.Diagnostic(block.line, "bad_include", problem))
            continue
        blocks.extend(included.blocks)
        document.diagnostics.extend(included.diagnostics)
    return blocks


def _included(
    conn: sqlite3.Connection, block: modoc.Include, chain: tuple, depth: int
) -> tuple[modoc.Document | None, str | None]:
    if depth >= MAX_INCLUDE_DEPTH:
        return None, (
            f"\\include{{{block.ref}}} is more than {MAX_INCLUDE_DEPTH} notes deep "
            f"-- a chain that long is a structure nobody meant"
        )
    mote, error = _find(block.ref, list_motes(conn))
    if mote is None:
        return None, f"\\include{{{block.ref}}}: {error}"
    if mote.id in chain:
        return None, (
            f"\\include{{{block.ref}}} would include a note that is already "
            f"including this one"
        )
    if not modoc.has_modoc(mote.properties):
        return None, f"\\include{{{block.ref}}}: that mote carries no note"
    return assemble(conn, mote.id, depth + 1, chain), None


def resolve(
    conn: sqlite3.Connection,
    note_id: str,
    document: modoc.Document,
    draw=None,
) -> Resolution:
    """Everything the note's macros come to, against the garden as it
    stands. There is no other thing they could come to: a note has one
    reading, which is why nothing here takes a step.

    `draw` stands in for core/plotview.py::figure, and exists for exactly
    one caller: Mora re-resolves the whole note on every pause in typing,
    and a sweep is hundreds of solves. A window that knows the garden has
    not moved since the last keystroke can hand in a memo of its own
    (mora/api.py::_Figures) rather than this module inventing a cache it
    has no way to invalidate -- resolve() sees one connection for one
    call and cannot know what happened between two of them."""
    anchor_id, diagnostics = modoc.anchor(conn, note_id)
    motes = list_motes(conn)
    garden = _Garden(conn, motes)

    links = {
        node.ref: _link(node.ref, motes, garden.relationships)
        for node in modoc.links(document)
    }
    values = {
        modoc.value_key(node): _value(node, anchor_id, motes, garden)
        for node in modoc.values(document)
    }
    # Keyed by the view each one names, so a note that shows the same
    # figure twice re-solves it once -- and a sweep is the most expensive
    # thing a note can ask for by some margin.
    drawing = draw or plotview.figure
    graphs = {
        key: drawing(conn, key, motes=motes)
        for key in dict.fromkeys(modoc.graph_key(n) for n in modoc.graphs(document))
    }
    pictures = {
        modoc.image_key(node): _picture(node.ref, motes)
        for node in modoc.images(document)
    }
    # One live solve answers every `\work` in the note, however many there are.
    explainer = derivations.Explainer(conn)
    works = {
        modoc.work_key(node): _work(conn, node, anchor_id, motes, explainer)
        for node in modoc.works(document)
    }
    rules = {
        node.rule: _cited(node.rule, motes, garden.relationships)
        for node in modoc.cited(document)
    }
    # One read of the garden per *kind* of section, however many times the
    # note asks for one -- the same reason `graphs` is keyed by its view.
    sections = {
        modoc.section_key(node): _section(conn, node)
        for node in modoc.sections(document)
    }
    return Resolution(
        anchor_id, links, values, graphs, pictures, works, sections, rules,
        diagnostics,
    )


# --- the calc sections -----------------------------------------------

SECTION_HEADINGS = {
    "given": "Given",
    "answers": "Answers",
    "assumptions": "Assumptions",
    "checks": "Checks",
    "verification": "Verification",
}


def _work(
    conn: sqlite3.Connection, node: modoc.Work, anchor_id: str, motes: list[Mote],
    explainer: derivations.Explainer,
) -> calc.Work:
    """`\\work{bd.l}` or `\\work{l}` -- the second read off the note's
    anchor, the same default `\\val` takes."""
    mote_id, key, error = _work_target(node.target, anchor_id, motes)
    if mote_id is None:
        return calc.Work(node.target, calc.Quantity(), error=error)
    return calc.work(conn, mote_id, key, node.format.unit, node.format.figures, explainer)


def _work_target(
    target: str, anchor_id: str, motes: list[Mote]
) -> tuple[str | None, str, str | None]:
    dotted = _DOTTED_REF_RE.match(target)
    if dotted:
        ref, key = dotted.groups()
        mote, error = _find(ref, motes)
        return (mote.id if mote else None), key, error
    if _PLAIN_PROPERTY_RE.match(target):
        return anchor_id, target, None
    return None, target, (
        f"{target!r} is not a value to show -- write \\work{{bd.l}} or, for the "
        f"note's own mote, \\work{{l}}"
    )


_SECTION_READERS = {
    "given": calc.givens,
    "answers": calc.answers,
    "assumptions": calc.assumptions,
}


def _cited(ref: str, motes: list[Mote], relationships: list[Relationship]) -> CitedRule:
    """The rule a `\\rule{...}` names, and the equation it currently says.

    A mote is only citable as a rule if it *is* one -- an `equation` plus an
    `applies_to` (core/rules.py). A mote with an equation and no
    `applies_to` is a half-built rule the solver skips, and quoting it in a
    document as though it governed something would be the document saying
    more than the garden does."""
    mote, error = _find(ref, motes)
    if mote is None:
        # A rule a worksheet left unnamed has no `name` property at all --
        # its label *is* its equation (core/rules.py::label_of). So cite it
        # by what it is called, which for that rule is the equation itself.
        mote = _rule_by_label(ref, motes, relationships)
        if mote is None:
            return CitedRule(ref, error=error)
    equation = mote.properties.get(rules_repo.EQUATION_KEY)
    if not rules_repo.has_equation(mote.properties):
        return CitedRule(ref, mote.id, _name(mote), error="that mote carries no equation")
    if mote.id not in rules_repo.rule_ids_in(relationships):
        return CitedRule(
            ref, mote.id, _name(mote), str(equation),
            error=(
                "that mote has an equation but no applies_to, so it governs "
                "nothing and the solver skips it"
            ),
        )
    return CitedRule(
        ref, mote.id, rules_repo.label_of(mote.properties, mote.id), str(equation)
    )


def _rule_by_label(
    ref: str, motes: list[Mote], relationships: list[Relationship]
) -> Mote | None:
    """A rule whose label matches, which is only ever reached for one that
    has no `name`. Exactly one match, on the same terms `_find` insists on:
    a citation is meant to pick one specific rule."""
    rule_ids = rules_repo.rule_ids_in(relationships)
    matches = [
        mote for mote in motes
        if mote.id in rule_ids
        and rules_repo.label_of(mote.properties, mote.id) == ref.strip()
    ]
    return matches[0] if len(matches) == 1 else None


def _section(conn: sqlite3.Connection, node: modoc.Section) -> SectionResult:
    heading = SECTION_HEADINGS.get(node.kind, node.kind)
    if node.kind == "checks":
        # A check is a relation, not a quantity, so a unit and a figure
        # count have nothing to apply to. Reported rather than ignored.
        error = None if node.format.empty else (
            "a check is a relation, not a quantity -- \\checks takes no unit or figures"
        )
        return SectionResult(node.kind, heading, checks=calc.checks(conn), error=error)

    if node.kind == "verification":
        # The hand value's unit is the table's, since each row compares
        # two values of one quantity; a format's unit converts both, and
        # its figures quote them.
        comparisons = tuple(
            dataclasses.replace(
                c,
                hand=c.hand.converted(node.format.unit),
                external=c.external.converted(node.format.unit),
            )
            for c in calc.verification(conn)
        )
        return SectionResult(node.kind, heading, comparisons=comparisons,
                             figures=node.format.figures)

    reader = _SECTION_READERS.get(node.kind)
    if reader is None:
        return SectionResult(node.kind, heading, error=f"no {node.kind!r} section")
    rows = tuple(
        dataclasses.replace(row, quantity=row.quantity.converted(node.format.unit))
        for row in reader(conn)
    )
    return SectionResult(node.kind, heading, rows=rows, figures=node.format.figures)


def problems(document: modoc.Document, resolution: Resolution) -> list[modoc.Diagnostic]:
    """Everything wrong with a note, in one list: what wouldn't parse and
    what wouldn't resolve.

    Here rather than in each surface because it is one question with one
    answer. It was briefly the CLI's own -- assembled inside `modoc
    check` -- and Mora promptly reported a note with a dangling
    `\\mote{...}` in it as having no problems at all, which is exactly
    the drift core/kinds.py exists to prevent for a different question.
    Line numbers come off the nodes, not off the resolution, so a
    reference that fails twice is reported at both places it failed."""
    found = list(document.diagnostics) + list(resolution.diagnostics)
    for node in modoc.links(document):
        target = resolution.links.get(node.ref)
        if target is not None and target.error:
            found.append(modoc.Diagnostic(
                node.line, "unresolved_link", f"\\mote{{{node.ref}}}: {target.error}"
            ))
    for node in modoc.values(document):
        result = resolution.values.get(modoc.value_key(node))
        if result is not None and result.error:
            found.append(modoc.Diagnostic(
                node.line, "unresolved_value", f"\\val{{{node.expression}}}: {result.error}"
            ))
    for node in modoc.graphs(document):
        drawn = resolution.graphs.get(modoc.graph_key(node))
        if drawn is not None and drawn.error:
            found.append(modoc.Diagnostic(
                node.line, "unresolved_graph", f"\\graph{{{node.view}}}: {drawn.error}"
            ))
    for node in modoc.images(document):
        found_picture = resolution.images.get(modoc.image_key(node))
        if isinstance(found_picture, str):
            found.append(modoc.Diagnostic(
                node.line, "unresolved_image", f"\\image{{{node.ref}}}: {found_picture}"
            ))
    for node in modoc.works(document):
        shown = resolution.works.get(modoc.work_key(node))
        if shown is not None and shown.error:
            found.append(modoc.Diagnostic(
                node.line, "unresolved_work", f"\\work{{{node.target}}}: {shown.error}"
            ))
    for node in modoc.cited(document):
        rule = resolution.rules.get(node.rule)
        if rule is not None and rule.error:
            found.append(modoc.Diagnostic(
                node.line, "unresolved_rule", f"\\rule{{{node.rule}}}: {rule.error}"
            ))
    for node in modoc.sections(document):
        section = resolution.sections.get(modoc.section_key(node))
        if section is not None and section.error:
            found.append(modoc.Diagnostic(
                node.line, "unresolved_section", f"\\{node.kind}: {section.error}"
            ))
    return sorted(found, key=lambda d: d.line)


# --- references ------------------------------------------------------

def _link(ref: str, motes: list[Mote], relationships: list[Relationship]) -> LinkTarget:
    mote, error = _find(ref, motes)
    if mote is None:
        return LinkTarget(ref, None, ref, [], error)
    return LinkTarget(ref, mote.id, _name(mote), kinds_repo.kinds_of(mote, relationships))


def _picture(ref: str, motes: list[Mote]) -> "images_repo.Picture | str":
    """The stored image a `\\image` names, or the reason it cannot be
    found -- a string, so one dangling figure is a message in the note and
    not an exception."""
    mote, error = images_repo.find(ref, motes)
    return error if mote is None else images_repo.picture(mote)


def _find(ref: str, motes: list[Mote]) -> tuple[Mote | None, str | None]:
    """An exact id, else a mote whose `name` matches uniquely.

    Ids win outright: the `@` picker in Mora inserts them precisely so a
    rename can't break a link, and a hand-typed name should never shadow
    one. Name matching tries exact first and only then case-insensitively,
    so "atlas" finding "Atlas" is a convenience rather than a way for two
    differently-cased motes to become ambiguous with each other."""
    ref = ref.strip()
    if not ref:
        return None, "empty reference"
    for mote in motes:
        if mote.id == ref:
            return mote, None

    exact = [m for m in motes if m.properties.get(NAME_KEY) == ref]
    matches = exact or [
        m for m in motes
        if isinstance(m.properties.get(NAME_KEY), str)
        and m.properties[NAME_KEY].casefold() == ref.casefold()
    ]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, f"{len(matches)} motes are named {ref!r} -- link by id instead"
    return None, f"no mote with id or name {ref!r}"


def _name(mote: Mote) -> str:
    name = mote.properties.get(NAME_KEY)
    return name if isinstance(name, str) and name else mote.id[:8]


def _anchor_for(
    ref: str | None, anchor_id: str, motes: list[Mote]
) -> tuple[str | None, str | None]:
    """Which mote a macro reads from -- the note's anchor, or the `[ref]`
    it named instead."""
    if ref is None:
        return anchor_id, None
    mote, error = _find(ref, motes)
    return (mote.id if mote else None), error


# --- values ----------------------------------------------------------

def _value(
    node: modoc.Value, anchor_id: str, motes: list[Mote], garden: _Garden
) -> ValueResult:
    mote_id, error = _anchor_for(node.ref, anchor_id, motes)
    if mote_id is None:
        return ValueResult(f"?{node.ref}", None, error)

    properties = garden.at(mote_id)
    if _PLAIN_PROPERTY_RE.match(node.expression) and node.expression in properties:
        # The fast path, and the reason \val{due} can be a date at all:
        # core/formula.py is numeric by construction, so a plain property
        # read has to happen before the expression evaluator sees it.
        return _property_result(properties, node.expression, node.format,
                                garden.sigma(mote_id, node.expression))

    dotted = _DOTTED_REF_RE.match(node.expression)
    if dotted:
        other_ref, other_key = dotted.groups()
        other, error = _find(other_ref, motes)
        if other is not None:
            other_properties = garden.at(other.id)
            if other_key in other_properties:
                return _property_result(other_properties, other_key, node.format,
                                        garden.sigma(other.id, other_key))
        # No mote named `other_ref` (or it doesn't carry `other_key`) --
        # fall through to the formula evaluator, which also resolves
        # `role.property` and a name-matched relationship neighbor.

    try:
        number = evaluate_expression(node.expression, garden.context(mote_id)).value()
    except (FormulaError, ZeroDivisionError) as e:
        return ValueResult(f"?{node.expression}", None, str(e))
    # An evaluated expression is in base SI (core/formula.py normalizes
    # every read), so a unit asked for here is the unit to *present* it in
    # and there is nothing to convert from. Significant figures still
    # apply, which is the half of a format an expression can honour.
    return ValueResult(
        calc.Quantity(number, node.format.unit).text(node.format.figures), number
    )


def _property_result(
    properties: dict, key: str, spec: modoc.Format | None = None,
    sigma: float | None = None,
) -> ValueResult:
    """A plain property read off some mote's properties, written in its own
    declared unit. Shared by the anchor-property fast path and the
    cross-mote `mote.property` fast path -- both read a value straight off
    a mote without going through the formula evaluator, so both display it
    in the unit it is recorded in rather than the solver's base SI.

    A `{unit, figures}` format converts it from there, which is the whole
    point of asking for one: the *stored* unit is the garden's, and what a
    document shows is the reader's (docs/simulation.md, *Display units
    versus stored units*)."""
    spec = spec or modoc.Format()
    raw = properties[key]
    unit_name = parse_units_property(properties.get(UNITS_KEY)).get(key)
    # "1" means truly unitless (core/units.py: DIMENSIONLESS with scale 1),
    # not a unit with the symbol "1" -- nothing to show after the number.
    quantity = calc.Quantity(raw, unit_name if unit_name != "1" else None, sigma)
    converted = quantity.converted(spec.unit)
    return ValueResult(converted.text(spec.figures), raw)
