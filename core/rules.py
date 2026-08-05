# core/rules.py
"""Rule and unknown discovery.

A rule is just a mote: one with an `equation` property and an
`applies_to` relationship pointing at a species. A species declares which
of its instances' properties the solver may determine, via an `unknowns`
property. Nothing else marks either as special -- same philosophy as
`core/species.py`.
"""
import sqlite3
from dataclasses import dataclass

from core.models import Mote
from core.motes import get_mote
from core.relationships import list_relationships
from core.species import species_of

APPLIES_TO = "applies_to"
EQUATION_KEY = "equation"
UNKNOWNS_KEY = "unknowns"


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
        if mote is None:
            continue
        equation = mote.properties.get(EQUATION_KEY)
        if not isinstance(equation, str) or not equation.strip():
            continue
        rules.append(Rule(mote=mote, species_id=rel.to_id, equation=equation))
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


def species_unknowns(conn: sqlite3.Connection, species_id: str) -> list[str]:
    species = get_mote(conn, species_id)
    if species is None:
        return []
    return parse_unknowns(species.properties.get(UNKNOWNS_KEY))


def unknown_keys(conn: sqlite3.Connection, mote_id: str) -> set[str]:
    """Property names the solver may determine for this mote -- the union
    over every species it is an instance of."""
    keys: set[str] = set()
    for species in species_of(conn, mote_id):
        keys.update(parse_unknowns(species.properties.get(UNKNOWNS_KEY)))
    return keys
