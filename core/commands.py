# core/commands.py
"""The console's merged grammar (docs/console.md -> Command line and
grammar). One line typed at the `$` prompt opens with one of three
things, decided by its first token alone:

    mo tion             `mo` and an app suffix: open that app (CONSOLE_APPS
                        below). The command line draws the typed `mo ` as
                        the [mo] box, so the line reads as the lockup.
    layout save two     a verb (core/reserved.py::RESERVED_VERBS)
    R1.x = 5 ohm        anything else: a modeling statement, handed to
                        core/command_bar.py::run_command unchanged

Verb-first, quote to escape. A first token that *is* a reserved word --
case-sensitive, lowercase -- is that word, even if a mote shares its
name. A quoted first token (`"log" -> R1`) or an `#id` is never a verb,
which is how such a mote stays reachable; whoever creates one is warned
(run_statement below). Nothing else about command_bar's grammar changes.

`@sel` stands for the selection's primary mote wherever a mote id is
expected: expand_sel() swaps it for `#<id>` before parsing.

Pure: no UI, no Api. The console's root API (console/api.py) is one
caller; the CLI or MCP can run the same lines."""
import re
import shlex
import sqlite3
from dataclasses import dataclass, replace
from typing import Sequence

from core import batch
from core import command_bar
from core import motes as motes_repo
from core import relationships as rel_repo
from core import species as species_repo
from core import undo
from core import units
from core.command_bar import CommandError, CommandResult
from core.reserved import RESERVED_VERBS, reserved_words


@dataclass(frozen=True)
class ConsoleApp:
    """One app the console can open by suffix (`mo tion`).

    `default_view` is None until the app's port step lands; while it is,
    `legacy` names the old window to launch instead (an api_common.APPS
    id, or "gardens" for the Gardens window). This table is the single
    source of truth for which apps are ported."""
    suffix: str
    name: str
    default_view: str | None = None
    legacy: str | None = None
    default_command: str | None = None  # canonical command for default_view
    # An app that opens several panes at once (`mo tion`): a layout tree
    # in the shell's JSON shape (console/frontend/js/tree.js) that takes
    # the focused pane's place. None opens `default_command` alone.
    layout: dict | None = None


CONSOLE_APPS: tuple[ConsoleApp, ...] = (
    ConsoleApp("nolith", "Monolith", default_view="monolith:table", default_command="find"),
    ConsoleApp("te", "Inspector", default_view="mote:props", default_command="open @sel"),
    ConsoleApp("xie", "Moxie", default_view="moxie:plot", default_command="plot"),
    ConsoleApp("tion", "Motion", default_view="motion:state", default_command="sim",
               layout={"split": "row", "sizes": [0.24, 0.46, 0.3], "children": [
                   {"command": "scenarios"}, {"command": "sim"}, {"command": "rule @sel"}]}),
    ConsoleApp("ra", "Mora", default_view="mora:edit", default_command="edit @sel",
               layout={"split": "row", "sizes": [0.22, 0.39, 0.39], "children": [
                   {"command": "notes"}, {"command": "edit @sel"}, {"command": "read @sel"}]}),
    ConsoleApp("nastery", "Monastery", legacy="monastery"),
    ConsoleApp("narch", "Monarch", legacy="monarch"),
    ConsoleApp("saic", "Home", legacy="gardens"),
)

# Verbs a view answers directly, rather than the root API acting on them.
VERB_VIEWS: dict[str, str] = {"log": "log", "scenarios": "motion:scenarios",
                              "worksheet": "mora:worksheet"}
# Verbs a view answers *with* their arguments: the whole line is the
# pane's command (`find status:open --sort due`).
ARG_VIEWS: dict[str, str] = {"find": "monolith:table", "open": "mote:props", "plot": "moxie:plot",
                             "fit": "moxie:fit", "sim": "motion:state", "rule": "motion:rule",
                             "why": "motion:rule", "edit": "mora:edit", "read": "mora:read",
                             "notes": "mora:notes"}

SET_VERB = "set"
CLEAR_FLAG = "--clear"
SCENARIO_FLAG = "--scenario"

LAYOUT_OPS: dict[str, str] = {
    "save": "save the current panes as ‹name›",
    "load": "restore a saved layout",
    "list": "list saved layouts",
    "rm": "delete a saved layout",
}

APP, VERB, STATEMENT = "app", "verb", "statement"
APP_VERB = "mo"

_ESCAPE_CHARS = ("'", '"', "#")
_FIRST_TOKEN_RE = re.compile(r"(\S*)(\s*)(.*)", re.DOTALL)
_WORD_PREFIX_RE = re.compile(r"[a-z]*")
_SEL = "@sel"


def find_app(suffix: str) -> ConsoleApp | None:
    return next((app for app in CONSOLE_APPS if app.suffix == suffix), None)


def app_description(app: ConsoleApp) -> str:
    if app.default_view:
        return f"{app.name} — {app.default_view}"
    if app.legacy:
        return f"{app.name} — old window"
    return f"{app.name} — not available yet"


# ---- parsing ------------------------------------------------------------

@dataclass(frozen=True)
class ParsedLine:
    kind: str  # APP, VERB or STATEMENT
    head: str  # the reserved word; "" for a statement
    args: tuple[str, ...]  # shlex tokens after `head`; () for a statement
    text: str  # the whole line, stripped


def parse(line: str) -> ParsedLine:
    """Which of the three forms `line` is. Raises CommandError only for
    a verb or app line whose arguments don't tokenize (an unclosed
    quote); a statement is never judged here -- run_command does that."""
    text = (line or "").strip()
    word, _sep, rest = _FIRST_TOKEN_RE.match(text).groups()
    if not word or word.startswith(_ESCAPE_CHARS):
        return ParsedLine(STATEMENT, "", (), text)
    if word not in RESERVED_VERBS:
        return ParsedLine(STATEMENT, "", (), text)
    try:
        args = tuple(shlex.split(rest))
    except ValueError as e:
        raise CommandError(f"{word}: {e}") from None
    if word == APP_VERB:
        # `mo tion`: the suffix is the head, anything after it the args.
        return ParsedLine(APP, args[0] if args else "", args[1:], text)
    return ParsedLine(VERB, word, args, text)


def expand_sel(line: str, selection: Sequence[str]) -> str:
    """`line` with every `@sel` outside quotes replaced by `#<primary
    id>` -- the first item of `selection`. Raises CommandError when the
    line uses `@sel` and nothing is selected. Batch (whole-set)
    semantics arrive with `set` in U2."""
    ref = f"#{selection[0]}" if selection else ""
    out: list[str] = []
    quote: str | None = None
    used = False
    i = 0
    while i < len(line):
        ch = line[i]
        if quote is None and _sel_at(line, i):
            out.append(ref)
            used = True
            i += len(_SEL)
            continue
        if ch in ("'", '"'):
            quote = None if quote == ch else (ch if quote is None else quote)
        out.append(ch)
        i += 1
    if used and not selection:
        raise CommandError("@sel is empty -- select a mote first")
    return "".join(out)


def resolve_view(command: str) -> tuple[str, str]:
    """(view id, canonical command) for the view that answers `command`
    -- what a pane mounts when a layout restores it. `""` is the empty
    pane (the view picker). Raises CommandError for a line no view
    answers."""
    parsed = parse(command)
    if not parsed.text:
        return "", ""
    if parsed.kind == VERB and parsed.head in VERB_VIEWS and not parsed.args:
        return VERB_VIEWS[parsed.head], parsed.text
    if parsed.kind == VERB and parsed.head in ARG_VIEWS:
        if parsed.head == "plot" and parsed.args[:1] in (("save",), ("export",)):
            raise CommandError(f"{parsed.text!r} is an action, not a pane")
        if parsed.head == "fit" and "--apply" in parsed.args:
            raise CommandError(f"{parsed.text!r} is a write, not a pane")
        return ARG_VIEWS[parsed.head], parsed.text
    if parsed.kind == APP and not parsed.args:
        app = find_app(parsed.head)
        if app is not None and app.default_view:
            return app.default_view, app.default_command or app.suffix
    raise CommandError(f"no view answers {parsed.text!r}")


def _sel_at(line: str, i: int) -> bool:
    if not line.startswith(_SEL, i):
        return False
    end = i + len(_SEL)
    before_ok = i == 0 or not _is_word_char(line[i - 1])
    after_ok = end >= len(line) or not _is_word_char(line[end])
    return before_ok and after_ok


def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


# ---- set ----------------------------------------------------------------

@dataclass(frozen=True)
class SetResult:
    """What a write verb did: a line for the log, how many motes it
    touched, and its writes as one undo step for the caller to push."""
    message: str
    count: int
    command: "undo.Command | None" = None  # push it: the whole batch is one undo step


WriteResult = SetResult


def run_set(conn: sqlite3.Connection, args: Sequence[str], selection: Sequence[str]) -> SetResult:
    """`set <target> <key> <value…>` or `set <target> <key> --clear`.

    `<target>` is `@sel` -- the **whole** selection, not just its primary,
    which is what makes `set` the batch edit (docs/console.md ->
    Selection) -- or `#<id>`, or a mote's name. The value is read the way
    `R1.x = 5 ohm` is (core/batch.py), so a unit lands in `units`.
    Raises CommandError for a line that says nothing it can do."""
    usage = f"usage: {RESERVED_VERBS[SET_VERB]['usage']}"
    args, scenario = _scenario_flag(args, usage)
    args = _dotted(conn, args)
    if len(args) < 3:
        raise CommandError(usage)
    target, key, rest = args[0], args[1], list(args[2:])
    ids = _set_targets(conn, target, selection)
    if scenario is not None:
        return _set_in_scenario(conn, scenario, ids, key, rest, usage)
    if rest == [CLEAR_FLAG]:
        count, command = batch.clear_property(conn, ids, key)
        verb = "cleared"
    elif CLEAR_FLAG in rest:
        raise CommandError(f"{CLEAR_FLAG} takes no value -- {usage}")
    else:
        count, command = batch.set_property(conn, ids, key, " ".join(rest))
        verb = "set"
    if count == 0 and verb == "set":
        raise CommandError("nothing to set -- the target no longer exists")
    noun = "mote" if count == 1 else "motes"
    return SetResult(f"{verb} {key} on {count} {noun}", count, command)


def _scenario_flag(args: Sequence[str], usage: str) -> tuple[tuple[str, ...], str | None]:
    """`--scenario ‹s›` anywhere in a set line: pulled out, its value kept."""
    args = list(args)
    if SCENARIO_FLAG not in args:
        return tuple(args), None
    i = args.index(SCENARIO_FLAG)
    if i + 1 >= len(args):
        raise CommandError(f"{SCENARIO_FLAG} needs a scenario -- {usage}")
    scenario = args[i + 1]
    del args[i:i + 2]
    return tuple(args), scenario


def _dotted(conn: sqlite3.Connection, args: tuple[str, ...]) -> tuple[str, ...]:
    """`set C1.voltage 30` is `set C1 voltage 30` -- the worksheet's way
    of naming a value. Only when the first word is not itself a mote's
    whole name, so a mote called `v1.2` is still reached as it always was."""
    if len(args) < 2 or "." not in args[0]:
        return args
    target, _dot, key = args[0].rpartition(".")
    if not target or not key:
        return args
    if not args[0].startswith(("#", _SEL)) and any(
            m.properties.get("name") == args[0] for m in motes_repo.list_motes(conn)):
        return args
    return (target, key, *args[1:])


def _set_in_scenario(conn, scenario: str, ids: list[str], key: str, rest: list[str],
                     usage: str) -> SetResult:
    """`set --scenario hot C1.voltage 30`: the value goes on that
    scenario's override of C1 (created, with the scenario itself, if
    need be), never on C1. `--clear` takes the override's value away, so
    the scenario reads the garden's again."""
    from core import motion
    if CLEAR_FLAG in rest and rest != [CLEAR_FLAG]:
        raise CommandError(f"{CLEAR_FLAG} takes no value -- {usage}")
    scenario_id, steps = motion.scenario_for_write(conn, scenario)
    overrides = []
    for mote_id in ids:
        override_id, created = motion.override_for_write(conn, scenario_id, mote_id)
        steps += created
        overrides.append((mote_id, override_id))
    override_ids = [o for _m, o in overrides]
    if rest == [CLEAR_FLAG]:
        count, command = batch.clear_property(conn, override_ids, key)
        verb = "cleared"
    else:
        count, command = batch.set_property(conn, override_ids, key, " ".join(rest))
        verb = "set"
    if command is not None:
        steps.append(command)
    if verb == "set":
        for mote_id, override_id in overrides:
            steps += _keep_units(conn, mote_id, override_id)
    noun = "mote" if count == 1 else "motes"
    message = f"{verb} {key} on {count} {noun} under {scenario}"
    return SetResult(message, count, undo.combine(message, steps) if steps else None)


def _keep_units(conn, mote_id: str, override_id: str) -> list:
    """An override replaces whole properties (core/scenarios.py::apply),
    so a unit written on it would replace the target's whole `units` map.
    Its map is filled out from the target's, so only the one entry differs."""
    override = motes_repo.get_mote(conn, override_id).properties
    if units.UNITS_KEY not in override:
        return []
    target = motes_repo.get_mote(conn, mote_id).properties
    merged = {**units.parse_units_property(target.get(units.UNITS_KEY)),
              **units.parse_units_property(override[units.UNITS_KEY])}
    if merged == override[units.UNITS_KEY]:
        return []
    before = {units.UNITS_KEY: override[units.UNITS_KEY]}
    motes_repo.update_mote(conn, override_id, {units.UNITS_KEY: merged})
    return [undo.for_update_mote(conn, override_id, before, {units.UNITS_KEY: merged},
                                 label="scenario units")]


# ---- rename, unlink, template ------------------------------------------
#
# The props pane's edits as commands (docs/console.md -> Settled for U3):
# each is a line, so it is in the history, the log and undo, and the CLI
# or MCP could run the same one. `@sel` is the whole selection for all of
# them, as for `set`.

def run_rename(conn: sqlite3.Connection, args: Sequence[str], selection: Sequence[str]) -> SetResult:
    """`rename <target> <old> <new>`: the value moves to the new key, and
    so does its entry in `units`. A mote that already has `<new>` is
    refused rather than overwritten -- a rename that silently destroyed a
    value would be an edit nobody asked for."""
    if len(args) != 3:
        raise CommandError(f"usage: {RESERVED_VERBS['rename']['usage']}")
    target, old, new = args
    if not new.strip() or new == old:
        raise CommandError("rename needs a different new key")
    ids = _set_targets(conn, target, selection)
    steps: list[undo.Command] = []
    for mote_id in ids:
        props = motes_repo.get_mote(conn, mote_id).properties
        if old not in props:
            continue
        if new in props:
            _undo_all(steps)
            raise CommandError(f"{_name_of(props, mote_id)} already has {new!r} -- clear it first")
        updates = {new: props[old]}
        before = {}
        unit_map = units.parse_units_property(props.get(units.UNITS_KEY))
        if old in unit_map:
            before[units.UNITS_KEY] = props[units.UNITS_KEY]
            unit_map[new] = unit_map.pop(old)
            updates[units.UNITS_KEY] = unit_map
        motes_repo.update_mote(conn, mote_id, updates)
        motes_repo.delete_property(conn, mote_id, old)
        steps.append(undo.combine(f"rename {old}", [
            undo.for_update_mote(conn, mote_id, before, updates, label=f"rename {old}"),
            undo.for_delete_property(conn, mote_id, old, props[old], label=f"rename {old}"),
        ]))
    if not steps:
        raise CommandError(f"no {old!r} to rename")
    return _written(f"renamed {old} to {new}", steps)


def run_unlink(conn: sqlite3.Connection, args: Sequence[str], selection: Sequence[str]) -> SetResult:
    """`unlink <target> <type> <other>` removes every `<type>` relationship
    between the two, in either direction; `unlink <target> #<rel id>`
    removes that one, which is what the props pane runs for a row."""
    usage = f"usage: {RESERVED_VERBS['unlink']['usage']}"
    if len(args) not in (2, 3):
        raise CommandError(usage)
    ids = set(_set_targets(conn, args[0], selection))
    rels = rel_repo.list_relationships(conn)
    if len(args) == 2:
        if not args[1].startswith("#"):
            raise CommandError(usage)
        wanted = [r for r in rels if r.id == args[1][1:] and ids & {r.from_id, r.to_id}]
        if not wanted:
            raise CommandError(f"no relationship {args[1]} on that mote")
    else:
        type_, other = args[1], args[2]
        others = set(_set_targets(conn, other, selection))
        wanted = [
            r for r in rels if r.type == type_
            and ((r.from_id in ids and r.to_id in others) or (r.to_id in ids and r.from_id in others))
        ]
        if not wanted:
            raise CommandError(f"no {type_!r} relationship between them")
    steps = []
    for rel in wanted:
        if rel_repo.delete_relationship(conn, rel.id):
            steps.append(undo.for_delete_relationship(
                conn, rel.id, rel.from_id, rel.to_id, rel.type, label=f"unlink {rel.type}"))
    noun = "relationship" if len(steps) == 1 else "relationships"
    return _written(f"removed {len(steps)} {noun}", steps)


def run_template(conn: sqlite3.Connection, args: Sequence[str], selection: Sequence[str]) -> SetResult:
    """`template <mote> <species>`: that mote is now the one new instances
    of the species start from. Replaces any earlier template rather than
    adding one, since a species answers with only its first
    (core/species.py::template_for) -- the same rule the old panel's
    button follows."""
    if len(args) != 2:
        raise CommandError(f"usage: {RESERVED_VERBS['template']['usage']}")
    (mote_id,) = one_target(conn, args[0], selection)
    (species_id,) = one_target(conn, args[1], selection)
    steps = []
    for rel in rel_repo.list_relationships(conn, type_=species_repo.TEMPLATE_OF):
        if rel.to_id == species_id and rel_repo.delete_relationship(conn, rel.id):
            steps.append(undo.for_delete_relationship(
                conn, rel.id, rel.from_id, rel.to_id, rel.type, label="template"))
    rel_id = species_repo.set_template(conn, mote_id, species_id)
    steps.append(undo.for_create_relationship(
        conn, rel_id, mote_id, species_id, species_repo.TEMPLATE_OF, label="template"))
    return _written(f"{args[0]} is now the template of {args[1]}", steps)


WRITE_VERBS = {"set": run_set, "rename": run_rename, "unlink": run_unlink, "template": run_template}


def one_target(conn, target: str, selection: Sequence[str]) -> list[str]:
    """Exactly one mote for `target` (`@sel` is the primary here)."""
    ids = _set_targets(conn, target, selection[:1])
    if len(ids) != 1:
        raise CommandError(f"{target} must name one mote")
    return ids


def _written(message: str, steps: list) -> SetResult:
    return SetResult(message, len(steps), undo.combine(message, steps) if steps else None)


def _undo_all(steps: list) -> None:
    for step in reversed(steps):
        step.undo()


def _name_of(properties: dict, mote_id: str) -> str:
    name = properties.get("name")
    return name if isinstance(name, str) and name else f"#{mote_id[:8]}"


def _set_targets(conn: sqlite3.Connection, target: str, selection: Sequence[str]) -> list[str]:
    if target == _SEL:
        if not selection:
            raise CommandError("@sel is empty -- select a mote first")
        return list(selection)
    if target.startswith("#"):
        mote_id = target[1:]
        if motes_repo.get_mote(conn, mote_id) is None:
            raise CommandError(f"no mote {target}")
        return [mote_id]
    named = [m.id for m in motes_repo.list_motes(conn) if m.properties.get("name") == target]
    if not named:
        raise CommandError(f"no mote named {target!r}")
    if len(named) > 1:
        raise CommandError(f"{len(named)} motes are named {target!r} -- use #id or @sel")
    return named


# ---- statements ---------------------------------------------------------

def run_statement(conn: sqlite3.Connection, text: str, confirm: bool = False) -> CommandResult:
    """core/command_bar.py::run_command, plus the one thing the console
    adds: a warning when the line just created a mote whose name is a
    reserved word, since that mote can't be reached by typing its bare
    name at the start of a line any more."""
    result = command_bar.run_command(conn, text, confirm=confirm)
    warning = _created_reserved_warning(conn, result)
    if warning:
        result = replace(result, message=result.message + warning)
    return result


def reserved_name_warning(name: str, mote_id: str) -> str | None:
    if name not in reserved_words():
        return None
    return (f' -- "{name}" is a console word, so reach this mote as '
            f'"{name}" (quoted) or #{mote_id[:8]}')


def _created_reserved_warning(conn: sqlite3.Connection, result: CommandResult) -> str | None:
    if not (result.ok and result.created and result.mote_id):
        return None
    mote = motes_repo.get_mote(conn, result.mote_id)
    name = mote.properties.get("name") if mote else None
    return reserved_name_warning(name, mote.id) if isinstance(name, str) else None


# ---- completion ---------------------------------------------------------

@dataclass(frozen=True)
class CompletionRow:
    value: str
    description: str = ""
    dim: bool = False
    current: bool = False


@dataclass(frozen=True)
class Completion:
    """`replace_from` is an index into the line: accepting rows[i]
    replaces line[replace_from:cursor] with its value. `ghost` is what
    the best row adds after what is already typed."""
    hint: str = ""
    rows: tuple[CompletionRow, ...] = ()
    ghost: str = ""
    replace_from: int = 0


def complete(
    conn: sqlite3.Connection,
    line: str,
    cursor: int | None = None,
    *,
    gardens: Sequence[str] = (),
    layouts: Sequence[str] = (),
    current_garden: str | None = None,
) -> Completion:
    """Completion for the token ending at `cursor` (end of line by
    default). The first token offers app suffixes, verbs and mote names;
    after `cd` and `layout` their own arguments; anything else is
    core/command_bar.py::suggest."""
    line = line or ""
    cursor = len(line) if cursor is None else max(0, min(cursor, len(line)))
    head = line[:cursor]
    lead = len(head) - len(head.lstrip())
    word, sep, rest = _FIRST_TOKEN_RE.match(head[lead:]).groups()
    if not sep:
        if _WORD_PREFIX_RE.fullmatch(word):
            return _complete_first(conn, line, cursor, word, lead)
        return _complete_statement(conn, line, cursor)
    if word == APP_VERB:
        done = rest.split() if not rest or rest[-1].isspace() else rest.split()[:-1]
        if done:
            return Completion(replace_from=cursor)
        return _complete_app("" if not rest or rest[-1].isspace() else rest.split()[-1], cursor)
    if word == "find":
        return _complete_from(RESERVED_VERBS[word]["usage"], _find_choices(conn, rest), rest, cursor)
    if word == SET_VERB:
        return _complete_from(RESERVED_VERBS[word]["usage"], _set_choices(conn, rest), rest, cursor)
    if word == "plot":
        from core import plotcmd
        rows = [CompletionRow(c.value, c.description) for c in plotcmd.choices(conn, rest)]
        return _complete_from(RESERVED_VERBS[word]["usage"], rows, rest, cursor)
    if word == "fit":
        from core import fit
        rows = [CompletionRow(c.value, c.description) for c in fit.choices(conn, rest)]
        return _complete_from(RESERVED_VERBS[word]["usage"], rows, rest, cursor)
    if word in ("sim", "rule", "why", "solve", "step"):
        from core import motion
        rows = [CompletionRow(v, d) for v, d in motion.choices(conn, word, rest)]
        return _complete_from(RESERVED_VERBS[word]["usage"], rows, rest, cursor)
    if word in ("edit", "read"):
        return _complete_from(RESERVED_VERBS[word]["usage"], _note_choices(conn, rest), rest, cursor)
    if word in ("open", "rename", "unlink", "template"):
        return _complete_from(RESERVED_VERBS[word]["usage"], _u3_choices(conn, word, rest), rest, cursor)
    if word in RESERVED_VERBS:
        choices = _verb_choices(word, rest, gardens, layouts, current_garden)
        return _complete_from(RESERVED_VERBS[word]["usage"], choices, rest, cursor)
    return _complete_statement(conn, line, cursor)


def _complete_first(conn, line: str, cursor: int, partial: str, lead: int) -> Completion:
    # An app you can open outranks one that isn't available yet, so `t`
    # offers `tion` before `te` (3e) and the ghost never suggests a dead end.
    verbs = [
        CompletionRow(verb, meta["describe"])
        for verb, meta in RESERVED_VERBS.items() if verb.startswith(partial)
    ]
    words = reserved_words()
    motes = [
        CompletionRow(name)
        for name in command_bar.suggest(conn, line, cursor) if name not in words
    ]
    rows = tuple(verbs + motes)
    return Completion("", rows, _ghost(rows, partial), lead)


def _complete_app(partial: str, cursor: int) -> Completion:
    """After `mo `: the apps. One you can open outranks one that isn't
    available yet, so `mo t` offers `tion` before a dead end (3e), and the
    apps that don't match stay listed, dimmed."""
    matching = [app for app in CONSOLE_APPS if app.suffix.startswith(partial)]
    apps = [_app_row(app) for app in sorted(matching, key=_unavailable)]
    dim = [
        _app_row(app, dim=True) for app in CONSOLE_APPS if not app.suffix.startswith(partial)
    ]
    rows = tuple(apps + dim)
    return Completion("mo ‹app›", rows, _ghost(rows, partial), cursor - len(partial))


def _unavailable(app: ConsoleApp) -> bool:
    return not (app.default_view or app.legacy)


def _app_row(app: ConsoleApp, dim: bool = False) -> CompletionRow:
    return CompletionRow(app.suffix, app_description(app), dim=dim)


def _verb_choices(verb, rest, gardens, layouts, current_garden) -> list[CompletionRow]:
    done = rest.split() if not rest or rest[-1].isspace() else rest.split()[:-1]
    if verb == "cd" and not done:
        return [CompletionRow(g, current=g == current_garden) for g in gardens]
    if verb == "layout" and not done:
        return [CompletionRow(op, desc) for op, desc in LAYOUT_OPS.items()]
    if verb == "layout" and len(done) == 1 and done[0] in ("save", "load", "rm"):
        return [CompletionRow(name) for name in layouts]
    return []


def _schema(conn):
    from core import table as table_repo  # core.table imports this module's neighbours
    motes, relationships, index = table_repo.read(conn)
    return table_repo.Schema.of(index, motes, relationships)


def _find_choices(conn, rest: str) -> list[CompletionRow]:
    """Field names, then a field's own values once it has an operator
    (core/findtext.py::find_completions); after `--sort`, `--group` or
    `--cols`, bare field names; a `-` starts a flag."""
    from core import findtext
    words = rest.split()
    partial = "" if not rest or rest[-1].isspace() else words[-1]
    before = words[-2] if partial and len(words) > 1 else (words[-1] if words and not partial else None)
    schema = _schema(conn)
    if partial.startswith("-") and before not in findtext.FLAGS:
        return [CompletionRow(flag) for flag in findtext.FLAGS]
    if before in (findtext.SORT_FLAG, findtext.GROUP_FLAG, findtext.COLS_FLAG):
        return [CompletionRow(value.rstrip(":"), description)
                for value, description in findtext.find_completions(schema, "")]
    return [CompletionRow(value, description)
            for value, description in findtext.find_completions(schema, partial)]


def _set_choices(conn, rest: str) -> list[CompletionRow]:
    """`@sel` and mote names for the target, then property keys."""
    done = rest.split() if not rest or rest[-1].isspace() else rest.split()[:-1]
    if not done:
        names = sorted({m.properties["name"] for m in motes_repo.list_motes(conn)
                        if isinstance(m.properties.get("name"), str)})
        return [CompletionRow(_SEL, "the whole selection")] + [CompletionRow(n) for n in names]
    from core import query
    if len(done) == 1:
        return [CompletionRow(c.field.key, f"{c.kind}, {c.fill}")
                for c in _schema(conn).columns
                if c.field.source == query.PROPERTY and not c.reserved]
    if len(done) == 2:
        column = _schema(conn).column(query.Field(query.PROPERTY, done[1]))
        return [CompletionRow(value) for value in column.values] + [
            CompletionRow("--clear", "remove the property")]
    return []


def _u3_choices(conn, verb: str, rest: str) -> list[CompletionRow]:
    """Targets first (`@sel` and names), then what each verb takes next:
    a property key to rename, a relationship type then another mote to
    unlink, a species to be the template of."""
    done = rest.split() if not rest or rest[-1].isspace() else rest.split()[:-1]
    names = sorted({m.properties["name"] for m in motes_repo.list_motes(conn)
                    if isinstance(m.properties.get("name"), str)})
    targets = [CompletionRow(_SEL, "the selection")] + [CompletionRow(n) for n in names]
    if not done:
        return targets if verb != "template" else [CompletionRow(n) for n in names]
    if verb == "rename" and len(done) == 1:
        return _set_choices(conn, "x ")
    if verb == "unlink" and len(done) == 1:
        types = sorted({r.type for r in rel_repo.list_relationships(conn)})
        return [CompletionRow(t) for t in types]
    if (verb == "unlink" and len(done) == 2) or (verb == "template" and len(done) == 1):
        return [CompletionRow(n) for n in names]
    return []


def _note_choices(conn, rest: str) -> list[CompletionRow]:
    """`@sel`, then the notes, then every other named mote (a new note)."""
    from core import modoc
    if rest.split() and rest[-1].isspace():
        return []
    motes = motes_repo.list_motes(conn)
    notes = sorted({m.properties["name"] for m in motes if isinstance(m.properties.get("name"), str)
                    and modoc.has_modoc(m.properties)})
    others = sorted({m.properties["name"] for m in motes if isinstance(m.properties.get("name"), str)}
                    - set(notes))
    quote = lambda n: shlex.quote(n)  # noqa: E731
    return ([CompletionRow("@sel", "the selection")]
            + [CompletionRow(quote(n), "note") for n in notes]
            + [CompletionRow(quote(n), "no note yet", dim=True) for n in others])


def _complete_from(hint: str, choices: list[CompletionRow], rest: str, cursor: int) -> Completion:
    partial = "" if not rest or rest[-1].isspace() else rest.split()[-1]
    needle = partial.lower()
    rows = tuple(row for row in choices if row.value.lower().startswith(needle))
    return Completion(hint, rows, _ghost(rows, partial), cursor - len(partial))


def _complete_statement(conn, line: str, cursor: int) -> Completion:
    head = line[:cursor]
    partial = _statement_partial(head)
    rows = tuple(CompletionRow(value) for value in command_bar.suggest(conn, line, cursor))
    return Completion("", rows, _ghost(rows, partial), cursor - len(partial))


def _statement_partial(head: str) -> str:
    """The token command_bar.suggest completes, by its own rules: the
    property after an `Name.`, else the trailing identifier."""
    prop_match = command_bar._PROP_TOKEN_RE.search(head)
    if prop_match:
        return prop_match.group(2)
    name_match = command_bar._NAME_TOKEN_RE.search(head)
    return name_match.group(1) if name_match else ""


def _ghost(rows: Sequence[CompletionRow], typed: str) -> str:
    best = next((row for row in rows if not row.dim), None)
    if not typed or best is None:
        return ""
    if not best.value.lower().startswith(typed.lower()):
        return ""
    return best.value[len(typed):]
