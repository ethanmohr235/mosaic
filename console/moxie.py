# console/moxie.py
"""`api.moxie`: the plot pane's bridge (docs/console.md -> U4).

    plot(command, coarse)       the figure a `plot …` line draws
    rewrite(command, change)    that line with a gesture applied (a knob
                                dragged, a zoom, a series switched off)
    sensitivity(command, x)     which knob moves the first series most at x
    export_csv(command, path)   the figure's points as a file
    export_publication(command, fmt, path)
                                svg, pdf or tex with ± band and data
    export_png(data_url, name, path)
                                the pane's own rasterized SVG as a file

Everything is answered from core/: the line is parsed and resolved by
core/plotcmd.py and drawn by core/plotview.py. Nothing here writes to the
garden -- `plot save` is a command the root runs (console/api.py), so it
is in the history, the log and undo like any other write.

A pane asks twice, a coarse pass (`coarse=True`, the same range sampled
at COARSE_POINTS) that lands in about a frame and then the full one; the
pane drops a reply that a newer request has overtaken.

Errors come back as `{"error": message}` rather than raising, because a
pane shows a bad line as its own fault and the rest of the window carries
on."""
import base64
import csv
import io
import logging
import time
from pathlib import Path
from typing import Any, Callable

from console.session import Session, locked
from core import fit as fit_repo
from core import plotexport
from core import tectonic
from core import plotcmd
from core import plotview
from core.command_bar import CommandError

logger = logging.getLogger(__name__)

COARSE_POINTS = 26


class MoxieApi:
    def __init__(self, session: Session, window: Callable[[], Any] = lambda: None):
        self._session = session
        self._window = window
        self._generations: dict[str, int] = {}

    def plot(self, command: str, coarse: bool = False, channel: str = "default",
             generation: int | None = None) -> dict[str, Any]:
        """Not @locked: the garden is read under the session lock and solved
        outside it, so a long sweep never holds up another pane. A newer
        request on the same `channel` (one pane) cancels this one between
        points; it then answers `{"cancelled": true}`."""
        try:
            return self._plot(command, coarse, self._claim(channel, generation))
        except (CommandError, ValueError, LookupError) as e:
            return {"error": str(e)}
        except Exception as e:  # never a traceback across the bridge
            logger.exception("plot failed: %r", command)
            return {"error": f"internal error: {e}"}

    def fit(self, command: str) -> dict[str, Any]:
        """The `fit` pane's answer: the fit a `fit …` line asks for,
        read under the session lock and solved outside it. Writes nothing
        -- `fit … --apply` is a command the root runs."""
        try:
            with self._session.lock:
                line = fit_repo.parse_text(command)
                if line.empty:
                    return {"empty": True, "usage": fit_repo.usage()}
                if line.apply:
                    raise CommandError("a pane shows a fit; run `--apply` from the command line")
                solve = fit_repo.prepare(self._session.conn, line)
            started = time.perf_counter()
            result = solve()
        except (CommandError, ValueError, LookupError) as e:
            return {"error": str(e)}
        except Exception as e:  # never a traceback across the bridge
            logger.exception("fit failed: %r", command)
            return {"error": f"internal error: {e}"}
        fitted = dict(zip((p.mote for p in result.points if p.name not in dict(result.skipped)),
                          result.fitted))
        return {
            "params": [{"ref": p.ref, "start": p.start, "value": p.value, "sigma": p.sigma,
                        "unit": p.unit} for p in result.params],
            "points": [{"name": p.name, "x": p.x, "y": p.y, "fitted": fitted.get(p.mote)}
                       for p in result.points],
            "skipped": [{"name": n, "why": w} for n, w in result.skipped],
            "curve": [list(c) for c in result.curve],
            "rms": result.rms, "r2": result.r2, "iterations": result.iterations,
            "converged": result.converged, "solves": result.solves,
            "message": result.message, "xLabel": result.x_label, "yLabel": result.y_label,
            "applyCommand": line.text(apply=True),
            "seconds": round(time.perf_counter() - started, 3),
        }

    @locked
    def rewrite(self, command: str, change: dict) -> dict[str, Any]:
        """A saved plot (`plot "Load curve"`) is written out as a typed
        line first, so it can be changed in place like any other."""
        try:
            line = self._line(command)
            if line.saved is not None:
                line = plotcmd.line_of(self._session.conn, plotcmd.resolve(self._session.conn, line).spec)
            return {"command": plotcmd.rewrite(line, change).text()}
        except (CommandError, ValueError, KeyError, TypeError) as e:
            return {"error": str(e)}

    @locked
    def sensitivity(self, command: str, x: float) -> dict[str, Any]:
        try:
            resolved = plotcmd.resolve(self._session.conn, self._line(command))
            rows = plotview.sensitivity(self._session.conn, resolved.spec, float(x))
        except (CommandError, ValueError) as e:
            return {"error": str(e)}
        refs = {(k.mote, k.key): k.ref for k in resolved.knobs}
        return {"rows": [{"ref": refs.get((r.mote, r.key), r.label), "value": r.value} for r in rows]}

    @locked
    def export_csv(self, command: str, path: str | None = None) -> dict[str, Any]:
        """The full-resolution figure's points. Re-drawn here rather than
        taken from the pane, so a file never holds a coarse pass."""
        try:
            resolved = plotcmd.resolve(self._session.conn, self._line(command))
            figure = self._figure(resolved, coarse=False)
        except (CommandError, ValueError) as e:
            return {"error": str(e)}
        if figure.error:
            return {"error": figure.error}
        columns, rows = plotview.csv_table(figure)
        target = self._target(path, f"{_filename(resolved, command)}.csv")
        if target is None:
            return {"cancelled": True}
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(columns)
        writer.writerows(rows)
        target.write_text(buffer.getvalue())
        return {"path": str(target)}

    @locked
    def export_publication(self, command: str, fmt: str, path: str | None = None) -> dict[str, Any]:
        """The plot as a publication figure -- svg, pdf or the pgfplots
        `.tex` -- with its propagated ± band and any `--data` points
        (core/plotexport.py). Re-solved here at full resolution, like
        `export_csv`. A pdf is compiled by tectonic, which can take a
        while the first time."""
        try:
            data, pub = plotexport.export(self._session.conn, command, fmt)
        except (plotexport.PlotExportError, tectonic.TectonicError) as e:
            return {"error": str(e)}
        target = self._target(path, f"{pub.name}.{fmt}")
        if target is None:
            return {"cancelled": True}
        (target.write_bytes if isinstance(data, bytes) else target.write_text)(data)
        return {"path": str(target), "problems": list(pub.problems)}

    def export_png(self, data_url: str, name: str = "plot", path: str | None = None) -> dict[str, Any]:
        """The pane rasterizes its own SVG -- it is what knows how the plot
        looks -- and this only decodes it and puts it where asked."""
        target = self._target(path, f"{name or 'plot'}.png")
        if target is None:
            return {"cancelled": True}
        _header, _, encoded = (data_url or "").partition(",")
        try:
            target.write_bytes(base64.b64decode(encoded, validate=True))
        except ValueError as e:
            return {"error": f"not an image: {e}"}
        return {"path": str(target)}

    # ---- private ----

    def _line(self, command: str) -> plotcmd.PlotLine:
        parsed = plotcmd.parse_text(command)
        if parsed.kind != plotcmd.PLOT:
            raise CommandError(f"{command!r} is an action, not a plot")
        return parsed.line

    def _claim(self, channel: str, generation: int | None) -> Callable[[], bool]:
        if generation is None:
            return lambda: False
        with self._session.lock:
            self._generations[channel] = max(self._generations.get(channel, 0), generation)
        return lambda: self._generations.get(channel, 0) > generation

    def _plot(self, command: str, coarse: bool, should_cancel) -> dict[str, Any]:
        with self._session.lock:
            line = self._line(command)
            if line.empty:
                return {"empty": True, "usage": plotcmd.usage()}
            resolved = plotcmd.resolve(self._session.conn, line)
            ground, motes = plotview.read_for(self._session.conn, resolved.spec)
        started = time.perf_counter()
        figure = self._figure(resolved, coarse, ground=ground, motes=motes, should_cancel=should_cancel)
        if figure.cancelled:
            return {"cancelled": True}
        return {
            "mode": figure.mode,
            "figure": plotview.figure_json(figure),
            "series": [{"id": s.id, "ref": s.ref, "hidden": s.hidden} for s in resolved.series],
            "knobs": [{"ref": k.ref, "value": k.value, "lo": k.lo, "hi": k.hi, "unit": k.unit}
                      for k in resolved.knobs],
            "xPinned": resolved.x_pinned,
            "saved": line.saved is not None,
            "name": resolved.name,
            "coarse": coarse,
            "seconds": round(time.perf_counter() - started, 3),
        }

    def _figure(self, resolved: plotcmd.Resolved, coarse: bool, **snapshot) -> plotview.Figure:
        spec = resolved.spec
        steps = max(int((spec.get("x") or {}).get("steps") or plotcmd.DEFAULT_STEPS),
                    int((spec.get("y") or {}).get("steps") or 0))
        points = min(steps + 1, COARSE_POINTS) if coarse else steps + 1
        return plotview.figure_from_spec(None if snapshot else self._session.conn, spec,
                                         name=resolved.name, view_id=resolved.view_id,
                                         max_points=points, **snapshot)

    def _target(self, path: str | None, filename: str) -> Path | None:
        if path:
            return Path(path).expanduser()
        window = self._window()
        if window is None:
            return None
        import webview
        chosen = window.create_file_dialog(webview.FileDialog.SAVE, save_filename=filename)
        if not chosen:
            return None
        return Path(chosen if isinstance(chosen, str) else chosen[0])


def _filename(resolved: plotcmd.Resolved, command: str) -> str:
    if resolved.name:
        return resolved.name
    return "plot"
