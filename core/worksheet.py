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
| `bd.l = sqrt(...)` | an **equation**: a rule mote, `applies_to bd`, and `l` free on `bd` |
| `bd.Ax = ?` | an **unknown**: `Ax` free on `bd`, with no value |
| `bd.Ax + bd.Bx == 0` | a **relation**: an equation, and no freedom declared |
| `bd.sigma <= 250` | a **check**: evaluated, never solved (`core/formula.py`) |
| `bd.R1 + bd.R2 ?= bd.W` | an **equality check**: stored as `==` with `check: true` |
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
from core import motes as motes_repo
from core import relationships as rel_repo
from core import rules as rules_repo
from core import simulation
from core import species as species_repo
from core import units as units_repo
from core.entities import Mote
from core.formula import MATH_CONSTANTS, TICK_NAME, FormulaError, parse_equation

# What a line turned out to be.
GIVEN = "given"
UNKNOWN = "unknown"
EQUATION = "equation"
BLANK = "blank"

COMMENT_MARKER = "!"
CHECK_MARKER = "?="
ASSIGN = "="
UNKNOWN_MARKER = "?"

# `lhs <op> rhs`, with the comparison left for core/formula.py to read. A
# lone `=` is matched only after `==` has been ruled out, which is what the
# negative lookarounds do.
_RELATION_RE = re.compile(r"^(?P<lhs>.+?)\s*(?P<op>\?=|==|<=|>=|<|>)\s*(?P<rhs>.+)$")
_ASSIGN_RE = re.compile(r"^(?P<lhs>[^=<>]+?)\s*=\s*(?P<rhs>.+)$")
_TARGET_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*\.\s*([A-Za-z_]\w*)\s*$")


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
    check: bool = False     # written `?=`: an equality checked, never solved
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
    value, unit = _as_number(rhs)
    if value is None:
        return _equation(number, text, f"{lhs} == {rhs}", name, lhs, assigns=True)
    mote, key = _target(lhs)
    return Line(number, text, GIVEN, mote=mote, key=key, value=value, unit=unit, name=name)


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
    givens = 0

    # Per line, because the two failures that can only be found now -- a
    # name matching two motes, a relation naming no mote at all -- belong
    # to one line and must not cost the other seven. Nothing is deleted
    # below unless every line was written, though: a line that failed is a
    # line whose rule would look abandoned.
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
    new = {line.key: line.value}
    if line.unit:
        unit_map = units_repo.parse_units_property(
            motes_repo.get_mote(conn, mote_id).properties.get(units_repo.UNITS_KEY))
        unit_map[line.key] = line.unit
        new[units_repo.UNITS_KEY] = unit_map
    motes_repo.update_mote(conn, mote_id, new)


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


# ---- rendering it back out ----


def read(conn: sqlite3.Connection) -> str:
    """The garden's calculation as a worksheet: its givens, a blank line,
    then its equations.

    Complete for what it covers, which is what makes the round trip safe to
    apply: every `once`-scoped rule is rendered, so applying what this
    returns deletes nothing. Givens are the numbers those equations read
    and are not free to determine -- a mote's own answer is not an input,
    and printing one as a given would pin it."""
    motes = {m.id: m for m in motes_repo.list_motes(conn)}
    rules = _existing_rules(conn)[0]
    free = simulation.garden_freedoms(conn)
    rendered = [_render(rule, motes, free) for rule in rules]
    # Read off the *qualified* relations, not the stored equations: a rule
    # written elsewhere carries bare names, and which mote those belong to
    # is exactly what rendering has just decided.
    referenced = {name for _line, relation in rendered for name in _bases(relation)}
    equations = [line for line, _relation in rendered]
    inputs = [
        *_render_givens(motes, referenced, free),
        *_render_unknowns(motes, referenced, free, rendered),
    ]
    return "\n".join([*inputs, *([""] if inputs and equations else []), *equations])


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
) -> list[str]:
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
        solved = free.get(mote.id, set())
        for key, value in sorted(mote.properties.items()):
            if key == units_repo.UNITS_KEY or key in solved:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            unit = unit_map.get(key)
            lines.append(f"{name}.{key} {ASSIGN} {value}{f' {unit}' if unit else ''}")
    return lines


def _render_unknowns(
    motes: dict[str, Mote],
    referenced: set[str],
    free: dict[str, set[str]],
    rendered: list[tuple[str, str]],
) -> list[str]:
    """`bd.Ax = ?` for every freedom no equation line already heads.

    Without this the projection would be incomplete in the one place it
    matters: a residual's unknowns appear in no assignment, so applying
    what `read()` returned would leave the system underdetermined even
    though the garden it came from was not."""
    headed = set()
    for _line, relation in rendered:
        match = _RELATION_RE.match(relation)
        target = _TARGET_RE.match(match.group("lhs")) if match else None
        if target is not None and match.group("op") == "==":
            headed.add((target.group(1), target.group(2)))

    lines = []
    for mote in sorted(motes.values(), key=lambda m: str(m.properties.get("name") or "")):
        name = mote.properties.get("name")
        if not isinstance(name, str) or name not in referenced:
            continue
        for key in sorted(free.get(mote.id, set())):
            if (name, key) not in headed:
                lines.append(f"{name}.{key} {ASSIGN} {UNKNOWN_MARKER}")
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
