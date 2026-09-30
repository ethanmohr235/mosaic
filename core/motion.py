# core/motion.py
"""Motion's grammar and answers (docs/console.md -> Settled for U5).

    sim [--ticks N] [--scenario s] [--vs s2|garden] [--solver name] [‹find conditions›]
    rule ‹rule›|#id                     a rule's equation and what it binds
    why ‹m.k›                           how the last solve determined a value
    solve [--live on|off]               one static solve, written, undoable

A sim pane is its command, like every pane: the line says how long the
run is, under which scenario, against which other run, and which values
to watch. run() answers it with one row per watched value, each carrying
its whole series, so a playhead scrubs without asking again.

**What a tick is.** One re-solve of the equation set, the tick the
solver's `tick` variable reads (core/simulation.py::run_time_series).
Row series are indexed by it: `series[t]` is the value after tick t's
solve, and `start` is the value before the run (the garden, under the
scenario). There is no dt and no seconds.

**Which rows.** With no conditions, the values that matter to the run:
everything the solver determined at some tick, every declared freedom
(a freedom nothing solved is `pinned` where it stands), and every given
a scenario varies -- the run's or the `--vs` one's. With find conditions
(core/findtext.py, the same as `find`), every numeric value of the
matched motes.

Nothing here writes except solve(), which returns its writes as one
undo step, provenance included."""
import math
import re
import shlex
import sqlite3
import time
from dataclasses import dataclass, field, replace
from typing import Sequence

from core import derivations as derivations_repo
from core import findtext
from core import motes as motes_repo
from core import query
from core import relationships as rel_repo
from core import rules as rules_repo
from core import scenarios as scenarios_repo
from core import simulation as simulation_repo
from core import solvers as solvers_repo
from core import species as species_repo
from core import table as table_repo
from core import undo
from core import units as units_repo
from core.command_bar import CommandError
from core.plotcmd import _Garden

SIM_VERB, RULE_VERB, WHY_VERB, SOLVE_VERB = "sim", "rule", "why", "solve"
SCENARIOS_VERB, STEP_VERB, PLAY_VERB, PAUSE_VERB = "scenarios", "step", "play", "pause"
TICKS_FLAG, SCENARIO_FLAG, VS_FLAG, SOLVER_FLAG = "--ticks", "--scenario", "--vs", "--solver"
FLAGS = (TICKS_FLAG, SCENARIO_FLAG, VS_FLAG, SOLVER_FLAG)
LIVE_FLAG = "--live"
# `--vs garden`: the other run is the garden as it stands, no scenario.
# A scenario that happens to be called "garden" is reached as #id.
VS_GARDEN = "garden"

DEFAULT_TICKS = 50
MAX_TICKS = 2000
# A watch list of more values than this is not being watched; the rest
# are counted, and find conditions narrow it.
MAX_ROWS = 200

# Row states, most telling first.
SOLVED, VARIED, PINNED, GIVEN = "solved", "varied", "pinned", "given"

# Timeline marker kinds.
CONSTRAINT, FAILED = "constraint", "failed"

_PREV_RE = re.compile(r"\bprev\s*\(")


# ---- sim: the grammar -----------------------------------------------------

@dataclass(frozen=True)
class SimLine:
    ticks: int | None = None
    scenario: str | None = None
    vs: str | None = None
    solver: str | None = None
    conditions: tuple[str, ...] = ()

    def text(self) -> str:
        words = [SIM_VERB]
        if self.ticks is not None:
            words += [TICKS_FLAG, str(self.ticks)]
        if self.scenario:
            words += [SCENARIO_FLAG, self.scenario]
        if self.vs:
            words += [VS_FLAG, self.vs]
        if self.solver:
            words += [SOLVER_FLAG, self.solver]
        return shlex.join(words + list(self.conditions))

    @property
    def tick_count(self) -> int:
        return self.ticks if self.ticks is not None else DEFAULT_TICKS


def parse(args: Sequence[str]) -> SimLine:
    """`sim`'s arguments. Flags anywhere; every other word is a find
    condition. Raises CommandError naming the word at fault."""
    fields: dict = {}
    conditions: list[str] = []
    i = 0
    while i < len(args):
        word = args[i]
        if word.startswith("--"):
            if word not in FLAGS:
                raise CommandError(f"sim takes {', '.join(FLAGS)} -- not {word}")
            if i + 1 >= len(args):
                raise CommandError(f"{word} needs a value")
            value = args[i + 1]
            i += 2
            if word == TICKS_FLAG:
                fields["ticks"] = _ticks(value)
            elif word == SCENARIO_FLAG:
                fields["scenario"] = value
            elif word == VS_FLAG:
                fields["vs"] = value
            else:
                if value not in solvers_repo.list_solvers():
                    raise CommandError(f"no solver {value!r} -- solvers: "
                                       f"{', '.join(solvers_repo.list_solvers())}")
                fields["solver"] = value
            continue
        findtext.parse_condition(word)  # raises FindSyntaxError (a ValueError) naming it
        conditions.append(word)
        i += 1
    return SimLine(conditions=tuple(conditions), **fields)


def parse_text(command: str) -> SimLine:
    try:
        words = shlex.split(command or "")
    except ValueError as e:
        raise CommandError(f"sim: {e}") from None
    if not words or words[0] != SIM_VERB:
        raise CommandError(f"{command!r} is not a sim line")
    return parse(words[1:])


def rewrite(line: SimLine, change: dict) -> SimLine:
    """A pane gesture as a line: `d` toggles `--vs garden`; the
    scenarios pane's ⏎ and `v` set `--scenario` and `--vs`."""
    known = {"vs", "scenario"}
    if not change or set(change) - known:
        raise CommandError(f"no such change: {sorted(set(change) - known) or 'nothing'}")
    return replace(line, **{k: (v or None) for k, v in change.items()})


def _ticks(value: str) -> int:
    try:
        count = int(value)
    except ValueError:
        raise CommandError(f"--ticks takes a whole number, not {value!r}") from None
    if not 1 <= count <= MAX_TICKS:
        raise CommandError(f"--ticks is 1 to {MAX_TICKS}")
    return count


# ---- sim: the run ---------------------------------------------------------

@dataclass(frozen=True)
class Row:
    mote_id: str
    key: str
    ref: str             # `C1.current`, or `#id.key` when the name is ambiguous
    state: str           # SOLVED, VARIED, PINNED or GIVEN
    unit: str | None
    start: float | None  # before the run
    series: tuple[float | None, ...]
    vs: tuple[float | None, ...] | None = None
    vs_start: float | None = None

    @property
    def extremes(self) -> tuple[int | None, int | None]:
        """(tick of the minimum, tick of the maximum) -- the first of each."""
        finite = [(v, t) for t, v in enumerate(self.series) if v is not None]
        if not finite:
            return None, None
        return min(finite)[1], max(finite, key=lambda p: (p[0], -p[1]))[1]


@dataclass(frozen=True)
class Marker:
    tick: int
    kind: str  # CONSTRAINT or FAILED
    text: str


@dataclass(frozen=True)
class SimRun:
    line: SimLine
    ticks: int                  # asked for
    count: int                  # computed (fewer when a tick failed)
    rows: tuple[Row, ...]
    markers: tuple[Marker, ...]
    scenario: str | None        # the scenario's name
    vs: str | None              # the other run's name ("garden" or a scenario's)
    vs_count: int = 0
    failed_at: int | None = None
    error: str | None = None
    vs_error: str | None = None
    hidden: int = 0             # rows past MAX_ROWS
    problems: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    seconds: float = 0.0


def run(conn: sqlite3.Connection, line: SimLine) -> SimRun:
    """Run the line: the time series (and the `--vs` one), then the
    watched rows with their series. Raises CommandError for a name the
    garden doesn't have; a tick that fails ends the run there and is a
    marker, not an error."""
    started = time.perf_counter()
    garden = _Garden(conn)
    scenario_id = garden.scenario(line.scenario)
    vs_id, vs_name = _vs(garden, line.vs)
    solver = line.solver or "linear"
    result = _series_run(conn, line.tick_count, solver, scenario_id)
    other = _series_run(conn, line.tick_count, solver, vs_id) if line.vs else None

    relationships = rel_repo.list_relationships(conn)
    hidden_ids = _unsimulated(conn, relationships)
    scenarios = {s.mote_id: s for s in scenarios_repo.list_scenarios(conn)}
    case = scenarios.get(scenario_id) if scenario_id else None
    other_case = scenarios.get(vs_id) if vs_id else None
    base = {m.id: dict(m.properties) for m in garden.motes if m.id not in hidden_ids}
    start = scenarios_repo.apply(base, case)
    vs_start = scenarios_repo.apply(base, other_case) if other is not None else None

    solved = {(c.mote_id, c.key) for step in result.steps for c in step.changes}
    freedoms = _freedoms(start, relationships)
    varied = _overridden(case)
    problems: list[str] = []
    if line.conditions:
        # Every number the matches hold, and what the run solves on them
        # even before they hold it.
        matched = _matched(conn, line.conditions, hidden_ids, problems)
        keys = {(m, key) for m in matched for key, value in start.get(m, {}).items()
                if _number(value) is not None}
        keys |= {k for k in solved | freedoms if k[0] in matched}
    else:
        keys = solved | freedoms | varied | _overridden(other_case)
    keys = {k for k in keys if k[0] in start}

    series = _values(start, result.steps, keys)
    vs_series = _values(vs_start, other.steps, keys) if other is not None else None
    ordered = sorted(keys, key=lambda k: (garden.text_of(*k).casefold(), k))
    rows = []
    for mote_id, key in ordered[:MAX_ROWS]:
        state = (SOLVED if (mote_id, key) in solved else VARIED if (mote_id, key) in varied
                 else PINNED if (mote_id, key) in freedoms else GIVEN)
        rows.append(Row(
            mote_id=mote_id, key=key, ref=garden.text_of(mote_id, key), state=state,
            unit=_unit(start.get(mote_id, {}), key),
            start=_number(start.get(mote_id, {}).get(key)),
            series=tuple(series[(mote_id, key)]),
            vs=tuple(vs_series[(mote_id, key)]) if vs_series is not None else None,
            vs_start=_number(vs_start.get(mote_id, {}).get(key)) if vs_start is not None else None,
        ))

    markers = [Marker(step.step, CONSTRAINT, problem)
               for step in result.steps for problem in step.constraint_violations]
    if result.failed_at is not None:
        markers.append(Marker(result.failed_at, FAILED, result.error or "the solve failed"))
    return SimRun(
        line=line, ticks=line.tick_count, count=len(result.steps), rows=tuple(rows),
        markers=tuple(markers), scenario=case.name if case else None,
        vs=vs_name if other is not None else None,
        vs_count=len(other.steps) if other is not None else 0,
        failed_at=result.failed_at, error=result.error,
        vs_error=other.error if other is not None else None,
        hidden=max(0, len(ordered) - MAX_ROWS), problems=tuple(problems),
        warnings=tuple(result.dimension_warnings),
        seconds=round(time.perf_counter() - started, 3),
    )


def _series_run(conn, ticks: int, solver: str, scenario_id: str | None):
    try:
        return simulation_repo.run_time_series(conn, ticks, solver=solver, scenario=scenario_id)
    except simulation_repo.SimulationError as e:
        # Only a strict up-front check raises here; a failed tick is kept.
        raise CommandError(str(e)) from None


def _vs(garden: _Garden, text: str | None) -> tuple[str | None, str | None]:
    if not text:
        return None, None
    if text == VS_GARDEN:
        return None, VS_GARDEN
    scenario_id = garden.scenario(text)
    name = next((s.name for s in scenarios_repo.list_scenarios(garden.conn)
                 if s.mote_id == scenario_id), text)
    return scenario_id, name


def _unsimulated(conn, relationships) -> set[str]:
    """Templates and overrides: placeholder and replacement values that
    no run solves (core/simulation.py leaves them out)."""
    return species_repo.template_mote_ids(conn) | scenarios_repo.override_mote_ids(relationships)


def _freedoms(properties: dict[str, dict], relationships) -> set[tuple[str, str]]:
    """Declared freedoms as the run reads them -- a scenario may change them."""
    view = simulation_repo._build_view(properties, relationships)
    return {(m, k) for m, keys in view.freedoms.items() for k in keys}


def _overridden(case: scenarios_repo.Scenario | None) -> set[tuple[str, str]]:
    if case is None:
        return set()
    return {(o.target_id, key) for o in case.overrides for key, value in o.properties.items()
            if _number(value) is not None}


def _matched(conn, conditions, hidden_ids, problems) -> set[str]:
    """The motes the find conditions match, run the way `find` runs them."""
    spec = findtext.parse_find(shlex.join(conditions))
    motes, relationships, index = table_repo.read(conn)
    schema = table_repo.Schema.of(index, motes, relationships)
    resolved, found = table_repo.resolve_query(spec.query, schema)
    problems.extend(found)
    return {m.id for m in query.evaluate(index, [m for m in motes if m.id not in hidden_ids], resolved)}


def _values(start: dict[str, dict], steps, keys) -> dict[tuple[str, str], list]:
    current = {k: _number(start.get(k[0], {}).get(k[1])) for k in keys}
    out: dict[tuple[str, str], list] = {k: [] for k in keys}
    for step in steps:
        for change in step.changes:
            variable = (change.mote_id, change.key)
            if variable in current:
                current[variable] = _number(change.new_value)
        for variable in keys:
            out[variable].append(current[variable])
    return out


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _unit(properties: dict, key: str) -> str | None:
    raw = units_repo.parse_units_property(properties.get(units_repo.UNITS_KEY)).get(key)
    return units_repo.format_unit(raw) if raw else None


def run_json(result: SimRun) -> dict:
    return {
        "command": result.line.text(),
        "solver": result.line.solver,
        "ticks": result.ticks,
        "count": result.count,
        "scenario": result.scenario,
        "vs": result.vs,
        "vsCount": result.vs_count,
        "failedAt": result.failed_at,
        "error": result.error,
        "vsError": result.vs_error,
        "hidden": result.hidden,
        "problems": list(result.problems),
        "warnings": list(result.warnings),
        "seconds": result.seconds,
        "markers": [{"tick": m.tick, "kind": m.kind, "text": m.text} for m in result.markers],
        "rows": [_row_json(row) for row in result.rows],
    }


def _row_json(row: Row) -> dict:
    lo, hi = row.extremes
    return {
        "moteId": row.mote_id, "key": row.key, "ref": row.ref, "state": row.state,
        "unit": row.unit, "start": row.start, "series": list(row.series),
        "vs": list(row.vs) if row.vs is not None else None, "vsStart": row.vs_start,
        "minTick": lo, "maxTick": hi,
    }


# ---- rule ------------------------------------------------------------------

@dataclass(frozen=True)
class Binding:
    mote_id: str
    key: str
    ref: str
    role: str  # "determines" | "reads"
    unit: str | None
    value: float | None
    series: tuple[float | None, ...] | None = None


@dataclass(frozen=True)
class RuleCard:
    mote_id: str
    name: str
    equation: str
    scope: str
    target: str
    check: bool
    anchors: tuple[str, ...]
    bindings: tuple[Binding, ...]
    hints: tuple[dict, ...] = ()


def rule_cards(conn: sqlite3.Connection, ref: str, *, ticks: int | None = None,
               scenario: str | None = None, solver: str | None = None) -> list[RuleCard]:
    """The card for rule `ref` (a name or `#id`) -- or, when `ref` names
    a mote that is not a rule, a card for every rule that touches it.

    With `ticks`, every binding also carries its series over that run
    (the shared playhead's run), so the pane reads a value at any tick
    without asking again."""
    garden = _Garden(conn)
    mote_id = _mote(garden, ref)
    scenario_id = garden.scenario(scenario)
    rules = rules_repo.list_rules(conn)
    index = simulation_repo.incidence_index(conn, scenario=scenario_id)
    wanted = [r for r in rules if r.mote.id == mote_id]
    if not wanted:
        touching = {t.rule_mote_id for t in index.touches
                    if mote_id == t.anchor_mote_id
                    or any(m == mote_id for m, _k in (*t.determines, *t.reads))}
        wanted = [r for r in rules if r.mote.id in touching]
    if not wanted:
        raise CommandError(f"{ref} is not a rule, and no rule touches it")
    variables = {v for r in wanted for t in index.touches if t.rule_mote_id == r.mote.id
                 for v in (*t.determines, *t.reads)}
    series = None
    if ticks is not None:
        result = _series_run(conn, ticks, solver or "linear", scenario_id)
        case = scenarios_repo.get_scenario(conn, scenario_id) if scenario_id else None
        start = scenarios_repo.apply({m.id: dict(m.properties) for m in garden.motes}, case)
        series = _values(start, result.steps, variables)
    cards = []
    for rule in wanted:
        touches = [t for t in index.touches if t.rule_mote_id == rule.mote.id]
        bindings, seen = [], set()
        for touch in touches:
            for role, variables_ in (("determines", touch.determines), ("reads", touch.reads)):
                for m, k in sorted(variables_):
                    if (m, k, role) in seen or m not in garden.by_id:
                        continue
                    seen.add((m, k, role))
                    bindings.append(Binding(
                        mote_id=m, key=k, ref=garden.text_of(m, k), role=role,
                        unit=garden.unit(m, k), value=garden.value(m, k),
                        series=tuple(series[(m, k)]) if series is not None else None,
                    ))
        cards.append(RuleCard(
            mote_id=rule.mote.id, name=rule.name, equation=rule.equation, scope=rule.scope,
            target=_label(garden, rule.target_id),
            check=bool(rule.mote.properties.get(rules_repo.CHECK_KEY)),
            anchors=tuple(sorted({_label(garden, t.anchor_mote_id) for t in touches})),
            bindings=tuple(bindings),
            hints=tuple(_hints(conn, garden, rule, scenario_id)),
        ))
    return cards


def _hints(conn, garden: _Garden, rule, scenario_id) -> list[dict]:
    """Names the equation can't settle on one mote: each with the motes
    it could mean, so wiring one is the fix (the old rule panel's hint)."""
    found = simulation_repo.unresolved_equation_names(
        conn, rule.mote.id, rule.equation, scenario=scenario_id)
    return [{"name": h["name"], "candidates": [_label(garden, c) for c in h["candidates"]]}
            for h in found]


def _mote(garden: _Garden, ref: str) -> str:
    if ref.startswith("#"):
        if ref[1:] not in garden.by_id:
            raise CommandError(f"no mote {ref}")
        return ref[1:]
    found = garden.by_name.get(ref, [])
    if not found:
        raise CommandError(f"no mote named {ref!r}")
    if len(found) > 1:
        raise CommandError(f"{len(found)} motes are named {ref!r} -- use #id")
    return found[0]


def _label(garden: _Garden, mote_id: str) -> str:
    mote = garden.by_id.get(mote_id)
    name = mote.properties.get("name") if mote else None
    return name if isinstance(name, str) and name else f"#{mote_id[:8]}"


def card_json(card: RuleCard) -> dict:
    return {
        "moteId": card.mote_id, "name": card.name, "equation": card.equation,
        "scope": card.scope, "target": card.target, "check": card.check,
        "anchors": list(card.anchors), "hints": list(card.hints),
        "bindings": [{"moteId": b.mote_id, "key": b.key, "ref": b.ref, "role": b.role,
                      "unit": b.unit, "value": b.value,
                      "series": list(b.series) if b.series is not None else None}
                     for b in card.bindings],
    }


# ---- why -------------------------------------------------------------------

def why(conn: sqlite3.Connection, ref: str) -> dict:
    """How the last written solve determined `ref` (`m.k` or `#id.k`),
    as a tree: each input the solve derived carries its own explanation."""
    garden = _Garden(conn)
    mote_id, key = garden.ref(ref)
    return _explanation_json(garden, derivations_repo.explain(conn, mote_id, key))


def _explanation_json(garden: _Garden, e) -> dict | None:
    if e is None:
        return None
    return {
        "ref": garden.text_of(e.mote_id, e.key) if e.mote_id in garden.by_id else f"#{e.mote_id}.{e.key}",
        "moteId": e.mote_id, "key": e.key, "value": e.value, "unit": _safe_unit(garden, e.mote_id, e.key),
        "method": e.method, "detail": e.detail, "note": e.note,
        "equations": [{"rule": q.rule_name, "ruleId": q.rule_mote_id, "equation": q.equation,
                       "anchor": _label(garden, q.anchor_mote_id)} for q in e.equations],
        "alsoDetermined": [garden.text_of(m, k) if m in garden.by_id else f"#{m}.{k}"
                           for m, k in e.also_determined],
        "inputs": [{"kind": i.kind, "ref": garden.text_of(i.mote_id, i.key)
                    if i.mote_id in garden.by_id else f"#{i.mote_id}.{i.key}",
                    "value": i.value, "derivation": _explanation_json(garden, i.derivation)}
                   for i in e.inputs],
    }


def _safe_unit(garden: _Garden, mote_id: str, key: str) -> str | None:
    return garden.unit(mote_id, key) if mote_id in garden.by_id else None


# ---- solve -----------------------------------------------------------------

@dataclass(frozen=True)
class Solve:
    message: str
    command: "undo.Command | None"
    changed: int
    result: "simulation_repo.SimulationResult | None" = None


def recurrences(conn: sqlite3.Connection) -> list[str]:
    """Rules that use prev(): they only mean something across ticks, so
    a static solve can't answer them -- that is what a sim is for."""
    return [r.name for r in rules_repo.list_rules(conn) if _PREV_RE.search(r.equation or "")]


def solve(conn: sqlite3.Connection, solver: str | None = None) -> Solve:
    """Solve the garden once and write the answer back, as one undo step
    that also puts the previous provenance back. Raises CommandError
    when the garden can't be solved (and then writes nothing)."""
    looping = recurrences(conn)
    if looping:
        raise CommandError(f"{looping[0]} uses prev(), which only a run can answer -- "
                           f"use sim; solve writes one static solve")
    before = {m.id: dict(m.properties) for m in motes_repo.list_motes(conn)}
    provenance = derivations_repo.snapshot(conn)
    try:
        result = simulation_repo.run_simulation(conn, solver=solver or "linear")
    except simulation_repo.SimulationError as e:
        raise CommandError(f"solve failed: {e}") from None
    after = {m.id: dict(m.properties) for m in motes_repo.list_motes(conn)}
    steps = []
    for mote_id, props in after.items():
        old = before.get(mote_id, {})
        changed = {k: v for k, v in props.items() if old.get(k, _MISSING) != v}
        if changed:
            steps.append(undo.for_update_mote(
                conn, mote_id, {k: old[k] for k in changed if k in old}, changed, label="solve"))
    replaced = derivations_repo.snapshot(conn)
    steps.append(undo.Command(
        label="solve",
        undo=lambda: derivations_repo.restore(conn, provenance),
        redo=lambda: derivations_repo.restore(conn, replaced),
    ))
    moved = len(steps) - 1
    message = f"solved · {moved} mote{'' if moved == 1 else 's'} changed"
    if result.unsolved:
        message += f" · {len(result.unsolved)} freedom(s) had no equation"
    if result.constraint_violations:
        count = len(result.constraint_violations)
        message += f" · {count} constraint{'' if count == 1 else 's'} broken"
    return Solve(message, undo.combine("solve", steps), moved, result)


_MISSING = object()


def live_solve(conn: sqlite3.Connection, edit: "undo.Command | None") -> tuple[str, "undo.Command | None"]:
    """After a write with `live` on: solve, and fold the solve into the
    edit's undo step so one undo takes back both. A solve that can't run
    leaves the edit as it is and says why."""
    if edit is None:
        return "", None
    try:
        solved = solve(conn)
    except CommandError as e:
        return f" · live: {e}", edit
    return f" · live: {solved.message}", undo.combine(edit.label, [edit, solved.command])


def parse_live(args: Sequence[str]) -> bool | None:
    """`solve` → None (just solve); `solve --live on|off` → the setting."""
    if not args:
        return None
    if len(args) == 2 and args[0] == LIVE_FLAG and args[1] in ("on", "off"):
        return args[1] == "on"
    raise CommandError("usage: solve [--live on|off]")


# ---- step ------------------------------------------------------------------

def parse_step(args: Sequence[str]) -> tuple[str, int]:
    """`step +N` / `step -N` move the playhead; `step N` puts it at tick
    N; `step` alone is `step +1`. ("by"|"to", n)."""
    if not args:
        return "by", 1
    if len(args) == 1 and re.fullmatch(r"[+-]?\d+", args[0]):
        text = args[0]
        return ("by" if text[0] in "+-" else "to"), int(text)
    raise CommandError("usage: step +N|-N|‹tick›")


# ---- scenarios ---------------------------------------------------------------

def scenarios(conn: sqlite3.Connection) -> list[dict]:
    """Every scenario with its overrides, each beside the garden's value."""
    garden = _Garden(conn)
    out = []
    for case in scenarios_repo.list_scenarios(conn):
        overrides = []
        for o in case.overrides:
            target = garden.by_id.get(o.target_id)
            for key, value in sorted(o.properties.items()):
                if key in (units_repo.UNITS_KEY,):
                    continue
                overrides.append({
                    "moteId": o.target_id, "key": key,
                    "ref": garden.text_of(o.target_id, key) if target else f"#{o.target_id}.{key}",
                    "value": value if _number(value) is None else _number(value),
                    "garden": target.properties.get(key) if target else None,
                    "unit": garden.unit(o.target_id, key) if target else None,
                })
        out.append({"id": case.mote_id, "name": case.name, "overrides": overrides})
    return out


def scenario_for_write(conn: sqlite3.Connection, text: str) -> tuple[str, list]:
    """The scenario `set --scenario ‹s›` writes into: an existing one by
    name or #id, or -- when no mote has that name -- a new mote so named,
    which becomes a scenario with its first override. Returns (id, undo
    steps for anything created)."""
    found = [s.mote_id for s in scenarios_repo.list_scenarios(conn)
             if s.name.casefold() == text.casefold() or f"#{s.mote_id}" == text]
    if len(found) == 1:
        return found[0], []
    if len(found) > 1:
        raise CommandError(f"{len(found)} scenarios are named {text!r} -- use #id")
    if text.startswith("#"):
        raise CommandError(f"{text} is not a scenario")
    if any(m.properties.get("name") == text for m in motes_repo.list_motes(conn)):
        raise CommandError(f"{text!r} is a mote but not a scenario -- name a new scenario "
                           f"something else")
    mote_id = motes_repo.create_mote(conn, {"name": text})
    return mote_id, [undo.for_create_mote(conn, mote_id, {"name": text}, label="new scenario")]


def override_for_write(conn: sqlite3.Connection, scenario_id: str, target_id: str) -> tuple[str, list]:
    """scenarios.get_or_create_override, with undo for what it created."""
    before = {r.id for r in rel_repo.list_relationships(conn)}
    existing = {m.id for m in motes_repo.list_motes(conn)}
    override_id = scenarios_repo.get_or_create_override(conn, scenario_id, target_id)
    if override_id in existing:
        return override_id, []
    steps = [undo.for_create_mote(conn, override_id, {}, label="scenario override")]
    for rel in rel_repo.list_relationships(conn):
        if rel.id not in before:
            steps.append(undo.for_create_relationship(
                conn, rel.id, rel.from_id, rel.to_id, rel.type, label="scenario override"))
    return override_id, steps


def usage() -> str:
    return "sim [--ticks N] [--scenario s] [--vs s|garden] [--solver name] [find conditions]"


def choices(conn: sqlite3.Connection, verb: str, rest: str) -> list[tuple[str, str]]:
    """Completion for the Motion verbs: flags, scenario names after
    `--scenario`/`--vs`, solver names, rule names, `m.k` refs for why."""
    words = rest.split()
    partial = "" if not rest or rest[-1].isspace() else words[-1]
    before = (words[-2] if partial and len(words) > 1 else (words[-1] if words and not partial else None))
    names = [s.name for s in scenarios_repo.list_scenarios(conn)]
    if verb == SIM_VERB:
        if before in (SCENARIO_FLAG, VS_FLAG):
            rows = [(_quoted(n), "scenario") for n in names]
            return rows + ([(VS_GARDEN, "the garden as it stands")] if before == VS_FLAG else [])
        if before == SOLVER_FLAG:
            return [(s, "solver") for s in solvers_repo.list_solvers()]
        if before == TICKS_FLAG:
            return [(str(DEFAULT_TICKS), "ticks")]
        return [(TICKS_FLAG, "how many ticks (default 50)"), (SCENARIO_FLAG, "run under a scenario"),
                (VS_FLAG, "compare with another run"), (SOLVER_FLAG, "solver backend")]
    if verb == RULE_VERB and not (words and not partial):
        rules = sorted({r.name for r in rules_repo.list_rules(conn)})
        return [("@sel", "the selected rule, or the rules touching it")] + [(_quoted(n), "rule") for n in rules]
    if verb == WHY_VERB and not (words and not partial):
        return [(f"{_quoted(name)}.{key}", "solved") for name, key in _solved_refs(conn)]
    if verb == SOLVE_VERB:
        if before == LIVE_FLAG:
            return [("on", "solve after every edit"), ("off", "")]
        if not words or (partial and len(words) == 1):
            return [(LIVE_FLAG, "solve after every edit made here")]
    if verb == STEP_VERB and not words:
        return [("+1", "next tick"), ("-1", "previous tick"), ("0", "first tick")]
    return []


def _solved_refs(conn) -> list[tuple[str, str]]:
    rows = conn.execute("SELECT DISTINCT mote_id, key FROM simulation_derivations").fetchall()
    garden = _Garden(conn)
    out = []
    for mote_id, key in rows:
        mote = garden.by_id.get(mote_id)
        name = mote.properties.get("name") if mote else None
        if isinstance(name, str) and len(garden.by_name.get(name, [])) == 1:
            out.append((name, key))
    return sorted(out)


def _quoted(name: str) -> str:
    return shlex.quote(name)
