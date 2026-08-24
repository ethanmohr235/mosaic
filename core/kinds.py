# core/kinds.py
"""What a mote *is*, derived rather than stored.

Species, template, folder, property model, rule and portal are the same
idea six times over: an ordinary mote that is one of these *because of
what points at it* (or, for a portal, because of the reserved properties
it carries). Nothing is written on a mote to say so, which is the whole
point of "everything is a mote" -- but it does mean the question "what
is this?" has to be answered by looking, and every layer used to answer
it for itself. The UI knew four of the six and called them Species,
Folder, Rule, Mote; the CLI knew property models; MCP knew species.

Called *kinds* and not *roles*, which is what this was: `core/roles.py`
now answers a different question entirely -- what an assembly's parts
are called, declared on a species and addressable in a formula
(`turbine.work`). The two are easy to confuse and worth keeping apart in
one sentence: **a kind is what a mote is; a role is what something else
calls it.** A kind is derived and never stored; a role is a declared
property. The frontend was already using this word (`kindLabel`,
`kindOf`, `#detail-type-label`), so the rename made existing names
correct rather than inventing one.

This module is the index, not the implementation. Each kind's detection
still lives in the module that owns the concept -- species_ids_in(),
folder_ids_in(), model_ids_in(), template_ids_in(), rule_ids_in() +
has_equation(), portal_target() -- and KINDS below just says which is
which. A declarative table rather than a registry providers-style: there
is no plugin story here, and docs/style.md rules out the global mutable
state a register_kind() would need.

Kinds are not exclusive. A mote can be a species and a folder at once
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
class Kind:
    """One kind and how to recognize it.

    `ids_in` recognizes it from the graph's shape (which motes a set of
    relationships makes into this). `matches` recognizes it from a mote's
    own properties. A kind may declare either or both: a rule needs both
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
KINDS: tuple[Kind, ...] = (
    Kind(SPECIES, "Species", ids_in=species.species_ids_in),
    Kind(TEMPLATE, "Template", ids_in=species.template_ids_in),
    Kind(FOLDER, "Folder", ids_in=folders.folder_ids_in),
    Kind(PROPERTY_MODEL, "Property model", ids_in=model_resolver.model_ids_in),
    Kind(RULE, "Rule", ids_in=rules.rule_ids_in, matches=rules.has_equation),
    Kind(PORTAL, "Portal", matches=lambda props: portals.portal_target(props) is not None),
)

KIND_NAMES: tuple[str, ...] = tuple(kind.name for kind in KINDS)
KIND_LABELS: dict[str, str] = {kind.name: kind.label for kind in KINDS}


def kinds_in(
    motes: Iterable[Mote], relationships: Iterable[Relationship]
) -> dict[str, list[str]]:
    """mote id -> the kinds it is, for every mote that is any.

    Each kind's id set is computed once over the relationship list and
    then intersected, rather than re-scanned per mote, because the
    callers are whole-subgraph classifiers (ui/api.py::focus()). Motes
    of no particular kind are left out entirely: absence says "ordinary
    mote" without every payload carrying an empty list for most of the
    graph.
    """
    relationships = list(relationships)
    id_sets = {
        kind.name: kind.ids_in(relationships) for kind in KINDS if kind.ids_in is not None
    }

    result: dict[str, list[str]] = {}
    for mote in motes:
        names = [kind.name for kind in KINDS if _is(kind, mote, id_sets)]
        if names:
            result[mote.id] = names
    return result


def kinds_of(mote: Mote, relationships: Iterable[Relationship]) -> list[str]:
    """The kinds one mote is. kinds_in() for a single mote."""
    return kinds_in([mote], relationships).get(mote.id, [])


def ids_with_kind(
    kind_name: str, motes: Iterable[Mote], relationships: Iterable[Relationship]
) -> list[str]:
    """Just the motes of one named kind, in the order given."""
    kinds = kinds_in(motes, relationships)
    return [mote.id for mote in motes if kind_name in kinds.get(mote.id, ())]


def _is(kind: Kind, mote: Mote, id_sets: dict[str, set[str]]) -> bool:
    if kind.ids_in is not None and mote.id not in id_sets[kind.name]:
        return False
    if kind.matches is not None and not kind.matches(mote.properties):
        return False
    return True
