# core/rules.py
"""Rule and unknown discovery.

A rule is just a mote: one with an `equation` property and an
`applies_to` relationship pointing at a species. A species declares which
of its instances' properties the solver may determine, via an `unknowns`
property. Nothing else marks either as special -- same philosophy as
`core/species.py`.
"""
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass

from core.entities import Mote, Relationship
from core.motes import get_mote
from core.relationships import list_relationships

APPLIES_TO = "applies_to"
EQUATION_KEY = "equation"
UNKNOWNS_KEY = "unknowns"


def rule_ids_in(relationships: Iterable[Relationship]) -> set[str]:
    """Which motes a set of relationships points at a species from -- the
    source of an applies_to. Being a rule also needs an `equation` (see
    has_equation), which is why this alone isn't the whole test: a mote
    with applies_to and no equation is a half-built rule, skipped by
    list_rules() rather than treated as one."""
    return {r.from_id for r in relationships if r.type == APPLIES_TO}


def has_equation(properties: dict) -> bool:
    """The property half of "is this a rule" -- the same check
    list_rules() makes before accepting one."""
    equation = properties.get(EQUATION_KEY)
    return isinstance(equation, str) and bool(equation.strip())


@dataclass(frozen=True)
class Rule:
    mote: Mote
    species_id: str
    equation: str

    @property
    def name(self) -> str:
        return str(self.mote.properties.get("name", self.mote.id[:8]))


def list_rules(conn: sqlite3.Connection) -> list[Rule]:
    """Every rule in the garden. A mote with an `applies_to` relationship
    but no `equation` yet is skipped rather than raising -- a half-built
    rule shouldn't break the whole run."""
    rules = []
    for rel in list_relationships(conn, type_=APPLIES_TO):
        mote = get_mote(conn, rel.from_id)
        if mote is None or not has_equation(mote.properties):
            continue
        rules.append(Rule(mote=mote, species_id=rel.to_id, equation=mote.properties[EQUATION_KEY]))
    return rules


def parse_unknowns(value) -> list[str]:
    """`unknowns` accepts a JSON list (["voltage"]) or a comma-separated
    string ("voltage, current"). The UI's plain property boxes make the
    string form far easier to type, so both are supported."""
    if value is None:
        return []
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(part).strip() for part in value if str(part).strip()]
    return []
