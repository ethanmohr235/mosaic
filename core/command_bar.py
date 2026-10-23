# core/command_bar.py
"""The statement grammar and execution -- one typed line edits the graph
without leaving the keyboard. The console's command line runs every line
that isn't a verb through here (core/commands.py).

Seven forms, tried in this order (first match wins):

    delete R1.resistance     drop one property
    delete R1                drop a mote (needs confirm=True -- see below)
    R1.resistance ?           freedom: find-or-create R1, toggle "resistance"
                              in its `freedoms` list (see core/rules.py) --
                              a second `?` un-frees it
    R1.resistance units kohm units: find-or-create R1, annotate an existing
                              (or not-yet-set) key with a unit from
                              core/units.py without touching its value
    R1.resistance = 500 ohms property: find-or-create R1, set/create the key,
                              trailing token resolved against core/units.py
    R1.voltage == ...         rule: find-or-create R1, create a rule mote
    beam.I / beam.I0 == 0.1   with that equation, applies_to R1. Any other
                              Name.prop reference resolves by garden-scope
                              name, so nothing is wired up for it.

                              Either side may be any expression the rule
                              language accepts -- an equation does not
                              have to be arranged as `Mote.property ==
                              <the rest>`. The mote the rule is *about*
                              is the first `Name.` in the line, which is
                              the same mote the assignment form names.
    R1 -> R2                  relationship, default type "related_to"
    R1 -> owns -> R2          relationship, explicit type
    R1 : Resistor              species: find-or-create both, instance_of
    R1                         navigate -- find-or-create, same as every
                              other form (a bare name that matches nothing
                              creates a mote with that name and jumps to it)

A bare mote name is a *name* search (case-insensitive, exact), the same
"pick one thing you can already see" rule core/formula.py's connected-name
references use. Two motes sharing a name is not resolved by guessing: it
comes back as an AmbiguousName error carrying every candidate, for a
caller to show as a pick list. `#<id>` (or an unambiguous id prefix)
always names one mote directly, which is how a caller resubmits a command
after the person picks -- swap the ambiguous token for `#<id>` and rerun.

A mote name used with the dotted syntax (`R1.resistance`, and both sides
of a rule) is a plain Python identifier by construction -- that's what
core/formula.py's own grammar requires of an attribute base, not a
restriction added here. A name with spaces or punctuation still works for
the relationship, species and navigate forms; quote it ('Living Room' or
"Living Room") so the parser doesn't split on the space.

Deletion is the one destructive form, so it does not execute on its own:
`run_command` returns a CommandResult with `needs_confirm=True` the first
time, and only deletes when the caller resubmits the identical command
with `confirm=True` -- the bar's "press Enter again" affordance.
"""
import ast
import contextvars
import re
import sqlite3
from dataclasses import dataclass, field, replace

from core import motes as motes_repo
from core import relationships as rel_repo
from core import rules as rules_repo
from core import species as species_repo
from core import undo
from core import units
from core.formula import (
    MATH_CONSTANTS, FormulaError, attribute_bases, function_reference_ids, parse_equation,
)
from core.entities import Mote
from core.simulation import unresolved_equation_names

_IDENTIFIER = r"[A-Za-z_]\w*"

# Deliberately not a shape: any `lhs == rhs`, with core/formula.py's own
# parser deciding whether it is a relation this engine can read. A rule
# is an *equation*, and which side a modeller writes a quantity on is
# their business -- `beam.I / beam.I0 == 0.1` says exactly what
# `beam.I == 0.1 * beam.I0` says (see core/formula.py's Ratio).
_RULE_RE = re.compile(r"^\s*(.+?)\s*==\s*(.+?)\s*$")
_FREEDOM_RE = re.compile(r"^\s*(.+?)\.([A-Za-z_]\w*)\s*\?\s*$")
_UNITS_RE = re.compile(r"^\s*(.+?)\.([A-Za-z_]\w*)\s+units\s+(.+?)\s*$", re.IGNORECASE)
_PROPERTY_RE = re.compile(r"^\s*(.+?)\.([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")
_DELETE_RE = re.compile(r"^\s*(?:delete|del)\s+(.+?)\s*$", re.IGNORECASE)
_SPECIES_RE = re.compile(r"^\s*(.+?)\s*:\s*(.+?)\s*$")
_RELATE_RE = re.compile(r"^\s*(.+?)\s*->\s*(.+?)\s*$")
_ID_REF_RE = re.compile(r"^#([0-9a-fA-F-]{3,})$")
_DEFAULT_REL_TYPE = "related_to"


class CommandError(ValueError):
    """A command that doesn't parse, or parses but can't run."""


class AmbiguousName(CommandError):
    """`token` matched more than one mote by name. `candidates` is every
    match, for a caller to show as a pick list; resubmitting with
    `#<id>` in place of the name is how a caller resolves it."""

    def __init__(self, token: str, candidates: list[Mote]):
        self.token = token
        self.candidates = candidates
        super().__init__(
            f"'{token}' matches {len(candidates)} motes -- pick one, or use #<id>"
        )


@dataclass
class CommandResult:
    ok: bool
    message: str
    mote_id: str | None = None
    needs_confirm: bool = False
    choices: list[dict] | None = None
    created: bool = False  # whether `mote_id` is a mote this command just made
    # Everything this line wrote, as one undo step, or None if it wrote
    # nothing. Returned rather than pushed: the undo stack belongs to the
    # surface that ran the line (core/undo.py).
    command: "undo.Command | None" = None


_SUGGEST_LIMIT = 20
_PROP_TOKEN_RE = re.compile(rf"({_IDENTIFIER})\.(\w*)$")
_NAME_TOKEN_RE = re.compile(rf"({_IDENTIFIER})$")


def suggest(conn: sqlite3.Connection, text: str, cursor: int | None = None) -> list[str]:
    """Tab-completion candidates for the token ending at `cursor` (end of
    `text` by default): property names when the token follows an
    unambiguous `Mote.`, mote names otherwise. Empty whenever there's
    nothing to complete -- no partial token, or `Mote.` names zero or
    more than one mote -- since a caller has nothing useful to offer the
    user in that case either.

    Deliberately narrower than the grammar `run_command` accepts: only
    plain-identifier mote names (`R1`, not `'Living Room'`) are
    completed, since a quoted or spaced name has no single trailing
    token to match against."""
    head = (text or "")[:cursor if cursor is not None else len(text or "")]

    prop_match = _PROP_TOKEN_RE.search(head)
    if prop_match:
        mote_token, partial = prop_match.groups()
        matches = _lookup(conn, mote_token)
        if len(matches) != 1:
            return []
        needle = partial.lower()
        keys = sorted(k for k in matches[0].properties.keys() if k.lower().startswith(needle))
        return keys[:_SUGGEST_LIMIT]

    name_match = _NAME_TOKEN_RE.search(head)
    if not name_match or not name_match.group(1):
        return []
    needle = name_match.group(1).lower()
    names = sorted({
        str(m.properties.get("name"))
        for m in motes_repo.list_motes(conn)
        if str(m.properties.get("name") or "").lower().startswith(needle)
    })
    return names[:_SUGGEST_LIMIT]


def run_command(conn: sqlite3.Connection, text: str, confirm: bool = False) -> CommandResult:
    """Parse and execute one line. Never raises: every CommandError (an
    unparseable line, an ambiguous name, a bad unit) becomes a failed
    CommandResult instead, since this always runs one keystroke away from
    the user's next command and a traceback is not a usable answer to
    show in a one-line input box."""
    text = (text or "").strip()
    if not text:
        return CommandResult(ok=False, message="")
    journal: list[undo.Command] = []
    token = _JOURNAL.set(journal)
    try:
        result = _dispatch(conn, text, confirm)
    except AmbiguousName as e:
        _roll_back(journal)
        return CommandResult(
            ok=False,
            message=str(e),
            choices=[_choice(conn, m) for m in e.candidates],
        )
    except CommandError as e:
        _roll_back(journal)
        return CommandResult(ok=False, message=str(e))
    finally:
        _JOURNAL.reset(token)
    if journal:
        result = replace(result, command=undo.combine(text, journal))
    return result


# ---- the journal: every write, and how to take it back --------------
#
# Each primitive write below records its inverse while a line runs, so
# `run_command` can hand back the whole line as one undo step whichever of
# the forms it took -- and so a line that fails halfway (`A -> B` where B
# turns out to be ambiguous after A was created) takes back what it had
# already written rather than leaving half a statement in the garden.

_JOURNAL: contextvars.ContextVar = contextvars.ContextVar("command_bar_journal", default=None)


def _record(command: undo.Command) -> None:
    journal = _JOURNAL.get()
    if journal is not None:
        journal.append(command)


def _roll_back(journal: list) -> None:
    for command in reversed(journal):
        command.undo()
    journal.clear()


def _create_mote(conn, properties: dict) -> str:
    mote_id = motes_repo.create_mote(conn, properties)
    _record(undo.for_create_mote(conn, mote_id, dict(properties), label="create"))
    return mote_id


def _update_mote(conn, mote_id: str, new_props: dict) -> None:
    old = motes_repo.get_mote(conn, mote_id).properties
    before = {k: old[k] for k in new_props if k in old}
    motes_repo.update_mote(conn, mote_id, new_props)
    _record(undo.for_update_mote(conn, mote_id, before, dict(new_props), label="set"))


def _delete_property(conn, mote_id: str, key: str) -> bool:
    old = motes_repo.get_mote(conn, mote_id).properties
    if key not in old:
        return False
    deleted = motes_repo.delete_property(conn, mote_id, key)
    if deleted:
        _record(undo.for_delete_property(conn, mote_id, key, old[key], label="delete"))
    return deleted


def _create_relationship(conn, from_id: str, to_id: str, type_: str) -> str:
    rel_id = rel_repo.create_relationship(conn, from_id, to_id, type_)
    _record(undo.for_create_relationship(conn, rel_id, from_id, to_id, type_, label="link"))
    return rel_id


def _delete_relationship(conn, rel) -> None:
    if rel_repo.delete_relationship(conn, rel.id):
        _record(undo.for_delete_relationship(conn, rel.id, rel.from_id, rel.to_id, rel.type,
                                             label="unlink"))


def _delete_mote(conn, mote_id: str) -> None:
    """Relationships are read before the delete, since they cascade with it."""
    mote = motes_repo.get_mote(conn, mote_id)
    touching = [
        {"id": r.id, "fromId": r.from_id, "toId": r.to_id, "type": r.type}
        for r in rel_repo.list_relationships(conn)
        if mote_id in (r.from_id, r.to_id)
    ]
    motes_repo.delete_mote(conn, mote_id)
    _record(undo.for_delete_mote(conn, mote_id, dict(mote.properties), touching, label="delete"))


def _dispatch(conn: sqlite3.Connection, text: str, confirm: bool) -> CommandResult:
    delete_match = _DELETE_RE.match(text)
    if delete_match:
        return _run_delete(conn, delete_match.group(1), confirm)

    if "?" in text:
        freedom_match = _FREEDOM_RE.match(text)
        if freedom_match:
            return _run_freedom(conn, freedom_match.group(1), freedom_match.group(2))

    if re.search(r"\bunits\b", text, re.IGNORECASE):
        units_match = _UNITS_RE.match(text)
        if units_match:
            return _run_units(conn, *units_match.groups())

    if "==" in text:
        rule_match = _RULE_RE.match(text)
        if not rule_match:
            raise CommandError(
                "a rule is `<expression> == <expression>`, "
                "e.g. R1.voltage == R1.current * R1.resistance"
            )
        return _run_rule(conn, *rule_match.groups())

    # A lone '=' (not '==', already handled above) is the property form.
    # Checked before '->' and ':' so a value like "R1.note = a -> b" (a
    # string that happens to contain those characters) is read as text,
    # not misparsed as a relationship.
    if "=" in text:
        prop_match = _PROPERTY_RE.match(text)
        if prop_match:
            return _run_property(conn, *prop_match.groups())

    if "->" in text:
        relate_match = _RELATE_RE.match(text)
        if relate_match:
            return _run_relate(conn, relate_match.group(1), relate_match.group(2))

    if ":" in text:
        species_match = _SPECIES_RE.match(text)
        if species_match:
            return _run_species(conn, species_match.group(1), species_match.group(2))

    return _run_navigate(conn, text)


# ---- the five forms -------------------------------------------------

def _run_property(conn, mote_token: str, key: str, value_text: str) -> CommandResult:
    mote_id, created = _find_or_create(conn, mote_token)
    old = motes_repo.get_mote(conn, mote_id).properties
    new_props, value, unit_name = property_update(old, key, value_text)
    _update_mote(conn, mote_id, new_props)

    label = _label_for(mote_token, created)
    display = f"{value} {unit_name}" if unit_name else str(value)
    return CommandResult(
        ok=True, mote_id=mote_id, created=created,
        message=f"{label}{'created, ' if created else ''}{key} = {display}",
    )


def _run_units(conn, mote_token: str, key: str, unit_text: str) -> CommandResult:
    unit_name = _resolve_unit(unit_text)
    if not unit_name:
        raise CommandError(f"'{unit_text.strip()}' is not a recognized unit")

    mote_id, created = _find_or_create(conn, mote_token)
    mote = motes_repo.get_mote(conn, mote_id)
    unit_map = units.parse_units_property(mote.properties.get(units.UNITS_KEY))
    unit_map[key] = unit_name
    _update_mote(conn, mote_id, {units.UNITS_KEY: unit_map})

    label = _label_for(mote_token, created)
    return CommandResult(
        ok=True, mote_id=mote_id, created=created,
        message=f"{label}{key} is now in {unit_name}",
    )


def _run_freedom(conn, mote_token: str, key: str) -> CommandResult:
    mote_id, created = _find_or_create(conn, mote_token)
    mote = motes_repo.get_mote(conn, mote_id)
    freedoms = rules_repo.parse_freedoms(mote.properties.get(rules_repo.FREEDOMS_KEY))

    label = _label_for(mote_token, created)
    if key in freedoms:
        freedoms.remove(key)
        verb = "no longer free"
    else:
        freedoms.append(key)
        verb = "free"
    _update_mote(conn, mote_id, {rules_repo.FREEDOMS_KEY: freedoms})

    return CommandResult(
        ok=True, mote_id=mote_id, created=created,
        message=f"{label}{key} is now {verb} for {_unquote(mote_token)}",
    )


def _run_rule(conn, lhs: str, rhs: str) -> CommandResult:
    full = f"{lhs} == {rhs}"
    anchor_token = _rule_anchor(full)
    anchor_id, created = _find_or_create(conn, anchor_token)

    # `Anchor.prop` inside the equation means "this mote's own prop" once
    # the rule is attached to Anchor with applies_to -- the equation
    # language already reads a bare `prop` that way, so the anchor's own
    # prefix is stripped rather than left for connected-name resolution
    # to fail on (Anchor is not its own neighbor). Any *other* Name.prop
    # reference is left alone; it resolves only if that mote is actually
    # wired to Anchor (see the unresolved-names check below).
    self_prefix = re.compile(rf"\b{re.escape(anchor_token)}\.")
    equation = self_prefix.sub("", full)

    # Named from the stripped equation, not the raw typed line -- "Rule:
    # x == y * 2" reads as a rule at a glance (in the graph, in search);
    # the anchor-prefixed original would look like a plain second mote
    # that happens to share half its name with the anchor.
    name = f"Rule: {equation}"
    rule_id = _create_mote(conn, {
        "name": name if len(name) <= 80 else name[:77] + "...",
        rules_repo.EQUATION_KEY: equation,
    })
    _create_relationship(conn, rule_id, anchor_id, rules_repo.APPLIES_TO)

    # Stub every property the equation reads as "" wherever it isn't
    # already set -- purely visual, so `v` shows x and y right away
    # instead of looking untouched until the solver eventually runs.
    #
    # This used to also draw a default-type relationship from the anchor
    # to every other mote the equation named, because a reference only
    # resolved if the two were wired. Garden-scope names removed that
    # reason, and with it the edges: a reference is legal because the name
    # is unique, so the only relationships left in a garden are the ones
    # that mean something.
    own_props, other_props = _equation_property_refs(equation)
    _stub_properties(conn, anchor_id, own_props)
    for name_token, keys in other_props.items():
        matches = _lookup(conn, name_token)
        if len(matches) == 1:
            _stub_properties(conn, matches[0].id, keys)

    label = _label_for(anchor_token, created)
    message = f"{label}rule saved: {equation}"
    colliding = unresolved_equation_names(conn, anchor_id, equation)
    if colliding:
        hints = ", ".join(
            f"{m['name']} ({len(m['candidates'])} motes -- try "
            f"`{anchor_token} -> {m['name']}` to pick one)"
            for m in colliding
        )
        message += f" -- ambiguous from {anchor_token}: {hints}"
    return CommandResult(ok=True, mote_id=rule_id, created=True, message=message)


def _rule_anchor(relation: str) -> str:
    """Which mote a typed rule is *about* -- the first `Name.` in the
    line, in source order.

    With the left side pinned to `Mote.property` this was simply the
    thing being assigned to. It still is for that form: the first `Name.`
    in `R1.voltage == R1.current * R1.resistance` is R1 either way. What
    changes is that a rule no longer has to be written as an assignment
    to have an anchor -- `beam.I / beam.I0 == 0.1` is about `beam`, and
    reading the first mention is both predictable and what someone
    typing left to right means.

    Rejects a line that mentions no mote at all (`x == 5`): an
    `applies_to` has to point somewhere, and guessing which mote the
    person meant is exactly the kind of guess this bar doesn't make.
    Rejects one core/formula.py can't parse, rather than saving a rule
    that will only fail later, at the confusing distance of a run."""
    try:
        parse_equation(relation)
    except FormulaError as e:
        raise CommandError(str(e)) from None
    bases = attribute_bases(relation)
    if not bases:
        raise CommandError(
            f"{relation!r} doesn't say which mote it is about -- put a mote name "
            f"in front of one of its properties, e.g. "
            f"`R1.voltage == R1.current * R1.resistance`"
        )
    return bases[0]


def _equation_property_refs(equation: str) -> tuple[set[str], dict[str, set[str]]]:
    """Every property `equation` reads, split into the anchor's own (a
    bare identifier -- the anchor's own prefix is already stripped by the
    time this runs) and other motes' (`Name.prop`). Feeds the empty-stub
    step in `_run_rule` only; never raises; a formula that doesn't parse
    or is empty simply refers to nothing."""
    try:
        tree = ast.parse(equation, mode="eval")
    except SyntaxError:
        return set(), {}

    attribute_value_ids = set()
    call_func_ids = set()
    other: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            other.setdefault(node.value.id, set()).add(node.attr)
            attribute_value_ids.add(id(node.value))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            call_func_ids.add(id(node.func))
    call_func_ids |= function_reference_ids(tree)  # `phi` in integrate(phi, ...)

    own = {
        node.id for node in ast.walk(tree)
        if isinstance(node, ast.Name)
        and id(node) not in attribute_value_ids
        and id(node) not in call_func_ids
        and node.id != "t"  # the reserved tick index, not a property -- core/reserved.py
        and node.id not in MATH_CONSTANTS  # bare `pi`/`e`, not a property -- core/formula.py
    }
    return own, other


def _stub_properties(conn, mote_id: str, keys) -> None:
    mote = motes_repo.get_mote(conn, mote_id)
    missing = {key: "" for key in keys if key not in mote.properties}
    if missing:
        _update_mote(conn, mote_id, missing)


def _run_relate(conn, from_token: str, rest: str) -> CommandResult:
    # A second '->' in `rest` means an explicit type was given:
    # "R1 -> owns -> R2" splits into type="owns", to="R2".
    if "->" in rest:
        type_token, _, to_token = rest.partition("->")
        type_ = type_token.strip()
        if not re.fullmatch(_IDENTIFIER, type_):
            raise CommandError(f"relationship type must be one word, got '{type_}'")
    else:
        type_, to_token = _DEFAULT_REL_TYPE, rest

    from_id, from_created = _find_or_create(conn, from_token)
    to_id, to_created = _find_or_create(conn, to_token)
    _create_relationship(conn, from_id, to_id, type_)

    created_tokens = [t for t, c in ((from_token, from_created), (to_token, to_created)) if c]
    note = f" ({', '.join(created_tokens)} created)" if created_tokens else ""
    return CommandResult(
        ok=True, mote_id=from_id, created=from_created,
        message=f"{from_token} --{type_}--> {to_token}{note}",
    )


def _run_species(conn, mote_token: str, species_token: str) -> CommandResult:
    mote_id, mote_created = _find_or_create(conn, mote_token)
    species_id, species_created = _find_or_create(conn, species_token)

    # Set, not add: `:` is one slot, same as the props pane's species
    # control, so a second use replaces rather than accumulates.
    for rel in rel_repo.list_relationships(conn, from_id=mote_id, type_=species_repo.INSTANCE_OF):
        _delete_relationship(conn, rel)
    _create_relationship(conn, mote_id, species_id, species_repo.INSTANCE_OF)

    created_tokens = [t for t, c in ((mote_token, mote_created), (species_token, species_created)) if c]
    note = f" ({', '.join(created_tokens)} created)" if created_tokens else ""
    return CommandResult(
        ok=True, mote_id=mote_id, created=mote_created,
        message=f"{mote_token} : {species_token}{note}",
    )


def _run_delete(conn, target: str, confirm: bool) -> CommandResult:
    if "." in target:
        mote_token, key = target.rsplit(".", 1)
        mote_id = _resolve_one(conn, mote_token)
        _delete_property(conn, mote_id, key)
        return CommandResult(ok=True, mote_id=mote_id, message=f"deleted {mote_token}.{key}")

    mote_id = _resolve_one(conn, target)
    if not confirm:
        return CommandResult(
            ok=False, mote_id=mote_id, needs_confirm=True,
            message=f"press Enter again to delete {target} and its relationships",
        )
    _delete_mote(conn, mote_id)
    return CommandResult(ok=True, message=f"deleted {target}")


def _run_navigate(conn, target: str) -> CommandResult:
    mote_id, created = _find_or_create(conn, target)
    label = f"{_unquote(target)} (new)" if created else target
    return CommandResult(ok=True, mote_id=mote_id, created=created, message=label)


# ---- name resolution --------------------------------------------------

def _lookup(conn: sqlite3.Connection, token: str) -> list[Mote]:
    token = _unquote(token)
    id_match = _ID_REF_RE.match(token)
    if id_match:
        prefix = id_match.group(1)
        exact = motes_repo.get_mote(conn, prefix)
        if exact:
            return [exact]
        return [m for m in motes_repo.list_motes(conn) if m.id.startswith(prefix)]

    needle = token.strip().lower()
    return [
        m for m in motes_repo.list_motes(conn)
        if str(m.properties.get("name", "")).strip().lower() == needle
    ]


def _resolve_one(conn, token: str, create_if_missing: bool = False) -> str:
    mote_id, _created = _find_or_create(conn, token, create_if_missing)
    return mote_id


def _find_or_create(conn, token: str, create_if_missing: bool = True) -> tuple[str, bool]:
    matches = _lookup(conn, token)
    if len(matches) == 1:
        return matches[0].id, False
    if len(matches) > 1:
        raise AmbiguousName(token, matches)
    if not create_if_missing:
        raise CommandError(f"no mote named '{token}'")
    new_id = _create_mote(conn, {"name": _unquote(token)})
    return new_id, True


def _unquote(token: str) -> str:
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _label_for(token: str, created: bool) -> str:
    return f"{_unquote(token)} ({'new' if created else 'found'}), " if created else ""


def _choice(conn, mote: Mote) -> dict:
    species = species_repo.species_of(conn, mote.id)
    return {
        "id": mote.id,
        "label": str(mote.properties.get("name") or mote.id),
        "speciesLabel": str(species[0].properties.get("name") or species[0].id) if species else None,
    }


# ---- value + unit parsing ----------------------------------------------

def property_update(old_props: dict, key: str, raw) -> tuple[dict, object, str | None]:
    """What `key = raw` writes onto a mote holding `old_props`: the value
    read as `R1.x = 5 ohm` reads it, and the mote's `units` map with the
    key relabelled when a unit was typed. Returns (properties to write,
    the value, the unit typed or None).

    The one place a typed value becomes a write: the statement form and
    the `set` verb (core/batch.py) both come through here."""
    value, unit_name = parse_value(raw)
    new = {key: value}
    if unit_name:
        unit_map = units.parse_units_property(old_props.get(units.UNITS_KEY))
        unit_map[key] = unit_name
        new[units.UNITS_KEY] = unit_map
    return new, value, unit_name


def parse_value(text: str):
    """`"500 ohms"` -> (500, "ohm"); `"500"` -> (500, None); anything else
    tries JSON (`"true"` -> True, `"[1, 2]"` -> [1, 2]) and falls back to
    the literal string.

    Public because it is the one answer to "what did that typed value
    mean": core/batch.py's bulk write reads a value exactly as `R1.x = 5
    ohm` does here, rather than growing a second, unit-blind reading of
    the same keystrokes."""
    if not isinstance(text, str):
        # Already a value rather than something typed -- a bridge handing
        # over a number or a bool has nothing to parse.
        return text, None
    text = text.strip()
    head, _, tail = text.rpartition(" ")
    if not head and text.endswith("%"):
        # Percent is the one unit nobody writes with a space in front of
        # it: `50%`, never `50 %`. Splitting it off here keeps the
        # general rule ("number, space, unit") intact rather than making
        # every unit suffix-matchable.
        head, tail = text[:-1], "%"
    if head:
        number = _to_number(head)
        unit_name = _resolve_unit(tail)
        if number is not None and unit_name:
            return number, unit_name
    return _coerce(text), None


def _to_number(text: str):
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return None


def _resolve_unit(token: str) -> str | None:
    token = token.strip()
    if not token:
        return None
    if units.is_unit(token):
        return token
    # Plural tolerance only -- "ohms" for "ohm" -- not a second unit
    # grammar: core/units.py's own registry decides everything past this.
    if token.endswith("s") and units.is_unit(token[:-1]):
        return token[:-1]
    return None


def _coerce(raw: str):
    import json
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw
