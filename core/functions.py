# core/functions.py
"""Function motes: a named expression of parameters, callable from any
formula.

A rule is an equation about specific motes, so it holds at one point: the
flux at whatever `reactor.r` is. A function is the relation itself, asked
at whatever point a caller names -- `phi(0)` in a document, `phi(reactor.r)`
in a rule. It is an ordinary mote (`philosophy.md`: no second object
model), carrying two reserved properties:

    name:        phi
    function:    A * sin(B * r) / r
    parameters:  r                    (a comma string or a JSON list)
    units:       {"r": "cm", "B": "1/cm"}
    A: 5e13      B: 0.0628            (constants: ordinary properties)

In the body, a bare name is a parameter first and a property of the
function mote second -- the same anchor rule a rule's bare names follow.
A parameter's unit lives in the mote's ordinary `units` map; it says what
a bare number passed to that parameter means. core/formula.py evaluates
calls; this module only says what a function mote *is*, so the worksheet,
the unit check and the evaluator read it one way.
"""
import json

FUNCTION_KEY = "function"
PARAMETERS_KEY = "parameters"


class FunctionDefinitionError(ValueError):
    """A function mote's `parameters` cannot be read."""


def is_function(properties: dict) -> bool:
    """Whether a mote is a function: it carries a `function` body."""
    body = dict.get(properties, FUNCTION_KEY)
    return isinstance(body, str) and bool(body.strip())


def body_of(properties: dict) -> str:
    return dict.get(properties, FUNCTION_KEY, "").strip()


def parameters_of(properties: dict) -> list[str]:
    """The parameter names, in call order. Accepts the same two spellings
    `freedoms` does. A name that is not an identifier, or a repeated one,
    is refused rather than skipped: a call binds arguments by position, so
    dropping one would shift every argument after it."""
    raw = dict.get(properties, PARAMETERS_KEY, [])
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.startswith("["):
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError as e:
                raise FunctionDefinitionError(f"parameters {raw!r} is not a list: {e}") from e
        else:
            raw = [p for p in (part.strip() for part in stripped.split(",")) if p]
    if not isinstance(raw, list) or not all(isinstance(p, str) for p in raw):
        raise FunctionDefinitionError(f"parameters must be a list of names, not {raw!r}")
    names = [p.strip() for p in raw]
    for name in names:
        if not name.isidentifier():
            raise FunctionDefinitionError(f"parameter {name!r} is not a name")
    if len(set(names)) != len(names):
        raise FunctionDefinitionError(f"parameters {names} repeat a name")
    return names
