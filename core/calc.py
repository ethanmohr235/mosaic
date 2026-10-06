# core/calc.py
"""What an engineering document says about a finished calculation.

A calc package has the same five sections whoever writes it: what was
given, what was assumed, the work, the answers, and the checks. This is
where each of those comes from, so `\\given`, `\\assumptions`, `\\work`,
`\\answers` and `\\checks` in a note (`core/modoc.py`) are *lookups* rather
than prose somebody retyped -- which is the whole promise the format
makes, kept for a whole section rather than for one number.

Nothing here computes anything the engine has not already computed.
`\\work` is a rendering of `core/derivations.py`'s explanation of a live
solve of the garden -- the same one `why` reads, so a document and a
debugger cannot come to disagree about how a value came out. `\\checks` is
`core/simulation.py::check_constraints`. The givens and answers are the
garden's own properties, split by whether the solver is free to determine
them.

## Substituted values are in declared units

`\\work` shows the equation, then the equation with values in it, then the
result. Those values are shown **in the unit each mote declares**, not in
the base SI the solver worked in: somebody who wrote `80 in` has to see
`80`, and the units written beside the numbers are what makes the line
correct. The cost is that the arithmetic does not always multiply out as
written -- `4.7 kohm * 3 mA` is `14.1 V`, which no reader can check by
eye.

So where **one line mixes units of the same dimension**, they are all
converted to one unit and the line says which (`SUBSTITUTED` carries a
`note`). That keeps every line either directly checkable or explicitly
labelled, and never silently wrong.

## `source` says where a value came from

A given may carry its provenance in the reserved `source` property -- a
per-property map, exactly the shape `units` has:

    source: {"x": "assumed", "sigma_fea": "external"}
    source: {"x": {"kind": "assumed", "note": "neglect self-weight"}}

`assumed` is what `\\assumptions` lists. `external` is a value that came
from somewhere outside the garden (an FEA probe, a measurement), which is
what the V&V table is built on. Both spellings of an entry are accepted
for the same reason `roles` accepts two: the short one is what somebody
types, and the long one is what they need the moment a value deserves a
sentence.
"""
import dataclasses
import json
import math
import re
import sqlite3
from dataclasses import dataclass

from core import derivations as derivations_repo
from core import motes as motes_repo
from core import simulation
from core import uncertainty as uncertainty_repo
from core import units as units_repo
from core import worksheet as worksheet_repo
from core.units import UnitError

SOURCE_KEY = "source"

# Where a value came from, when it says.
ASSUMED = "assumed"
EXTERNAL = "external"
MEASURED = "measured"
FITTED = "fitted"  # written by `fit … --apply` (core/fit.py)
SOURCES = (ASSUMED, EXTERNAL, MEASURED, FITTED)

# The three lines of shown work.
SYMBOLIC = "symbolic"
SUBSTITUTED = "substituted"
RESULT = "result"


@dataclass(frozen=True)
class Source:
    """Where one value came from. `note` is the sentence a reader needs
    when the bare word is not enough."""
    kind: str
    note: str = ""
    # For an external value: the key on the same mote holding the hand
    # calculation it verifies (`sigma_fea` checks `sigma`). Empty for one
    # that verifies nothing, which the V&V table then leaves out.
    checks: str = ""


def parse_source_property(value) -> dict[str, Source]:
    """A mote's `source` map, read forgivingly.

    Same discipline `core/units.py::parse_units_property` follows: a
    malformed map leaves the mote unannotated rather than breaking the
    document that reads it. An unrecognized kind is *kept* rather than
    dropped, though -- somebody writing `source: {"x": "handbook"}` means
    something by it, and `\\given` can show it even if `\\assumptions`
    does not claim it."""
    if not isinstance(value, dict):
        return {}
    found: dict[str, Source] = {}
    for key, entry in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(entry, str) and entry.strip():
            found[key] = Source(entry.strip())
        elif isinstance(entry, dict) and isinstance(entry.get("kind"), str):
            note = entry.get("note")
            checks = entry.get("checks")
            found[key] = Source(
                entry["kind"].strip(),
                note if isinstance(note, str) else "",
                checks.strip() if isinstance(checks, str) else "",
            )
    return found


# ---- a value, and how it reads ----


@dataclass(frozen=True)
class Quantity:
    """A number and the unit it is recorded in. `None` for a property no
    mote holds yet, which reads as an empty cell rather than as a zero.

    `sigma` is its ± in the same unit, where it has one: a given's own
    (`uncertainty`), or a result's as `core/uncertainty.py` propagated it."""
    value: object = None
    unit: str | None = None
    sigma: float | None = None

    @property
    def numeric(self) -> bool:
        return isinstance(self.value, (int, float)) and not isinstance(self.value, bool)

    def converted(self, unit: str | None) -> "Quantity":
        """This quantity in `unit`. Unchanged when either unit is missing,
        when they match, or when the conversion is not possible -- a
        document asking for inches from a mass must show the mass rather
        than nothing at all."""
        if not unit or not self.unit or unit == self.unit or not self.numeric:
            return self
        try:
            value = units_repo.convert(float(self.value), self.unit, unit)
            # A spread scales and never shifts: ± 1 °C is ± 1 K, not ± 274.
            sigma = None if self.sigma is None else abs(
                units_repo.convert(self.sigma, self.unit, unit)
                - units_repo.convert(0.0, self.unit, unit))
            return Quantity(value, unit, sigma)
        except UnitError:
            return self

    def text(self, figures: int | None = None) -> str:
        """The number with its unit after it, rounded to `figures`
        significant figures if asked."""
        if self.value is None:
            return ""
        shown = round_to(self.value, figures) if self.numeric and figures else self.value
        text = format_value(shown) if not self.numeric else _number(shown, figures)
        if self.numeric and self.sigma is not None:
            # A ± to two significant figures, whatever the value is quoted
            # to: more would claim to know the uncertainty's uncertainty.
            text = f"{text} ± {_number(self.sigma, _SIGMA_FIGURES) if self.sigma else '0'}"
        if self.unit and self.unit != "1" and self.numeric:
            return f"{text} {units_repo.format_unit(self.unit)}"
        return text


def format_value(value: object) -> str:
    """One reading of a property for every surface. Numbers are rounded to
    six significant figures and switch to scientific notation outside a
    plain range -- `%g`'s own rule, which also strips a solved integral
    float's trailing `.0` for free ("12.0 tasks are closed" reads like a
    bug) -- so a unit conversion or a solved value doesn't render as fifteen
    digits of float noise.

    Six figures is the *default* precision, not a claim about the value: a
    document quoting an answer to three figures says so
    (`\\val{bd.l}{3}`), and `_number()` below keeps the trailing zeros that
    makes significant."""
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


def round_to(value, figures: int | None):
    """`value` to `figures` significant figures.

    Significant figures rather than decimal places, because that is what an
    engineering answer is quoted to: 3 s.f. of 0.00123456 is 0.00123, and
    of 123456 is 123000. `%g` already rounds that way, so this is a round
    trip through it rather than a hand-rolled logarithm."""
    if figures is None or figures < 1:
        return value
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return value
    if value == 0:
        return 0
    return float(f"{float(value):.{figures}g}")


def _number(value, figures: int | None) -> str:
    """A number as a document should print it, to `figures` significant
    figures, **keeping the trailing zeros**.

    `%g` would not: `f"{2.5:.3g}"` is `2.5`, which quietly quotes two
    figures where three were claimed. In an engineering document that third
    digit is the claim -- `2.50` says the hundredths place was measured and
    `2.5` says it was not -- so the figure count is honoured by choosing the
    number of *decimals* from the exponent rather than by trusting `%g` to
    keep the width.

    Which form to use is a **wider** band than `%g`'s: `f"{2032:.3g}"` is
    `2.03e+03`, and `2030 mm` is what belongs in a document. `%g` switches
    to scientific as soon as the exponent reaches the figure count, which
    for three figures means every number over a thousand. Rounding first
    and then choosing the decimals lets fixed notation express the rounding
    honestly -- `2030` has three significant figures and a placeholder
    zero, which is exactly what was meant."""
    if figures is None or figures < 1 or not isinstance(value, (int, float)):
        return format_value(value)
    number = float(round_to(value, figures))
    if number == 0:
        return "0"
    exponent = int(f"{abs(number):e}".split("e")[1])
    if -4 <= exponent < _FIXED_LIMIT:
        return f"{number:.{max(figures - 1 - exponent, 0)}f}"
    mantissa, _, power = f"{number:.{figures - 1}e}".partition("e")
    return f"{mantissa}e{int(power)}"


# Past ten million a number reads better in scientific notation than as a
# row of digits; below a ten-thousandth, likewise. Between the two, fixed.
_FIXED_LIMIT = 7
_SIGMA_FIGURES = 2


def quantity(properties: dict, key: str) -> Quantity:
    """A property as a Quantity, with the ± its own `uncertainty` entry
    declares. A result's propagated ± is added by the callers that asked
    for one (`answers`), since finding it costs solves."""
    unit = units_repo.parse_units_property(properties.get(units_repo.UNITS_KEY)).get(key)
    return Quantity(properties.get(key), unit, uncertainty_repo.sigma_of(properties, key))


# ---- the sections ----


@dataclass(frozen=True)
class Row:
    """One line of a given, answer or assumption table."""
    mote_id: str
    mote: str
    key: str
    quantity: Quantity
    source: Source | None = None

    @property
    def label(self) -> str:
        return f"{self.mote}.{self.key}"

    def text(self, figures: int | None = None) -> str:
        return self.quantity.text(figures)


def givens(conn: sqlite3.Connection) -> tuple[Row, ...]:
    """Every number the garden's equations read and are not free to
    determine, by mote then key.

    Scoped to the motes the equations actually mention, which is what makes
    this a *calculation's* givens rather than a dump of the garden: a
    homework mote's `estimate: 3` is a real property and not an input to
    anything. Same rule `core/worksheet.py` renders its given lines from,
    so a document and a worksheet agree about what counts as given."""
    return _rows(conn, free=False)


def answers(conn: sqlite3.Connection) -> tuple[Row, ...]:
    """Every value the solver is free to determine, with what it currently
    holds. A freedom with no value yet comes back with an empty quantity
    rather than being hidden -- an answer nobody has computed is exactly
    what a reader needs to notice.

    Where any given carries a ±, each answer carries its propagated one
    (`core/uncertainty.py`). A hand-written `uncertainty` entry on a
    freedom is never shown: it is not what the ± is."""
    rows = _rows(conn, free=True)
    if not uncertainty_repo.has_uncertainty(conn):
        return rows
    report = uncertainty_repo.propagate(conn)
    return tuple(
        dataclasses.replace(r, quantity=dataclasses.replace(
            r.quantity, sigma=_propagated(report, r.mote_id, r.key)))
        for r in rows
    )


def _propagated(report, mote_id: str, key: str) -> float | None:
    found = report.of(mote_id, key)
    return None if found is None else found.sigma


def assumptions(conn: sqlite3.Connection) -> tuple[Row, ...]:
    """Every value marked `source: assumed`.

    Not scoped to the equations, unlike the givens: an assumption may be
    about something no equation touches ("neglect self-weight"), and
    leaving it out of the document because nothing computed with it would
    defeat the point of writing it down."""
    return tuple(
        row for row in _all_rows(conn)
        if row.source is not None and row.source.kind == ASSUMED
    )


def external(conn: sqlite3.Connection) -> tuple[Row, ...]:
    """Every value marked `source: external` -- an FEA probe, a
    measurement. What the V&V table is built from."""
    return tuple(
        row for row in _all_rows(conn)
        if row.source is not None and row.source.kind in (EXTERNAL, MEASURED)
    )


def _rows(conn: sqlite3.Connection, free: bool) -> tuple[Row, ...]:
    referenced = worksheet_repo.calculation_mote_ids(conn)
    freedoms = simulation.garden_freedoms(conn)
    return tuple(
        row for row in _all_rows(conn)
        if row.mote_id in referenced
        and ((row.key in freedoms.get(row.mote_id, set())) is free)
    )


def _all_rows(conn: sqlite3.Connection) -> tuple[Row, ...]:
    """Every mote's own quantities, ordered by mote name then key, with a
    freedom's missing value included.

    A quantity is a number, and that one filter is also what keeps the
    machinery out: every reserved property holds a map or a string
    (`units`, `equation`, `freedoms`, `source`, `name`...), so none of them
    is a quantity and none has to be named here. Checking
    `RESERVED_PROPERTIES` explicitly would say the same thing twice and
    would make this module import the one that imports it."""
    rows = []
    freedoms = simulation.garden_freedoms(conn)
    for mote in motes_repo.list_motes(conn):
        name = _name(mote.properties, mote.id)
        sources = parse_source_property(mote.properties.get(SOURCE_KEY))
        for key in sorted(set(mote.properties) | freedoms.get(mote.id, set())):
            value = quantity(mote.properties, key)
            if value.value is not None and not value.numeric:
                continue
            rows.append(Row(mote.id, name, key, value, sources.get(key)))
    return tuple(sorted(rows, key=lambda r: (r.mote.casefold(), r.key)))


def _name(properties: dict, mote_id: str) -> str:
    name = properties.get("name")
    return name if isinstance(name, str) and name else mote_id[:8]


def checks(conn: sqlite3.Connection, scenario: str | None = None) -> tuple:
    """Every comparison relation, with whether the garden's values satisfy
    it -- `core/simulation.py::check_constraints` unchanged. Here so a note
    reaches one module for all five sections rather than three."""
    return tuple(simulation.check_constraints(conn, scenario))


# ---- verification: hand vs. external ----


@dataclass(frozen=True)
class Comparison:
    """One row of a V&V table: a hand-calculated value beside the external
    value that verifies it, and how far apart they are.

    `external` is converted into the hand value's unit so the two columns
    read against each other. `error` is the percentage by which the hand
    value differs from the external one, *relative to the external* --
    the external value (an FEA probe, a measurement) is the reference the
    hand calc is being verified against. None where it cannot be
    computed, and `problem` says why."""
    mote_id: str
    mote: str
    key: str
    external_key: str
    hand: Quantity
    external: Quantity
    note: str = ""
    error: float | None = None
    problem: str = ""

    @property
    def label(self) -> str:
        return f"{self.mote}.{self.key}"


def verification(conn: sqlite3.Connection) -> tuple[Comparison, ...]:
    """Every external value that names the hand value it verifies.

    Paired through the external value's own `source` entry, which is
    where the pairing was decided to live: `bd.sigma_fea` carrying
    `source: {sigma_fea: {kind: external, note: "FEA probe", checks:
    "sigma"}}` sits beside `bd.sigma` in the table. An external value that
    names nothing is not a comparison, and is left to `\given`'s
    listing of where values came from."""
    found = []
    for mote in motes_repo.list_motes(conn):
        name = _name(mote.properties, mote.id)
        for external_key, source in parse_source_property(
            mote.properties.get(SOURCE_KEY)
        ).items():
            if source.kind not in (EXTERNAL, MEASURED) or not source.checks:
                continue
            found.append(_compare(
                mote.id, name, source.checks, external_key, source.note,
                quantity(mote.properties, source.checks),
                quantity(mote.properties, external_key),
            ))
    return tuple(sorted(found, key=lambda c: (c.mote.casefold(), c.key, c.external_key)))


def _compare(
    mote_id: str, mote: str, key: str, external_key: str, note: str,
    hand: Quantity, external: Quantity,
) -> Comparison:
    def made(problem: str = "", error: float | None = None, shown: Quantity = external):
        return Comparison(mote_id, mote, key, external_key, hand, shown, note, error, problem)

    if not hand.numeric:
        return made(f"{mote}.{key} holds no number yet -- solve first")
    if not external.numeric:
        return made(f"{mote}.{external_key} holds no number")
    if bool(hand.unit) != bool(external.unit) and "1" not in (hand.unit, external.unit):
        return made(f"one of {key} and {external_key} has a unit and the other does not")
    shown = external
    if hand.unit and external.unit and hand.unit != external.unit:
        try:
            shown = Quantity(
                units_repo.convert(float(external.value), external.unit, hand.unit), hand.unit)
        except UnitError as e:
            return made(str(e))
    if shown.value == 0:
        return made("the external value is zero, so a percentage error is undefined",
                    shown=shown)
    error = (float(hand.value) - float(shown.value)) / abs(float(shown.value)) * 100.0
    return made(error=error, shown=shown)


# ---- shown work ----


@dataclass(frozen=True)
class Step:
    """One line of shown work. `note` says what was done to the numbers,
    where anything was."""
    kind: str
    text: str
    note: str = ""


@dataclass(frozen=True)
class Work:
    """A value, and the three lines that show how it came out.

    `note` set with no steps is a complete answer rather than a failure:
    a stored value has no work to show, and saying so is the honest
    result."""
    label: str
    quantity: Quantity
    steps: tuple[Step, ...] = ()
    also_determined: tuple[str, ...] = ()
    note: str | None = None
    error: str | None = None

    @property
    def shown(self) -> bool:
        return bool(self.steps)


def work(
    conn: sqlite3.Connection,
    mote_id: str,
    key: str,
    unit: str | None = None,
    figures: int | None = None,
    explainer: derivations_repo.Explainer | None = None,
) -> Work:
    """How `key` on `mote_id` came out, as lines a document can print.

    Explained from a live solve of the garden as it stands
    (`core/derivations.py`), the same thing `why` reads -- so the document
    and the debugger explain one value one way, and neither can be older
    than the last edit. Pass one `explainer` for a whole document and it
    solves once. A value no solve determines has no work to show and says
    so. Never raises: a note must render with the broken part visibly
    broken."""
    properties = {m.id: m.properties for m in motes_repo.list_motes(conn)}
    if mote_id not in properties:
        return Work(f"{mote_id[:8]}.{key}", Quantity(), error=f"no mote {mote_id[:8]!r}")

    names = {mid: _name(props, mid) for mid, props in properties.items()}
    label = f"{names[mote_id]}.{key}"
    stored = quantity(properties[mote_id], key)

    explanation = (explainer or derivations_repo.Explainer(conn)).explain(mote_id, key, depth=1)
    if not explanation.derived:
        return Work(label, stored.converted(unit),
                    note=explanation.note or "not determined by a solve")

    shown = _live(stored, explanation).converted(unit)
    steps, note = _steps(explanation, properties, names, shown, figures)
    return Work(
        label, shown, steps,
        tuple(f"{names.get(m, m[:8])}.{k}" for m, k in explanation.also_determined),
        note=note,
    )


def _live(stored: Quantity, explanation) -> Quantity:
    """The result a solve of the garden as it stands finds. The stored
    number is shown when it agrees (so a solved garden reads exactly as it
    always did); when a given was edited and nobody re-solved, the stored
    answer is stale and the document would contradict its own
    substitution, so the live one is shown instead."""
    live = _from_base(explanation.value, stored.unit) if stored.unit \
        else Quantity(explanation.value, None)
    if stored.numeric and math.isclose(float(stored.value), float(live.value),
                                       rel_tol=1e-9, abs_tol=1e-12):
        return stored
    return Quantity(live.value, live.unit)


def _steps(
    explanation, properties: dict[str, dict], names: dict[str, str],
    shown: Quantity, figures: int | None,
) -> tuple[tuple[Step, ...], str | None]:
    """The symbolic line, the substituted line, and the result.

    A solver block determined several values from several equations at
    once, so it gets one symbolic line per equation and no substituted one
    -- there is no single expression to put numbers into, and inventing one
    would imply an attribution the solve never made."""
    equations = tuple(e.equation for e in explanation.equations)
    steps = [Step(SYMBOLIC, equation) for equation in equations]
    note = None
    if len(equations) == 1:
        substituted, note = _substitute(
            equations[0], explanation, properties, names, shown
        )
        if substituted:
            steps.append(Step(SUBSTITUTED, substituted, note or ""))
    else:
        note = (
            f"solved together with {len(equations)} equations, so there is no "
            f"one expression to substitute into"
        )
    steps.append(Step(RESULT, shown.text(figures)))
    return tuple(steps), note


def _substitute(
    equation: str, explanation, properties: dict[str, dict], names: dict[str, str],
    shown: Quantity,
) -> tuple[str, str | None]:
    """`equation` with each value the solve read written in its place.

    Substituted **as it was written**: a reference spelled `bd.x` is
    replaced where `bd.x` appears, and a bare `x` where `x` does. That
    keeps this to a text rewrite over names the AST already found, rather
    than a second resolver that would have to agree with
    core/formula.py's three scopes about which mote a name means.

    A reference the solve did not record as a read is left alone -- the
    freedom being solved for is one, and leaving `l` standing on the left
    of `l == sqrt(...)` is exactly right."""
    reads = _reads(explanation, properties, names)
    if not reads:
        return "", None
    converted, note = _reconcile(reads, shown)
    pattern = _reference_pattern(converted)
    if pattern is None:
        return "", None
    # Deliberately not `figures`: a figure count is a claim about the
    # *answer's* precision, and padding a given to it would claim precision
    # the given never stated. `80 in` quoted to four figures is still
    # `80 in`; only the result line becomes `100.0 in`.
    return pattern.sub(
        lambda m: _bracketed(converted[m.group(0)].text()), equation
    ), note


def _bracketed(text: str) -> str:
    """A substituted quantity in parentheses where it needs them.

    `sqrt(bd.x^2)` becoming `sqrt(80 in^2)` would read as eighty square
    inches -- the exponent has to bind to the whole quantity, so it gets
    written `(80 in)^2`, which is how it would be written by hand anyway. A
    negative number is bracketed for the same reason a unit is."""
    if not text:
        return text
    return f"({text})" if " " in text or text.startswith("-") else text


def _reads(explanation, properties: dict[str, dict], names: dict[str, str]) -> dict[str, Quantity]:
    """Each value the solve read, keyed by every spelling that could name
    it in the equation: `bd.x` and, when the read is on the anchor, a bare
    `x`.

    The recorded value is in base SI (`core/derivations.py`), so it is
    converted back into the unit its mote declares -- which is the whole
    reason this cannot just print what the record holds."""
    reads: dict[str, Quantity] = {}
    for read in explanation.inputs:
        declared = quantity(properties.get(read.mote_id, {}), read.key)
        value = _from_base(read.value, declared.unit) if declared.unit \
            else Quantity(read.value, None)
        name = names.get(read.mote_id)
        spellings = ((f"{name}.{read.key}",) if name else ()) + (read.key,)
        for spelling in spellings:
            reads.setdefault(spelling, value)
    return reads


def _from_base(value: float, unit: str) -> Quantity:
    """A recorded base-SI number back in the unit its mote declares."""
    try:
        return Quantity(units_repo.from_base(float(value), unit), unit)
    except (UnitError, TypeError, ValueError):
        return Quantity(value, None)


def _reconcile(
    reads: dict[str, Quantity], shown: Quantity
) -> tuple[dict[str, Quantity], str | None]:
    """One unit per dimension in this line, converting where it mixes.

    Declared units are what a reader wants to see, but they do not multiply
    out: `4.7 kohm * 3 mA` is `14.1 V` and no reader can check that by eye.
    Where one line mixes units of the same dimension, they are all brought
    to one -- the result's, when the result shares that dimension, else the
    first the line mentions -- and the line says so. Every line is then
    either directly checkable or explicitly labelled."""
    by_dimension: dict[tuple, list[str]] = {}
    for spelling, value in reads.items():
        if value.unit and value.numeric:
            try:
                by_dimension.setdefault(
                    units_repo.dimension_of(value.unit), []).append(spelling)
            except UnitError:
                continue

    converted = dict(reads)
    changed: list[str] = []
    for dimension, spellings in by_dimension.items():
        present = {reads[s].unit for s in spellings}
        if len(present) < 2:
            continue
        target = _target_unit(dimension, present, spellings, reads, shown)
        for spelling in spellings:
            converted[spelling] = reads[spelling].converted(target)
        changed.append(units_repo.format_unit(target))
    if not changed:
        return converted, None
    return converted, f"converted to {', '.join(sorted(set(changed)))}"


def _target_unit(dimension, present: set, spellings: list[str], reads, shown: Quantity) -> str:
    if shown.unit and shown.unit in present:
        return shown.unit
    if shown.unit:
        try:
            if units_repo.dimension_of(shown.unit) == dimension:
                return shown.unit
        except UnitError:
            pass
    return reads[spellings[0]].unit


def _reference_pattern(reads: dict[str, Quantity]):
    """One alternation over every spelling, longest first, so `bd.x` is
    matched before the bare `x` inside it. The lookarounds keep `x` from
    matching inside `max` or after a dot it does not own."""
    spellings = sorted(reads, key=len, reverse=True)
    if not spellings:
        return None
    body = "|".join(re.escape(s) for s in spellings)
    return re.compile(rf"(?<![\w.])(?:{body})(?![\w.])")
