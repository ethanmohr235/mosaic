# core/rules.py
"""Rule and unknown discovery.

A rule is just a mote: one with an `equation` property and an
`applies_to` relationship. Which of a mote's properties the solver may
determine -- its **freedoms** -- comes from a `freedoms` property.
Nothing else marks either as special -- same philosophy as
`core/species.py`.

`freedoms` is the modelling word for what the solver calls an unknown,
and the rename is not cosmetic: these are the properties a thing is free
to have determined, its degrees of freedom, and a user asking "what is
this turbine free to determine?" is asking a modelling question. The
solver's own vocabulary is left alone -- `core/formula.py`'s `Unknown`,
`Context.unknown_keys` and both solver backends still say unknown,
because at that layer it is the right word. `core/simulation.py`'s
`_context()` is the one translation point.

What `applies_to` points at decides how widely the rule is asserted, and
that is *inferred from the target* rather than declared separately:

    a species  ->  once per instance   ("each")
    any mote   ->  once, about it      ("once")

"Acting as a species" is the same test `core/species.py`'s list_species
uses -- being the target of an `instance_of` or `template_of`
relationship. One relationship type covers both, and the common case
reads exactly as it always has.

The cost of inferring is that making the first instance of a mote flips
every rule asserted about it. That is not acceptable silently, so the
resolved scope is carried on the Rule and reported by the pre-flight
check (see core/simulation.py's check_roles) rather than left to be
discovered as a quietly different answer.
"""
import sqlite3
from dataclasses import dataclass

from core.models import Mote
from core.motes import get_mote
from core.relationships import list_relationships
from core.species import INSTANCE_OF, TEMPLATE_OF

APPLIES_TO = "applies_to"
EQUATION_KEY = "equation"
FREEDOMS_KEY = "freedoms"

# How widely a rule is asserted -- see the module docstring.
EACH = "each"
ONCE = "once"


@dataclass(frozen=True)
class Rule:
    """`target_id` is whatever `applies_to` points at: a species when
    `scope` is EACH, the single mote the rule is about when it is ONCE.
    It was called `species_id` while only the first was possible."""
    mote: Mote
    target_id: str
    equation: str
    scope: str = EACH

    @property
    def name(self) -> str:
        return str(self.mote.properties.get("name", self.mote.id[:8]))


def list_rules(conn: sqlite3.Connection) -> list[Rule]:
    """Every rule in the garden. A mote with an `applies_to` relationship
    but no `equation` yet is skipped rather than raising -- a half-built
    rule shouldn't break the whole run."""
    relationships = list_relationships(conn)
    acting_as_species = {
        rel.to_id for rel in relationships if rel.type in (INSTANCE_OF, TEMPLATE_OF)
    }

    rules = []
    for rel in (r for r in relationships if r.type == APPLIES_TO):
        mote = get_mote(conn, rel.from_id)
        if mote is None:
            continue
        equation = mote.properties.get(EQUATION_KEY)
        if not isinstance(equation, str) or not equation.strip():
            continue
        rules.append(
            Rule(
                mote=mote,
                target_id=rel.to_id,
                equation=equation,
                scope=EACH if rel.to_id in acting_as_species else ONCE,
            )
        )
    return rules


def parse_freedoms(value) -> list[str]:
    """`freedoms` accepts a JSON list (["voltage"]) or a comma-separated
    string ("voltage, current"). The UI's plain property boxes make the
    string form far easier to type, so both are supported."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []
