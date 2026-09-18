# motion/api.py
"""JS-facing bridge for Motion, the simulation window.

A sixth adapter over core/, following monolith/api.py's shape: one
long-lived connection held as instance state, every exposed method
serialized behind a lock because pywebview calls in from a pool of
worker threads, and a best-effort sync on the way in and out.

This window is where simulation lives now. It was Monarch's -- a
settings panel, a scrub bar and a runs list bolted onto a graph editor
that is not about simulation -- and moving it here is the whole point of
the app: scenarios, solvers, runs, provenance and rule authoring are one
subject, and they were the one subject Monarch could not give a full
window to.

Three lines this bridge does not cross. It computes no simulation --
core/simulation.py does, and this only chooses the arguments. It shapes
no board -- motion/payload.py does. And it never writes a value it did
not recompute itself: `commit` re-runs the requested ticks rather than
trusting numbers a preview handed back to the frontend -- the refusal
Monarch's own commit made before this moved, and for the same reason.

# What is deliberately absent

The design this window was built from shows a hardware-in-the-loop rail
and a solver menu of ODE integrators (RK45, BDF2, dt in milliseconds, a
tolerance, substep retries). Neither exists in core: there is no device
layer at all, and core/simulation.py has no dt -- a tick is one re-solve
of the equation set, not an integration step. Rather than stub them into
plausible-looking numbers, both are left out, and the seams where they
would attach are named: `list_solvers` reports the real registry
(core/solver_registry.py) instead of a fixed menu, so a future
integrator appears here by being registered rather than by editing this
file, and motion/payload.py's `tick_axis` is the one place that decides
what the timeline is counting.
"""
import dataclasses
import logging
import threading

import webview

from api_common import CommandBarApi, InspectorApi
from api_common import coerce_properties as _coerce_properties
from api_common import open_app as _open_app
from api_common import open_gardens as _open_gardens
from api_common import species_ids as _species_ids
from api_common import synchronized as _synchronized
from api_common import _visible_keys
from core import derivations as derivations_repo
from core import gardens as gardens_repo
from core import graph as graph_repo
from core import motes as motes_repo
from core import relationships as rel_repo
from core import rules as rules_repo
from core import scenarios as scenarios_repo
from core import simulation as simulation_repo
from core import solvers as solvers_repo
from core import species as species_repo
from core import sync as sync_repo
from core.db import get_connection, initialize_database
from core.units import UNITS_KEY, parse_units_property
from motion import payload as payload_repo

logger = logging.getLogger(__name__)

# Same reasoning as monolith/api.py: Motion is opened *from* Monarch as
# often as on its own, so two processes holding the same garden file
# open at once is the normal case here, not the exception.
_BUSY_TIMEOUT_MS = 5000

# What a preview runs when the frontend doesn't say. Matches the step
# count Monarch's sim panel defaulted to, so a habit carried over from
# there lands on the same run.
_DEFAULT_STEPS = 10


def _time_series_payload(result: "simulation_repo.TimeSeriesResult") -> dict:
    """Shapes a simulation_repo.TimeSeriesResult for the JS bridge.

    Moved here from monarch/api.py with the window it serves. The steps
    themselves go through core's own step_payloads(), shared with the
    MCP adapter; only the run-level fields around them are this bridge's
    business. (StepResult.values/unsolved are keyed by (mote_id, key)
    tuples, which don't survive JSON -- that is why step_payloads exists
    and why unsolved counts stay prose-only inside each step's summary.)

    `constraint_violations` is flat and already labelled with its step
    (see TimeSeriesResult), because the dock shows one advisory line for
    the run rather than a per-tick badge -- a limit broken at any tick is
    worth seeing whichever tick you happen to be scrubbed to. The
    per-tick marks under the timeline come from payload.build_events,
    which reads the same violations off the steps."""
    steps = simulation_repo.step_payloads(result.steps)
    return {
        "steps": steps,
        "axis": payload_repo.tick_axis(len(steps)),
        "events": payload_repo.build_events(steps),
        "failed_at": result.failed_at,
        "error": result.error,
        "run_id": result.run_id,
        "run_number": result.run_number,
        "dimension_warnings": result.dimension_warnings,
        "constraint_violations": result.constraint_violations,
    }


def _explanation_payload(explanation) -> dict | None:
    """Shapes a core/derivations.py Explanation for the JS bridge.

    Recursive, mirroring the structure itself: an input this run derived
    carries its own explanation, and one it did not carries None, which
    is where the panel stops expanding. `also_determined` is flattened
    from tuples into dicts, since tuples reach JS as arrays and a value's
    address is easier to read as named fields."""
    if explanation is None:
        return None
    return {
        "mote_id": explanation.mote_id,
        "key": explanation.key,
        "value": explanation.value,
        "step": explanation.step,
        "method": explanation.method,
        "detail": explanation.detail,
        "note": explanation.note,
        "equations": [
            {
                "rule_mote_id": e.rule_mote_id,
                "rule_name": e.rule_name,
                "anchor_mote_id": e.anchor_mote_id,
                "equation": e.equation,
            }
            for e in explanation.equations
        ],
        "also_determined": [
            {"mote_id": mote_id, "key": key}
            for mote_id, key in explanation.also_determined
        ],
        "inputs": [
            {
                "kind": i.kind,
                "mote_id": i.mote_id,
                "key": i.key,
                "value": i.value,
                "derivation": _explanation_payload(i.derivation),
            }
            for i in explanation.inputs
        ],
    }


class Api(InspectorApi, CommandBarApi):
    """Every read re-reads the whole garden, the same choice the other
    windows make (monarch/api.py::focus, monolith/api.py::table):
    Motion is routinely open beside Monarch on the same garden file, and
    a board showing a rule that was edited in the other window a minute
    ago is worse than a read costing a few milliseconds."""

    def __init__(self, garden: str | None = None, focus: str | None = None):
        self._lock = threading.Lock()
        self._garden = gardens_repo.resolve_garden(garden)
        # `run_motion.py --focus <id>`: the frontend otherwise opens with
        # nothing selected.
        self._initial_focus = focus
        self._conn = self._open(self._garden)
        self._window: webview.Window | None = None
        self._best_effort_sync()

    def _open(self, garden: str):
        conn = get_connection(garden)
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        initialize_database(conn)
        return conn

    def _best_effort_sync(self) -> None:
        """run_motion.py is its own launch path, so a rule edited here
        must converge on its own rather than depending on another
        window's session having been open to carry it. Never allowed to
        block opening, switching or closing this window."""
        if gardens_repo.get_sync_dir(self._garden) is None:
            return
        try:
            sync_repo.run(self._conn, self._garden)
        except Exception:
            logger.exception("garden sync failed for %r", self._garden)

    # ---- sync ----

    @_synchronized
    def refresh(self) -> dict:
        """Manual refresh: sync (if configured), then let the caller
        re-fetch the board. An unconfigured sync is not an error --
        refresh is also just "reload from disk"."""
        if gardens_repo.get_sync_dir(self._garden) is None:
            return {"synced": False}
        result = sync_repo.run(self._conn, self._garden)
        return {"synced": True, **result}

    @_synchronized
    def shutdown(self) -> None:
        self._best_effort_sync()
        self._conn.close()

    # ---- app switcher ----

    @_synchronized
    def open_app(self, app_id: str, mote_id: str | None = None) -> dict:
        """Launch another MonOSS app on this same garden from the ⌘J
        switcher -- see api_common.open_app for why this is a detached
        process rather than a second window here."""
        return _open_app(app_id, self._garden, mote_id)

    # ---- gardens ----

    @_synchronized
    def current_garden(self) -> str:
        return self._garden

    @_synchronized
    def list_gardens(self) -> list[str]:
        return gardens_repo.list_gardens()

    @_synchronized
    def switch_garden(self, name: str) -> dict:
        resolved = gardens_repo.resolve_garden(name)
        self._conn.close()
        self._conn = self._open(resolved)
        self._garden = resolved
        self._best_effort_sync()
        return {"garden": resolved}

    @_synchronized
    def open_gardens(self, sync: bool = False) -> dict:
        """Open the shared Gardens window (gardens_app/): create, rename,
        delete, import, export and sync, for every garden rather than
        just this one.

        All six windows expose this same method, which is the point --
        garden management used to exist once, privately, inside
        Monarch's own modal, so doing any of it from here meant opening
        Monarch first. Switching which garden *this* window shows stays
        here (switch_garden above); everything about the gardens
        themselves is one window all six can open."""
        return _open_gardens(self._garden, sync=sync)

    # ---- the board ----

    def _simulable(self):
        """The garden as a run sees it: every mote except the ones core
        deliberately leaves out of a solve.

        Templates hold placeholder values for a species and overrides
        hold replacement values for one mote (core/scenarios.py). Neither
        is real state, and core/simulation.py excludes both -- so drawing
        them as nodes would put motes on the canvas that no run will ever
        move, which is exactly the confusion this window exists to
        remove. They are still reachable: an override shows up as the
        chip on its scenario, which is where it means something."""
        motes, relationships = graph_repo.whole_garden(self._conn)
        excluded = (
            species_repo.template_mote_ids(self._conn)
            | scenarios_repo.override_mote_ids(relationships)
        )
        motes = [m for m in motes if m.id not in excluded]
        visible = {m.id for m in motes}
        relationships = [
            r for r in relationships
            if r.from_id in visible and r.to_id in visible
        ]
        return motes, relationships

    @_synchronized
    def board(self, scenario: str | None = None) -> dict:
        """The window's whole answer in one call: nodes, edges, rules,
        scenarios, runs and the solver registry.

        One call rather than six because they must agree. A node's kind
        depends on which scenario is active (see payload.varied_ids), and
        a rule's bindings are resolved against the same incidence index
        the edges are drawn from -- fetched separately, a scenario switch
        could paint the nodes for one scenario and the arrows for
        another."""
        motes, relationships = self._simulable()
        labels = {m.id: str(m.properties.get("name", m.id[:8])) for m in motes}
        scenarios = scenarios_repo.list_scenarios(self._conn)
        varied = payload_repo.varied_ids(scenarios, scenario)

        index = simulation_repo.incidence_index(self._conn, scenario=scenario)
        rules = rules_repo.list_rules(self._conn)
        values = {
            (mote.id, key): value
            for mote in motes
            for key, value in mote.properties.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }

        return {
            "garden": self._garden,
            "focus": self._initial_focus,
            "nodes": [n.to_json() for n in payload_repo.build_nodes(motes, relationships, varied)],
            "edges": [e.to_json() for e in payload_repo.build_edges(relationships, index)],
            "rules": [
                c.to_json()
                for c in payload_repo.build_rule_cards(rules, index, labels, values)
            ],
            "scenarios": payload_repo.scenario_payload(scenarios),
            "activeScenario": scenario,
            "solvers": solvers_repo.list_solvers(),
            "runs": [dataclasses.asdict(r) for r in simulation_repo.list_runs(self._conn, limit=40)],
            # The palette universe rides along with the answer rather
            # than being a call of its own -- see api_common.species_ids.
            "speciesIds": _species_ids(self._conn),
        }

    @_synchronized
    def inspect(self, mote_id: str, scenario: str | None = None) -> dict | None:
        """One mote's current state and neighbourhood, for the inspector
        tab. The values are the garden's own, not a tick's: the frontend
        already holds every step and overlays the scrubbed-to values
        itself, so re-deriving them here would be a second answer to a
        question the dock has already answered.

        `scenario`, when given, folds that scenario's override for this
        mote (if any) over the garden's own properties -- the same merge
        a run would see (core/scenarios.py::apply). Build mode edits
        against this merged view, and write through update_mote_in_scenario,
        so a property the user is looking at and the one their edit lands
        on are always the same one."""
        mote = motes_repo.get_mote(self._conn, mote_id)
        if mote is None:
            return None
        properties = mote.properties
        if scenario is not None:
            case = scenarios_repo.get_scenario(self._conn, scenario)
            properties = scenarios_repo.apply({mote_id: properties}, case)[mote_id]
        _motes, relationships = self._simulable()
        labels = {
            m.id: str(m.properties.get("name", m.id[:8]))
            for m in graph_repo.whole_garden(self._conn)[0]
        }
        neighbours = [
            {
                "id": r.id,
                "type": r.type,
                "direction": "out" if r.from_id == mote_id else "in",
                "otherId": r.to_id if r.from_id == mote_id else r.from_id,
                "otherLabel": labels.get(r.to_id if r.from_id == mote_id else r.from_id, "?"),
            }
            for r in relationships
            if mote_id in (r.from_id, r.to_id)
        ]
        return {
            "id": mote.id,
            "label": labels.get(mote.id, mote.id[:8]),
            "properties": properties,
            "freedoms": rules_repo.parse_freedoms(
                properties.get(rules_repo.FREEDOMS_KEY)
            ),
            "relationships": neighbours,
        }

    # ---- running ----

    @_synchronized
    def list_solvers(self) -> list[str]:
        """Every registered solver backend name (see core/solvers.py).

        Read from the registry rather than listed here, which is the
        seam the module docstring names: a solver this window has never
        heard of appears in its menu by being registered in
        core/solver_registry.py."""
        return solvers_repo.list_solvers()

    @_synchronized
    def preview(
        self, step_count: int = _DEFAULT_STEPS, solver: str = "linear",
        solver_configuration: dict | None = None, scenario: str | None = None,
        strict_units: bool = False,
    ) -> dict:
        """Runs step_count ticks entirely in memory -- nothing is written
        to this garden. The frontend scrubs through the returned steps
        client-side; committing a chosen tick is a separate call.

        `scenario` names a mote gathering overrides (see board()): the
        garden is previewed as that scenario would have it. A preview
        writes nothing either way -- the difference is which question is
        being asked, not whether the answer is kept.

        `strict_units` is the same opt-in core/simulation.py's
        run_time_series() takes: promotes a dimensional mismatch from a
        logged warning to a run-stopping error, surfaced to the frontend
        the same way any other failed preview is."""
        result = simulation_repo.run_time_series(
            self._conn, step_count, commit=False, solver=solver,
            solver_configuration=solver_configuration, scenario=scenario,
            strict_units=strict_units,
        )
        return _time_series_payload(result)

    @_synchronized
    def compare(
        self, step_count: int = _DEFAULT_STEPS, solver: str = "linear",
        scenario_a: str | None = None, scenario_b: str | None = None,
        strict_units: bool = False,
    ) -> dict:
        """Two previews of the same garden under two scenarios, for the
        side-by-side boards and the shaded delta band between their
        traces.

        Both are ordinary in-memory previews run back to back; there is
        no paired solve in core and inventing one here would be a second
        engine. Returned as two independent payloads rather than a
        pre-computed diff because the two runs can disagree about which
        values even exist -- a scenario that changes `freedoms` makes a
        value solvable that the other run never determines -- and a
        subtraction would have to silently pick a side. The frontend
        pairs the lanes it can and shows the rest as single-sided."""
        return {
            "a": {
                "scenario": scenario_a,
                **_time_series_payload(simulation_repo.run_time_series(
                    self._conn, step_count, commit=False,
                    solver=solver, scenario=scenario_a, strict_units=strict_units,
                )),
            },
            "b": {
                "scenario": scenario_b,
                **_time_series_payload(simulation_repo.run_time_series(
                    self._conn, step_count, commit=False,
                    solver=solver, scenario=scenario_b, strict_units=strict_units,
                )),
            },
        }

    @_synchronized
    def lanes(self, steps: list[dict], limit: int | None = None) -> list[dict]:
        """The timeline's traces for a set of steps the caller already
        holds.

        Takes the steps back rather than re-running, because the caller
        got them from preview()/compare()/run_ticks() and re-solving to
        redraw a lane list would make a scenario switch cost two runs.
        Nothing here is written, so echoed-back numbers are only ever
        drawn -- the refusal to trust them applies to `commit`, which
        recomputes instead (see commit())."""
        motes, _relationships = self._simulable()
        labels = {m.id: str(m.properties.get("name", m.id[:8])) for m in motes}
        base = {m.id: m.properties for m in motes}
        units = {
            m.id: parse_units_property(m.properties.get(UNITS_KEY))
            for m in motes
        }
        built = payload_repo.build_lanes(
            steps, base, labels, units=units, limit=limit or payload_repo.MAX_LANES
        )
        return [lane.to_json() for lane in built]

    @_synchronized
    def commit(
        self, step_count: int, solver: str = "linear", solver_configuration: dict | None = None,
        detail: str | None = None, strict_units: bool = False,
    ) -> dict:
        """Re-runs step_count ticks (deterministic given the same garden
        and rules) and this time writes the final cumulative result,
        logging one entry the same way a single run_simulation() always
        has. Never trusts step values echoed back from a prior preview
        call -- the only numbers that ever reach the database are ones
        this recomputes itself.

        Takes no scenario: committing one is refused by the engine, since
        a scenario answers what *would* happen and that is not the
        garden's state. Use save() to keep a scenario run.

        `detail` is passed straight through to run_time_series() -- see
        there. Fast mode passes "fast" so its own auto-commits can be
        told apart from an ordinary manual commit."""
        result = simulation_repo.run_time_series(
            self._conn, step_count, commit=True, solver=solver,
            solver_configuration=solver_configuration, detail=detail,
            strict_units=strict_units,
        )
        return _time_series_payload(result)

    @_synchronized
    def save(
        self, step_count: int, solver: str = "linear",
        solver_configuration: dict | None = None, scenario: str | None = None,
        strict_units: bool = False,
    ) -> dict:
        """Like commit, but never writes the graph -- logs every computed
        tick for later review without any of them becoming the garden's
        canonical state. Always re-runs the full step_count (unlike
        commit, which only re-runs up to whichever tick was scrubbed to),
        since there's no "committed position" for a save.

        This is the only way a scenario run can be kept."""
        result = simulation_repo.run_time_series(
            self._conn, step_count, commit=False, save=True,
            solver=solver, solver_configuration=solver_configuration, scenario=scenario,
            strict_units=strict_units,
        )
        return _time_series_payload(result)

    # ---- runs ----

    @_synchronized
    def list_runs(self, limit: int | None = None) -> list[dict]:
        """Every logged run, most recent first -- backs the saved-runs
        list in the left rail."""
        return [dataclasses.asdict(r) for r in simulation_repo.list_runs(self._conn, limit=limit)]

    @_synchronized
    def get_run(self, run_id: str) -> dict | None:
        run = simulation_repo.get_run(self._conn, run_id)
        return dataclasses.asdict(run) if run is not None else None

    @_synchronized
    def run_changes(self, run_id: str) -> list[dict]:
        """A logged run's net old->new changes. For the tick-by-tick
        breakdown, see run_ticks."""
        return [dataclasses.asdict(c) for c in simulation_repo.run_changes(self._conn, run_id)]

    @_synchronized
    def run_ticks(self, run_id: str) -> dict:
        """A logged run's own internal ticks, shaped exactly like
        preview()'s so loading a saved run into the dock needs no second
        rendering path -- the scrubber cannot tell the two apart, which
        is the point."""
        steps = simulation_repo.step_payloads(simulation_repo.run_ticks(self._conn, run_id))
        return {
            "steps": steps,
            "axis": payload_repo.tick_axis(len(steps)),
            "events": payload_repo.build_events(steps),
            "run_id": run_id,
        }

    # ---- explanation ----

    @_synchronized
    def why(self, mote_id: str, key: str, run_id: str | None = None) -> dict | None:
        """How a solved value was determined: the equations that produced
        it, the known values they read, and -- recursively -- how each of
        those was itself determined (see core/derivations.py).

        Reads the run log, so it needs a run to have happened; a
        discarded preview leaves nothing to read. Values come back in
        base SI units, which is what the solver worked in, so the numbers
        beside an equation multiply out -- unlike the inspector, which
        shows each mote's own declared unit."""
        explanation = derivations_repo.explain(self._conn, mote_id, key, run_id=run_id)
        return _explanation_payload(explanation)

    @_synchronized
    def dependencies(self, mote_id: str, key: str) -> dict:
        """Everything the dependency index knows about one value, in one
        call -- what could move if it changed, what determining it needs,
        and which equations produce or read it.

        Fetched together because the panel shows them together, and the
        index is rebuilt per call anyway (see core/incidence.py): three
        round trips would each rebuild it."""
        index = simulation_repo.incidence_index(self._conn)
        target = (mote_id, key)
        requirement = index.requires(target)
        return {
            "free": index.is_free(target),
            "determinable": requirement.determinable,
            "impact": [
                {
                    "mote_id": effect.variable[0],
                    "key": effect.variable[1],
                    "distance": effect.distance,
                    "through": sorted({t.rule_name for t in effect.through}),
                }
                for effect in index.impact(target)
            ],
            "needs": [
                {"mote_id": n.variable[0], "key": n.variable[1], "status": n.status}
                for n in requirement.needs
            ],
            "producers": sorted({t.rule_name for t in requirement.producers}),
            "consumers": sorted({t.rule_name for t in index.consumers(target)}),
        }

    @_synchronized
    def check_roles(self) -> dict:
        """How every assembly is wired to its declared parts, plus which
        rules are asserted about a single mote rather than a species (see
        core/simulation.py's check_roles). The pre-flight this window
        offers before a long run."""
        report = simulation_repo.check_roles(self._conn)
        return {
            "motes_checked": report.motes_checked,
            "problems": report.problems,
            "once_scoped": report.once_scoped,
        }

    @_synchronized
    def mote_history(self, mote_id: str) -> dict[str, list[float]]:
        """Every value a mote's properties have taken across the garden's
        committed run history -- backs the inspector's sparklines."""
        return simulation_repo.mote_history(self._conn, mote_id)

    # ---- authoring (Build mode) ----

    @_synchronized
    def check_equation_names(
        self, mote_id: str, equation: str, scenario: str | None = None
    ) -> list[dict]:
        """Names `equation` references that aren't reachable from
        `mote_id` yet but do match some other mote in the garden --
        candidates for the equation editor's "connect them?" prompt (see
        docs/simulation.md's "Connected-name references"). Thin wrapper
        over core.simulation.unresolved_equation_names(): this bridge
        shapes an answer, it doesn't decide what one means.

        Called on blur, alongside the equation write itself, so a name
        that only becomes reachable once wired up here is never
        second-guessed once the relationship exists."""
        results = simulation_repo.unresolved_equation_names(
            self._conn, mote_id, equation, scenario=scenario
        )
        labels = {m.id: str(m.properties.get("name", m.id[:8])) for m in motes_repo.list_motes(self._conn)}
        return [
            {
                "name": r["name"],
                "candidates": [
                    {"id": cid, "label": labels.get(cid, cid[:8])}
                    for cid in r["candidates"]
                ],
            }
            for r in results
        ]

    @_synchronized
    def create_mote(self, raw_properties: dict, id: str | None = None) -> dict:
        mote_id = motes_repo.create_mote(self._conn, _coerce_properties(raw_properties), id=id)
        return dataclasses.asdict(motes_repo.get_mote(self._conn, mote_id))

    @_synchronized
    def update_mote(self, mote_id: str, raw_properties: dict) -> dict:
        motes_repo.update_mote(self._conn, mote_id, _coerce_properties(raw_properties))
        return dataclasses.asdict(motes_repo.get_mote(self._conn, mote_id))

    @_synchronized
    def update_mote_in_scenario(
        self, scenario_id: str, mote_id: str, raw_properties: dict
    ) -> dict:
        """Build mode's write path while a scenario is active: the edit
        lands on that scenario's override for `mote_id`, not the mote
        itself, so looking at a hypothetical never means overwriting the
        garden's own state (core/scenarios.py's whole point). The override
        is created -- with the `varies`/`overrides` relationships that
        wire it to this scenario and this mote -- the first time this is
        called for a mote that scenario doesn't already vary."""
        override_id = scenarios_repo.get_or_create_override(self._conn, scenario_id, mote_id)
        motes_repo.update_mote(self._conn, override_id, _coerce_properties(raw_properties))
        return dataclasses.asdict(motes_repo.get_mote(self._conn, override_id))

    @_synchronized
    def delete_property(self, mote_id: str, key: str) -> bool:
        return motes_repo.delete_property(self._conn, mote_id, key)

    @_synchronized
    def delete_mote(self, mote_id: str) -> bool:
        return motes_repo.delete_mote(self._conn, mote_id)

    @_synchronized
    def create_relationship(self, from_id: str, to_id: str, type_: str) -> dict:
        rel_id = rel_repo.create_relationship(self._conn, from_id, to_id, type_)
        return dataclasses.asdict(rel_repo.get_relationship(self._conn, rel_id))

    @_synchronized
    def update_relationship(
        self, rel_id: str, from_id: str | None = None, to_id: str | None = None, type_: str | None = None
    ) -> dict:
        rel_repo.update_relationship(self._conn, rel_id, from_id, to_id, type_)
        return dataclasses.asdict(rel_repo.get_relationship(self._conn, rel_id))

    @_synchronized
    def delete_relationship(self, rel_id: str) -> bool:
        return rel_repo.delete_relationship(self._conn, rel_id)

    # ---- the shared inspector, with Motion's two differences ----

    def set_inspector_scenario(self, scenario_id: str | None) -> None:
        """Told by the frontend whenever the scenario picker moves.

        The inspector is one surface in six windows and knows nothing
        about scenarios -- only this window has them. Rather than widen
        its bridge contract with a parameter five apps would always pass
        None for, Motion remembers which hypothetical is on screen and
        bends its own two inspector methods around it."""
        self._inspector_scenario = scenario_id or None

    def _inspector_payload(self, mote_id: str) -> dict | None:
        """The shared payload, but showing the scenario's values.

        Build mode reads and writes through the same merged view -- see
        `inspect` -- so that the property a person is looking at and the
        one their edit lands on are always the same property. The
        inspector has to obey that too, or a scenario would show the
        garden's number and silently overwrite the override."""
        answer = super()._inspector_payload(mote_id)
        scenario = getattr(self, "_inspector_scenario", None)
        if answer is None or scenario is None:
            return answer
        case = scenarios_repo.get_scenario(self._conn, scenario)
        answer["properties"] = scenarios_repo.apply(
            {mote_id: answer["properties"]}, case
        )[mote_id]
        answer["keys"] = _visible_keys(answer["properties"])
        return answer

    @_synchronized
    def inspector_set_property(self, mote_id: str, key: str, raw_value) -> dict | None:
        """While a scenario is selected the write is redirected onto that
        scenario's override mote instead of the mote itself: looking at a
        hypothetical must never mean overwriting the garden's own state,
        which is core/scenarios.py's entire point. Same rule the Build
        mode inspector already followed, now enforced one layer down so
        every editing affordance the shared panel grew -- add a property,
        rename a key, toggle a boolean -- inherits it rather than each
        having to remember."""
        scenario = getattr(self, "_inspector_scenario", None)
        target = mote_id
        if scenario is not None:
            target = scenarios_repo.get_or_create_override(self._conn, scenario, mote_id)
        motes_repo.update_mote(self._conn, target, _coerce_properties({key: raw_value}))
        return self._inspector_payload(mote_id)

    def command_run(self, text: str, confirm: bool = False) -> dict:
        """While a scenario is selected, the whole command bar is refused
        rather than risk one of its writing forms landing on the garden
        itself -- core/command_bar.py has no notion of a scenario
        override, and inspector_set_property's redirect above is the one
        place that rule is enforced. Blocking navigation along with the
        writes is the conservative call: teaching core/command_bar.py
        which of its five forms are read-only would duplicate its own
        dispatch logic here just to spare one of them.

        Not @_synchronized: it does no work of its own besides the
        scenario check, and delegating to CommandBarApi.command_run
        (which *is* synchronized) from inside an already-held lock would
        deadlock on self._lock."""
        if getattr(self, "_inspector_scenario", None) is not None:
            return {
                "ok": False, "moteId": None, "needsConfirm": False, "choices": None,
                "message": "a scenario is active -- edit this mote from the inspector instead",
            }
        return super().command_run(text, confirm=confirm)

    @_synchronized
    def inspector_why(self, mote_id: str, key: str) -> dict | None:
        """The provenance answer, flattened into lines the shared panel
        can print without knowing what a derivation is.

        Motion is the only window that can answer this -- it is the one
        with a solver and a run log -- so api_common.InspectorApi returns
        None and the other five never draw the control.

        Values are in base SI, which is what the solver worked in, so the
        numbers beside an equation multiply out."""
        explanation = derivations_repo.explain(self._conn, mote_id, key)
        if explanation is None:
            return None

        lines = [f"{explanation.method}: {explanation.detail or ''}".strip()]
        for equation in explanation.equations:
            lines.append(f"{equation.rule_name}: {equation.equation}")
        for value in explanation.inputs:
            where = "" if value.mote_id == mote_id else f"{value.mote_id[:8]} "
            lines.append(f"  reads {where}{value.key} = {value.value}")
        return {"lines": lines}
