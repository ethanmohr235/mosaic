# core/function_text.py
"""A function call written out as its body, for shown work.

`\\work` on `reactor.center = phi(0)` has to show what `phi(0)` *is*, or the
line with values written into it is just `phi(0)` again and checks nothing.
So between the equation and the substituted line, `\\work` prints the
equation with every call replaced by its body and the arguments put in
for the parameters:

    reactor.center == phi(0)
    reactor.center == phi.A * sin(phi.B * (0 cm)) / (0 cm)
    reactor.center == 5e13 * sin((0.0628 1/cm) * (0 cm)) / (0 cm)

A constant the body reads bare is qualified with the function's name, so
the substituted line can write its value in the way it writes any other
reference. A bare number passed to a parameter that declares a unit is
written with it -- `phi(0)` meant 0 cm, and the line says so. This is text
for a reader and never evaluated; the evaluator is core/formula.py.
"""
import ast
import itertools
from dataclasses import dataclass, field

from core import functions as functions_repo
from core import units as units_repo
from core.formula import (
    ALLOWED_CALLS, MATH_CONSTANTS, TICK_NAME, FormulaError, is_bare_number, parse_expression,
)

# How deep calls inside bodies are written out. A guard against a
# pathological garden, not a display limit; recursion is refused anyway.
_DEPTH = 8


@dataclass
class _Expansion:
    functions: dict[str, tuple[str, list[str], dict[str, str]]]
    quantities: dict[str, str] = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=itertools.count)
    expanded: bool = False

    def quantity(self, text: str) -> ast.Name:
        """A stand-in name for `(0 cm)`, which is not Python, replaced in
        the text after unparsing."""
        placeholder = f"__quantity{next(self._ids)}__"
        self.quantities[placeholder] = text
        return ast.Name(placeholder, ast.Load())


def expand(equation: str, properties: dict[str, dict]) -> str | None:
    """`equation` with each call to a function mote written out as its
    body, or None when it calls none (or does not parse)."""
    state = _Expansion(_functions(properties))
    if not state.functions:
        return None
    try:
        tree = _rewrite(parse_expression(equation), {}, "", state, ())
    except (FormulaError, functions_repo.FunctionDefinitionError, SyntaxError):
        return None
    if not state.expanded:
        return None
    text = ast.unparse(tree).replace("**", "^")
    for placeholder, quantity in state.quantities.items():
        text = text.replace(placeholder, quantity)
    return text


def _functions(properties: dict[str, dict]) -> dict[str, tuple[str, list[str], dict[str, str]]]:
    """Each function, by name: body, parameters and declared units. A name
    two motes share is left out -- the evaluator refuses it, so there is
    no one body to show."""
    by_name: dict[str, list[dict]] = {}
    for props in properties.values():
        name = props.get("name")
        if isinstance(name, str) and functions_repo.is_function(props):
            by_name.setdefault(name, []).append(props)
    out = {}
    for name, found in by_name.items():
        if len(found) != 1:
            continue
        props = found[0]
        try:
            parameters = functions_repo.parameters_of(props)
        except functions_repo.FunctionDefinitionError:
            continue
        units = units_repo.parse_units_property(props.get(units_repo.UNITS_KEY))
        out[name] = (functions_repo.body_of(props), parameters, units)
    return out


def _rewrite(
    node: ast.AST, bindings: dict[str, ast.AST], owner: str, state: _Expansion,
    calls: tuple[str, ...],
) -> ast.AST:
    """One node with calls written out. Inside a body (`owner` set), a
    parameter becomes its argument and any other bare name becomes
    `owner.name`; outside, names are left as the equation wrote them."""
    if isinstance(node, ast.Name):
        return _name(node, bindings, owner)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        args = [_rewrite(a, bindings, owner, state, calls) for a in node.args]
        written = state.functions.get(node.func.id)
        if node.func.id in ALLOWED_CALLS or written is None or node.func.id in calls \
                or len(calls) >= _DEPTH or len(args) != len(written[1]):
            return ast.Call(node.func, args, node.keywords)
        return _body(node.func.id, written, node.args, args, state, calls)
    for name, value in ast.iter_fields(node):
        if isinstance(value, ast.AST):
            setattr(node, name, _rewrite(value, bindings, owner, state, calls))
        elif isinstance(value, list):
            setattr(node, name, [_rewrite(v, bindings, owner, state, calls)
                                 if isinstance(v, ast.AST) else v for v in value])
    return node


def _body(
    name: str, written: tuple[str, list[str], dict[str, str]], typed: list[ast.AST],
    args: list[ast.AST], state: _Expansion, calls: tuple[str, ...],
) -> ast.AST:
    body, parameters, units = written
    bindings = {}
    for parameter, source, arg in zip(parameters, typed, args):
        unit = units.get(parameter)
        bindings[parameter] = (state.quantity(f"({ast.unparse(arg)} {unit})")
                               if unit and is_bare_number(source) else arg)
    state.expanded = True
    return _rewrite(parse_expression(body), bindings, name, state, (*calls, name))


def _name(node: ast.Name, bindings: dict[str, ast.AST], owner: str) -> ast.AST:
    if node.id in bindings:
        return bindings[node.id]
    if not owner or node.id == TICK_NAME or node.id in MATH_CONSTANTS \
            or node.id.startswith("__quantity"):
        return node
    return ast.Attribute(ast.Name(owner, ast.Load()), node.id, ast.Load())
