# core/formula_tex.py
"""A rule's equation as LaTeX, for KaTeX in the console and for `.tex`.

    to_tex("delta == P * L**3 / (48 * E * I)")
    -> "\\delta = \\frac{P\\,L^{3}}{48\\,E\\,I}"

The equation is parsed by core/formula.py's own parser (so `^` means
what it means there) and walked here against the same call whitelist:
`CALL_TEX` has one entry per name in `formula.ALLOWED_CALLS`, which
tests/test_formula_tex.py's drift test holds to. Nothing is evaluated.

Anything this cannot draw -- a formula that does not parse, a node the
grammar would reject -- comes back as None, and the caller shows the
equation as text the way it always has. A typeset equation is a reading
aid; it never decides whether a formula is valid."""
import ast
import math
import re
from collections.abc import Callable

from core import formula

# Binding strength of what a node renders as. A child weaker than its
# parent needs brackets; a fraction is self-delimiting everywhere except
# as the base of a power.
_ADD, _MUL, _NEG, _POW, _FRAC, _ATOM = 1, 2, 3, 4, 5, 6

GREEK = frozenset({
    "alpha", "beta", "gamma", "delta", "epsilon", "varepsilon", "zeta", "eta", "theta",
    "vartheta", "iota", "kappa", "lambda", "mu", "nu", "xi", "pi", "rho", "sigma", "tau",
    "upsilon", "phi", "varphi", "chi", "psi", "omega",
    "Gamma", "Delta", "Theta", "Lambda", "Xi", "Pi", "Sigma", "Upsilon", "Phi", "Psi", "Omega",
})

_COMPARE_TEX = {ast.Eq: "=", ast.LtE: r"\le", ast.GtE: r"\ge", ast.Lt: "<", ast.Gt: ">"}
CHECK_TEX = r"\stackrel{?}{=}"
_TRAILING_DIGITS = re.compile(r"^([A-Za-z]+)(\d+)$")


class _Unsupported(Exception):
    """Raised inside the walk; to_tex() turns it into None."""


def to_tex(equation: str, check: bool = False) -> str | None:
    """`equation` (a relation or a bare expression) as LaTeX, or None.
    `check` draws `==` as `?=`, the worksheet's spelling of a check."""
    try:
        node = formula._parse_ast(equation)
        if isinstance(node, ast.Compare):
            return _relation(node, check)
        return _expr(node)[0]
    except (formula.FormulaError, _Unsupported, RecursionError):
        return None


def _relation(node: ast.Compare, check: bool) -> str:
    if len(node.ops) != 1 or type(node.ops[0]) not in _COMPARE_TEX:
        raise _Unsupported()
    op = node.ops[0]
    sign = CHECK_TEX if check and isinstance(op, ast.Eq) else _COMPARE_TEX[type(op)]
    return f"{_expr(node.left)[0]} {sign} {_expr(node.comparators[0])[0]}"


def _expr(node: ast.AST) -> tuple[str, int]:
    """(tex, binding strength) for one node."""
    if isinstance(node, ast.Constant):
        return _number(node.value), _ATOM
    if isinstance(node, ast.Name):
        return name_tex(node.id), _ATOM
    if isinstance(node, ast.UnaryOp):
        return _unary(node)
    if isinstance(node, ast.BinOp):
        return _binary(node)
    if isinstance(node, ast.Call):
        return _call(node), _ATOM
    if isinstance(node, ast.Attribute):
        return _attribute(node), _ATOM
    raise _Unsupported()


def _number(value) -> str:
    """As written for a reader: plain between 0.001 and 99999, else
    `m \\times 10^{n}` (`10^{6}` alone when the mantissa is 1)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Unsupported()
    if value == 0 or 1e-3 <= abs(value) < 1e5:
        return f"{value:.12g}"
    exponent = math.floor(math.log10(abs(value)))
    mantissa = f"{value / 10 ** exponent:.12g}"
    power = rf"10^{{{exponent}}}"
    return power if mantissa == "1" else rf"{mantissa} \times {power}"


def _bracket(tex: str) -> str:
    return rf"\left({tex}\right)"


def _side(node: ast.AST, weakest: int) -> str:
    """A child's tex, bracketed when it binds no tighter than `weakest`."""
    tex, strength = _expr(node)
    return _bracket(tex) if strength <= weakest else tex


def _unary(node: ast.UnaryOp) -> tuple[str, int]:
    if isinstance(node.op, ast.UAdd):
        return _expr(node.operand)
    if not isinstance(node.op, ast.USub):
        raise _Unsupported()
    return f"-{_side(node.operand, _ADD)}", _NEG


def _binary(node: ast.BinOp) -> tuple[str, int]:
    op = node.op
    if isinstance(op, ast.Add):
        right = _expr(node.right)
        term = _bracket(right[0]) if right[1] == _NEG else right[0]
        return f"{_side(node.left, 0)} + {term}", _ADD
    if isinstance(op, ast.Sub):
        return f"{_side(node.left, 0)} - {_side(node.right, _ADD)}", _ADD
    if isinstance(op, ast.Mult):
        left, right = _side(node.left, _ADD), _side(node.right, _NEG)
        return f"{left}{_joint(node.left, node.right, right)}{right}", _MUL
    if isinstance(op, ast.Div):
        return rf"\frac{{{_expr(node.left)[0]}}}{{{_expr(node.right)[0]}}}", _FRAC
    if isinstance(op, ast.Pow):
        return f"{_side(node.left, _FRAC)}^{{{_expr(node.right)[0]}}}", _POW
    raise _Unsupported()


def _joint(left: ast.AST, right: ast.AST, right_tex: str) -> str:
    """How a product is written. Juxtaposed with a thin space when both
    factors read as single symbols (`48 E I`, `M c`); a dot when either is
    a word or a dotted reference, where juxtaposition would run the names
    together (`density · n.v`), and before a number."""
    if right_tex[:1].isdigit():
        return r" \cdot "
    if isinstance(left, ast.Constant) and _symbol(right):
        return r"\,"
    return r"\," if _symbol(left) and _symbol(right) else r" \cdot "


def _symbol(node: ast.AST) -> bool:
    """A factor that sets as one letter: a one-letter or Greek name (with
    a subscript or not), a constant, or a power or bracket of one."""
    if isinstance(node, ast.Name):
        base = node.id.partition("_")[0]
        digits = _TRAILING_DIGITS.match(node.id)
        return len(digits.group(1) if digits else base) == 1 or base in GREEK or node.id == formula.TICK_NAME
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Pow):
        return _symbol(node.left)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return _symbol(node.left) and _symbol(node.right)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Div)):
        return True  # bracketed or a fraction: self-delimiting
    return isinstance(node, (ast.Constant, ast.Call))


def name_tex(identifier: str) -> str:
    """A variable name: Greek spelled out becomes the letter, `a_b` and
    `R1` become subscripts, and a longer word is set upright-italic as one
    symbol rather than read as a product of letters."""
    if identifier == formula.TICK_NAME:
        return "t"
    base, _, sub = identifier.partition("_")
    if not sub:
        digits = _TRAILING_DIGITS.match(identifier)
        if digits:
            base, sub = digits.groups()
    tex = _word(base)
    return f"{tex}_{{{_word(sub, upright=True)}}}" if sub else tex


def _word(word: str, upright: bool = False) -> str:
    if word in GREEK:
        return f"\\{word}"
    if len(word) <= 1 or word.isdigit():
        return word
    return rf"\mathrm{{{word}}}" if upright else rf"\mathit{{{word}}}"


def _attribute(node: ast.Attribute) -> str:
    """`bd.l`, a role's or a named mote's property, and
    `targets('a').v` -- the mote set read upright, the property as a
    variable."""
    return f"{_owner(node.value)}.{name_tex(node.attr)}"


def _owner(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return rf"\mathrm{{{_escape(node.id)}}}"
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
            and node.func.id in formula.NAVIGATION_CALLS:
        return _call(node)
    raise _Unsupported()


def _escape(text: str) -> str:
    return text.replace("\\", "").replace("_", r"\_").replace("{", "").replace("}", "")


def _quoted(node: ast.AST) -> str:
    if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
        raise _Unsupported()
    return _escape(node.value)


def _args(node: ast.Call) -> list[str]:
    return [_expr(a)[0] for a in node.args]


def _one(node: ast.Call) -> str:
    if len(node.args) != 1:
        raise _Unsupported()
    return _expr(node.args[0])[0]


def _call(node: ast.Call) -> str:
    if not isinstance(node.func, ast.Name) or node.keywords:
        raise _Unsupported()
    render = CALL_TEX.get(node.func.id)
    if render is None:
        raise _Unsupported()
    return render(node)


def _fn(command: str) -> Callable[[ast.Call], str]:
    return lambda node: rf"{command}{_bracket(_one(node))}"


def _navigation(arrow: str) -> Callable[[ast.Call], str]:
    def render(node: ast.Call) -> str:
        if len(node.args) != 1:
            raise _Unsupported()
        return rf"{arrow}{{\mathrm{{{_quoted(node.args[0])}}}}}"
    return render


def _previous(node: ast.Call) -> str:
    return rf"\operatorname{{prev}}{_bracket(_one(node))}"


def _property(node: ast.Call) -> str:
    if len(node.args) < 2:
        raise _Unsupported()
    relationship, operation = _quoted(node.args[0]), _quoted(node.args[1])
    inputs = ", ".join(_expr(a)[0] for a in node.args[2:])
    return rf"\operatorname{{{operation}}}_{{\mathrm{{{relationship}}}}}{_bracket(inputs)}"


def _extreme(command: str) -> Callable[[ast.Call], str]:
    return lambda node: rf"{command}{_bracket(', '.join(_args(node)))}"


# One renderer per callable formula.ALLOWED_CALLS admits -- the drift
# test in tests/test_formula_tex.py asserts the two sets are equal.
CALL_TEX: dict[str, Callable[[ast.Call], str]] = {
    "targets": _navigation(r"\overrightarrow"),
    "sources": _navigation(r"\overleftarrow"),
    "sum": lambda node: rf"\sum {_one(node)}",
    "count": lambda node: rf"\#{_bracket(_one(node))}",
    "abs": lambda node: rf"\left|{_one(node)}\right|",
    "min": _extreme(r"\min"),
    "max": _extreme(r"\max"),
    "sin": _fn(r"\sin"), "cos": _fn(r"\cos"), "tan": _fn(r"\tan"),
    "arcsin": _fn(r"\arcsin"), "arccos": _fn(r"\arccos"), "arctan": _fn(r"\arctan"),
    "log": _fn(r"\ln"), "ln": _fn(r"\ln"), "log10": _fn(r"\log_{10}"), "exp": _fn(r"\exp"),
    formula.SQRT_CALL: lambda node: rf"\sqrt{{{_one(node)}}}",
    formula.PREVIOUS_CALL: _previous,
    formula.PROPERTY_CALL: _property,
}
