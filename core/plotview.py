# core/plotview.py
"""Drawing a saved Moxie view from somewhere that is not Moxie.

A saved plot is an ordinary mote carrying a `plot` property
(core/sweep.py::PLOT_KEY), and what that property holds is the
*specification* -- which motes, which keys, which ranges, which
scenarios -- never the points it drew. Moxie writes it; this reads it
back and re-solves it, which is the whole reason a view is worth
referencing at all: `\\graph{Load curve}` in a note shows what the garden
says now, not what it said the afternoon the plot was saved.

Moxie itself does not use this module. It has a frontend holding a live
grid, a cursor, knobs and a coarse-then-fine refinement pass, and it
builds its own model in JavaScript because every one of those things is
interaction rather than data. What a *reader* needs is the opposite: one
finished picture, computed once, with no state behind it. So this is the
non-interactive half -- the specification, the sweep, and the reduction
to lines, cells or dots -- and it lives in core/ rather than in mora/
because the CLI prints the same figure that Mora draws, from the same
answer.

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
            return None, f"{name_of(mote)!r} carries no saved view -- save one in Moxie first"

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
    return None, f"no saved view named {ref!r} -- save one in Moxie first"


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
    if not isinstance(spec, dict):
        return Figure(
            name=name_of(view), view_id=view.id,
            error="this view's saved specification is unreadable",
        )

    directory = _Directory(motes)
    mode = spec.get("mode") or "sweep"
    if mode not in _MODES:
        return Figure(name=name_of(view), view_id=view.id,
                      error=f"unknown plot mode {mode!r}")
    try:
        ground = read_garden(conn, _scenario_ids(spec))
        if mode == "surface":
            return _surface(spec, view, directory, ground, max_points)
        if mode == "correlate":
            return _correlate(spec, view, directory, ground)
        return _sweep(spec, view, directory, ground, max_points)
    except Exception as e:                      # noqa: BLE001 -- see the module docstring
        logger.warning("saved view %r failed to draw: %s", ref, e)
        return Figure(mode=mode, name=name_of(view), view_id=view.id, error=str(e))


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
    spec: dict, view: Mote, directory: _Directory, ground: Ground, max_points: int
) -> Figure:
    axis = _axis(spec.get("x"), max_points)
    series_specs = [s for s in spec.get("series") or [] if _names_a_property(s)]
    if axis is None or not series_specs:
        return Figure(name=name_of(view), view_id=view.id,
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
        mode="sweep", name=name_of(view), view_id=view.id,
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
    spec: dict, view: Mote, directory: _Directory, ground: Ground, max_points: int
) -> Figure:
    x_axis = _axis(spec.get("x"), max(2, min(max_points, NOTE_SURFACE_POINTS)))
    y_axis = _axis(spec.get("y"), max(2, min(max_points, NOTE_SURFACE_POINTS)))
    output = spec.get("output")
    if x_axis is None or y_axis is None or not _names_a_property(output):
        return Figure(mode="surface", name=name_of(view), view_id=view.id,
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
        mode="surface", name=name_of(view), view_id=view.id,
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


def _correlate(spec: dict, view: Mote, directory: _Directory, ground: Ground) -> Figure:
    x_key, y_key = spec.get("xKey"), spec.get("yKey")
    if not x_key or not y_key:
        return Figure(mode="correlate", name=name_of(view), view_id=view.id,
                      error="this view has no two property names saved on it")
    dots = run_correlation(
        ground, x_key, y_key, _cases(spec, [])[0],
        spec.get("solver", "linear"), spec.get("solverConfiguration"),
    )
    xs = [d.x for d in dots]
    ys = [d.y for d in dots]
    return Figure(
        mode="correlate", name=name_of(view), view_id=view.id,
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
        cases["base"] = Case("base", None, _overrides(spec))
    return list(cases.values())


def _scenario_ids(spec: dict) -> list[str]:
    ids = [s.get("scenario") for s in spec.get("series") or [] if isinstance(s, dict)]
    ids.append(spec.get("baseline") if spec.get("ghost") else None)
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
