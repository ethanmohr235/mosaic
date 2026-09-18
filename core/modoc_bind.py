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
That second one used to be a `t+N` preview living here, and removing it
is what makes the boundary between the windows honest -- Motion is where
a garden moves through time, Moxie is where a property is swept, and a
note reads the state those produce. And it never raises: every failure
lands in the returned diagnostics, because a note with one bad
expression in it must still render the other nineteen.
"""
import json
import re
import sqlite3
from dataclasses import dataclass, field

from core import modoc
from core import kinds as kinds_repo
from core import plotview
from core.entities import Mote, Relationship
from core.formula import Context, FormulaError, evaluate_expression
from core.motes import list_motes
from core.relationships import list_relationships
from core.simulation import expression_context
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
class Resolution:
    anchor_id: str
    links: dict[str, LinkTarget] = field(default_factory=dict)
    values: dict[str, ValueResult] = field(default_factory=dict)
    graphs: dict[str, plotview.Figure] = field(default_factory=dict)
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

    def at(self, mote_id: str) -> dict:
        return self.properties.get(mote_id, {})

    def context(self, mote_id: str) -> Context:
        return expression_context(self.properties, self.relationships, mote_id)


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
    return Resolution(anchor_id, links, values, graphs, diagnostics)


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
    return sorted(found, key=lambda d: d.line)


# --- references ------------------------------------------------------

def _link(ref: str, motes: list[Mote], relationships: list[Relationship]) -> LinkTarget:
    mote, error = _find(ref, motes)
    if mote is None:
        return LinkTarget(ref, None, ref, [], error)
    return LinkTarget(ref, mote.id, _name(mote), kinds_repo.kinds_of(mote, relationships))


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
        return _property_result(properties, node.expression)

    dotted = _DOTTED_REF_RE.match(node.expression)
    if dotted:
        other_ref, other_key = dotted.groups()
        other, error = _find(other_ref, motes)
        if other is not None:
            other_properties = garden.at(other.id)
            if other_key in other_properties:
                return _property_result(other_properties, other_key)
        # No mote named `other_ref` (or it doesn't carry `other_key`) --
        # fall through to the formula evaluator, which also resolves
        # `role.property` and a name-matched relationship neighbor.

    try:
        number = evaluate_expression(node.expression, garden.context(mote_id)).value()
    except (FormulaError, ZeroDivisionError) as e:
        return ValueResult(f"?{node.expression}", None, str(e))
    return ValueResult(format_value(number), number)


def _property_result(properties: dict, key: str) -> ValueResult:
    """A plain property read off some mote's properties, formatted with
    its declared unit suffix if it has one. Shared by the anchor-property
    fast path and the cross-mote `mote.property` fast path -- both read a
    value straight off a mote without going through the formula
    evaluator, so both display it in its own declared unit rather than
    the solver's base SI."""
    raw = properties[key]
    unit_name = parse_units_property(properties.get(UNITS_KEY)).get(key)
    text = format_value(raw)
    # "1" means truly unitless (core/units.py: DIMENSIONLESS with scale 1),
    # not a unit with the symbol "1" -- nothing to show after the number.
    if unit_name and unit_name != "1" and isinstance(raw, (int, float)) and not isinstance(raw, bool):
        text = f"{text} {format_unit(unit_name)}"
    return ValueResult(text, raw)


def format_value(value: object) -> str:
    """One reading of a property for every surface. Numbers are rounded
    to six significant figures and switch to scientific notation outside
    a plain range -- `%g`'s own rule, which also strips a solved integral
    float's trailing `.0` for free ("12.0 tasks are closed" reads like a
    bug) -- so a unit conversion or a solved value doesn't render as
    fifteen digits of float noise."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return _format_number(value)
    if isinstance(value, (list, dict)):
        return json.dumps(value, separators=(", ", ": "))
    return str(value)


def _format_number(value: float) -> str:
    if value == 0:
        return "0"
    text = f"{value:.6g}"
    if "e" in text:
        mantissa, exponent = text.split("e")
        text = f"{mantissa}e{int(exponent)}"
    return text
