# core/roles.py
"""Roles: the named parts an assembly is wired to.

A role is not a new kind of object. It is an ordinary relationship whose
*type* is the role's name:

    Rankine Cycle #1 --turbine--> HP Turbine
    Rankine Cycle #1 --boiler---> Boiler A

A rule anchored on the cycle reaches its parts by navigating those edges,
which has always worked -- `targets('turbine').work` solves an unknown on
the turbine today. What this module adds is the *declaration*: a species
saying which roles its instances are expected to have, so that a
mis-wired assembly is caught before it is solved, an arity is a stated
intention rather than an accident of how many edges happen to exist, and
a formula can address a part by name (`turbine.work`).

Deliberately free of any import from core/formula.py, so that module can
import this one without a cycle. That is why the arity rules here return
a *message* rather than raising: the wording lives in one place, but each
walk raises its own error type. core/formula.py and core/dimensions.py
walk the same grammar and must agree about roles exactly as they already
agree about the call whitelist.
"""
from dataclasses import dataclass

ROLES_KEY = "roles"

# How many motes a role is expected to reach. `one` collapses to a single
# value in a formula; `many` always yields a list, even when only one mote
# is wired, so that `sum(...)` is required from the start rather than
# becoming required the day a second one is added.
ONE = "one"
MANY = "many"
ARITIES = (ONE, MANY)

ARITY_KEY = "arity"
SPECIES_KEY = "species"


@dataclass(frozen=True)
class Role:
    """One declared part of an assembly."""
    name: str
    arity: str = ONE
    species_id: str | None = None

    @property
    def collapses(self) -> bool:
        return self.arity == ONE


def parse_roles(value) -> dict[str, Role]:
    """A species' `roles` property, in any of the shapes a user can type.

    Accepts the same range `core/rules.py`'s freedom list does, and for
    the same reason -- the UI's plain property boxes make the terse forms
    far easier to enter than JSON:

        ["turbine", "boiler"]                  every role, arity one
        "turbine, boiler"                      the same, comma-separated
        {"turbine": "one", "heaters": "many"}  arity per role
        {"turbine": {"arity": "one", "species": "<mote-id>"}}

    Anything unrecognizable is ignored rather than raising: a half-typed
    declaration should not break a garden that does not use roles yet.
    """
    if isinstance(value, str):
        return _named(part.strip() for part in value.split(","))
    if isinstance(value, list):
        return _named(str(part).strip() for part in value)
    if isinstance(value, dict):
        return {
            name: _role(name, declaration)
            for name, declaration in value.items()
            if isinstance(name, str) and name.strip()
        }
    return {}


def _named(names) -> dict[str, Role]:
    return {name: Role(name) for name in names if name}


def _role(name: str, declaration) -> Role:
    if isinstance(declaration, str):
        return Role(name, _arity(declaration))
    if isinstance(declaration, dict):
        species = declaration.get(SPECIES_KEY)
        return Role(
            name=name,
            arity=_arity(declaration.get(ARITY_KEY, ONE)),
            species_id=species if isinstance(species, str) and species.strip() else None,
        )
    return Role(name)


def _arity(value) -> str:
    """An unrecognized arity falls back to `one` rather than raising --
    the same forgiving reading parse_roles() takes overall. A typo makes
    the role stricter, never silently looser."""
    return value if value in ARITIES else ONE


def arity_problem(role: Role, count: int) -> str | None:
    """Why this many motes is wrong for this role, or None if it is fine.

    A `one` role reaching zero motes or several is a wiring mistake, and
    guessing which mote was meant is exactly the kind of silent choice
    that hides it. A `many` role accepts any number, zero included -- an
    assembly with no feedwater heaters is a legitimate assembly.
    """
    if role.arity == MANY:
        return None
    if count == 1:
        return None
    if count == 0:
        return (
            f"role {role.name!r} reaches no mote -- expected exactly one, "
            f"so check the {role.name!r} relationship"
        )
    return (
        f"role {role.name!r} reaches {count} motes -- it is declared "
        f"{ONE!r}, so declare it {MANY!r} or remove the extra "
        f"{role.name!r} relationship(s)"
    )


def species_problem(role: Role, mote_id: str, species_ids: list[str]) -> str | None:
    """Why this mote may not fill this role, or None if it may.

    A role with no declared species accepts anything, which is the
    default: naming the species is an optional tightening, not a
    prerequisite for using roles at all."""
    if role.species_id is None or role.species_id in species_ids:
        return None
    return f"role {role.name!r} is filled by a mote that is not an instance of the declared species"
