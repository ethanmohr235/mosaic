# core/plotcmd.py
"""The `plot` verb's grammar (docs/console.md -> Settled for U4).

A plot pane is its command, so everything a plot shows -- what is
plotted, against what, over which range, under which knobs -- is in the
line, and a layout that restores the line restores the plot:

    plot bd.sigma bd.tau vs bd.l 4:24 --steps 200
    plot bd.sigma vs bd.l 4:24 --set bd.E=200e9 --scenario hot --log
    plot bd.sigma vs bd.l 4:24 by bd.w 1:3      a surface: one value over two axes
    plot sigma vs l --across                    one dot per mote carrying both keys
    plot "Load curve"      plot #id             a saved plot, re-solved
    plot save "Load curve" [‹plot line›]         the only write
    plot export csv|png|svg|pdf|tex [path]
    plot bd.sigma vs bd.l 4:24 --data sigma vs l --from bench.csv
                                                 measured points over the curve

Names are garden-scope names (`bd.l`, the worksheet's), or `#id.key`.
`--hide ‹m.k›` keeps a series in the legend without drawing it.

Two halves, kept apart on purpose. parse() and PlotLine.text() are pure
text: a pane that drags a knob rewrites its line (rewrite()) without
asking the garden anything. resolve() is where names meet the garden --
it turns a line into the specification a saved view holds
(core/sweep.py::PLOT_KEY), which core/plotview.py draws. So a line saved
with `plot save` is exactly what `\\graph{…}` in a note re-solves."""
import math
import re
import shlex
import sqlite3
from dataclasses import dataclass, replace
from typing import Sequence

from core import knobs as knobs_repo
from core import motes as motes_repo
from core import scenarios as scenarios_repo
from core import simulation as simulation_repo
from core import plotview
from core.command_bar import CommandError
from core.knobs import number_text as _number_text
from core.sweep import PLOT_KEY, TIME_KEY

PLOT_VERB = "plot"
SAVE, EXPORT = "save", "export"
EXPORT_FORMATS = ("csv", "png", "svg", "pdf", "tex")
VS, BY = "vs", "by"
STEPS_FLAG, SET_FLAG, SCENARIO_FLAG = "--steps", "--set", "--scenario"
LOG_FLAG, ACROSS_FLAG, HIDE_FLAG = "--log", "--across", "--hide"
DATA_FLAG, FROM_FLAG = "--data", "--from"
FLAGS = (STEPS_FLAG, SET_FLAG, SCENARIO_FLAG, LOG_FLAG, ACROSS_FLAG, HIDE_FLAG, DATA_FLAG, FROM_FLAG)

# `plot m.y vs t [lo:hi]`: x is the tick of a run (core/simulation.py's
# `iterate`, what `sim` walks). Whole ticks from tick 0, the sim's default
# length when no range is given.
TIME = TIME_KEY
DEFAULT_TICKS = 50
MAX_TICKS = 2000

DEFAULT_STEPS = 200
DEFAULT_SURFACE_STEPS = 40
MAX_STEPS = 2000
# How wide a first range is around a property's current value: half to
# double, wide enough that the curve has shape, narrow enough to stay
# near the model that was built.
_SUGGESTION_SPAN = (0.5, 2.0)

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_RANGE_RE = re.compile(rf"^({_NUMBER}):({_NUMBER})$")

PLOT, SAVED = "plot", "saved"


def usage() -> str:
    from core.reserved import RESERVED_VERBS
    return f"usage: {RESERVED_VERBS[PLOT_VERB]['usage']}"


# ---- the line -----------------------------------------------------------

@dataclass(frozen=True)
class PlotLine:
    """One plot as typed. Refs are kept as written (`bd.l`), which is what
    lets a rewrite round-trip without the garden."""
    ys: tuple[str, ...] = ()
    x: str | None = None
    range: tuple[float, float] | None = None
    by: str | None = None
    by_range: tuple[float, float] | None = None
    across: bool = False
    steps: int | None = None
    sets: tuple[knobs_repo.KnobSpec, ...] = ()
    scenario: str | None = None
    log: bool = False
    hide: tuple[str, ...] = ()
    saved: str | None = None
    # Measured points to draw over the curve (the keys the data motes carry,
    # as `fit` reads them), narrowed to one CSV import with `source`. Drawn
    # by the publication export; a pane keeps them in its line.
    data: tuple[str, str] | None = None
    source: str | None = None

    @property
    def empty(self) -> bool:
        return not self.ys and self.saved is None

    @property
    def mode(self) -> str:
        if self.across:
            return "correlate"
        return "surface" if self.by else "sweep"

    def text(self) -> str:
        if self.saved is not None:
            return f"{PLOT_VERB} {_quote(self.saved)}"
        if self.empty:
            return PLOT_VERB
        parts = [PLOT_VERB, *map(_quote, self.ys), VS, _quote(self.x or "")]
        if self.range:
            parts.append(_range_text(self.range))
        if self.by:
            parts += [BY, _quote(self.by)]
            if self.by_range:
                parts.append(_range_text(self.by_range))
        if self.across:
            parts.append(ACROSS_FLAG)
        if self.steps is not None:
            parts += [STEPS_FLAG, str(self.steps)]
        parts += [_quote(word) for word in knobs_repo.words(self.sets)]
        if self.scenario:
            parts += [SCENARIO_FLAG, _quote(self.scenario)]
        if self.log:
            parts.append(LOG_FLAG)
        for ref in self.hide:
            parts += [HIDE_FLAG, _quote(ref)]
        if self.data:
            parts += [DATA_FLAG, _quote(self.data[0]), VS, _quote(self.data[1])]
            if self.source:
                parts += [FROM_FLAG, _quote(self.source)]
        return " ".join(parts)


@dataclass(frozen=True)
class PlotCommand:
    """What a `plot …` line asks for: draw (`line`), save the line (or,
    with no line, the focused plot pane's) as `name`, or export the
    focused pane as `format` to `path` (a save dialog when None)."""
    kind: str  # PLOT, SAVE or EXPORT
    line: PlotLine | None = None
    name: str | None = None
    format: str | None = None
    path: str | None = None


def parse(args: Sequence[str]) -> PlotCommand:
    """The tokens after `plot` (shlex-split, as core/commands.py::parse
    gives them). Raises CommandError with the usage for a line that
    isn't one."""
    args = list(args)
    if args and args[0] == SAVE:
        if len(args) < 2 or not args[1].strip():
            raise CommandError("usage: plot save ‹name› [‹plot line›]")
        rest = args[2:]
        if rest and rest[0] == PLOT_VERB:
            rest = rest[1:]
        return PlotCommand(SAVE, parse_line(rest) if rest else None, name=args[1].strip())
    if args and args[0] == EXPORT:
        if len(args) not in (2, 3) or args[1] not in EXPORT_FORMATS:
            raise CommandError("usage: plot export csv|png|svg|pdf|tex [path]")
        return PlotCommand(EXPORT, format=args[1], path=args[2] if len(args) == 3 else None)
    return PlotCommand(PLOT, parse_line(args))


def parse_text(command: str) -> PlotCommand:
    """A whole `plot …` line, as a pane holds it."""
    try:
        tokens = shlex.split(command or "")
    except ValueError as e:
        raise CommandError(f"plot: {e}") from None
    if not tokens or tokens[0] != PLOT_VERB:
        raise CommandError(f"not a plot line: {command!r} -- {usage()}")
    return parse(tokens[1:])


def parse_line(tokens: Sequence[str]) -> PlotLine:
    groups: list[list[str]] = [[]]
    keywords: list[str] = []
    fields: dict = {"sets": [], "hide": []}
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token in (LOG_FLAG, ACROSS_FLAG):
            fields[token[2:]] = True
        elif token == DATA_FLAG:
            if i + 3 >= len(tokens) or tokens[i + 2] != VS:
                raise CommandError(f"--data takes ‹ykey› vs ‹xkey› -- {usage()}")
            fields["data"] = (tokens[i + 1], tokens[i + 3])
            i += 3
        elif token == FROM_FLAG:
            if i + 1 >= len(tokens):
                raise CommandError(f"{token} needs an import name -- {usage()}")
            i += 1
            fields["source"] = tokens[i]
        elif token in (STEPS_FLAG, SET_FLAG, SCENARIO_FLAG, HIDE_FLAG):
            if i + 1 >= len(tokens):
                raise CommandError(f"{token} needs a value -- {usage()}")
            i += 1
            _flag(fields, token, tokens[i])
        elif token.startswith("--"):
            raise CommandError(f"unknown flag {token} -- {usage()}")
        elif token in (VS, BY):
            if token in keywords or (token == BY and VS not in keywords):
                raise CommandError(f"misplaced {token!r} -- {usage()}")
            keywords.append(token)
            groups.append([])
        else:
            groups[-1].append(token)
        i += 1

    sets = tuple(fields.pop("sets"))
    hide = tuple(fields.pop("hide"))
    if not keywords:
        words = groups[0]
        if not words and not sets and not hide and not fields:
            return PlotLine()
        if len(words) == 1 and not sets and not hide and not fields:
            return PlotLine(saved=words[0])
        raise CommandError(f"plot what against what? -- {usage()}")

    ys = tuple(groups[0])
    if not ys:
        raise CommandError(f"nothing to plot before `vs` -- {usage()}")
    x, x_range = _axis_words(groups[1], VS)
    by = by_range = None
    if BY in keywords:
        by, by_range = _axis_words(groups[2], BY)
        if len(ys) != 1:
            raise CommandError("a surface (`by`) plots exactly one value")
    across = bool(fields.get("across"))
    if across and (x_range or by or len(ys) != 1):
        raise CommandError("--across takes two keys and no range: plot ‹key› vs ‹key› --across")
    if x == TIME:
        if by or across:
            raise CommandError("time is the x axis of a sweep: plot ‹m.y›… vs t [lo:hi], no `by` or --across")
        _time_range(x_range)
    return PlotLine(
        ys=ys, x=x, range=x_range, by=by, by_range=by_range, across=across,
        steps=fields.get("steps"), sets=sets, scenario=fields.get("scenario"),
        log=bool(fields.get("log")), hide=hide,
        data=fields.get("data"), source=fields.get("source"),
    )



def _flag(fields: dict, flag: str, value: str) -> None:
    if flag == STEPS_FLAG:
        try:
            steps = int(value)
        except ValueError:
            raise CommandError(f"--steps takes a whole number, not {value!r}") from None
        if not 1 <= steps <= MAX_STEPS:
            raise CommandError(f"--steps is 1 to {MAX_STEPS}")
        fields["steps"] = steps
    elif flag == SET_FLAG:
        fields["sets"].append(knobs_repo.parse_set(value))
    elif flag == SCENARIO_FLAG:
        fields["scenario"] = value
    else:
        fields["hide"].append(value)


def _time_range(span: tuple[float, float] | None) -> tuple[int, int]:
    """The ticks `lo:hi` of a time axis (default 0:50): whole numbers,
    from 0 up, at most MAX_TICKS."""
    if span is None:
        return 0, DEFAULT_TICKS
    lo, hi = span
    if not (float(lo).is_integer() and float(hi).is_integer()) or not 0 <= lo < hi <= MAX_TICKS:
        raise CommandError(f"a time axis is whole ticks, lo:hi from 0 up to {MAX_TICKS} -- "
                           f"e.g. plot a.y vs t 0:50")
    return int(lo), int(hi)


def _axis_words(words: list[str], keyword: str) -> tuple[str, tuple[float, float] | None]:
    if not words:
        raise CommandError(f"`{keyword}` needs a value to sweep -- {usage()}")
    if len(words) > 2:
        raise CommandError(f"after `{keyword}`: one value and an optional lo:hi, not {' '.join(words)!r}")
    span = None
    if len(words) == 2:
        match = _RANGE_RE.match(words[1])
        if not match:
            raise CommandError(f"{words[1]!r} is not a range -- write lo:hi, e.g. 4:24")
        span = (float(match.group(1)), float(match.group(2)))
        if span[0] == span[1]:
            raise CommandError("a range needs two different ends")
    return words[0], span


# ---- rewriting ----------------------------------------------------------

def rewrite(line: PlotLine, change: dict) -> PlotLine:
    """`line` with one gesture applied -- what a pane does when a knob is
    dragged, a range zoomed or a series switched off, so the command keeps
    carrying the whole view. `change` is one of:

        {"set": {"ref": "bd.E", "value": 2e11}}   add or move a knob
        {"unset": "bd.E"}                         drop a knob
        {"range": [lo, hi]}                       the x range (a zoom)
        {"log": true|false}
        {"hide": "bd.tau"}                        toggle a series
        {"steps": n}
    """
    if line.saved is not None or line.empty:
        raise CommandError("only a typed plot line can be changed in place")
    change = change or {}
    if "set" in change:
        spec = knobs_repo.KnobSpec(change["set"]["ref"], float(change["set"]["value"]))
        return replace(line, sets=knobs_repo.add_to_line(line.sets, spec))
    if "unset" in change:
        return replace(line, sets=tuple(s for s in line.sets if s.ref != change["unset"]))
    if "range" in change:
        lo, hi = (float(v) for v in change["range"])
        if line.x == TIME:  # a zoom lands between ticks: widen to whole ones
            lo, hi = math.floor(min(lo, hi)), math.ceil(max(lo, hi))
            if lo == hi:
                hi += 1
            return replace(line, range=(float(max(lo, 0)), float(hi)))
        if lo == hi:
            raise CommandError("a range needs two different ends")
        return replace(line, range=(min(lo, hi), max(lo, hi)))
    if "log" in change:
        return replace(line, log=bool(change["log"]))
    if "hide" in change:
        ref = change["hide"]
        hide = tuple(r for r in line.hide if r != ref) if ref in line.hide else line.hide + (ref,)
        return replace(line, hide=hide)
    if "steps" in change:
        return replace(line, steps=int(change["steps"]))
    raise CommandError(f"unknown plot change {change!r}")


# ---- meeting the garden -------------------------------------------------

@dataclass(frozen=True)
class Knob:
    ref: str
    mote: str
    key: str
    value: float
    lo: float
    hi: float
    unit: str | None = None


@dataclass(frozen=True)
class SeriesRef:
    id: str
    ref: str
    hidden: bool


@dataclass(frozen=True)
class Resolved:
    """A line turned into a specification, plus what a pane draws around
    the figure: the legend (hidden series too), the knobs with a range to
    drag them over, and whether x is one of the solver's freedoms."""
    spec: dict
    name: str = ""
    view_id: str | None = None
    series: tuple[SeriesRef, ...] = ()
    knobs: tuple[Knob, ...] = ()
    x_pinned: bool = False
    # (data y key, data x key, import name) from --data / --from
    overlay: tuple[str, str, str | None] | None = None


class _Garden:
    """Names, values and units read once for a whole line."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.motes = motes_repo.list_motes(conn)
        self.by_id = {m.id: m for m in self.motes}
        self.by_name: dict[str, list[str]] = {}
        for mote in self.motes:
            name = mote.properties.get("name")
            if isinstance(name, str) and name:
                self.by_name.setdefault(name, []).append(mote.id)

    def ref(self, text: str) -> tuple[str, str]:
        """(mote id, key) for `name.key` or `#id.key`. Ambiguity is
        refused, as the worksheet refuses it."""
        if text.startswith("#"):
            mote_id, dot, key = text[1:].partition(".")
            if not dot or not key:
                raise CommandError(f"{text!r} names no property -- write #id.key")
            if mote_id not in self.by_id:
                raise CommandError(f"no mote #{mote_id}")
            return mote_id, key
        name, dot, key = text.rpartition(".")
        if not dot or not name or not key:
            raise CommandError(f"{text!r} names no property -- write mote.key, e.g. bd.l")
        found = self.by_name.get(name, [])
        if not found:
            raise CommandError(f"no mote named {name!r}")
        if len(found) > 1:
            raise CommandError(f"{len(found)} motes are named {name!r} -- use #id.{key}")
        return found[0], key

    def value(self, mote_id: str, key: str) -> float | None:
        value = self.by_id[mote_id].properties.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)

    def unit(self, mote_id: str, key: str) -> str | None:
        return plotview._Directory([self.by_id[mote_id]]).unit(mote_id, key)

    def text_of(self, mote_id: str, key: str) -> str:
        """The ref a rewrite writes back: the name when it's unambiguous."""
        mote = self.by_id.get(mote_id)
        name = mote.properties.get("name") if mote else None
        if isinstance(name, str) and name and len(self.by_name.get(name, [])) == 1:
            return f"{name}.{key}"
        return f"#{mote_id}.{key}"

    def scenario(self, text: str | None) -> str | None:
        if not text:
            return None
        scenarios = scenarios_repo.list_scenarios(self.conn)
        if text.startswith("#") and any(s.mote_id == text[1:] for s in scenarios):
            return text[1:]
        found = [s.mote_id for s in scenarios if s.name.casefold() == text.casefold()]
        if not found:
            names = ", ".join(s.name for s in scenarios) or "none in this garden"
            raise CommandError(f"no scenario named {text!r} -- scenarios: {names}")
        if len(found) > 1:
            raise CommandError(f"{len(found)} scenarios are named {text!r} -- use #id")
        return found[0]


def suggest_range(value: float | None) -> tuple[float, float]:
    """A first range around what a property is now. Zero (or no value)
    has no scale to derive one from, so it gets a plain unit range."""
    if not value:
        return (0.0, 1.0)
    low, high = (v * value for v in _SUGGESTION_SPAN)
    return (low, high) if low < high else (high, low)


def resolve(conn: sqlite3.Connection, line: PlotLine) -> Resolved:
    """The specification `line` asks for. Raises CommandError for a name
    or scenario the garden doesn't have; a saved plot comes back as it
    was saved, knobs included."""
    garden = _Garden(conn)
    if line.saved is not None:
        ref = line.saved[1:] if line.saved.startswith("#") else line.saved
        view, error = plotview.find_view(ref, garden.motes)
        if view is None:
            raise CommandError(error)
        spec = view.properties.get(PLOT_KEY)
        if not isinstance(spec, dict):
            raise CommandError("this saved plot's specification is unreadable")
        return Resolved(
            spec, name=plotview.name_of(view), view_id=view.id,
            series=tuple(SeriesRef(s.get("id") or f"s{i}", garden.text_of(s["mote"], s["key"]), False)
                         for i, s in enumerate(spec.get("series") or [])
                         if isinstance(s, dict) and s.get("mote") and s.get("key")
                         and s["mote"] in garden.by_id),
            knobs=tuple(_knob(garden, garden.text_of(k["mote"], k["key"]), k["mote"], k["key"],
                              float(k["value"]))
                        for k in spec.get("knobs") or []
                        if isinstance(k, dict) and k.get("mote") in garden.by_id),
        )

    scenario_id = garden.scenario(line.scenario)
    knobs = tuple(_knob(garden, k.ref, k.mote, k.key, k.value)
                  for k in knobs_repo.resolve(garden, line.sets))
    spec: dict = {
        "mode": line.mode,
        "knobs": [{"mote": k.mote, "key": k.key, "value": k.value} for k in knobs],
        "yScale": "log" if line.log else "linear",
    }
    if scenario_id:
        spec["scenario"] = scenario_id
    if line.across:
        spec["xKey"], spec["yKey"] = line.x, line.ys[0]
        return Resolved(spec, knobs=knobs)

    if line.x == TIME:
        return _resolve_time(garden, line, spec, scenario_id, knobs)

    x_mote, x_key = garden.ref(line.x)
    freedoms = simulation_repo.garden_freedoms(conn)
    if line.by:
        steps = line.steps or DEFAULT_SURFACE_STEPS
        spec["x"] = _axis(garden, x_mote, x_key, line.range, steps)
        spec["y"] = _axis(garden, *garden.ref(line.by), line.by_range, steps)
        out_mote, out_key = garden.ref(line.ys[0])
        spec["output"] = {"mote": out_mote, "key": out_key}
        return Resolved(spec, series=(SeriesRef("s0", line.ys[0], False),), knobs=knobs,
                        x_pinned=x_key in freedoms.get(x_mote, set()))

    spec["x"] = _axis(garden, x_mote, x_key, line.range, line.steps or DEFAULT_STEPS)
    series, legend = [], []
    for index, ref in enumerate(line.ys):
        mote_id, key = garden.ref(ref)
        series_id = f"s{index}"
        legend.append(SeriesRef(series_id, ref, ref in line.hide))
        if ref not in line.hide:
            series.append({"id": series_id, "mote": mote_id, "key": key, "scenario": scenario_id})
    spec["series"] = series
    return Resolved(spec, series=tuple(legend), knobs=knobs,
                    x_pinned=x_key in freedoms.get(x_mote, set()),
                    overlay=(*line.data, line.source) if line.data else None)


def _resolve_time(garden: _Garden, line: PlotLine, spec: dict, scenario_id, knobs) -> Resolved:
    """`plot … vs t`: a sweep whose x is the tick, so it draws, saves,
    exports and fits through everything a sweep does (core/sweep.py)."""
    lo, hi = _time_range(line.range)
    spec["x"] = {"time": True, "lo": lo, "hi": hi, "steps": hi - lo}
    series, legend = [], []
    for index, ref in enumerate(line.ys):
        mote_id, key = garden.ref(ref)
        series_id = f"s{index}"
        legend.append(SeriesRef(series_id, ref, ref in line.hide))
        if ref not in line.hide:
            series.append({"id": series_id, "mote": mote_id, "key": key, "scenario": scenario_id})
    spec["series"] = series
    return Resolved(spec, series=tuple(legend), knobs=knobs,
                    overlay=(*line.data, line.source) if line.data else None)


def line_of(conn: sqlite3.Connection, spec: dict) -> PlotLine:
    """A saved specification written back as a typed line, so a pane
    showing `plot "Load curve"` can be changed in place. What a line
    can't say -- a per-series scenario other than the first, an `extra`
    override, a baseline -- is left out."""
    garden = _Garden(conn)
    sets = tuple(knobs_repo.KnobSpec(garden.text_of(k["mote"], k["key"]), float(k["value"]))
                 for k in spec.get("knobs") or [] if isinstance(k, dict) and k.get("mote") in garden.by_id)
    log = spec.get("yScale") == "log"
    series = [s for s in spec.get("series") or [] if isinstance(s, dict) and s.get("mote") in garden.by_id]
    scenario_id = spec.get("scenario") or next((s.get("scenario") for s in series if s.get("scenario")), None)
    scenario = next((s.name for s in scenarios_repo.list_scenarios(conn) if s.mote_id == scenario_id), None)
    mode = spec.get("mode") or "sweep"
    if mode == "correlate":
        return PlotLine(ys=(spec.get("yKey") or "",), x=spec.get("xKey"), across=True,
                        sets=sets, scenario=scenario, log=log)
    x = spec.get("x") or {}
    if x.get("time"):
        return PlotLine(ys=tuple(garden.text_of(s["mote"], s["key"]) for s in series), x=TIME,
                        range=(float(x["lo"]), float(x["hi"])), sets=sets, scenario=scenario, log=log)
    if x.get("mote") not in garden.by_id:
        raise CommandError("this saved plot's axis names a mote that is gone")
    x_ref = garden.text_of(x["mote"], x["key"])
    x_range = (float(x["lo"]), float(x["hi"]))
    steps = int(x.get("steps") or DEFAULT_STEPS)
    if mode == "surface":
        y, out = spec.get("y") or {}, spec.get("output") or {}
        return PlotLine(ys=(garden.text_of(out["mote"], out["key"]),), x=x_ref, range=x_range,
                        by=garden.text_of(y["mote"], y["key"]),
                        by_range=(float(y["lo"]), float(y["hi"])), steps=steps,
                        sets=sets, scenario=scenario, log=log)
    return PlotLine(ys=tuple(garden.text_of(s["mote"], s["key"]) for s in series), x=x_ref,
                    range=x_range, steps=steps, sets=sets, scenario=scenario, log=log)


def _axis(garden: _Garden, mote_id: str, key: str, span, steps: int) -> dict:
    lo, hi = span or suggest_range(garden.value(mote_id, key))
    return {"mote": mote_id, "key": key, "lo": lo, "hi": hi, "steps": steps}


def _knob(garden: _Garden, ref: str, mote_id: str, key: str, value: float) -> Knob:
    lo, hi = suggest_range(garden.value(mote_id, key) or value)
    return Knob(ref, mote_id, key, value, min(lo, value), max(hi, value), garden.unit(mote_id, key))


# ---- text helpers -------------------------------------------------------

def _quote(text: str) -> str:
    return shlex.quote(text)


def _range_text(span: tuple[float, float]) -> str:
    return f"{_number_text(span[0])}:{_number_text(span[1])}"


# ---- completion ---------------------------------------------------------

@dataclass(frozen=True)
class Choice:
    value: str
    description: str = ""


def choices(conn: sqlite3.Connection, rest: str) -> list[Choice]:
    """What may come next after `plot `: `save`/`export` or a value
    first, `vs`/`by` once there is something to plot, a scenario after
    `--scenario`, and garden-scope refs (numeric properties and the
    solver's freedoms) almost everywhere else."""
    words = rest.split()
    partial = "" if not rest or rest[-1].isspace() else words[-1]
    done = words if not partial else words[:-1]
    before = done[-1] if done else None
    if done and done[0] == EXPORT:
        return [Choice(f) for f in EXPORT_FORMATS] if len(done) == 1 else []
    if done and done[0] == SAVE:
        if len(done) == 1:
            return [Choice(plotview.name_of(v), "saved plot")
                    for v in plotview.list_views(motes_repo.list_motes(conn))]
        return []
    if partial.startswith("-"):
        return [Choice(flag) for flag in FLAGS]
    if before == SCENARIO_FLAG:
        return [Choice(s.name, "scenario") for s in scenarios_repo.list_scenarios(conn)]
    if before == STEPS_FLAG:
        return [Choice(str(DEFAULT_STEPS))]
    known = refs(conn)
    if not done:
        saved = [Choice(plotview.name_of(v), "saved plot")
                 for v in plotview.list_views(motes_repo.list_motes(conn))]
        return known + saved + [Choice(SAVE, "save the focused plot as a mote"),
                               Choice(EXPORT, "export the focused plot")]
    keywords = []
    if VS not in done:
        keywords.append(Choice(VS, "then the value to sweep"))
    elif BY not in done and _RANGE_RE.match(before or ""):
        keywords.append(Choice(BY, "a second axis: a surface"))
    if before == VS:
        keywords.append(Choice(TIME, "time: the ticks of a run (0:50)"))
    return keywords + known


def refs(conn: sqlite3.Connection) -> list[Choice]:
    garden = _Garden(conn)
    freedoms = simulation_repo.garden_freedoms(conn)
    rows: dict[str, Choice] = {}
    for mote in garden.motes:
        if PLOT_KEY in mote.properties:
            continue
        keys = [k for k, v in mote.properties.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)]
        keys += [k for k in sorted(freedoms.get(mote.id, set())) if k not in keys]
        for key in keys:
            ref = garden.text_of(mote.id, key)
            if ref.startswith("#"):
                continue
            free = key in freedoms.get(mote.id, set())
            rows.setdefault(ref, Choice(ref, "solved" if free else "given"))
    return sorted(rows.values(), key=lambda c: c.value.casefold())


# ---- the one write ------------------------------------------------------

def run_save(conn: sqlite3.Connection, command: PlotCommand):
    """`plot save ‹name› ‹plot line›`: the line's specification written as
    a saved plot (core/plotview.py::save_plot). Returns (message, undo
    Command). A line naming a saved plot copies that one's spec."""
    if command.kind != SAVE or command.line is None or command.line.empty:
        raise CommandError("usage: plot save ‹name› ‹plot line›")
    resolved = resolve(conn, command.line)
    spec = dict(resolved.spec)
    _mote_id, created, undo_command = plotview.save_plot(conn, command.name, spec)
    verb = "saved" if created else "updated"
    return f"{verb} plot {command.name!r}", undo_command
