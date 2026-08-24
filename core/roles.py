# core/roles.py
"""What a mote *is*, derived rather than stored.

Species, template, folder, property model, rule and portal are the same
idea six times over: an ordinary mote that plays a role because of what
points at it (or, for a portal, because of the reserved properties it
carries). Nothing is written on a mote to say so, which is the whole
point of "everything is a mote" -- but it does mean the question "what
is this?" has to be answered by looking, and every layer used to answer
it for itself. The UI knew four of the six and called them Species,
Folder, Rule, Mote; the CLI knew property models; MCP knew species.

This module is the index, not the implementation. Each role's detection
still lives in the module that owns the concept -- species_ids_in(),
folder_ids_in(), model_ids_in(), template_ids_in(), rule_ids_in() +
has_equation(), portal_target() -- and ROLES below just says which is
which. A declarative table rather than a registry providers-style: there
is no plugin story here, and docs/style.md rules out the global mutable
state a register_role() would need.

Roles are not exclusive. A mote can be a species and a folder at once
(one is about instances, the other about organization), and a template
of one species can be an instance of another.
"""
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from core import folders, model_resolver, portals, rules, species
from core.entities import Mote, Relationship

SPECIES = "species"
TEMPLATE = "template"
FOLDER = "folder"
PROPERTY_MODEL = "property_model"
RULE = "rule"
PORTAL = "portal"


@dataclass(frozen=True)
class Role:
    """One role and how to recognize it.

    `ids_in` recognizes it from the graph's shape (which motes a set of
    relationships makes into this). `matches` recognizes it from a mote's
    own properties. A role may declare either or both: a rule needs both
    (an `applies_to` relationship *and* an `equation`), which is exactly
    the pair core/rules.py::list_rules() already insists on, so a
    half-built rule is not called one here either.
    """
    name: str
    label: str
    ids_in: Callable[[Iterable[Relationship]], set[str]] | None = None
    matches: Callable[[dict], bool] | None = None


# Declaration order is display order -- what a mote is *for* before what
# it's made of, so a species reads as a species even when it also holds
# an equation.
ROLES: tuple[Role, ...] = (
    Role(SPECIES, "Species", ids_in=species.species_ids_in),
    Role(TEMPLATE, "Template", ids_in=species.template_ids_in),
    Role(FOLDER, "Folder", ids_in=folders.folder_ids_in),
    Role(PROPERTY_MODEL, "Property model", ids_in=model_resolver.model_ids_in),
    Role(RULE, "Rule", ids_in=rules.rule_ids_in, matches=rules.has_equation),
    Role(PORTAL, "Portal", matches=lambda props: portals.portal_target(props) is not None),
)

ROLE_NAMES: tuple[str, ...] = tuple(role.name for role in ROLES)
ROLE_LABELS: dict[str, str] = {role.name: role.label for role in ROLES}


def roles_in(
    motes: Iterable[Mote], relationships: Iterable[Relationship]
) -> dict[str, list[str]]:
    """mote id -> the roles it plays, for every mote that plays any.

    Each role's id set is computed once over the relationship list and
    then intersected, rather than re-scanned per mote, because the
    callers are whole-subgraph classifiers (ui/api.py::focus()). Motes
    with no role are left out entirely: absence says "ordinary mote"
    without every payload carrying an empty list for most of the graph.
    """
    relationships = list(relationships)
    id_sets = {
        role.name: role.ids_in(relationships) for role in ROLES if role.ids_in is not None
    }

    result: dict[str, list[str]] = {}
    for mote in motes:
        names = [role.name for role in ROLES if _plays(role, mote, id_sets)]
        if names:
            result[mote.id] = names
    return result


def roles_of(mote: Mote, relationships: Iterable[Relationship]) -> list[str]:
    """The roles one mote plays. roles_in() for a single mote."""
    return roles_in([mote], relationships).get(mote.id, [])


def ids_with_role(
    role_name: str, motes: Iterable[Mote], relationships: Iterable[Relationship]
) -> list[str]:
    """Just the motes playing one named role, in the order given."""
    roles = roles_in(motes, relationships)
    return [mote.id for mote in motes if role_name in roles.get(mote.id, ())]


def _plays(role: Role, mote: Mote, id_sets: dict[str, set[str]]) -> bool:
    if role.ids_in is not None and mote.id not in id_sets[role.name]:
        return False
    if role.matches is not None and not role.matches(mote.properties):
        return False
    return True
