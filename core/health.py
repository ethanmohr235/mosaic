# core/health.py
"""Is this model solvable, and if not, which line is wrong?

A solve either answers or fails with a message about the system as a
whole -- *underdetermined*, *inconsistent*, *could not be resolved*. That
is correct and useless: "more unknowns than independent equations" is true
of the whole garden and tells nobody which line to go fix. A worksheet
whose eight lines are one short needs to be told *which* value nothing
constrains, before it is run.

So this reports on the model rather than on a run, and it answers **before
anything is solved**. Everything it says comes from what already exists:
`core/incidence.py`'s bipartite equation/value relation for the counts and
the culprits, and `core/simulation.py::check_garden` for the units. Nothing
here re-derives either -- a second account of what depends on what would
drift from the equations, which is the whole reason that index is built
fresh on every query.

**The unit check is already per line.** `check_garden` labels each problem
with the rule it found it on, and a worksheet rule is labelled by its own
equation (`core/rules.py::label_of`), so a warning reads back as the line
that caused it without this module arranging anything.

**The count is not a verdict.** `equations == unknowns` is neither
necessary nor sufficient: an overdetermined system is normal and fine as
long as the extra equations agree (in any circuit one Kirchhoff equation
is implied by the others), and a square system can still be singular. So
the count is reported as a count, and the *culprits* are reported
separately, because those are the part somebody can act on:

    unconstrained   free, and no equation can determine it   <- go write one
    missing         read by an equation, and nothing has it  <- go give it a value
    inert           an equation that determines nothing      <- the surplus suspect

**Display units are not here.** `roadmap.md` asks for a stored unit and a
displayed one to be told apart; the decision was that the displayed one is
a surface's own state rather than anything written into the garden, so core
offers only the conversion (`core/units.py::convert`) and `stored_unit()`
below to say what a value is actually recorded in. Nothing about a person's
current view of a number belongs in their data.
"""
import sqlite3
from dataclasses import dataclass

from core import incidence as incidence_repo
from core import motes as motes_repo
from core import rules as rules_repo
from core import simulation
from core import units as units_repo
from core.formula import FormulaError, Unknown, relation_operator

# What the counts add up to. Never a pass/fail -- see the module docstring.
BALANCED = "balanced"
UNDERDETERMINED = "underdetermined"
OVERDETERMINED = "overdetermined"

# Why a value is a culprit.
UNCONSTRAINED = "unconstrained"
MISSING = "missing"


@dataclass(frozen=True)
class Culprit:
    """One value standing between this model and an answer. `label` is how
    to say it to a person -- `bd.Ax`, the same way a line writes it."""
    variable: Unknown
    label: str
    reason: str


@dataclass(frozen=True)
class Inert:
    """An equation that touches nothing the solver may determine. Not an
    error: a check is *meant* to constrain rather than determine, and a
    rule reading only givens may be there to be read by `\\answers`. It is
    named because when a system is overdetermined, this is the honest
    shortlist -- rather than pretending one equation can be blamed."""
    equation: str
    label: str
    anchor: str


@dataclass(frozen=True)
class Group:
    """One coupled block of unknowns, as something to say to a person."""
    labels: tuple[str, ...]
    equations: tuple[str, ...]
    shortfall: int

    @property
    def message(self) -> str:
        values = ", ".join(self.labels)
        if not self.equations:
            return f"nothing can determine {values} -- no equation constrains it"
        short = abs(self.shortfall)
        need = "another equation" if short == 1 else f"{short} more equations"
        if self.shortfall > 0:
            return (
                f"{len(self.labels)} unknowns ({values}) share "
                f"{len(self.equations)} equation(s) -- {need} needed"
            )
        return (
            f"{len(self.labels)} unknown(s) ({values}) have "
            f"{len(self.equations)} equations -- {short} more than needed, "
            f"which is fine as long as they agree"
        )


@dataclass(frozen=True)
class Health:
    """What the model looks like before it is run."""
    equations: int = 0
    unknowns: int = 0
    checks: int = 0
    groups: tuple[Group, ...] = ()
    culprits: tuple[Culprit, ...] = ()
    inert: tuple[Inert, ...] = ()
    unit_problems: tuple[str, ...] = ()

    @property
    def verdict(self) -> str:
        """Read off the *blocks*, not off the totals. Three unknowns and
        three equations that happen to be two blocks -- one short, one
        surplus -- is an underdetermined model whose totals balance
        perfectly, and calling that balanced would be the most misleading
        thing this module could say."""
        if any(group.shortfall > 0 for group in self.groups):
            return UNDERDETERMINED
        return OVERDETERMINED if self.groups else BALANCED

    @property
    def solvable(self) -> bool:
        """Nothing is structurally wrong. Deliberately not "will solve": a
        block whose counts balance can still be singular or inconsistent,
        and only running it can say."""
        return self.verdict != UNDERDETERMINED

    @property
    def summary(self) -> str:
        parts = [
            f"{self.equations} equation{'' if self.equations == 1 else 's'}",
            f"{self.unknowns} unknown{'' if self.unknowns == 1 else 's'}",
        ]
        if self.checks:
            parts.append(f"{self.checks} check{'' if self.checks == 1 else 's'}")
        line = f"{', '.join(parts)} -- {self.verdict}"
        if self.culprits:
            line += f"; {len(self.culprits)} to fix"
        return line


def check(conn: sqlite3.Connection, scenario: str | None = None) -> Health:
    """The model's health, without solving it.

    `scenario` asks the same question about a hypothetical -- what it would
    make determinable and what it would leave missing -- which matters most
    when a scenario changes `freedoms` rather than a value."""
    index = simulation.incidence_index(conn, scenario)
    labels = _labels(conn)
    equations, checks = _count(index)
    return Health(
        equations=equations,
        unknowns=len(index.freedoms),
        checks=checks,
        groups=_groups(index, labels),
        culprits=_culprits(index, labels),
        inert=_inert(index, labels),
        unit_problems=tuple(simulation.check_garden(conn).problems),
    )


def _count(index: incidence_repo.Index) -> tuple[int, int]:
    """`(equations, checks)`. A comparison relation is checked rather than
    solved (`core/simulation.py` splits them before anything is solved), so
    counting one among the equations would make a model look balanced
    because of a line that cannot determine anything.

    A relation that doesn't parse counts as neither: this reports on a
    model, and a broken relation is a problem an editor already showed on
    its own line."""
    equations = checks = 0
    for touch in index.touches:
        try:
            operator = relation_operator(touch.equation)
        except FormulaError:
            continue
        if operator == "==":
            equations += 1
        else:
            checks += 1
    return equations, checks


def _groups(index: incidence_repo.Index, labels: dict[str, str]) -> tuple[Group, ...]:
    """The blocks whose counts do not balance, worst first. A block that
    balances is not news, so it is left out -- this list is meant to be
    short enough to read and act on."""
    groups = [
        Group(
            labels=tuple(_label(v, labels) for v in sorted(block.variables)),
            equations=tuple(touch.equation for touch in block.equations),
            shortfall=block.shortfall,
        )
        for block in index.blocks() if block.shortfall != 0
    ]
    return tuple(sorted(groups, key=lambda g: (-g.shortfall, g.labels)))


def _culprits(index: incidence_repo.Index, labels: dict[str, str]) -> tuple[Culprit, ...]:
    """Every free value nothing can determine, and every value an equation
    reads that nothing holds.

    Both come out of `requires()`, which is where the distinction is
    already drawn: a prerequisite is stored, derived, or missing. The
    culprits are the ones that are neither of the first two."""
    found: dict[Unknown, Culprit] = {}
    for variable in sorted(index.freedoms):
        requirement = index.requires(variable)
        if not requirement.producers:
            found[variable] = Culprit(
                variable, _label(variable, labels),
                "nothing can determine it -- no equation constrains this value",
            )
        for need in requirement.missing:
            found.setdefault(need.variable, Culprit(
                need.variable, _label(need.variable, labels),
                "has no value, and nothing can determine it",
            ))
    return tuple(found[v] for v in sorted(found))


def _inert(index: incidence_repo.Index, labels: dict[str, str]) -> tuple[Inert, ...]:
    return tuple(
        Inert(touch.equation, touch.rule_name, labels.get(touch.anchor_mote_id, touch.anchor_mote_id[:8]))
        for touch in index.touches
        if not touch.determines
    )


def _labels(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        mote.id: str(mote.properties.get("name") or mote.id[:8])
        for mote in motes_repo.list_motes(conn)
    }


def _label(variable: Unknown, labels: dict[str, str]) -> str:
    mote_id, key = variable
    return f"{labels.get(mote_id, mote_id[:8])}.{key}"


def stored_unit(properties: dict, key: str) -> str | None:
    """What a value is actually recorded in, which is the half of "display
    vs stored units" that lives in the garden. The other half is a
    surface's own state; converting between the two is
    `core/units.py::convert`, and nothing here holds a display preference.
    """
    return units_repo.parse_units_property(properties.get(units_repo.UNITS_KEY)).get(key)
