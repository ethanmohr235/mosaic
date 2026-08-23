# core/units.py
"""Optional units for numerical properties.

A mote may carry a `units` property -- a dict mapping property key to
unit name (`{"resistance": "kohm"}`) -- declaring what its own numbers
mean. Units are opt-in per property: a key with no entry is simply
unitless, and a garden that never sets `units` behaves exactly as it did
before this module existed.

This module is pure: no database, no formula, no simulation. It knows
units and dimensions and nothing else, the same way core/providers.py
knows nothing about the mote graph. Units are registered explicitly at
import time -- never parsed as arbitrary expressions from a database
string -- so naming a unit can't make MonOSS interpret something it
wasn't built to.

See docs/units_proposal.
"""
from dataclasses import dataclass

# The reserved property key holding a mote's per-property unit map.
UNITS_KEY = "units"

# Exponents are rounded to this many places so accumulated float error in
# a chain of multiplications can't make two genuinely equal dimensions
# compare unequal.
_EXPONENT_PLACES = 9


class UnitError(ValueError):
    """A unit could not be recognized, or was used incompatibly."""


class UnknownUnit(UnitError):
    """No unit is registered under the requested name."""


class IncompatibleUnits(UnitError):
    """Two units measure different physical dimensions, so no conversion
    between them exists."""


@dataclass(frozen=True)
class Dimension:
    """Exponents over the SI base dimensions this system tracks.

    Five axes, not seven: amount-of-substance and luminous intensity have
    no use case here yet, and adding them later is two more fields rather
    than a redesign. Exponents are floats because `**` in a formula
    accepts a fractional constant exponent.
    """
    length: float = 0.0
    mass: float = 0.0
    time: float = 0.0
    current: float = 0.0
    temperature: float = 0.0

    def __post_init__(self):
        for field_name in _AXES:
            object.__setattr__(
                self, field_name, round(float(getattr(self, field_name)), _EXPONENT_PLACES)
            )

    def __add__(self, other: "Dimension") -> "Dimension":
        """Multiplying two quantities adds their dimensions."""
        return Dimension(*(getattr(self, a) + getattr(other, a) for a in _AXES))

    def __sub__(self, other: "Dimension") -> "Dimension":
        """Dividing two quantities subtracts their dimensions."""
        return Dimension(*(getattr(self, a) - getattr(other, a) for a in _AXES))

    def __mul__(self, factor: float) -> "Dimension":
        """Raising a quantity to a power scales its dimension."""
        return Dimension(*(getattr(self, a) * factor for a in _AXES))

    @property
    def is_dimensionless(self) -> bool:
        return all(getattr(self, a) == 0.0 for a in _AXES)

    @property
    def symbol(self) -> str:
        """A readable rendering for error messages: `kg·m²·s⁻³·A⁻¹`."""
        parts = [
            f"{_AXIS_SYMBOLS[a]}{_exponent(getattr(self, a))}"
            for a in _AXES
            if getattr(self, a) != 0.0
        ]
        return "·".join(parts) if parts else "dimensionless"


_AXES = ("length", "mass", "time", "current", "temperature")
_AXIS_SYMBOLS = {
    "length": "m", "mass": "kg", "time": "s", "current": "A", "temperature": "K",
}
_SUPERSCRIPTS = {
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹", "-": "⁻", ".": "·",
}

DIMENSIONLESS = Dimension()


def _exponent(value: float) -> str:
    """Superscript for an exponent, omitted entirely when it's 1."""
    if value == 1.0:
        return ""
    text = str(int(value)) if value == int(value) else str(value)
    return "".join(_SUPERSCRIPTS.get(c, c) for c in text)


@dataclass(frozen=True)
class Unit:
    """`value_in_base = value * scale + offset`.

    `offset` exists because not every unit is a simple multiple of its
    base: Celsius and Fahrenheit are affine in Kelvin, and treating them
    as if they scaled from zero would quietly produce wrong numbers
    rather than an error.
    """
    name: str
    dimension: Dimension
    scale: float = 1.0
    offset: float = 0.0


_UNITS: dict[str, Unit] = {}
_RESOLVED: dict[str, Unit] = {}


def register_unit(
    name: str, dimension: Dimension, scale: float = 1.0, offset: float = 0.0
) -> None:
    """Register one atomic unit. Refuses to overwrite an existing name --
    a collision (e.g. a generated prefix landing on a name that already
    means something else) is a bug to fix at import time, not something
    to resolve silently by last-one-wins."""
    if name in _UNITS:
        raise UnitError(f"unit {name!r} is already registered")
    _UNITS[name] = Unit(name, dimension, scale, offset)


def unit(name: str) -> Unit:
    """Resolve a unit name, which may be a registered atomic unit (`ohm`)
    or a compound expression over them (`m/s`, `kg/m^3`, `J/(kg*K)`).

    Compounds are unavoidable: the property-model capabilities already in
    this codebase declare units like `J/(kg*K)`, and registering every
    useful combination of base units up front is not possible. They are
    parsed by a small dedicated tokenizer (see _parse_compound) rather
    than evaluated, keeping the same "never interpret a database string
    as code" guarantee the provider registry has.
    """
    text = name.strip()
    if text in _UNITS:
        return _UNITS[text]
    if text in _RESOLVED:
        return _RESOLVED[text]
    resolved = _parse_compound(text)
    _RESOLVED[text] = resolved
    return resolved


def is_unit(name: str) -> bool:
    try:
        unit(name)
    except UnitError:
        return False
    return True


def list_units() -> list[str]:
    """Every atomic registered unit. Compounds built from them are
    unbounded, so they aren't (and can't be) enumerated here."""
    return sorted(_UNITS)


def dimension_of(name: str) -> Dimension:
    return unit(name).dimension


def to_base(value: float, name: str) -> float:
    """Convert a value expressed in `name` into its dimension's base
    (SI) unit -- the representation everything downstream computes in."""
    u = unit(name)
    return value * u.scale + u.offset


def from_base(value: float, name: str) -> float:
    """Inverse of to_base(): a base-unit value expressed back in `name`."""
    u = unit(name)
    return (value - u.offset) / u.scale


def convert(value: float, from_name: str, to_name: str) -> float:
    """Convert between two units of the same dimension."""
    source, target = unit(from_name), unit(to_name)
    if source.dimension != target.dimension:
        raise IncompatibleUnits(
            f"cannot convert {from_name!r} ({source.dimension.symbol}) to "
            f"{to_name!r} ({target.dimension.symbol}) -- different dimensions"
        )
    return from_base(to_base(value, from_name), to_name)


# --- Compound unit expressions -------------------------------------------
#
# A deliberately tiny grammar, hand-tokenized rather than run through
# `ast`/`eval` -- the same discipline core/formula.py follows for
# equations, for the same reason:
#
#     expr     := factor (('*' | '·' | '/' | <implicit>) factor)*
#     factor   := atom ('^' exponent)?
#     atom     := identifier | number | '(' expr ')'
#     exponent := ['-'] number   (or a superscript run: m³, s⁻¹)
#
# Multiplication and division associate left to right, so `J/kg*K` reads
# as `(J/kg)*K`. Juxtaposition means multiplication, which is what makes
# `J/(kg K)` work.

_SUPERSCRIPT_VALUES = {v: k for k, v in _SUPERSCRIPTS.items()}
_OPERATORS = {"*", "·", "/", "^", "(", ")"}


def _tokenize(text: str) -> list[str]:
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            tokens.append(" ")
            index += 1
        elif char in _OPERATORS:
            tokens.append(char)
            index += 1
        elif char in _SUPERSCRIPT_VALUES:
            run = ""
            while index < len(text) and text[index] in _SUPERSCRIPT_VALUES:
                run += _SUPERSCRIPT_VALUES[text[index]]
                index += 1
            tokens.extend(["^", run])
        elif char.isdigit() or char == "-":
            run = ""
            while index < len(text) and (text[index].isdigit() or text[index] in ".-"):
                run += text[index]
                index += 1
            tokens.append(run)
        elif char.isalpha() or char == "°":
            run = ""
            while index < len(text) and (text[index].isalpha() or text[index] == "°"):
                run += text[index]
                index += 1
            tokens.append(run)
        else:
            raise UnitError(f"unexpected character {char!r} in unit {text!r}")
    return tokens


def _parse_compound(text: str) -> Unit:
    """Parse a compound unit expression into a single Unit.

    Affine units (degC, degF) are rejected inside a compound: an offset
    is only meaningful for a bare quantity, and silently dropping it --
    treating `degC/s` as `K/s` -- would produce numbers that look
    plausible and are wrong. A bare affine unit never reaches here, since
    unit() finds it in the atomic registry first.
    """
    if not text:
        raise UnitError("empty unit")
    tokens = _tokenize(text)
    parser = _Parser(tokens, text)
    dimension, scale = parser.parse()
    return Unit(text, dimension, scale)


class _Parser:
    def __init__(self, tokens: list[str], source: str):
        self.tokens = tokens
        self.source = source
        self.position = 0

    def parse(self) -> tuple[Dimension, float]:
        result = self._expr()
        self._skip_space()
        if self.position < len(self.tokens):
            raise UnitError(
                f"unexpected {self.tokens[self.position]!r} in unit {self.source!r}"
            )
        return result

    def _skip_space(self) -> None:
        while self.position < len(self.tokens) and self.tokens[self.position] == " ":
            self.position += 1

    def _peek(self) -> str | None:
        self._skip_space()
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _expr(self) -> tuple[Dimension, float]:
        dimension, scale = self._factor()
        while True:
            token = self._peek()
            if token in ("*", "·"):
                self.position += 1
                other, other_scale = self._factor()
                dimension, scale = dimension + other, scale * other_scale
            elif token == "/":
                self.position += 1
                other, other_scale = self._factor()
                dimension, scale = dimension - other, scale / other_scale
            elif token is not None and token != ")":
                # Juxtaposition is multiplication: `J/(kg K)`.
                other, other_scale = self._factor()
                dimension, scale = dimension + other, scale * other_scale
            else:
                return dimension, scale

    def _factor(self) -> tuple[Dimension, float]:
        dimension, scale = self._atom()
        if self._peek() == "^":
            self.position += 1
            exponent = self._number()
            return dimension * exponent, scale ** exponent
        return dimension, scale

    def _atom(self) -> tuple[Dimension, float]:
        token = self._peek()
        if token is None:
            raise UnitError(f"unit {self.source!r} ends unexpectedly")
        if token == "(":
            self.position += 1
            result = self._expr()
            if self._peek() != ")":
                raise UnitError(f"unclosed '(' in unit {self.source!r}")
            self.position += 1
            return result
        self.position += 1
        if token[0].isdigit() or token[0] == "-":
            return DIMENSIONLESS, _to_number(token, self.source)
        if token not in _UNITS:
            raise UnknownUnit(f"no unit registered as {token!r} (in {self.source!r})")
        atomic = _UNITS[token]
        if atomic.offset:
            raise UnitError(
                f"{token!r} is an offset unit and can only be used on its own, "
                f"not combined as in {self.source!r}"
            )
        return atomic.dimension, atomic.scale

    def _number(self) -> float:
        token = self._peek()
        if token is None:
            raise UnitError(f"unit {self.source!r} is missing an exponent")
        self.position += 1
        return _to_number(token, self.source)


def _to_number(token: str, source: str) -> float:
    try:
        return float(token)
    except ValueError:
        raise UnitError(f"{token!r} is not a number in unit {source!r}") from None


def describe(dimension: Dimension) -> str:
    """Name a dimension as readably as possible for an error message --
    the familiar unit symbol where one exists (`V` rather than
    `kg·m²·s⁻³·A⁻¹`), falling back to the base-dimension product."""
    return _CANONICAL.get(dimension, dimension.symbol)


# --- The built-in catalog -------------------------------------------------
#
# Deliberately small and extensible rather than exhaustive. Base SI first,
# then named derived units, then the common non-SI units a real model is
# likely to be written in.

_LENGTH = Dimension(length=1)
_MASS = Dimension(mass=1)
_TIME = Dimension(time=1)
_CURRENT = Dimension(current=1)
_TEMPERATURE = Dimension(temperature=1)

_FORCE = Dimension(mass=1, length=1, time=-2)
_ENERGY = Dimension(mass=1, length=2, time=-2)
_POWER = Dimension(mass=1, length=2, time=-3)
_PRESSURE = Dimension(mass=1, length=-1, time=-2)
_FREQUENCY = Dimension(time=-1)
_VOLTAGE = Dimension(mass=1, length=2, time=-3, current=-1)
_RESISTANCE = Dimension(mass=1, length=2, time=-3, current=-2)
_CHARGE = Dimension(current=1, time=1)
_VOLUME = Dimension(length=3)

# Registered under the names that take SI prefixes below. Mass is the odd
# one out: the SI base unit is the kilogram, but the *prefixable* unit is
# the gram, so `kg` arrives via the prefix loop rather than being
# registered here.
_PREFIXABLE = [
    ("m", _LENGTH, 1.0),
    ("g", _MASS, 0.001),
    ("s", _TIME, 1.0),
    ("A", _CURRENT, 1.0),
    ("K", _TEMPERATURE, 1.0),
    ("N", _FORCE, 1.0),
    ("J", _ENERGY, 1.0),
    ("W", _POWER, 1.0),
    ("Pa", _PRESSURE, 1.0),
    ("Hz", _FREQUENCY, 1.0),
    ("V", _VOLTAGE, 1.0),
    ("ohm", _RESISTANCE, 1.0),
    ("C", _CHARGE, 1.0),
    ("L", _VOLUME, 0.001),
    ("bar", _PRESSURE, 1e5),
]

_PREFIXES = {
    "T": 1e12, "G": 1e9, "M": 1e6, "k": 1e3, "h": 1e2,
    "c": 1e-2, "m": 1e-3, "u": 1e-6, "µ": 1e-6, "n": 1e-9, "p": 1e-12,
}

for _name, _dimension, _scale in _PREFIXABLE:
    register_unit(_name, _dimension, _scale)
    for _prefix, _factor in _PREFIXES.items():
        register_unit(_prefix + _name, _dimension, _scale * _factor)

# Non-SI units, registered unprefixed. Prefixing these is what would
# create genuine collisions -- milli-inch would be `min`, which already
# means a minute -- so they deliberately sit outside the loop above.
register_unit("min", _TIME, 60.0)
register_unit("hr", _TIME, 3600.0)
register_unit("day", _TIME, 86400.0)
register_unit("in", _LENGTH, 0.0254)
register_unit("ft", _LENGTH, 0.3048)
register_unit("mi", _LENGTH, 1609.344)
register_unit("lb", _MASS, 0.45359237)
register_unit("atm", _PRESSURE, 101325.0)
register_unit("psi", _PRESSURE, 6894.757293168361)

# Affine temperature units: a degree Celsius is the same *size* as a
# kelvin but starts elsewhere, and Fahrenheit differs in both. These are
# exactly why Unit carries an offset at all.
register_unit("degC", _TEMPERATURE, 1.0, 273.15)
register_unit("degF", _TEMPERATURE, 5.0 / 9.0, 273.15 - 32.0 * 5.0 / 9.0)

_CANONICAL = {
    _LENGTH: "m", _MASS: "kg", _TIME: "s", _CURRENT: "A", _TEMPERATURE: "K",
    _FORCE: "N", _ENERGY: "J", _POWER: "W", _PRESSURE: "Pa",
    _FREQUENCY: "Hz", _VOLTAGE: "V", _RESISTANCE: "ohm", _CHARGE: "C",
    _VOLUME: "m³",
}


def parse_units_property(value) -> dict[str, str]:
    """Read a mote's `units` property into a clean key -> unit-name map.

    Tolerant by design, like core/rules.py: parse_unknowns(): a malformed
    or half-typed `units` value should leave a mote un-annotated, not
    break every simulation that touches it. Unknown unit *names* are not
    filtered here -- that's a real mistake worth reporting where it's
    used, rather than silently dropping.
    """
    if not isinstance(value, dict):
        return {}
    return {
        str(key): v.strip()
        for key, v in value.items()
        if isinstance(v, str) and v.strip()
    }
