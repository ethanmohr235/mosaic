# core/quantity_literals.py
"""A number with a unit, written inside a formula: `r / 51 cm`.

A bare number in a formula is base SI, which is right for `2 * x` and
wrong for a textbook's `1 - (r/51)^2` with r in cm -- 51 m is not what
the book meant. Writing the unit says what was meant, the same way a
given says it (`p12.d = 109 cm`).

## Spelling

A number, optional spaces, then a unit that **runs to the next space**:
`9.81 m/s^2`, `1.3 kJ/(kg*K)`, `51 cm^2` (51 square centimetres, not 51 cm
squared). Spaces are allowed inside the unit's own brackets (`kJ/(kg K)`),
and a unit also stops at a `,` or an unmatched `)`, so `(r / 51 cm)^2` and
`phi(5 cm, 2 s)` read as meant. The quantity is one atom: `r / 51 cm` is
`r / (51 cm)`.

A number followed by a word is a syntax error in the expression language
otherwise, so no formula that parsed before means anything different now.

## How it reaches the walkers

`rewrite` turns each quantity into a call of the reserved `__unit__`
(`__unit__(51, 'cm')`) before the text is parsed, so every walker --
the evaluator, the unit check, the LaTeX renderer, shown work -- sees an
ordinary `ast.Call` and their drift tests make each handle it. Stored
equation text is never rewritten; it keeps what the person typed.

`spans` gives where each quantity sits in the typed text, for the two
places that rewrite formula text by regex (the worksheet qualifying bare
names, shown work substituting values) and must not reach into `2 m`
because a property happens to be called `m`.
"""
import ast
import copy
import keyword
import re
from dataclasses import dataclass

from core.units import UnitError, format_unit, is_unit, unit

QUANTITY_CALL = "__unit__"

# A number that starts a token: not the `2` of `x2`, nor the `12` of `p12.r`.
_NUMBER_RE = re.compile(r"(?<![\w.])(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?")
_SPACES_RE = re.compile(r"[ \t]*")
# The leading name of a unit: what the compound grammar reads as an atom.
_WORD_RE = re.compile(r"(?:[^\W\d_]|[°%])+")
_UNIT_PUNCTUATION = set("°%·*/^-._") | set("⁰¹²³⁴⁵⁶⁷⁸⁹⁻")


class QuantityError(ValueError):
    """A number followed by something that should have been a unit."""


@dataclass(frozen=True)
class Quantity:
    """One `51 cm` in a formula: where it sits, and what it says."""
    start: int
    end: int
    number: str
    unit: str

    @property
    def call(self) -> str:
        return f"{QUANTITY_CALL}({self.number}, {self.unit!r})"


def rewrite(text: str) -> str:
    """`text` with each quantity written as a `__unit__` call. Raises
    QuantityError for a number followed by a word that is not a unit."""
    out, at = [], 0
    for found in _scan(text, strict=True):
        out.append(text[at:found.start])
        out.append(found.call)
        at = found.end
    out.append(text[at:])
    return "".join(out)


def spans(text: str) -> list[tuple[int, int]]:
    """Where each quantity sits in `text`. Never raises: a text that does
    not parse simply has the quantities found before the fault."""
    try:
        return [(q.start, q.end) for q in _scan(text, strict=False)]
    except QuantityError:
        return []


def outside(spans_: list[tuple[int, int]], index: int) -> bool:
    """Whether `index` falls outside every span -- for a regex rewrite
    that must leave a quantity's unit alone."""
    return not any(start <= index < end for start, end in spans_)


def _scan(text: str, strict: bool) -> list[Quantity]:
    found: list[Quantity] = []
    quoted = _quoted_ranges(text)
    for match in _NUMBER_RE.finditer(text):
        if not outside(quoted, match.start()):
            continue
        if found and match.start() < found[-1].end:
            continue  # a digit inside the last quantity's unit (`s^-1`)
        quantity = _quantity_at(text, match, strict)
        if quantity is not None:
            found.append(quantity)
    return found


def _quoted_ranges(text: str) -> list[tuple[int, int]]:
    """The string literals (`targets('contains')`), which hold no numbers."""
    ranges, quote, start = [], None, 0
    for index, char in enumerate(text):
        if quote and char == quote:
            ranges.append((start, index + 1))
            quote = None
        elif not quote and char in "'\"":
            quote, start = char, index
    return ranges


def _quantity_at(text: str, number: re.Match, strict: bool) -> Quantity | None:
    """The quantity a number starts, or None when no unit follows it."""
    begin = _SPACES_RE.match(text, number.end()).end()
    word = _WORD_RE.match(text, begin)
    if word is None:
        return None
    if word.end() < len(text) and text[word.end()] == "(":
        return None  # a call (`min(`), not a unit
    end = _unit_end(text, begin)
    run = text[begin:end]
    if is_unit(run):
        return Quantity(number.start(), end, number.group(0), run)
    if not strict or keyword.iskeyword(word.group(0)):
        return None
    if is_unit(word.group(0)):
        raise QuantityError(
            f"`{run}` is not a unit -- a unit runs to the next space, so put a space "
            f"after `{number.group(0)} {word.group(0)}`"
        )
    raise QuantityError(
        f"`{word.group(0)}` after {number.group(0)} is not a recognized unit -- "
        f"for a product, write `{number.group(0)} * {word.group(0)}`"
    )


def _unit_end(text: str, index: int) -> int:
    """Where a unit starting at `index` ends: the next space outside its
    own brackets, a `,`, an unmatched `)`, or anything no unit contains."""
    depth = 0
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            if depth == 0:
                break
            depth -= 1
        elif char.isspace():
            if depth == 0:
                break
        elif not (char.isalnum() or char in _UNIT_PUNCTUATION):
            break
        index += 1
    return index


# ---- reading a quantity back off a parsed tree ----


def parts(node: ast.AST) -> tuple[float, str] | None:
    """`(51, 'cm')` for a `__unit__` call, or None for any other node."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id == QUANTITY_CALL):
        return None
    args = node.args
    if (len(args) != 2 or node.keywords
            or not all(isinstance(a, ast.Constant) for a in args)
            or isinstance(args[0].value, bool)
            or not isinstance(args[0].value, (int, float))
            or not isinstance(args[1].value, str)):
        raise QuantityError(f"{QUANTITY_CALL}() is internal -- write a quantity as `51 cm`")
    return args[0].value, args[1].value


def is_affine(unit_name: str) -> bool:
    """A unit with an offset (`degC`), whose negation is not a sign flip."""
    try:
        return unit(unit_name).offset != 0
    except UnitError:
        return False


def number_text(value: float) -> str:
    """A quantity's number as typed, near enough: `51`, `1.3`, `1e-05`."""
    return format(value, ".12g")


def text(value: float, unit_name: str) -> str:
    """How a quantity reads to a person: `51 cm`, `400 °C`."""
    return f"{number_text(value)} {format_unit(unit_name)}"


def unparse(node: ast.AST) -> str:
    """A tree as formula text: `^` for powers and each quantity bracketed
    as typed, `(51 cm)` -- bracketed so `(51 cm)^2` cannot read back as
    51 cm²."""
    tree = copy.deepcopy(node)
    written: dict[str, str] = {}
    for parent in ast.walk(tree):
        for field, value in ast.iter_fields(parent):
            replaced = [_placeholder(v, written) for v in value] if isinstance(value, list) \
                else _placeholder(value, written)
            setattr(parent, field, replaced)
    out = ast.unparse(tree).replace("**", "^")
    for placeholder, quantity in written.items():
        out = out.replace(placeholder, quantity)
    return out


def _placeholder(node, written: dict[str, str]):
    found = parts(node) if isinstance(node, ast.AST) else None
    if found is None:
        return node
    name = f"__quantity_text{len(written)}__"
    value, unit_name = found
    written[name] = f"({number_text(value)} {unit_name})"
    return ast.Name(name, ast.Load())
