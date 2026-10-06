# core/plotexport.py
"""A plot as a publication figure: SVG, pgfplots (`.tex`) or a PDF.

The line is the one `plot` takes (`core/plotcmd.py`), so what is exported
is what the pane was drawing, re-solved at full resolution -- plus the two
things a pane does not draw (decided 2026-10-05, `roadmap.md` -> *Plots*):

- **a ± band.** Where any given carries an `uncertainty`, each series gets
  the propagated σ at every x, drawn as a shaded band. It is derived the way
  `core/uncertainty.py` derives a point's σ -- central differences of the
  solve, root-sum-square -- but across the sweep: two extra sweeps per
  uncertain given, each the ordinary sweep with that given nudged as a knob,
  so unit handling and failed points are exactly the figure's own.
- **measured points** (`--data ‹ykey› vs ‹xkey› [--from ‹import›]`), picked
  as `fit` picks them, with error bars from each data mote's own
  `uncertainty`.

Three writers share one layout decision (white ground, Okabe-Ito colours,
unit-labelled axes, one panel per unit band, log axes where the plot is
log): `svg()` draws it itself, `pgfplots()` is the LaTeX figure
(`core/texexport.py` draws a note's `\\graph` with the same code, without the
overlay), and `pdf()` compiles the latter standalone with tectonic.
Sweeps only: a surface or a correlation has no publication form yet.
"""
import math
import sqlite3
from dataclasses import dataclass, field
from xml.sax.saxutils import escape as xml_escape

from core import fit as fit_repo
from core import plotcmd, plotview, simulation
from core import uncertainty as uncertainty_repo
from core.command_bar import CommandError

FORMATS = ("svg", "pdf", "tex")

# Okabe-Ito: distinguishable in print and to colour-blind readers.
PALETTE = ("#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00", "#56B4E9")
_NUDGE = 1e-4


class PlotExportError(ValueError):
    """The plot cannot be exported; the message is the sentence to show."""


@dataclass(frozen=True)
class Overlay:
    label: str
    points: tuple  # (x, y, sx | None, sy | None)


@dataclass(frozen=True)
class Publication:
    figure: dict
    # series id -> σ at each of its points (None where it has no point)
    sigma: dict[str, list] = field(default_factory=dict)
    overlay: Overlay | None = None
    name: str = "plot"
    problems: tuple[str, ...] = ()


# --- building --------------------------------------------------------

def build(conn: sqlite3.Connection, line_text: str) -> Publication:
    """The line, solved at full resolution, with its band and overlay."""
    try:
        command = plotcmd.parse_text(line_text)
    except CommandError as e:
        raise PlotExportError(str(e)) from e
    if command.kind != plotcmd.PLOT or command.line is None or command.line.empty:
        raise PlotExportError("export needs a plot line, e.g. plot bd.l vs bd.x 1:9")
    try:
        resolved = plotcmd.resolve(conn, command.line)
    except CommandError as e:
        raise PlotExportError(str(e)) from e
    spec = resolved.spec
    if spec.get("mode") != "sweep":
        raise PlotExportError(f"only sweep plots export as figures, not {spec.get('mode')}")
    steps = int((spec.get("x") or {}).get("steps") or plotcmd.DEFAULT_STEPS)
    figure = plotview.figure_from_spec(conn, spec, name=resolved.name, view_id=resolved.view_id,
                                       max_points=steps + 1)
    if figure.error:
        raise PlotExportError(figure.error)
    problems: list[str] = []
    sigma = _band(conn, spec, figure, steps, problems)
    overlay = _overlay(conn, spec, resolved.overlay) if resolved.overlay else None
    return Publication(plotview.figure_json(figure), sigma, overlay,
                       resolved.name or "plot", tuple(problems))


def _band(conn, spec: dict, figure, steps: int, problems: list[str]) -> dict[str, list]:
    """σ(x) per series from nudging each uncertain given as a knob."""
    from core.motes import list_motes
    listed = list_motes(conn)
    properties = {m.id: dict(m.properties) for m in listed}
    labels = {m.id: (m.properties.get("name") if isinstance(m.properties.get("name"), str)
                     else m.id[:8]) for m in listed}
    givens, notes = uncertainty_repo._inputs(properties, simulation.garden_freedoms(conn), labels)
    problems.extend(notes)
    x_axis = spec["x"]
    givens = [g for g in givens if (g.mote_id, g.key) != (x_axis.get("mote"), x_axis.get("key"))]
    if not givens:
        return {}
    knobs = [k for k in spec.get("knobs") or []]
    base = {s.id: s.points for s in figure.series}
    variance = {sid: [0.0] * len(points) for sid, points in base.items()}
    for given in givens:
        x = float(properties[given.mote_id][given.key])
        h = _NUDGE * max(abs(x), given.sigma) or _NUDGE
        sides = []
        for value in (x + h, x - h):
            others = [k for k in knobs if (k["mote"], k["key"]) != (given.mote_id, given.key)]
            nudged = {**spec, "knobs": others + [{"mote": given.mote_id, "key": given.key, "value": value}]}
            drawn = plotview.figure_from_spec(conn, nudged, max_points=steps + 1)
            if drawn.error:
                problems.append(f"{given.label}: the plot does not solve with it nudged ({drawn.error})")
                sides = []
                break
            sides.append({s.id: s.points for s in drawn.series})
        if not sides:
            continue
        up, down = sides
        for sid, points in base.items():
            for i, point in enumerate(points):
                hi = up.get(sid, [None] * len(points))[i] if i < len(up.get(sid, [])) else None
                lo = down.get(sid, [None] * len(points))[i] if i < len(down.get(sid, [])) else None
                if point is None or hi is None or lo is None:
                    continue
                derivative = (hi[1] - lo[1]) / (2 * h)
                variance[sid][i] += (derivative * given.sigma) ** 2
    return {sid: [None if base[sid][i] is None else math.sqrt(v) for i, v in enumerate(vs)]
            for sid, vs in variance.items()}


def _overlay(conn, spec: dict, wanted) -> Overlay:
    data_y, data_x, source = wanted
    series = [s for s in spec.get("series") or [] if s.get("mote") and s.get("key")]
    if not series:
        raise PlotExportError("--data needs a plotted series to draw over")
    if spec["x"].get("time"):
        raise PlotExportError("--data over a time axis isn't drawn yet -- plot against a property")
    garden = plotcmd._Garden(conn)
    x_ref = (spec["x"]["mote"], spec["x"]["key"])
    y_ref = (series[0]["mote"], series[0]["key"])
    line = fit_repo.FitLine(y="", x="", data_y=data_y, data_x=data_x, source=source)
    try:
        points = fit_repo._data(conn, garden, line, x_ref, y_ref)
    except CommandError as e:
        raise PlotExportError(str(e)) from e
    return Overlay(f"{data_y} vs {data_x}" + (f" ({source})" if source else ""),
                   tuple((p.x, p.y, p.sx, p.sy) for p in points))


# --- shared layout ---------------------------------------------------

def _bands(figure: dict) -> list[dict]:
    if figure["bands"]:
        return [{"label": b["label"], "unit": b.get("unit") or "", "series": b["series"]}
                for b in figure["bands"]]
    return [{"label": figure["yLabel"], "unit": "", "series": [s["id"] for s in figure["series"]]}]


def y_label(band: dict) -> str:
    label, unit = band["label"], band["unit"]
    if unit and not any(f"{o}{unit}{c}" in label for o, c in ("[]", "()")):
        return f"{label} [{unit}]"
    return label


def _range(values: list[float], log: bool) -> tuple[float, float]:
    values = [v for v in values if v is not None and (v > 0 or not log)]
    if not values:
        return (1.0, 10.0) if log else (0.0, 1.0)
    lo, hi = min(values), max(values)
    if log:
        return 10 ** math.floor(math.log10(lo)), 10 ** math.ceil(math.log10(hi))
    if lo == hi:
        return lo - 1, hi + 1
    pad = (hi - lo) * 0.05
    return lo - pad, hi + pad


def nice_ticks(lo: float, hi: float, target: int = 6) -> list[float]:
    if hi <= lo:
        return [lo]
    raw = (hi - lo) / max(target, 1)
    magnitude = 10 ** math.floor(math.log10(raw))
    step = next(m * magnitude for m in (1, 2, 2.5, 5, 10) if m * magnitude >= raw)
    first = math.ceil(lo / step - 1e-9) * step
    out, tick = [], first
    while tick <= hi + step * 1e-9:
        out.append(round(tick, 12))
        tick += step
    return out


def log_ticks(lo: float, hi: float) -> list[float]:
    low, high = math.floor(math.log10(lo)), math.ceil(math.log10(hi))
    return [10.0 ** e for e in range(low, high + 1)]


def tick_text(value: float) -> str:
    if value == 0:
        return "0"
    if abs(value) >= 1e5 or abs(value) < 1e-3:
        mantissa, exponent = f"{value:.2e}".split("e")
        mantissa = mantissa.rstrip("0").rstrip(".")
        return f"{mantissa}×10{_superscript(int(exponent))}"
    return f"{value:.6g}"


def _superscript(n: int) -> str:
    return str(n).translate(str.maketrans("-0123456789", "⁻⁰¹²³⁴⁵⁶⁷⁸⁹"))


# --- SVG -------------------------------------------------------------

WIDTH, PANEL_HEIGHT = 720, 300
LEFT, RIGHT, TOP, BOTTOM = 80, 170, 16, 52


def svg(pub: Publication) -> str:
    fig = pub.figure
    bands = _bands(fig)
    height = (PANEL_HEIGHT) * len(bands)
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
           f'viewBox="0 0 {WIDTH} {height}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
           f'<rect width="{WIDTH}" height="{height}" fill="#ffffff"/>']
    colours = {s["id"]: PALETTE[i % len(PALETTE)] for i, s in enumerate(fig["series"])}
    xs = [p[0] for s in fig["series"] for p in s["points"] if p is not None]
    if pub.overlay:
        xs += [p[0] for p in pub.overlay.points]
    x_lo, x_hi = (min(xs), max(xs)) if xs else (0.0, 1.0)
    if x_lo == x_hi:
        x_lo, x_hi = x_lo - 1, x_hi + 1
    for index, band in enumerate(bands):
        out += _panel(pub, band, index, colours, x_lo, x_hi, last=index == len(bands) - 1)
    out.append("</svg>")
    return "\n".join(out)


def _panel(pub: Publication, band: dict, index: int, colours: dict, x_lo: float, x_hi: float,
           last: bool) -> list[str]:
    fig = pub.figure
    log = fig.get("yScale") == "log"
    series = [s for s in fig["series"] if s["id"] in band["series"] and not s["ghost"]] \
        if fig["bands"] else [s for s in fig["series"] if not s["ghost"]]
    # y values the panel must hold: curves, their band, and the data (once, on the first panel)
    ys = []
    for s in series:
        sig = pub.sigma.get(s["id"])
        for i, p in enumerate(s["points"]):
            if p is None:
                continue
            ys.append(p[1])
            if sig and sig[i]:
                ys += [p[1] - sig[i], p[1] + sig[i]]
    if pub.overlay and index == 0:
        for _x, y, _sx, sy in pub.overlay.points:
            ys += [y] + ([y - sy, y + sy] if sy else [])
    y_lo, y_hi = _range(ys, log)
    top = index * PANEL_HEIGHT + TOP
    plot_w, plot_h = WIDTH - LEFT - RIGHT, PANEL_HEIGHT - TOP - BOTTOM

    def X(x):
        return LEFT + (x - x_lo) / (x_hi - x_lo) * plot_w

    def Y(y):
        if log:
            y = max(y, y_lo)
            frac = (math.log10(y) - math.log10(y_lo)) / (math.log10(y_hi) - math.log10(y_lo))
        else:
            frac = (y - y_lo) / (y_hi - y_lo)
        return top + plot_h - frac * plot_h

    out = [f'<g>', f'<rect x="{LEFT}" y="{top}" width="{plot_w}" height="{plot_h}" fill="none" stroke="#000"/>']
    for tick in nice_ticks(x_lo, x_hi):
        out.append(f'<line x1="{X(tick):.2f}" y1="{top}" x2="{X(tick):.2f}" y2="{top + plot_h}" stroke="#ddd"/>')
        out.append(f'<text x="{X(tick):.2f}" y="{top + plot_h + 16}" text-anchor="middle">{xml_escape(tick_text(tick))}</text>')
    for tick in (log_ticks(y_lo, y_hi) if log else nice_ticks(y_lo, y_hi)):
        if not y_lo <= tick <= y_hi:
            continue
        out.append(f'<line x1="{LEFT}" y1="{Y(tick):.2f}" x2="{LEFT + plot_w}" y2="{Y(tick):.2f}" stroke="#ddd"/>')
        out.append(f'<text x="{LEFT - 6}" y="{Y(tick) + 4:.2f}" text-anchor="end">{xml_escape(tick_text(tick))}</text>')
    out.append(f'<text x="{LEFT + plot_w / 2}" y="{top + plot_h + 38}" text-anchor="middle">{xml_escape(fig["xLabel"])}</text>')
    cx, cy = 18, top + plot_h / 2
    out.append(f'<text transform="rotate(-90 {cx} {cy})" x="{cx}" y="{cy}" text-anchor="middle">{xml_escape(y_label(band))}</text>')
    for s in series:
        colour = colours[s["id"]]
        sig = pub.sigma.get(s["id"])
        if sig:
            upper = [(p[0], p[1] + sig[i]) for i, p in enumerate(s["points"]) if p is not None]
            lower = [(p[0], p[1] - sig[i]) for i, p in enumerate(s["points"]) if p is not None][::-1]
            if upper:
                pts = " ".join(f"{X(x):.2f},{Y(y):.2f}" for x, y in upper + lower)
                out.append(f'<polygon points="{pts}" fill="{colour}" fill-opacity="0.2" stroke="none"/>')
        for run in _runs(s["points"]):
            pts = " ".join(f"{X(x):.2f},{Y(y):.2f}" for x, y in run)
            out.append(f'<polyline points="{pts}" fill="none" stroke="{colour}" stroke-width="1.8" stroke-linejoin="round"/>')
    if pub.overlay and index == 0:
        for x, y, sx, sy in pub.overlay.points:
            if sy:
                out.append(f'<line x1="{X(x):.2f}" y1="{Y(y - sy):.2f}" x2="{X(x):.2f}" y2="{Y(y + sy):.2f}" stroke="#000"/>')
            if sx:
                out.append(f'<line x1="{X(x - sx):.2f}" y1="{Y(y):.2f}" x2="{X(x + sx):.2f}" y2="{Y(y):.2f}" stroke="#000"/>')
            out.append(f'<circle cx="{X(x):.2f}" cy="{Y(y):.2f}" r="3.2" fill="#000"/>')
    lx, ly = LEFT + plot_w + 14, top + 8
    for s in series:
        out.append(f'<line x1="{lx}" y1="{ly}" x2="{lx + 22}" y2="{ly}" stroke="{colours[s["id"]]}" stroke-width="2"/>')
        out.append(f'<text x="{lx + 28}" y="{ly + 4}">{xml_escape(s["label"])}</text>')
        ly += 18
    if pub.overlay and index == 0:
        out.append(f'<circle cx="{lx + 11}" cy="{ly}" r="3.2" fill="#000"/>')
        out.append(f'<text x="{lx + 28}" y="{ly + 4}">{xml_escape(pub.overlay.label)}</text>')
    out.append("</g>")
    return out


def _runs(points: list) -> list[list[tuple]]:
    """Maximal stretches of solved points; a hole breaks the line."""
    runs, current = [], []
    for p in points:
        if p is None:
            if current:
                runs.append(current)
            current = []
        else:
            current.append((p[0], p[1]))
    if current:
        runs.append(current)
    return runs


# --- pgfplots / PDF --------------------------------------------------

def pgfplots(pub: Publication) -> str:
    """The figure as LaTeX: one `axis` per unit band, in a `tikzpicture`
    each. Needs pgfplots with its `fillbetween` library."""
    return sweep_tikz(pub.figure, pub.sigma, pub.overlay)


def sweep_tikz(fig: dict, sigma: dict | None = None, overlay: Overlay | None = None) -> str:
    from core.texexport import escape
    sigma = sigma or {}
    bands = _bands(fig)
    colours = {s["id"]: PALETTE[i % len(PALETTE)] for i, s in enumerate(fig["series"])}
    pictures = []
    for index, band in enumerate(bands):
        series = [s for s in fig["series"] if not s["ghost"]
                  and (not fig["bands"] or s["id"] in band["series"])]
        plots = []
        for s in series:
            colour = _rgb(colours[s["id"]])
            sig = sigma.get(s["id"])
            if sig:
                for tag, sign in (("lo", -1), ("hi", 1)):
                    plots.append(f"\\addplot[name path={tag}{s['id']}, draw=none, forget plot] "
                                 f"table[row sep=\\\\] {{\n{_table([(p[0], p[1] + sign * sig[i]) for i, p in enumerate(s['points']) if p is not None])}\n}};")
                plots.append(f"\\addplot[{colour}, fill opacity=0.2, forget plot] "
                             f"fill between[of=lo{s['id']} and hi{s['id']}];")
            rows = "\n".join("  " + ("nan nan" if p is None else f"{p[0]:.8g} {p[1]:.8g}") + r" \\"
                             for p in s["points"])
            plots.append(f"\\addplot[{colour}, thick, mark=none] table[row sep=\\\\] {{\n{rows}\n}};\n"
                         f"\\addlegendentry{{{escape(s['label'])}}}")
        if overlay and index == 0:
            rows = "\n".join(f"  {x:.8g} {y:.8g} {sx or 0:.8g} {sy or 0:.8g} \\\\" for x, y, sx, sy in overlay.points)
            plots.append("\\addplot[only marks, mark=*, mark size=1.6pt, black, "
                         "error bars/.cd, x dir=both, x explicit, y dir=both, y explicit] "
                         f"table[row sep=\\\\, x index=0, y index=1, x error index=2, y error index=3] {{\n{rows}\n}};\n"
                         f"\\addlegendentry{{{escape(overlay.label)}}}")
        ymode = "log" if fig.get("yScale") == "log" else "normal"
        pictures.append(
            "\\begin{tikzpicture}\n\\begin{axis}[width=0.9\\linewidth, height=6cm, grid=major, "
            f"xlabel={{{escape(fig['xLabel'])}}}, ylabel={{{escape(y_label(band))}}}, "
            f"ymode={ymode}, legend pos=outer north east, unbounded coords=jump]\n"
            + "\n".join(plots) + "\n\\end{axis}\n\\end{tikzpicture}")
    return "\n\n".join(pictures)


def _rgb(hex_colour: str) -> str:
    r, g, b = (int(hex_colour[i:i + 2], 16) for i in (1, 3, 5))
    return f"color={{rgb,255:red,{r};green,{g};blue,{b}}}"


def _table(points) -> str:
    return "\n".join(f"  {x:.8g} {y:.8g} \\\\" for x, y in points)


STANDALONE = r"""\documentclass[border=6pt]{standalone}
\usepackage{pgfplots}
\usepgfplotslibrary{fillbetween}
\pgfplotsset{compat=1.17}
\begin{document}
%s
\end{document}
"""


def standalone(pub: Publication) -> str:
    return STANDALONE % pgfplots(pub)


def pdf(pub: Publication, fetch=None) -> bytes:
    from core import tectonic
    return tectonic.compile_pdf(standalone(pub), fetch=fetch)


def export(conn: sqlite3.Connection, line_text: str, fmt: str, fetch=None) -> tuple[bytes | str, Publication]:
    """The plot as `fmt` (svg text, tex text or pdf bytes), and what was
    drawn, whose `problems` the caller reports."""
    if fmt not in FORMATS:
        raise PlotExportError(f"export format is one of {', '.join(FORMATS)}")
    pub = build(conn, line_text)
    if fmt == "svg":
        return svg(pub), pub
    if fmt == "tex":
        return standalone(pub), pub
    return pdf(pub, fetch=fetch), pub
