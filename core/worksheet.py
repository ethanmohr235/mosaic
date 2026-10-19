# core/worksheet.py
"""A garden's calculation as lines of text, top to bottom.

    bd.x = 80 in
    bd.y = 60 in
    bd.l = sqrt(bd.x^2 + bd.y^2)
    bd.Ax + bd.Bx == 0            ! ΣFx

`FEA-Homework-4` is the case this exists for: 10 motes and 10
relationships to express about 9 numbers and 5 equations. Six of those
relationships existed only so `bd.l` would resolve -- garden-scope names
deleted those (`docs/simulation.md`, *Name references*) -- and the rest was
a mote and a rule per line, made one dialog at a time. Eight lines say the
same thing.

**A worksheet is a projection, never a store.** There is no `worksheet`
property and no saved source text: `read()` renders the garden's equations
and their inputs back as lines, and `apply()` writes lines into ordinary
motes, rules, `applies_to` and freedoms. That is `philosophy.md`'s rule --
"editors are shortcuts over the graph, never a separate store" -- taken at
its word, and what it costs is real: **comments, blank lines and the order
you typed in are not recoverable.** A line's trailing `! name` survives
because it becomes the rule's `name`; nothing else about the text does.
What `read()` gives back instead is its own order: one block per
independent calculation, inputs first, each equation after the ones it
reads.

**Applying replaces.** The lines are the whole set of this garden's
`once`-scoped equations, so a rule whose line is gone is deleted. That is
what makes the editor an editor rather than an append-only log, and it is
the one destructive thing here -- `Applied.deleted` says what went, for a
caller to show.

**Two kinds of rule, and only one of them is a worksheet's business.** A
rule pointing at a mote acting as a species is asserted once per instance
-- a reusable model, which species and templates are still for -- and is
neither rendered nor deleted here. `Applied.untouched` counts them, so a
surface can say so rather than implying a worksheet is the whole garden.

## Lines

| line | means |
| --- | --- |
| `bd.x = 80 in` | a **given**: the property, and `in` in the mote's `units` map |
| `bd.x = 80 ± 0.5 in` | a given with a ±, written to its `uncertainty` entry (`+-` also works; `± 2%` is relative) |
| `bd.l = sqrt(...)` | an **equation**: a rule mote, `applies_to bd`, and `l` free on `bd` |
| `bd.Ax = ?` | an **unknown**: `Ax` free on `bd`, with no value |
| `bd.Ax + bd.Bx == 0` | a **relation**: an equation, and no freedom declared |
| `bd.sigma <= 250` | a **check**: evaluated, never solved (`core/formula.py`) |
| `bd.R1 + bd.R2 ?= bd.W` | an **equality check**: stored as `==` with `check: true` |
| `phi(r [cm]) = A * sin(B * r) / r` | a **function**: the mote `phi` with that `function` body, `parameters: [r]` and `cm` for `r` in its `units` |
| `... ! ΣFx` | names the rule that line makes |
| (blank) | ignored |

A single `=` is a given when its right side is a bare number, an unknown
when it is `?`, and an equation otherwise. That is the whole rule, and it
is what the shapes above already read as: `80 in` is not an expression
(`in` is a Python keyword, so it cannot even parse as one), and
`sqrt(bd.x^2 + bd.y^2)` obviously is.

**`=` and `==` differ in one way that matters.** Both make an equation;
only `=` also declares its left side a freedom. `=` is the assignment --
solve for this -- and `==` is a relation between values that are free for
some other reason, which is how a residual is written without the parser
having to guess which of two unknowns it determines.

`= ?` exists because a residual names two unknowns and says nothing about
which to solve for -- and guessing is exactly what the engine refuses to
do. `bd.Ax = ?` is how the person who wrote the residual says which
values it is allowed to determine, and it is what makes the projection
complete: every freedom either heads an assignment line or has a `= ?` one.

`?=` is the worksheet's spelling of an equality that is checked rather
than solved -- `ΣFy` verifying the reactions two moment equations found.
It is not in the expression language: the rule stores `==` and carries
`check: true` (`core/rules.py`), which is what every other surface reads.
Retyping the line with `==` makes it an equation again.

One asymmetry worth knowing: applying **adds** freedoms and never removes
one, unlike rules. A freedom may have been declared in another surface
about a mote the worksheet happens to mention, and taking it away would be
reaching past what the lines are about.
"""
import ast
import re
import sqlite3
from dataclasses import dataclass, field, replace

from core import command_bar
from core import functions as functions_repo
from core import motes as motes_repo
from core import relationships as rel_repo
from core import rules as rules_repo
from core import simulation
from core import uncertainty as uncertainty_repo
from core import undo
from core import species as species_repo
from core import units as units_repo
from core.entities import Mote
from core.formula import (
    MATH_CONSTANTS, TICK_NAME, FormulaError, function_name_problem, parse_equation,
    parse_expression,
)

# What a line turned out to be.
GIVEN = "given"
UNKNOWN = "unknown"
EQUATION = "equation"
FUNCTION = "function"
BLANK = "blank"

COMMENT_MARKER = "!"
CHECK_MARKER = "?="
SPREAD_MARKER = "±"

# `<value> ± <spread> [unit]`: the spread is a number (in the value's unit)
# or a percentage. `+-` and `+/-` are the ASCII spellings.
_SPREAD_RE = re.compile(
    r"^(?P<value>.+?)\s*(?:±|\+/-|\+-)\s*"
    r"(?P<spread>[0-9]*\.?[0-9]+(?:[eE][-+]?[0-9]+)?\s*%?)\s*(?P<unit>.*)$"
)
ASSIGN = "="
UNKNOWN_MARKER = "?"

# `lhs <op> rhs`, with the comparison left for core/formula.py to read. A
# lone `=` is matched only after `==` has been ruled out, which is what the
# negative lookarounds do.
_RELATION_RE = re.compile(r"^(?P<lhs>.+?)\s*(?P<op>\?=|==|<=|>=|<|>)\s*(?P<rhs>.+)$")
_ASSIGN_RE = re.compile(r"^(?P<lhs>[^=<>]+?)\s*=\s*(?P<rhs>.+)$")
_TARGET_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*$")
# `phi(r [cm], z) = body`: a call on the left of a single `=`. Unambiguous,
# since an assignment's left side is otherwise always `mote.key`.
_FUNCTION_RE = re.compile(
    r"^(?P<name>[A-Za-z_]\w*)\s*\((?P<parameters>[^()]*)\)\s*=(?!=)\s*(?P<body>.+)$")
_PARAMETER_RE = re.compile(r"^(?P<name>[A-Za-z_]\w*)\s*(?:\[\s*(?P<unit>[^\]]+?)\s*\])?$")


@dataclass(frozen=True)
class Line:
    """One line, read but not yet written. `problem` set means the line is
    reported and skipped rather than raising: a worksheet is edited a
    keystroke at a time, and one bad line must not cost the other seven."""
    number: int
    text: str
    kind: str = BLANK
    mote: str = ""          # the name left of the dot, for a given or an assignment
    key: str = ""           # the property left of the dot
    value: object = None    # a given's value, in the unit below
    unit: str | None = None
    relation: str = ""      # an equation's text, as core/formula.py reads it
    name: str = ""          # the trailing `! ...`
    free: bool = False      # whether this line makes `key` a freedom of `mote`
    spread: object = None   # a given's ±: a number in `unit`, or "2%"
    check: bool = False     # written `?=`: an equality checked, never solved
    parameters: tuple[tuple[str, str | None], ...] = ()  # a function's, with units
    problem: str = ""

    @property
    def ok(self) -> bool:
        return not self.problem


@dataclass(frozen=True)
class Worksheet:
    lines: tuple[Line, ...] = ()

    @property
    def problems(self) -> tuple[str, ...]:
        return tuple(f"line {line.number}: {line.problem}" for line in self.lines if line.problem)

    @property
    def givens(self) -> tuple[Line, ...]:
        return self._of(GIVEN)

    @property
    def unknowns(self) -> tuple[Line, ...]:
        return self._of(UNKNOWN)

    @property
    def equations(self) -> tuple[Line, ...]:
        return self._of(EQUATION)

    @property
    def functions(self) -> tuple[Line, ...]:
        return self._of(FUNCTION)

    def _of(self, kind: str) -> tuple[Line, ...]:
        return tuple(line for line in self.lines if line.kind == kind and line.ok)


@dataclass(frozen=True)
class Applied:
    """What writing a worksheet did. Counts rather than ids for the three
    ordinary cases, and the labels of what was deleted, because that is
    the one part somebody may want to undo."""
    motes_created: tuple[str, ...] = ()
    givens_written: int = 0
    rules_written: int = 0
    deleted: tuple[str, ...] = ()
    untouched: int = 0
    problems: tuple[str, ...] = field(default_factory=tuple)


# ---- reading text ----


def parse(text: str) -> Worksheet:
    """Every line of `text`, read into what it means. Never raises."""
    return Worksheet(tuple(
        _line(number, raw) for number, raw in enumerate((text or "").splitlines(), start=1)
    ))


def _line(number: int, raw: str) -> Line:
    body, name = _split_comment(raw)
    if not body:
        return Line(number, raw.strip(), BLANK, name=name)
    try:
        return _read(number, raw.strip(), body, name)
    except FormulaError as e:
        return Line(number, raw.strip(), problem=str(e))


def _split_comment(raw: str) -> tuple[str, str]:
    """`! ΣFx` off the end. A `!` cannot appear in a relation this engine
    reads -- there is no `!=` in the grammar -- so splitting on the first
    one needs no escaping rule."""
    body, _, comment = raw.partition(COMMENT_MARKER)
    return body.strip(), comment.strip()


def _read(number: int, text: str, body: str, name: str) -> Line:
    function = _FUNCTION_RE.match(body)
    if function:
        return _function(number, text, function)
    relation = _RELATION_RE.match(body)
    if relation and relation.group("op") == CHECK_MARKER:
        lhs, rhs = relation.group("lhs"), relation.group("rhs")
        line = _equation(number, text, f"{lhs.strip()} == {rhs.strip()}", name, lhs, assigns=False)
        return replace(line, check=True)
    if relation:
        return _equation(number, text, body, name, relation.group("lhs"), assigns=False)

    assignment = _ASSIGN_RE.match(body)
    if not assignment:
        raise FormulaError(
            f"{body!r} is not a worksheet line -- write `bd.x = 80 in` for a given, "
            f"`bd.l = sqrt(bd.x^2 + bd.y^2)` for an equation"
        )
    lhs, rhs = assignment.group("lhs"), assignment.group("rhs")
    if rhs.strip() == UNKNOWN_MARKER:
        mote, key = _target(lhs)
        return Line(number, text, UNKNOWN, mote=mote, key=key, name=name, free=True)
    spread = None
    with_spread = _SPREAD_RE.match(rhs)
    if with_spread:
        rhs = f"{with_spread['value']} {with_spread['unit']}".strip()
        spread = with_spread["spread"].replace(" ", "")
        spread = spread if spread.endswith("%") else float(spread)
    value, unit = _as_number(rhs)
    if value is None:
        if spread is not None:
            raise FormulaError(
                f"only a given can carry a ± -- {rhs!r} is not a number; an "
                f"equation's ± is propagated from its inputs, never written")
        return _equation(number, text, f"{lhs} == {rhs}", name, lhs, assigns=True)
    mote, key = _target(lhs)
    return Line(number, text, GIVEN, mote=mote, key=key, value=value, unit=unit, name=name,
                spread=spread)


def _function(number: int, text: str, match: re.Match) -> Line:
    """`phi(r [cm]) = body`. The name and every parameter are checked here,
    and the body parsed, so a line that could never be called says so as it
    is typed rather than when something first calls it."""
    name = match.group("name")
    problem = function_name_problem(name)
    if problem:
        raise FormulaError(problem)
    parameters = []
    for raw in (part.strip() for part in match.group("parameters").split(",")):
        if not raw:
            continue
        parameter = _PARAMETER_RE.match(raw)
        if parameter is None:
            raise FormulaError(f"{raw!r} is not a parameter -- write `r` or `r [cm]`")
        unit = parameter.group("unit")
        if unit and not units_repo.is_unit(unit):
            raise FormulaError(f"{unit!r} is not a unit (parameter {parameter.group('name')})")
        parameters.append((parameter.group("name"), unit))
    names = [p for p, _unit in parameters]
    if len(set(names)) != len(names):
        raise FormulaError(f"{name}() repeats a parameter name")
    body = match.group("body").strip()
    parse_expression(body)  # raises FormulaError, which _line() reports
    return Line(number, text, FUNCTION, mote=name, relation=body, parameters=tuple(parameters))


def _as_number(rhs: str) -> tuple[float | int | None, str | None]:
    """A given's right side: a number, optionally with a unit. Anything
    else -- an expression, a word, a date -- is None, which sends the line
    to `_equation`. `core/command_bar.py::parse_value` does the reading, so
    `80 in` and `22.5%` mean here exactly what they mean typed into the
    command bar."""
    value, unit = command_bar.parse_value(rhs)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None, None
    return value, unit


def _equation(number: int, text: str, relation: str, name: str, lhs: str, assigns: bool) -> Line:
    parse_equation(relation)  # raises FormulaError, which _line() reports
    target = _TARGET_RE.match(lhs)
    # A freedom only for a line written with a single `=` whose left side is
    # exactly one mote's one property. `=` is the assignment -- "solve for
    # this" -- and `==` is a relation between values that are free for some
    # other reason: `bd.Ax + bd.Bx == 0` says nothing about which of the two
    # to solve for, and a comparison can never determine a value at all
    # (docs/simulation.md, *Comparison relations*). Keeping those apart is
    # also what makes `read()` safe to apply: it writes `==` for any rule
    # whose left side is not already free, so a round trip cannot widen the
    # model behind the reader's back.
    free = assigns and target is not None
    return Line(
        number, text, EQUATION,
        mote=target.group(1) if target else "",
        key=target.group(2) if target else "",
        relation=relation,
        name=name,
        free=free,
    )


def _target(lhs: str) -> tuple[str, str]:
    match = _TARGET_RE.match(lhs)
    if not match:
        raise FormulaError(
            f"{lhs.strip()!r} is not a property to give a value to -- "
            f"write `<mote>.<property> = <number>`"
        )
    return match.group(1), match.group(2)


# ---- writing it into the garden ----


def apply(conn: sqlite3.Connection, text: str) -> Applied:
    """Write a worksheet into the garden, and delete the rules whose lines
    are gone. See the module docstring: this replaces the garden's
    `once`-scoped equations rather than adding to them."""
    sheet = parse(text)
    index = _by_name(conn)
    created: list[str] = []
    problems = list(sheet.problems)
    written_rules: set[str] = set()
    written_functions: set[str] = set()
    givens = 0

    # Per line, because the two failures that can only be found now -- a
    # name matching two motes, a relation naming no mote at all -- belong
    # to one line and must not cost the other seven. Nothing is deleted
    # below unless every line was written, though: a line that failed is a
    # line whose rule would look abandoned.
    for line in sheet.functions:
        try:
            written_functions.add(_write_function(conn, index, created, line))
        except FormulaError as e:
            problems.append(f"line {line.number}: {e}")
    for line in sheet.givens:
        try:
            _write_given(conn, index, created, line)
            givens += 1
        except FormulaError as e:
            problems.append(f"line {line.number}: {e}")
    for line in sheet.unknowns:
        try:
            _free(conn, _resolve(conn, index, created, line.mote), line.key)
        except FormulaError as e:
            problems.append(f"line {line.number}: {e}")
    for line in sheet.equations:
        try:
            written_rules.add(_write_rule(conn, index, created, line))
        except FormulaError as e:
            problems.append(f"line {line.number}: {e}")

    ours, untouched = _existing_rules(conn)
    deleted = []
    if not problems:
        for rule in ours:
            if rule.mote.id in written_rules:
                continue
            motes_repo.delete_mote(conn, rule.mote.id)
            deleted.append(rule.name)
        deleted.extend(_unwrite_functions(conn, written_functions))

    return Applied(
        motes_created=tuple(created),
        givens_written=givens,
        rules_written=len(written_rules),
        deleted=tuple(deleted),
        untouched=untouched,
        problems=tuple(problems),
    )


def _by_name(conn: sqlite3.Connection) -> dict[str, list[str]]:
    index: dict[str, list[str]] = {}
    for mote in motes_repo.list_motes(conn):
        name = mote.properties.get("name")
        if isinstance(name, str) and name:
            index.setdefault(name, []).append(mote.id)
    return index


def _resolve(conn, index: dict[str, list[str]], created: list[str], name: str) -> str:
    """The mote a line names, made if it isn't there yet.

    Ambiguity is refused rather than guessed at, the same rule
    `core/formula.py::name_match` applies when the solve runs -- writing to
    one of two motes called `bd` would be the silent choice that hides the
    problem."""
    found = index.get(name, [])
    if len(found) > 1:
        raise FormulaError(
            f"'{name}' names {len(found)} motes ({', '.join(sorted(m[:8] for m in found))}) "
            f"-- rename all but one"
        )
    if found:
        return found[0]
    mote_id = motes_repo.create_mote(conn, {"name": name})
    index[name] = [mote_id]
    created.append(name)
    return mote_id


def _write_given(conn, index, created: list[str], line: Line) -> None:
    mote_id = _resolve(conn, index, created, line.mote)
    properties = motes_repo.get_mote(conn, mote_id).properties
    new = {line.key: line.value}
    if line.unit:
        unit_map = units_repo.parse_units_property(properties.get(units_repo.UNITS_KEY))
        unit_map[line.key] = line.unit
        new[units_repo.UNITS_KEY] = unit_map
    # The line is the whole account of this given, so a ± it no longer
    # carries is removed -- the same replacing a deleted line does to a rule.
    spreads = properties.get(uncertainty_repo.UNCERTAINTY_KEY)
    spreads = dict(spreads) if isinstance(spreads, dict) else {}
    if line.spread is not None:
        spreads[line.key] = line.spread
    else:
        spreads.pop(line.key, None)
    if spreads:
        new[uncertainty_repo.UNCERTAINTY_KEY] = spreads
    motes_repo.update_mote(conn, mote_id, new)
    if not spreads and uncertainty_repo.UNCERTAINTY_KEY in properties:
        motes_repo.delete_property(conn, mote_id, uncertainty_repo.UNCERTAINTY_KEY)


def _write_function(conn, index, created: list[str], line: Line) -> str:
    """The function mote this line is, made or updated, and its id. The
    line is the whole account of the function, so a parameter's unit it no
    longer declares -- or a parameter it no longer has -- leaves `units`."""
    mote_id = _resolve(conn, index, created, line.mote)
    properties = motes_repo.get_mote(conn, mote_id).properties
    unit_map = units_repo.parse_units_property(properties.get(units_repo.UNITS_KEY))
    try:
        old = functions_repo.parameters_of(properties)
    except functions_repo.FunctionDefinitionError:
        old = []
    for parameter in old:
        unit_map.pop(parameter, None)
    for parameter, unit in line.parameters:
        unit_map.pop(parameter, None)
        if unit:
            unit_map[parameter] = unit
    new = {
        functions_repo.FUNCTION_KEY: line.relation,
        functions_repo.PARAMETERS_KEY: [p for p, _unit in line.parameters],
    }
    if unit_map:
        new[units_repo.UNITS_KEY] = unit_map
    motes_repo.update_mote(conn, mote_id, new)
    if not unit_map and units_repo.UNITS_KEY in properties:
        motes_repo.delete_property(conn, mote_id, units_repo.UNITS_KEY)
    return mote_id


def _unwrite_functions(conn, written: set[str]) -> list[str]:
    """Every function whose line is gone stops being one: its body and
    parameters go, and the mote too when nothing else is left on it. Its
    constants are givens with lines of their own, so a mote still holding
    one is kept. Named `phi()` in what was deleted."""
    gone = []
    for mote in motes_repo.list_motes(conn):
        if mote.id in written or not functions_repo.is_function(mote.properties):
            continue
        name = mote.properties.get("name") or mote.id[:8]
        try:
            parameters = functions_repo.parameters_of(mote.properties)
        except functions_repo.FunctionDefinitionError:
            parameters = []
        rest = {k: v for k, v in mote.properties.items() if k not in (
            "name", functions_repo.FUNCTION_KEY, functions_repo.PARAMETERS_KEY)}
        unit_map = {k: v for k, v in units_repo.parse_units_property(
            rest.pop(units_repo.UNITS_KEY, None)).items() if k not in parameters}
        if not rest and not unit_map:
            motes_repo.delete_mote(conn, mote.id)
        else:
            for key in (functions_repo.FUNCTION_KEY, functions_repo.PARAMETERS_KEY):
                motes_repo.delete_property(conn, mote.id, key)
            if unit_map:
                motes_repo.update_mote(conn, mote.id, {units_repo.UNITS_KEY: unit_map})
            else:
                motes_repo.delete_property(conn, mote.id, units_repo.UNITS_KEY)
        gone.append(f"{name}()")
    return gone


def _write_rule(conn, index, created: list[str], line: Line) -> str:
    """Find-or-create the rule this line is, and return its id.

    Matched on its anchor plus its equation text, so re-applying an
    unchanged worksheet writes nothing new, and an *edited* line replaces
    the rule it used to be rather than leaving both behind (the old one
    has no line, so the delete pass above takes it)."""
    anchor_id = _resolve(conn, index, created, _anchor(line))
    rule_id = _find_rule(conn, anchor_id, line.relation)
    properties = {rules_repo.EQUATION_KEY: line.relation}
    if line.name:
        properties["name"] = line.name
    if line.check:
        properties[rules_repo.CHECK_KEY] = True
    if rule_id is None:
        rule_id = motes_repo.create_mote(conn, properties)
        rel_repo.create_relationship(conn, rule_id, anchor_id, rules_repo.APPLIES_TO)
    else:
        motes_repo.update_mote(conn, rule_id, properties)
        if not line.name:
            motes_repo.delete_property(conn, rule_id, "name")
        if not line.check:
            motes_repo.delete_property(conn, rule_id, rules_repo.CHECK_KEY)
    if line.free:
        _free(conn, _resolve(conn, index, created, line.mote), line.key)
    return rule_id


def _anchor(line: Line) -> str:
    """Which mote a line's rule is *about*: the mote on its left where it
    has one, else the first name the relation mentions. Reading left to
    right is both predictable and what somebody typing one means."""
    if line.mote:
        return line.mote
    bases = _bases(line.relation)
    if not bases:
        raise FormulaError(
            f"{line.relation!r} doesn't say which mote it is about -- put a mote name "
            f"in front of one of its properties, e.g. `bd.Ax + bd.Bx == 0`"
        )
    return bases[0]


def _find_rule(conn, anchor_id: str, relation: str) -> str | None:
    for rel in rel_repo.list_relationships(conn, type_=rules_repo.APPLIES_TO):
        if rel.to_id != anchor_id:
            continue
        mote = motes_repo.get_mote(conn, rel.from_id)
        if mote is not None and mote.properties.get(rules_repo.EQUATION_KEY) == relation:
            return mote.id
    return None


def _free(conn, mote_id: str, key: str) -> None:
    mote = motes_repo.get_mote(conn, mote_id)
    freedoms = rules_repo.parse_freedoms(mote.properties.get(rules_repo.FREEDOMS_KEY))
    if key not in freedoms:
        motes_repo.update_mote(
            conn, mote_id, {rules_repo.FREEDOMS_KEY: [*freedoms, key]})


def _existing_rules(conn: sqlite3.Connection) -> tuple[list[rules_repo.Rule], int]:
    """This garden's rules split into the worksheet's own -- the ones
    asserted about a single mote -- and the ones it leaves alone. A rule
    pointing at a species is a reusable model asserted once per instance,
    which is not a line and must not be deleted as if it were."""
    ours, untouched = [], 0
    for rule in rules_repo.list_rules(conn):
        if rule.scope == rules_repo.ONCE:
            ours.append(rule)
        else:
            untouched += 1
    return ours, untouched


def apply_undoable(conn: sqlite3.Connection, text: str) -> tuple[Applied, "undo.Command | None"]:
    """apply(), and its writes as one undo step -- every mote it made, every
    value, freedom and rule it wrote, every rule it deleted."""
    before = undo.capture(conn)
    applied = apply(conn, text)
    return applied, undo.since(conn, before, "apply the worksheet")


@dataclass(frozen=True)
class Preview:
    """What one line will become, for an editor's margin: its kind, why it
    can't be written, and -- for a line naming a value -- that value as the
    garden holds it now (an unknown's last solved value)."""
    number: int
    kind: str
    problem: str = ""
    value: str = ""


def preview(conn: sqlite3.Connection, text: str) -> list[Preview]:
    """Every line of `text`, read and not written. Ambiguous names are
    found here too, since they can only be found against a garden."""
    index = _by_name(conn)
    by_id = {m.id: m for m in motes_repo.list_motes(conn)}
    out = []
    for line in parse(text).lines:
        problem, value = line.problem, ""
        if not problem and line.mote:
            found = index.get(line.mote, [])
            if len(found) > 1:
                problem = f"'{line.mote}' names {len(found)} motes -- rename all but one"
            elif found and (line.kind == UNKNOWN or (line.kind == EQUATION and line.free)):
                value = _shown(by_id[found[0]].properties, line.key)
        out.append(Preview(line.number, _kind(line), problem, value))
    return out


RELATION, CHECK = "relation", "check"


def _kind(line: Line) -> str:
    """The five shapes the table in docs/simulation.md names: an equation
    that frees a value, a relation that frees nothing, and a check."""
    if line.kind != EQUATION:
        return line.kind
    if line.check:
        return CHECK
    return EQUATION if line.free else RELATION


def _shown(properties: dict, key: str) -> str:
    value = properties.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    unit = units_repo.parse_units_property(properties.get(units_repo.UNITS_KEY)).get(key)
    from core.calc import Quantity
    return Quantity(value, unit).text()


# ---- rendering it back out ----


def read(conn: sqlite3.Connection) -> str:
    """The garden's calculation as a worksheet: one block per independent
    calculation, a blank line between them.

    A block is the equations that share a mote, with the givens and
    unknowns of those motes above them, and its equations in dependency
    order: a line comes after the lines that work out what it reads. In a
    garden of many unrelated calculations that is what keeps one of them
    on one screen.

    Complete for what it covers, which is what makes the round trip safe to
    apply: every `once`-scoped rule is rendered, so applying what this
    returns deletes nothing. Givens are the numbers those equations read
    and are not free to determine -- a mote's own answer is not an input,
    and printing one as a given would pin it."""
    motes = {m.id: m for m in motes_repo.list_motes(conn)}
    rules = _existing_rules(conn)[0]
    free = simulation.garden_freedoms(conn)
    rendered = [_render(rule, motes, free) for rule in rules]
    functions = _render_functions(motes)
    # Read off the *qualified* relations, not the stored equations: a rule
    # written elsewhere carries bare names, and which mote those belong to
    # is exactly what rendering has just decided. A function's constants
    # are inputs too, and go with it, in a leading block of definitions.
    called = {name for name, _line in functions}
    referenced = {name for _line, relation in rendered for name in _bases(relation)} | called
    inputs = [
        *_render_givens(motes, referenced, free),
        *_render_unknowns(motes, referenced, free, rendered),
    ]
    blocks = []
    if functions:
        blocks.append("\n".join([
            *(line for _name, line in functions),
            *(line for name, line in inputs if name in called),
        ]))
    for names, equations in _calculations(rendered):
        block = [line for name, line in inputs if name in names and name not in called]
        blocks.append("\n".join([*block, *_in_order(equations)]))
    return "\n\n".join(blocks)


def _render_functions(motes: dict[str, Mote]) -> list[tuple[str, str]]:
    """`phi(r [cm]) = body` for every function mote, by name. A function
    whose parameters cannot be read is still rendered, with what it has,
    so applying the sheet back cannot delete it behind the reader's back."""
    lines = []
    for mote in sorted(motes.values(), key=lambda m: str(m.properties.get("name") or "")):
        name = mote.properties.get("name")
        if not functions_repo.is_function(mote.properties) or not isinstance(name, str):
            continue
        try:
            parameters = functions_repo.parameters_of(mote.properties)
        except functions_repo.FunctionDefinitionError:
            parameters = []
        unit_map = units_repo.parse_units_property(mote.properties.get(units_repo.UNITS_KEY))
        shown = ", ".join(
            f"{p} [{unit_map[p]}]" if p in unit_map else p for p in parameters)
        lines.append((name, f"{name}({shown}) {ASSIGN} {functions_repo.body_of(mote.properties)}"))
    return lines


def _calculations(rendered: list[tuple[str, str]]) -> list[tuple[set[str], list[tuple[str, str]]]]:
    """The rendered equations split into independent calculations: two
    share one when they mention a mote in common. In the order their first
    rule was made."""
    groups: list[tuple[set[str], list[int]]] = []
    for index, (_line, relation) in enumerate(rendered):
        names = set(_bases(relation))
        joined = [g for g in groups if g[0] & names]
        groups = [g for g in groups if not any(g is j for j in joined)]
        members = [index, *(i for g in joined for i in g[1])]
        groups.append((names.union(*(g[0] for g in joined)), sorted(members)))
    groups.sort(key=lambda g: g[1][0])
    return [(names, [rendered[i] for i in members]) for names, members in groups]


def _in_order(equations: list[tuple[str, str]]) -> list[str]:
    """Equation lines ordered so each comes after the ones that head what
    it reads. Ties, and a simultaneous system (which has no such order),
    keep the order the rules were made in."""
    heads = [_head(relation) for _line, relation in equations]
    needs = [
        {j for j, head in enumerate(heads) if head is not None and j != i and head in _refs(relation)}
        for i, (_line, relation) in enumerate(equations)
    ]
    pending, placed, out = list(range(len(equations))), set(), []
    while pending:
        ready = next((i for i in pending if needs[i] <= placed), pending[0])
        pending.remove(ready)
        placed.add(ready)
        out.append(equations[ready][0])
    return out


def _head(relation: str) -> tuple[str, str] | None:
    """`(mote, key)` an equality's left side names, when it is one property."""
    match = _RELATION_RE.match(relation)
    if not match or match.group("op") != "==":
        return None
    target = _TARGET_RE.match(match.group("lhs"))
    return (target.group(1), target.group(2)) if target else None


def _refs(relation: str) -> set[tuple[str, str]]:
    """Every `Name.property` a relation mentions, as `(name, property)`."""
    try:
        tree = ast.parse(relation, mode="eval")
    except SyntaxError:
        return set()
    return {
        (node.value.id, node.attr) for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
    }


def calculation_mote_ids(conn: sqlite3.Connection) -> set[str]:
    """Every mote the garden's own equations mention -- which is what makes
    a *calculation's* givens different from a dump of the garden. A homework
    mote's `estimate: 3` is a real property and not an input to anything.

    Public because `core/calc.py` scopes `\\given` and `\\answers` the same
    way this scopes its given lines, and two answers to "which motes is
    this calculation about" would be one too many."""
    motes = {m.id: m for m in motes_repo.list_motes(conn)}
    free = simulation.garden_freedoms(conn)
    rendered = [_render(rule, motes, free) for rule in _existing_rules(conn)[0]]
    referenced = {name for _line, relation in rendered for name in _bases(relation)}
    referenced |= {name for name, _line in _render_functions(motes)}
    return {
        mote.id for mote in motes.values()
        if isinstance(mote.properties.get("name"), str)
        and mote.properties["name"] in referenced
    }


def _render(
    rule: rules_repo.Rule, motes: dict[str, Mote], free: dict[str, set[str]]
) -> tuple[str, str]:
    """One rule as `(line, qualified relation)`.

    A bare property name is prefixed with its anchor's name, since a line's
    anchor is read from the first `Name.` in it -- an equation written
    elsewhere (`voltage == current * resistance`) has to come back as a
    line that says which mote it is about. The relation comes back
    alongside because the givens are found from it, and re-deriving it from
    the line would mean parsing the `!` and the `=` back off again."""
    relation = rule.equation
    anchor = motes.get(rule.target_id)
    name = anchor.properties.get("name") if anchor else None
    if isinstance(name, str) and name.isidentifier():
        relation = _qualify(relation, name)
    line = (_as_check(relation) if rule.is_check
            else _as_assignment(relation, free.get(rule.target_id, set())))
    declared = rule.mote.properties.get("name")
    if isinstance(declared, str) and declared.strip():
        line = f"{line}  {COMMENT_MARKER} {declared.strip()}"
    return line, relation


def _as_assignment(relation: str, free: set[str]) -> str:
    """`bd.l == sqrt(...)` written back as `bd.l = ...`, which is how it was
    typed.

    Only when the left side is one property *and* that property is a
    freedom of the anchor. `=` on a line makes its left side free when the
    worksheet is applied, so writing one for a property that is not free
    would quietly widen the model on a round trip -- which is the one thing
    a projection must never do. A residual and a comparison keep their
    operator because that is what they say."""
    match = _RELATION_RE.match(relation)
    if not match or match.group("op") != "==":
        return relation
    target = _TARGET_RE.match(match.group("lhs"))
    if target is None or target.group(2) not in free:
        return relation
    return f"{match.group('lhs').strip()} {ASSIGN} {match.group('rhs').strip()}"


def _as_check(relation: str) -> str:
    """An equality marked `check: true` written back as `?=`. A comparison
    marked the same way keeps its operator, which already says it."""
    match = _RELATION_RE.match(relation)
    if not match or match.group("op") != "==":
        return relation
    return f"{match.group('lhs').strip()} {CHECK_MARKER} {match.group('rhs').strip()}"


def _render_givens(
    motes: dict[str, Mote], referenced: set[str], free: dict[str, set[str]]
) -> list[tuple[str, str]]:
    """Every number an equation reads and is not free to determine.

    A freedom is deliberately left out: a mote's own answer is not an
    input, and printing `bd.l = 100` as a given would pin the very value
    the worksheet solves for the next time it was applied."""
    lines = []
    for mote in sorted(motes.values(), key=lambda m: str(m.properties.get("name") or "")):
        name = mote.properties.get("name")
        if not isinstance(name, str) or name not in referenced:
            continue
        unit_map = units_repo.parse_units_property(mote.properties.get(units_repo.UNITS_KEY))
        spreads = uncertainty_repo.parse_uncertainty_property(
            mote.properties.get(uncertainty_repo.UNCERTAINTY_KEY))[0]
        solved = free.get(mote.id, set())
        for key, value in sorted(mote.properties.items()):
            if key == units_repo.UNITS_KEY or key in solved:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            unit = unit_map.get(key)
            spread = spreads.get(key)
            pm = ""
            if spread is not None:
                pm = (f" {SPREAD_MARKER} {spread.absolute:g}" if spread.absolute is not None
                      else f" {SPREAD_MARKER} {spread.relative * 100:g}%")
            lines.append((name, f"{name}.{key} {ASSIGN} {value}{pm}{f' {unit}' if unit else ''}"))
    return lines


def _render_unknowns(
    motes: dict[str, Mote],
    referenced: set[str],
    free: dict[str, set[str]],
    rendered: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    """`bd.Ax = ?` for every freedom no equation line already heads.

    Without this the projection would be incomplete in the one place it
    matters: a residual's unknowns appear in no assignment, so applying
    what `read()` returned would leave the system underdetermined even
    though the garden it came from was not."""
    headed = {head for _line, relation in rendered if (head := _head(relation)) is not None}

    lines = []
    for mote in sorted(motes.values(), key=lambda m: str(m.properties.get("name") or "")):
        name = mote.properties.get("name")
        if not isinstance(name, str) or name not in referenced:
            continue
        for key in sorted(free.get(mote.id, set())):
            if (name, key) not in headed:
                lines.append((name, f"{name}.{key} {ASSIGN} {UNKNOWN_MARKER}"))
    return lines


def _bases(relation: str) -> list[str]:
    """Every name used as a `Name.property` base in a relation, in source
    order -- which mote a line is about, and which motes a worksheet's
    givens come from."""
    bases, _bare = _names(relation)
    return bases


def _qualify(relation: str, anchor: str) -> str:
    _names_found, bare = _names(relation)
    if not bare:
        return relation
    pattern = re.compile(rf"(?<![.\w]){'|'.join(re.escape(b) for b in sorted(bare))}(?!\w)")
    return pattern.sub(lambda m: f"{anchor}.{m.group(0)}", relation)


def _names(relation: str) -> tuple[list[str], set[str]]:
    """`(dotted bases in source order, bare property names)`. A call's own
    name, the tick index and the math constants are none of those."""
    try:
        tree = ast.parse(relation, mode="eval")
    except SyntaxError:
        return [], set()
    dotted, calls, bases = [], set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            dotted.append(id(node.value))
            if node.value.id not in bases:
                bases.append(node.value.id)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.add(id(node.func))
    bare = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and id(node) not in dotted and id(node) not in calls
        and node.id != TICK_NAME and node.id not in MATH_CONSTANTS
    }
    return bases, bare
