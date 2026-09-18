# moxie/api.py
"""JS-facing bridge for Moxie, the graphing window.

A seventh adapter over core/, following motion/api.py's shape: one
long-lived connection held as instance state, a lock because pywebview
calls in from a pool of worker threads, and a best-effort sync on the way
in and out.

Moxie asks one question Motion cannot: *how does this value move as that
one changes*. Motion solves the garden as it stands, or forward through
time; neither has an answer shaped like a curve. Sweeping is that answer,
and core/sweep.py computes it -- this file only chooses the arguments.

# Two lines this bridge does not cross

It **computes no simulation**: core/sweep.py and core/simulation.py do.
And it **writes no property value, ever**. That second line is stricter
than Motion's, deliberately. Motion can commit, because committing is
what Motion is for; a graphing tool that could quietly change the model
it is graphing would be a trap, and the one thing it must be is a mirror.
The only thing Moxie writes is a saved-plot mote, when asked in so many
words -- and the embedded inspector writes properties exactly as it does
in the other six windows, which is where that power belongs.

# Why the lock is held so briefly

Every other window's bridge runs each method under `self._lock` start to
finish, which is right when a method is a database read and some shaping.
A sweep is neither: it is hundreds of solves, seconds of CPU, and the
whole point is that a newer request can cancel it. Holding the lock
across that would block the very call meant to cancel it -- so a sweep
takes the lock only long enough to read the garden (core/sweep.read_garden)
and solves the snapshot outside it. Cancellation is a generation counter:
the frontend numbers its requests, a newer number lands, and the running
sweep notices between points and stops.

# What is deliberately absent

The design this window was built from draws a hardcoded 900 mb "detector
clamp" and a threshold line you type a number into. Neither is invented
here: a limit on a value is already a thing in MonOSS -- a rule whose
relation is `<=` rather than `==` (docs/simulation.md, "Comparison
relations") -- and every solve already checks them. So the plot shades
where the garden's own constraints broke instead, which needs no new
concept and stays right even where the bound itself moves with the sweep.
"""
import csv
import io
import logging
import threading
from pathlib import Path

import webview

from api_common import CommandBarApi, InspectorApi
from api_common import open_app as _open_app
from api_common import open_gardens as _open_gardens
from api_common import synchronized as _synchronized
from core import gardens as gardens_repo
from core import motes as motes_repo
from core import scenarios as scenarios_repo
from core import simulation as simulation_repo
from core import solvers as solvers_repo
from core import species as species_repo
from core import sweep as sweep_repo
from core import sync as sync_repo
from core.db import get_connection, initialize_database
from core.graph import whole_garden
from core.sweep import Axis, Case, PLOT_KEY, has_plot
from moxie import payload as payload_repo

logger = logging.getLogger(__name__)

# Same reasoning as motion/api.py: Moxie is opened *from* Motion or
# Monarch as often as on its own, so two processes holding the same
# garden file open at once is the normal case here, not the exception.
_BUSY_TIMEOUT_MS = 5000

# The species every saved plot is declared an instance of, so a garden's
# plots show up as one kind of thing in Monarch without Monarch knowing
# this window exists. Monolith does the same for its saved views.
PLOT_SPECIES_NAME = "Saved Plot"

# What a sweep runs when the frontend doesn't say. 200 intervals is 201
# solves -- fine at the scale MonOSS is built for, and fine enough that
# the coarse pass below is about responsiveness rather than necessity.
_DEFAULT_STEPS = 200

# Every nth point of the first pass. A curve at 1/8 resolution is drawn
# before the full one is a quarter done, and is already the right shape.
_COARSE_STRIDE = 8


class Api(InspectorApi, CommandBarApi):
    """Moxie's bridge. Reads the garden, sweeps snapshots of it, and
    writes nothing but a saved plot."""

    def __init__(self, garden: str | None = None, focus: str | None = None):
        self._lock = threading.Lock()
        self._garden = gardens_repo.resolve_garden(garden)
        # `run_moxie.py --focus <id>`: the frontend otherwise opens with
        # whichever mote carries the most numbers.
        self._initial_focus = focus
        self._conn = self._open(self._garden)
        self._window: webview.Window | None = None
        # The newest request number the frontend has issued, per channel.
        # Per channel and not one counter, because the grid puts several
        # plots on screen at once and they sweep independently: one
        # counter would mean the second plot's first frame cancelled the
        # first plot's sweep, and neither would ever finish.
        self._generations: dict[str, int] = {}
        self._best_effort_sync()

    def _open(self, garden: str):
        conn = get_connection(garden)
        conn.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        initialize_database(conn)
        return conn

    def _best_effort_sync(self) -> None:
        """run_moxie.py is its own launch path, so a value edited
        elsewhere must converge on its own rather than depending on
        another window's session having been open to carry it. Never
        allowed to block opening, switching or closing this window."""
        if gardens_repo.get_sync_dir(self._garden) is None:
            return
        try:
            sync_repo.run(self._conn, self._garden)
        except Exception:
            logger.exception("garden sync failed for %r", self._garden)

    # ---- sync ----

    @_synchronized
    def refresh(self) -> dict:
        if gardens_repo.get_sync_dir(self._garden) is None:
            return {"synced": False}
        result = sync_repo.run(self._conn, self._garden)
        return {"synced": True, **result}

    @_synchronized
    def shutdown(self) -> None:
        self._best_effort_sync()
        self._conn.close()

    # ---- app switcher and gardens ----

    @_synchronized
    def open_app(self, app_id: str, mote_id: str | None = None) -> dict:
        return _open_app(app_id, self._garden, mote_id)

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
        return _open_gardens(self._garden, sync=sync)

    # ---- the board ----

    @_synchronized
    def board(self) -> dict:
        """The window's whole opening answer: every mote that holds a
        number, the scenarios that can serve as baselines, the solver
        registry, and the plots already saved here.

        One call rather than four because they must agree -- a saved plot
        names motes and scenarios, and a rail fetched separately could be
        drawn before either existed."""
        motes, _relationships = whole_garden(self._conn)
        freedoms = simulation_repo.garden_freedoms(self._conn)
        labels = self._species_labels()
        rows = [
            row for row in (
                payload_repo.mote_row(m, freedoms.get(m.id, set()), labels.get(m.id))
                for m in motes
                if not has_plot(m.properties)
            ) if row is not None
        ]
        rows.sort(key=lambda r: r["name"].casefold())
        return {
            "garden": self._garden,
            "focus": self._initial_focus,
            "motes": rows,
            "scenarios": [
                {"id": s.mote_id, "name": s.name}
                for s in scenarios_repo.list_scenarios(self._conn)
            ],
            "solvers": solvers_repo.list_solvers(),
            "plots": self._plots(motes),
        }

    def _species_labels(self) -> dict[str, str]:
        """Each mote's species name, for the rail's second line. Read once
        for the whole garden rather than per mote: species_of() is a
        query, and the rail asks for every mote there is."""
        labels: dict[str, str] = {}
        for species in species_repo.list_species(self._conn):
            for instance in species_repo.instances_of(self._conn, species.id):
                labels[instance.id] = _label(species)
        return labels

    def _plots(self, motes: list) -> list[dict]:
        """Every mote carrying a `plot`. Asked of the property rather than
        of core/kinds.py, for the reason monolith/api.py::_views gives
        about saved views: carrying a saved plot is data, like a url, and
        making it a kind would displace the mote's real species in every
        other window's type label."""
        plots = [
            {"id": m.id, "name": _label(m), "plot": m.properties.get(PLOT_KEY)}
            for m in motes if has_plot(m.properties)
        ]
        return sorted(plots, key=lambda p: p["name"].casefold())

    # ---- sweeping ----

    def _snapshot(self, scenario_ids: list[str]) -> sweep_repo.Ground:
        """The garden read under the lock, to be solved outside it."""
        with self._lock:
            return sweep_repo.read_garden(self._conn, scenario_ids)

    def _claim(self, channel: str, generation: int | None):
        """Take this request's number and hand back a cancellation check.

        A sweep is cancelled by a *newer* request on the same channel,
        not by a message: the frontend bumps the number on every
        keystroke and slider frame, and whichever sweep is running
        notices between points. A channel is one plot of the grid, so
        plots cancel themselves and never each other. A caller that
        passes no generation is never cancelled, which is what a one-shot
        (an export, a test) wants."""
        if generation is None:
            return lambda: False
        with self._lock:
            self._generations[channel] = max(self._generations.get(channel, 0), generation)
        return lambda: self._generations.get(channel, 0) > generation

    def sweep(self, spec: dict, indices: list[int] | None = None,
              generation: int | None = None, channel: str = "default") -> dict:
        """Solve the garden once per case at every point of the axis and
        return the curves. Writes nothing, logs no run.

        `indices` restricts the pass to those points of the axis, which is
        how the coarse-then-fine refinement works: the frontend asks for
        every eighth point, draws it, then asks for the rest.

        Not @_synchronized -- see the module docstring. The garden is read
        under the lock and the solving happens outside it, so a slider
        being dragged can cancel the sweep it started a frame ago."""
        cases = _cases(spec)
        axis = _axis(spec["x"])
        series = spec.get("series", [])
        outputs = list({(s["mote"], s["key"]) for s in series})
        ids = {
            (s.get("case", "base"), s["mote"], s["key"]): s["id"] for s in series
        }
        result = sweep_repo.run_sweep(
            self._snapshot([c.scenario_id for c in cases]),
            axis, cases, outputs,
            spec.get("solver", "linear"), spec.get("solverConfiguration"),
            indices=indices, should_cancel=self._claim(channel, generation),
        )
        return payload_repo.sweep_payload(result, ids)

    def sweep_plan(self, spec: dict) -> dict:
        """What this plot will cost, without running it.

        The run rail states the number outright rather than capping it,
        which was the decision: several series sharing a scenario and a
        set of knobs share one solve per point, so the honest figure is
        cases x points, and a user adding a twelfth series should be able
        to see what they just bought."""
        cases = _cases(spec)
        steps = int(spec["x"].get("steps", _DEFAULT_STEPS))
        return {
            "cases": len(cases),
            "points": steps + 1,
            "solves": len(cases) * (steps + 1),
            "coarseSolves": len(cases) * (steps // _COARSE_STRIDE + 1),
            "series": len(spec.get("series", [])),
        }

    def coarse_indices(self, steps: int) -> list[int]:
        """The first pass's points, and always the last one: a curve whose
        right edge is missing reads as a curve that ends early."""
        indices = list(range(0, steps + 1, _COARSE_STRIDE))
        if indices[-1] != steps:
            indices.append(steps)
        return indices

    def surface(self, spec: dict, stride: int = 1,
                generation: int | None = None, channel: str = "default") -> dict:
        """One output over two swept properties. `stride` takes every nth
        point of both axes, which is the same coarse-first idea one
        dimension up -- and one dimension up is where it stops being a
        nicety: a full grid is the square of a sweep."""
        case = _cases(spec)[0]
        result = sweep_repo.run_surface(
            self._snapshot([case.scenario_id]),
            _axis(spec["x"]), _axis(spec["y"]),
            (spec["output"]["mote"], spec["output"]["key"]),
            case, spec.get("solver", "linear"), spec.get("solverConfiguration"),
            stride=stride, should_cancel=self._claim(channel, generation),
        )
        return payload_repo.surface_payload(result)

    def correlate(self, spec: dict) -> list[dict]:
        """One dot per mote carrying both properties, after a single
        solve. Not a sweep -- nothing is varied, so no axis is needed."""
        case = _cases(spec)[0]
        dots = sweep_repo.run_correlation(
            self._snapshot([case.scenario_id]),
            spec["xKey"], spec["yKey"], case,
            spec.get("solver", "linear"), spec.get("solverConfiguration"),
        )
        return payload_repo.correlation_payload(dots)

    def sensitivity(self, spec: dict, x: float, nudge: float = 0.05) -> list[dict]:
        """How much the plotted value moves when each knob does, right
        here -- one extra solve per knob, not per point.

        A normalized derivative: the fractional change in the output over
        the fractional change in the input, so knobs measured in
        completely different units rank against each other honestly. The
        answer is local to this x and says so; a curve's sensitivities at
        one end of a sweep need not resemble the other's."""
        base_case = _cases(spec)[0]
        output = (spec["output"]["mote"], spec["output"]["key"])
        knobs = spec.get("knobs", [])
        ground = self._snapshot([base_case.scenario_id])
        axis = _axis(spec["x"])
        at = [_index_of(axis, x)]

        reference = self._probe(ground, axis, base_case, output, at)
        if reference is None:
            return []
        rows = []
        for knob in knobs:
            moved = _nudged(base_case, knob, nudge)
            value = self._probe(ground, axis, moved, output, at)
            if value is None:
                continue
            rows.append({
                "mote": knob["mote"], "key": knob["key"], "label": knob.get("label"),
                "value": ((value - reference) / max(abs(reference), 1e-12)) / nudge,
            })
        return sorted(rows, key=lambda r: abs(r["value"]), reverse=True)

    def _probe(self, ground, axis: Axis, case: Case, output, at: list[int]):
        """The plotted value at one point of the axis, for one case."""
        result = sweep_repo.run_sweep(
            ground, axis, [case], [output], indices=at,
        )
        if not result.points:
            return None
        return result.points[0].values.get((case.key, *output))

    # ---- saved plots ----

    def _plot_species_id(self) -> str:
        """Finds or creates the `Saved Plot` species, the same lazy
        pattern monolith/api.py::_view_species_id uses -- creating rather
        than answering None, since save_plot always needs one."""
        for mote in species_repo.list_species(self._conn):
            name = mote.properties.get("name")
            if isinstance(name, str) and name.casefold() == PLOT_SPECIES_NAME.casefold():
                return mote.id
        return motes_repo.create_mote(self._conn, {"name": PLOT_SPECIES_NAME})

    @_synchronized
    def save_plot(self, name: str, plot: dict, plot_id: str | None = None) -> dict:
        """A plot is an ordinary mote with a `plot` property, so it syncs,
        shows up in Monarch's graph, and can be linked from a Mora note
        like anything else.

        What is written is the *specification* -- which motes, which keys,
        which ranges, which scenarios -- and never the points it drew.
        That is the whole difference between a saved plot and a
        screenshot: reopening one re-solves the garden as it stands now,
        so a plot embedded in a note cannot go stale the way a pasted
        number does."""
        properties = {"name": name or "Untitled plot", PLOT_KEY: plot}
        if plot_id and motes_repo.get_mote(self._conn, plot_id) is not None:
            motes_repo.update_mote(self._conn, plot_id, properties)
            return {"id": plot_id, "created": False}
        new_id = motes_repo.create_mote(self._conn, properties)
        species_repo.set_species(self._conn, new_id, self._plot_species_id())
        return {"id": new_id, "created": True}

    @_synchronized
    def delete_plot(self, plot_id: str) -> dict:
        """Deletes the plot mote outright. What is lost is a saved
        specification rather than any of the garden's own data, so the
        frontend's own confirmation is enough -- the same call monolith
        makes about deleting a saved view."""
        return {"deleted": bool(motes_repo.delete_mote(self._conn, plot_id))}

    # ---- leaving with an answer ----

    def export_csv(self, columns: list[str], rows: list[list], name: str = "sweep") -> dict:
        """The evaluated points as a file: one x column and one per series.

        Shaped by the frontend rather than re-swept here, deliberately --
        exporting must give you the numbers you are looking at, including
        whichever refinement pass produced them, not a fresh run that
        might differ by a solver's last digit."""
        path = self._save_dialog(f"{name}.csv")
        if path is None:
            return {"cancelled": True}
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        writer.writerows(rows)
        path.write_text(buffer.getvalue())
        return {"path": str(path)}

    def export_png(self, data_url: str, name: str = "plot") -> dict:
        """The plot as an image. The frontend rasterizes its own SVG --
        it is the thing that knows what the plot looks like -- and this
        only decodes the data URL and puts it where the user asked."""
        path = self._save_dialog(f"{name}.png")
        if path is None:
            return {"cancelled": True}
        path.write_bytes(_decode_data_url(data_url))
        return {"path": str(path)}

    def _save_dialog(self, filename: str) -> Path | None:
        if self._window is None:
            return None
        chosen = self._window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename=filename
        )
        if not chosen:
            return None
        return Path(chosen if isinstance(chosen, str) else chosen[0])


def _label(mote) -> str:
    name = mote.properties.get("name")
    return str(name) if name not in (None, "") else mote.id[:8]


def _axis(spec: dict) -> Axis:
    return Axis(
        mote_id=spec["mote"], key=spec["key"],
        lo=float(spec["lo"]), hi=float(spec["hi"]),
        steps=int(spec.get("steps", _DEFAULT_STEPS)),
    )


def _cases(spec: dict) -> list[Case]:
    """The distinct what-ifs this plot asks for, defaulting to one.

    A plot with no cases declared is still asking a question -- the
    garden as it stands -- so it gets the one unnamed case rather than an
    empty sweep that would draw nothing and look like a failure."""
    raw = spec.get("cases") or [{"key": "base"}]
    return [
        Case(
            key=c.get("key", "base"),
            scenario_id=c.get("scenario"),
            overrides=_overrides(c.get("overrides")),
        )
        for c in raw
    ]


def _overrides(raw) -> dict[str, dict[str, float]]:
    """Knob values off the wire, coerced to numbers. A knob that arrives
    as a string (every HTML range input's value is one) would otherwise
    reach the solver as text and fail there, far from the cause."""
    if not isinstance(raw, dict):
        return {}
    return {
        mote_id: {k: float(v) for k, v in values.items()}
        for mote_id, values in raw.items()
        if isinstance(values, dict)
    }


def _nudged(case: Case, knob: dict, fraction: float) -> Case:
    """`case` with one knob moved by a fraction of itself, for a
    finite-difference probe. A knob sitting at zero has no fraction of
    itself to move by, so it is nudged by the fraction outright."""
    value = float(knob["value"])
    moved = value * (1 + fraction) if value else fraction
    overrides = {m: dict(v) for m, v in case.overrides.items()}
    overrides.setdefault(knob["mote"], {})[knob["key"]] = moved
    return Case(key=case.key, scenario_id=case.scenario_id, overrides=overrides)


def _index_of(axis: Axis, x: float) -> int:
    """The axis point nearest an x the cursor is sitting at. Sensitivity
    is probed at a real point of the sweep rather than between two, so
    the number it reports belongs to a curve the user can see."""
    fraction = (x - axis.lo) / (axis.hi - axis.lo)
    return max(0, min(axis.steps, round(fraction * axis.steps)))


def _decode_data_url(data_url: str) -> bytes:
    import base64
    _header, _, encoded = data_url.partition(",")
    return base64.b64decode(encoded)
