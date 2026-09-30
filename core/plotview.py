# core/plotview.py
"""Drawing a plot: a saved view, or a typed `plot …` line.

A saved plot is an ordinary mote carrying a `plot` property
(core/sweep.py::PLOT_KEY), and what that property holds is the
*specification* -- which motes, which keys, which ranges, which
scenarios -- never the points it drew. `plot save` writes it
(save_plot below); this reads it back and re-solves it, which is the whole reason a view is worth
referencing at all: `\\graph{Load curve}` in a note shows what the garden
says now, not what it said the afternoon the plot was saved.

The console's `plot …` pane draws through here too: core/plotcmd.py
turns the typed line into the same specification a saved view holds, and
figure_from_spec() solves it. A pane asks twice -- a coarse pass, then the
full one -- by passing a smaller `max_points`; interaction (a cursor,
dragged knobs) stays in the pane and reaches here only as a rewritten
command. The CLI prints the same figure a note or a pane draws, from the
same answer.

# Cheaper than the window it came from

A saved plot's axis is usually 200 intervals, which is right for a
window you are dragging a cursor across and wrong for a figure between
two paragraphs. `max_points` resamples the axis -- the same range, fewer
solves -- because the alternative is a note that costs a thousand solves
to re-render every time its author types a character. The range, the
cases and the series are the saved ones exactly; only how finely the
curve is sampled is this module's decision.

# Nothing raises

Same bargain as core/modoc_bind.py, for the same reason: a note with one
broken figure in it must still render the other nineteen. A missing
view, an unreadable specification, a sweep that blows up -- each comes
back as a Figure carrying an `error` and no points.
"""
import logging
import sqlite3
from dataclasses import dataclass, field

from core import units as units_repo
from core.entities import Mote
from core.motes import list_motes
from core.sweep import (
    Axis,
    Case,
    PLOT_KEY,
    Ground,
    has_plot,
    read_garden,
    run_correlation,
    run_sweep,
    run_surface,
)

logger = logging.getLogger(__name__)

NAME_KEY = "name"

# How many points a figure outside Moxie is sampled at. Enough that a
# curve reads as a curve and a kink in it is visible; few enough that a
# note carrying three figures re-renders while you are still typing.
NOTE_POINTS = 60

# A surface is the square of a sweep, so it gets a coarser grid still.
NOTE_SURFACE_POINTS = 24

_MODES = ("sweep", "surface", "correlate")


@dataclass(frozen=True)
class Band:
    """One Y axis. Series that measure the same dimension share one;
    series that do not cannot honestly share a scale, which is the only
    thing this grouping is deciding."""
    dimension: str | None
    unit: str | None
    label: str
    lo: float
    hi: float
    series: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class FigureSeries:
    """One curve, as `[x, y]` pairs -- with a `None` in place of any point
    that did not solve.

    The hole is kept rather than closed up, and that is not fussiness: a
    renderer handed a dense list would draw a straight segment across a
    range where the model has no answer at all, which is a lie the eye
    believes completely (frontend_shared/plot.js::segmentsOf)."""
    id: str
    label: str
    unit: str | None
    dimension: str | None
    band: int
    points: list[list[float] | None] = field(default_factory=list)
    ghost: list[list[float] | None] = field(default_factory=list)


@dataclass(frozen=True)
class Span:
    """A stretch of the x axis where something was true of every point in
    it -- a failed solve, or a broken constraint."""
    x0: float
    x1: float
    message: str


@dataclass(frozen=True)
class Figure:
    """One saved view, re-solved. `mode` says which of the three shapes
    below carries the data, and every other field is either shared or
    empty."""
    mode: str = "sweep"
    name: str = ""
    view_id: str | None = None
    x_label: str = ""
    y_label: str = ""
    y_scale: str = "linear"
    x_lo: float = 0.0
    x_hi: float = 1.0
    y_lo: float = 0.0
    y_hi: float = 1.0
    # sweep
    bands: list[Band] = field(default_factory=list)
    series: list[FigureSeries] = field(default_factory=list)
    failed: list[Span] = field(default_factory=list)
    violated: list[Span] = field(default_factory=list)
    broken: list[str] = field(default_factory=list)
    # surface
    cells: list[dict] = field(default_factory=list)
    rows: int = 0
    cols: int = 0
    lo: float | None = None
    hi: float | None = None
    # correlate
    dots: list[dict] = field(default_factory=list)
    solves: int = 0
    error: str | None = None


# --- finding the view ------------------------------------------------

def list_views(motes: list[Mote]) -> list[Mote]:
    """Every mote carrying a saved plot. Asked of the property rather
    than of core/kinds.py, for the reason moxie/api.py::_plots gives:
    carrying a plot is data, like a url."""
    return [m for m in motes if has_plot(m.properties)]


def find_view(ref: str, motes: list[Mote]) -> tuple[Mote | None, str | None]:
    """The saved view a reference names -- an exact id, else a unique
    name among the views only.

    Names are matched against views rather than against the whole garden
    on purpose: `\\graph{Load curve}` is asking for a picture, so an
    ordinary mote that happens to share the name is not a candidate and
    not an ambiguity either. Ids still win outright, and a mote named by
    id that carries no plot is told so rather than reported missing --
    "that mote has no saved view on it" is a different fix from "no such
    mote"."""
    ref = (ref or "").strip()
    if not ref:
        return None, "name a saved view, e.g. \\graph{Load curve}"

    views = list_views(motes)
    for mote in motes:
        if mote.id == ref:
            if has_plot(mote.properties):
                return mote, None
            return None, f"{name_of(mote)!r} carries no saved view -- save one with `plot save`"

    exact = [m for m in views if m.properties.get(NAME_KEY) == ref]
    matches = exact or [
        m for m in views
        if isinstance(m.properties.get(NAME_KEY), str)
        and m.properties[NAME_KEY].casefold() == ref.casefold()
    ]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, f"{len(matches)} saved views are named {ref!r} -- reference one by id instead"
    return None, f"no saved view named {ref!r} -- save one with `plot save`"


def name_of(mote: Mote) -> str:
    name = mote.properties.get(NAME_KEY)
    return name if isinstance(name, str) and name else mote.id[:8]


# --- drawing it ------------------------------------------------------

def figure(
    conn: sqlite3.Connection,
    ref: str,
    *,
    motes: list[Mote] | None = None,
    max_points: int = NOTE_POINTS,
) -> Figure:
    """The saved view `ref` names, re-solved against the garden as it
    stands. Writes nothing -- a sweep never does."""
    motes = list_motes(conn) if motes is None else motes
    view, error = find_view(ref, motes)
    if view is None:
        return Figure(name=(ref or "").strip(), error=error)

    spec = view.properties.get(PLOT_KEY)
    name, view_id = name_of(view), view.id
    if isinstance(spec, dict) and spec.get("mode") == "surface":
        # A full grid is the square of a sweep, so a note samples it
        # more coarsely still.
        max_points = min(max_points, NOTE_SURFACE_POINTS)
    if not isinstance(spec, dict):
        return Figure(
            name=name, view_id=view_id,
            error="this view's saved specification is unreadable",
        )

    return figure_from_spec(conn, spec, name=name, view_id=view_id,
                            motes=motes, max_points=max_points)


def figure_from_spec(
    conn: sqlite3.Connection,
    spec: dict,
    *,
    name: str = "",
    view_id: str | None = None,
    motes: list[Mote] | None = None,
    max_points: int = NOTE_POINTS,
) -> Figure:
    """A plot specification -- the same shape a saved view holds -- drawn
    against the garden as it stands. What figure() does once it has found
    the view, and what the console's `plot …` pane asks for directly
    (core/plotcmd.py builds the spec from the line). Writes nothing."""
    motes = list_motes(conn) if motes is None else motes
    directory = _Directory(motes)
    mode = spec.get("mode") or "sweep"
    if mode not in _MODES:
        return Figure(name=name, view_id=view_id, error=f"unknown plot mode {mode!r}")
    try:
        ground = read_garden(conn, _scenario_ids(spec))
        if mode == "surface":
            return _surface(spec, name, view_id, directory, ground, max_points)
        if mode == "correlate":
            return _correlate(spec, name, view_id, directory, ground)
        return _sweep(spec, name, view_id, directory, ground, max_points)
    except Exception as e:                      # noqa: BLE001 -- see the module docstring
        logger.warning("plot %r failed to draw: %s", name or spec, e)
        return Figure(mode=mode, name=name, view_id=view_id, error=str(e))


class _Directory:
    """Mote names and declared units, read once. Every label on a figure
    wants both, and asking the garden per series would be one query per
    curve for an answer that cannot change mid-figure."""

    def __init__(self, motes: list[Mote]) -> None:
        self._motes = {m.id: m for m in motes}

    def label(self, mote_id: str, key: str) -> str:
        mote = self._motes.get(mote_id)
        return f"{name_of(mote) if mote else mote_id[:8]}.{key}"

    def unit(self, mote_id: str, key: str) -> str | None:
        mote = self._motes.get(mote_id)
        if mote is None:
            return None
        raw = units_repo.parse_units_property(
            mote.properties.get(units_repo.UNITS_KEY)
        ).get(key)
        return units_repo.format_unit(raw) if raw else None

    def dimension(self, mote_id: str, key: str) -> str | None:
        """What a property *measures*, which is the real question behind
        "may these two share an axis": `kohm` and `ohm` agree here and
        `ohm` and `volt` do not."""
        mote = self._motes.get(mote_id)
        if mote is None:
            return None
        raw = units_repo.parse_units_property(
            mote.properties.get(units_repo.UNITS_KEY)
        ).get(key)
        if not raw:
            return None
        try:
            return units_repo.describe(units_repo.dimension_of(raw))
        except units_repo.UnitError:
            return None

    def axis_label(self, axis: dict) -> str:
        unit = self.unit(axis["mote"], axis["key"])
        return self.label(axis["mote"], axis["key"]) + (f" ({unit})" if unit else "")


# --- the three shapes ------------------------------------------------

def _sweep(
    spec: dict, name: str, view_id: str | None, directory: _Directory, ground: Ground,
    max_points: int,
) -> Figure:
    axis = _axis(spec.get("x"), max_points)
    series_specs = [s for s in spec.get("series") or [] if _names_a_property(s)]
    if axis is None or not series_specs:
        return Figure(name=name, view_id=view_id,
                      error="this view has no axis or no series saved on it")

    cases = _cases(spec, series_specs)
    outputs = list({(s["mote"], s["key"]) for s in series_specs})
    result = run_sweep(
        ground, axis, cases, outputs,
        spec.get("solver", "linear"), spec.get("solverConfiguration"),
    )

    ghosting = bool(spec.get("ghost")) and bool(spec.get("baseline"))
    rows = []
    for index, s in enumerate(series_specs):
        key = (_case_key(s), s["mote"], s["key"])
        ghost_key = ("ghost", s["mote"], s["key"])
        points = [_point(p, key) for p in result.points]
        ghost = [_point(p, ghost_key) for p in result.points] if ghosting else []
        rows.append({
            "id": s.get("id") or f"s{index}",
            "label": s.get("label") or directory.label(s["mote"], s["key"]),
            "unit": directory.unit(s["mote"], s["key"]),
            "dimension": directory.dimension(s["mote"], s["key"]),
            "points": points,
            "ghost": ghost,
        })

    log = spec.get("yScale") == "log"
    bands = _bands(rows, log)
    series = [
        FigureSeries(
            id=r["id"], label=r["label"], unit=r["unit"], dimension=r["dimension"],
            band=next(
                (i for i, b in enumerate(bands) if r["id"] in b.series), 0
            ),
            points=r["points"], ghost=r["ghost"],
        )
        for r in rows
    ]
    return Figure(
        mode="sweep", name=name, view_id=view_id,
        x_label=directory.axis_label(spec["x"]),
        y_label=bands[0].label if bands else "value",
        y_scale="log" if log else "linear",
        x_lo=axis.lo, x_hi=axis.hi,
        y_lo=bands[0].lo if bands else 0.0, y_hi=bands[0].hi if bands else 1.0,
        bands=bands, series=series,
        failed=_spans(result.points, lambda p: bool(p.errors),
                      lambda p: next(iter(p.errors.values()))),
        violated=_spans(
            result.points, lambda p: bool(p.violations),
            lambda p: "\n".join(result.broken_constraints)
            or "a constraint is not satisfied",
        ),
        broken=result.broken_constraints,
        solves=result.solves,
        error=None if any(any(p for p in s.points) for s in series)
        else "nothing solved across this range",
    )


def _surface(
    spec: dict, name: str, view_id: str | None, directory: _Directory, ground: Ground,
    max_points: int,
) -> Figure:
    x_axis = _axis(spec.get("x"), max(2, max_points))
    y_axis = _axis(spec.get("y"), max(2, max_points))
    output = spec.get("output")
    if x_axis is None or y_axis is None or not _names_a_property(output):
        return Figure(mode="surface", name=name, view_id=view_id,
                      error="this view has no two axes and an output saved on it")

    result = run_surface(
        ground, x_axis, y_axis, (output["mote"], output["key"]),
        _cases(spec, [])[0], spec.get("solver", "linear"),
        spec.get("solverConfiguration"),
    )
    span = result.span
    # Real row/col indices are Moxie's business (a finer pass lands on top
    # of a coarser one); a figure is drawn once, so the grid is dense and
    # the cells are numbered from zero.
    cols = sorted({c.col for c in result.cells})
    rows = sorted({c.row for c in result.cells})
    col_at = {v: i for i, v in enumerate(cols)}
    row_at = {v: i for i, v in enumerate(rows)}
    return Figure(
        mode="surface", name=name, view_id=view_id,
        x_label=directory.axis_label(spec["x"]),
        y_label=directory.axis_label(spec["y"]),
        x_lo=x_axis.lo, x_hi=x_axis.hi, y_lo=y_axis.lo, y_hi=y_axis.hi,
        cells=[
            {"r": row_at[c.row], "c": col_at[c.col], "x": c.x, "y": c.y,
             "v": c.value, "error": c.error}
            for c in result.cells
        ],
        rows=len(rows), cols=len(cols),
        lo=span[0] if span else None, hi=span[1] if span else None,
        solves=result.solves,
        error=None if span else "nothing solved across this grid",
    )


def _correlate(
    spec: dict, name: str, view_id: str | None, directory: _Directory, ground: Ground
) -> Figure:
    x_key, y_key = spec.get("xKey"), spec.get("yKey")
    if not x_key or not y_key:
        return Figure(mode="correlate", name=name, view_id=view_id,
                      error="this view has no two property names saved on it")
    dots = run_correlation(
        ground, x_key, y_key, _cases(spec, [])[0],
        spec.get("solver", "linear"), spec.get("solverConfiguration"),
    )
    xs = [d.x for d in dots]
    ys = [d.y for d in dots]
    return Figure(
        mode="correlate", name=name, view_id=view_id,
        x_label=x_key, y_label=y_key,
        y_scale="log" if spec.get("yScale") == "log" else "linear",
        x_lo=min(xs) if xs else 0.0, x_hi=max(xs) if xs else 1.0,
        y_lo=min(ys) if ys else 0.0, y_hi=max(ys) if ys else 1.0,
        dots=[{"id": d.mote_id, "name": d.name, "x": d.x, "y": d.y} for d in dots],
        solves=1,
        error=None if dots else f"no mote carries both {x_key!r} and {y_key!r}",
    )


def _point(point, key) -> list[float] | None:
    value = point.values.get(key)
    return None if value is None else [point.x, value]


# --- reading the specification ---------------------------------------

def _names_a_property(spec) -> bool:
    return isinstance(spec, dict) and bool(spec.get("mote")) and bool(spec.get("key"))


def _axis(spec, max_points: int) -> Axis | None:
    """The saved axis, resampled. The range is the author's and untouched;
    only the number of intervals across it is ours."""
    if not _names_a_property(spec):
        return None
    try:
        lo, hi = float(spec["lo"]), float(spec["hi"])
        steps = int(spec.get("steps") or NOTE_POINTS)
    except (TypeError, ValueError):
        return None
    if lo == hi:
        return None
    return Axis(spec["mote"], spec["key"], lo, hi, max(1, min(steps, max_points - 1)))


def _case_key(series: dict) -> str:
    """Moxie's own key, character for character (moxie/frontend/app.js::
    caseKeyOf). Two series that differ only in which property they read
    share a case and therefore one solve per point; the string is how
    they recognize each other, so it has to be the same string on both
    sides."""
    extra = series.get("extra")
    tail = f"{extra['mote']}.{extra['key']}={extra['value']}" if extra else "-"
    return f"sc:{series.get('scenario') or '-'}|ex:{tail}"


def _overrides(spec: dict, extra=None) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for knob in spec.get("knobs") or []:
        if not _names_a_property(knob):
            continue
        try:
            out.setdefault(knob["mote"], {})[knob["key"]] = float(knob["value"])
        except (TypeError, ValueError, KeyError):
            continue
    if extra and _names_a_property(extra):
        try:
            out.setdefault(extra["mote"], {})[extra["key"]] = float(extra["value"])
        except (TypeError, ValueError, KeyError):
            pass
    return out


def _cases(spec: dict, series_specs: list[dict]) -> list[Case]:
    """The distinct what-ifs this view asks for -- Moxie's casesFor(),
    server side. A view with no series still asks one question (the
    garden as it stands, under its knobs), which is what a surface and a
    correlation are."""
    cases: dict[str, Case] = {}
    for s in series_specs:
        key = _case_key(s)
        if key in cases:
            continue
        cases[key] = Case(key, s.get("scenario") or None, _overrides(spec, s.get("extra")))
    if spec.get("ghost") and spec.get("baseline"):
        cases["ghost"] = Case("ghost", spec["baseline"], _overrides(spec))
    if not cases:
        cases["base"] = Case("base", spec.get("scenario") or None, _overrides(spec))
    return list(cases.values())


def _scenario_ids(spec: dict) -> list[str]:
    ids = [s.get("scenario") for s in spec.get("series") or [] if isinstance(s, dict)]
    ids.append(spec.get("baseline") if spec.get("ghost") else None)
    ids.append(spec.get("scenario"))
    return [i for i in ids if i]


# --- reducing the answer ---------------------------------------------

def _bands(rows: list[dict], log: bool) -> list[Band]:
    """Series grouped by what they measure, each with the range its own
    members actually cover. One band is one Y axis; the first is the
    left-hand one.

    moxie/payload.py::unit_bands answers the same question for the live
    window, from the rail's property rows. This answers it from points,
    because a figure has no rail behind it."""
    groups: list[dict] = []
    for row in rows:
        found = next((g for g in groups if g["dimension"] == row["dimension"]), None)
        if found is None:
            found = {"dimension": row["dimension"], "unit": row["unit"],
                     "keys": [], "series": [], "lo": None, "hi": None}
            groups.append(found)
        found["series"].append(row["id"])
        label = row["label"].rsplit(".", 1)[-1]
        if label not in found["keys"]:
            found["keys"].append(label)
        if found["unit"] != row["unit"]:
            # Two commensurable units written differently (kohm and ohm).
            # The axis label stops claiming either.
            found["unit"] = None
        for point in row["points"]:
            if point is None:
                continue
            y = point[1]
            found["lo"] = y if found["lo"] is None else min(found["lo"], y)
            found["hi"] = y if found["hi"] is None else max(found["hi"], y)

    bands = []
    for group in groups:
        lo, hi = _padded(group["lo"], group["hi"], log)
        bands.append(Band(
            dimension=group["dimension"], unit=group["unit"],
            # A garden that declares no units still has something true to
            # put on the axis: the name of what is being plotted.
            label=" / ".join(group["keys"]) + (f" ({group['unit']})" if group["unit"] else ""),
            lo=lo, hi=hi, series=group["series"],
        ))
    return bands


def _padded(lo, hi, log: bool) -> tuple[float, float]:
    """A band's range with breathing room, and never a degenerate one.

    The same reasoning as moxie/frontend/app.js::padBand: several points
    agreeing to fourteen decimal places are one value as far as an axis
    goes, and padding by a proportion of a span that small leaves the
    axis labelled with sixteen identical numbers."""
    if lo is None or hi is None:
        return (0.0, 1.0)
    if log:
        lo = max(lo, 1e-9)
        return (lo, max(hi, lo * 10))
    if hi - lo <= abs(hi) * 1e-9:
        step = abs(hi) * 0.5 or 1.0
        return (lo - step, hi + step)
    pad = (hi - lo) * 0.06
    lo, hi = lo - pad, hi + pad
    if lo > 0 and lo < (hi - lo) * 0.4:
        lo = 0.0
    return (lo, hi)


def _spans(points, hit, message) -> list[Span]:
    """Contiguous runs of x where something was true, as one shaded span
    rather than forty stripes."""
    spans: list[Span] = []
    open_span: dict | None = None
    for point in points:
        if hit(point):
            if open_span is None:
                open_span = {"x0": point.x, "x1": point.x, "message": message(point)}
            else:
                open_span["x1"] = point.x
        elif open_span is not None:
            spans.append(Span(**open_span))
            open_span = None
    if open_span is not None:
        spans.append(Span(**open_span))
    return spans


# --- the rest of what a plot pane needs ------------------------------

PLOT_SPECIES_NAME = "Saved Plot"
SENSITIVITY_NUDGE = 0.05


def figure_json(figure: Figure) -> dict:
    """A Figure on the wire, flat and already reduced: a renderer
    (console/frontend/js/plot.js, or Mora's) decides nothing about it
    beyond colour and size."""
    return {
        "mode": figure.mode,
        "name": figure.name,
        "viewId": figure.view_id,
        "xLabel": figure.x_label,
        "yLabel": figure.y_label,
        "yScale": figure.y_scale,
        "xLo": figure.x_lo, "xHi": figure.x_hi,
        "yLo": figure.y_lo, "yHi": figure.y_hi,
        "bands": [
            {"label": b.label, "unit": b.unit, "lo": b.lo, "hi": b.hi, "series": b.series}
            for b in figure.bands
        ],
        "series": [
            {"id": s.id, "label": s.label, "unit": s.unit, "band": s.band,
             "points": s.points, "ghost": s.ghost}
            for s in figure.series
        ],
        "failed": [{"x0": s.x0, "x1": s.x1, "message": s.message} for s in figure.failed],
        "violated": [{"x0": s.x0, "x1": s.x1, "message": s.message} for s in figure.violated],
        "broken": figure.broken,
        "cells": figure.cells,
        "rows": figure.rows, "cols": figure.cols,
        "lo": figure.lo, "hi": figure.hi,
        "dots": figure.dots,
        "solves": figure.solves,
        "error": figure.error,
    }


def csv_table(figure: Figure) -> tuple[list[str], list[list]]:
    """The points a figure drew, as columns and rows: x then one column
    per series for a sweep (blank where a point failed), x, y and the
    value for a surface, and one row per mote for a correlation."""
    if figure.mode == "surface":
        return ([figure.x_label, figure.y_label, "value"],
                [[c["x"], c["y"], c["v"]] for c in figure.cells])
    if figure.mode == "correlate":
        return (["mote", figure.x_label, figure.y_label],
                [[d["name"], d["x"], d["y"]] for d in figure.dots])
    xs: list[float] = []
    for s in figure.series:
        for point in s.points:
            if point is not None and point[0] not in xs:
                xs.append(point[0])
    xs.sort()
    by_x = [{p[0]: p[1] for p in s.points if p is not None} for s in figure.series]
    return ([figure.x_label] + [s.label for s in figure.series],
            [[x] + [values.get(x) for values in by_x] for x in xs])


@dataclass(frozen=True)
class Sensitivity:
    """How much `output` moves at one x when one knob moves: the
    fractional change in the output over the fractional change in the
    knob, so knobs in different units rank against each other honestly."""
    mote: str
    key: str
    label: str
    value: float


def sensitivity(
    conn: sqlite3.Connection, spec: dict, x: float, nudge: float = SENSITIVITY_NUDGE
) -> list[Sensitivity]:
    """Each knob's pull on the first series at the axis point nearest
    `x`, strongest first -- one extra solve per knob, not per point. Local
    to that x: a curve's sensitivities at one end of a sweep need not
    resemble the other's. Empty for anything but a sweep with knobs."""
    axis = _axis(spec.get("x"), 10**9)
    series = [s for s in spec.get("series") or [] if _names_a_property(s)]
    knobs = [k for k in spec.get("knobs") or [] if _names_a_property(k)]
    if (spec.get("mode") or "sweep") != "sweep" or axis is None or not series or not knobs:
        return []
    first = series[0]
    output = (first["mote"], first["key"])
    base = _cases(spec, [first])[0]
    ground = read_garden(conn, _scenario_ids(spec))
    fraction = (x - axis.lo) / (axis.hi - axis.lo)
    at = [max(0, min(axis.steps, round(fraction * axis.steps)))]

    def probe(case: Case) -> float | None:
        result = run_sweep(ground, axis, [case], [output], indices=at)
        return result.points[0].values.get((case.key, *output)) if result.points else None

    reference = probe(base)
    if reference is None:
        return []
    directory = _Directory(list_motes(conn))
    rows = []
    for knob in knobs:
        value = float(knob["value"])
        moved = value * (1 + nudge) if value else nudge
        overrides = {m: dict(v) for m, v in base.overrides.items()}
        overrides.setdefault(knob["mote"], {})[knob["key"]] = moved
        answer = probe(Case(base.key, base.scenario_id, overrides))
        if answer is None:
            continue
        rows.append(Sensitivity(
            knob["mote"], knob["key"], directory.label(knob["mote"], knob["key"]),
            ((answer - reference) / max(abs(reference), 1e-12)) / nudge,
        ))
    return sorted(rows, key=lambda r: abs(r.value), reverse=True)


def save_plot(conn: sqlite3.Connection, name: str, spec: dict) -> tuple[str, bool, "undo.Command"]:
    """Write `spec` as a saved view named `name`: (mote id, created, undo).

    A saved plot is an ordinary mote with a `plot` property, declared an
    instance of `Saved Plot`, so it syncs, shows in the graph and is what
    `\\graph{name}` in a note draws. What is written is the specification,
    never the points. A view already carrying that name is updated in
    place rather than duplicated, which keeps `\\graph{name}` unambiguous."""
    from core import species as species_repo
    from core import undo
    from core.motes import create_mote, get_mote, update_mote

    name = (name or "").strip()
    if not name:
        raise ValueError("a saved plot needs a name")
    properties = {NAME_KEY: name, PLOT_KEY: spec}
    existing = [m for m in list_views(list_motes(conn)) if m.properties.get(NAME_KEY) == name]
    if len(existing) > 1:
        raise ValueError(f"{len(existing)} saved plots are named {name!r} -- rename one first")
    if existing:
        mote_id = existing[0].id
        old = get_mote(conn, mote_id).properties
        before = {k: old[k] for k in properties if k in old}
        update_mote(conn, mote_id, properties)
        return mote_id, False, undo.for_update_mote(conn, mote_id, before, properties,
                                                    label="save plot")
    species_id = next(
        (m.id for m in species_repo.list_species(conn)
         if isinstance(m.properties.get(NAME_KEY), str)
         and m.properties[NAME_KEY].casefold() == PLOT_SPECIES_NAME.casefold()),
        None,
    )
    steps = []
    if species_id is None:
        species_props = {NAME_KEY: PLOT_SPECIES_NAME}
        species_id = create_mote(conn, species_props)
        steps.append(undo.for_create_mote(conn, species_id, species_props, label="save plot"))
    mote_id = create_mote(conn, properties)
    species_repo.set_species(conn, mote_id, species_id)
    # Deleting the mote (this step's undo) cascades away the instance_of
    # relationship set_species just made.
    steps.append(undo.for_create_mote(conn, mote_id, properties, label="save plot"))
    return mote_id, True, undo.combine("save plot", steps)
