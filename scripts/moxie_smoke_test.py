#!/usr/bin/env python3
"""Regression smoke test for Moxie, the graphing window.

Not named test_*.py, for the same reason scripts/motion_smoke_test.py
isn't: it spawns a real pywebview window against a real WebKit/GTK
display, which the pytest suite deliberately assumes nothing about. Run
it by hand after touching moxie/:

    .venv/bin/python scripts/moxie_smoke_test.py

Run it alone. The pywebview window suites interfere with each other, and
two at once produce failures that are about the display and not the code.

What it is for is the half of Moxie that Python tests cannot reach --
that clicking a property really draws a curve, that the numbers on that
curve are the ones the solver found, that dragging a knob really moves
it, that a point which failed to solve leaves a *gap* rather than a line
drawn through it, and that the grid really holds two plots that do not
interfere. The bridge is tests/test_moxie_api.py's job and the engine is
tests/test_sweep.py's; what is untested anywhere else is the wiring.

The garden it seeds is a voltage divider, because every number in it has
a closed form: current is voltage / (r_a + r_b). A check that says "the
curve has 201 points" proves the window drew something; a check that
says "the point at 20 V reads 0.0667" proves it drew the right thing.
"""
import functools
import http.server
import json
import socketserver
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.db as db

_TMP = Path(tempfile.mkdtemp(prefix="moxie-smoke-"))
db.GARDENS_DIR = _TMP / "gardens"

import webview  # noqa: E402  (after GARDENS_DIR is patched, matching app startup order)

from core.db import get_connection, initialize_database  # noqa: E402
from core.gardens import create_garden  # noqa: E402
from core.motes import create_mote, get_mote, list_motes  # noqa: E402
from core.relationships import create_relationship  # noqa: E402
from core.species import INSTANCE_OF  # noqa: E402
from core.sweep import PLOT_KEY  # noqa: E402
from moxie.api import Api  # noqa: E402
from moxie.app import DEFAULT_HTTP_PORT  # noqa: E402
from window_launcher import choose_port  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "moxie" / "frontend"
GARDEN = "smoke"

# The ports pywebview pins the other windows to -- including **Gardens at
# 42007**, which is the one that matters here. Gardens is not in
# api_common.APPS (it is a window onto the list of gardens rather than
# onto a garden), so it is exactly the port a seventh window is likely to
# claim by counting the six apps and adding one. This suite runs with it
# occupied and serving Gardens' own frontend, so a Moxie that asked for
# it would not merely fail -- it would load the Gardens page against its
# own bridge, as Mora once did.
OCCUPIED = (
    (42001, "Monarch", "monarch/frontend"),
    (42002, "Mora", "mora/frontend"),
    (42003, "Monastery", "monastery/frontend"),
    (42005, "Monolith", "monolith/frontend"),
    (42006, "Motion", "motion/frontend"),
    (42007, "Gardens", "gardens_app/frontend"),
)

ids = {}
results = []
_page_loaded = threading.Event()


def get_conn():
    conn = get_connection(GARDEN)
    initialize_database(conn)
    return conn


def check(name, fn):
    try:
        fn()
        results.append((name, True, ""))
        print(f"PASS  {name}")
    except Exception as e:
        results.append((name, False, str(e)))
        print(f"FAIL  {name}: {e}")


def js(window, code):
    return window.evaluate_js(code)


def wait_until(condition, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if condition():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def count(window, selector):
    return js(window, f"document.querySelectorAll({json.dumps(selector)}).length")


def text(window, selector):
    return js(window, f"""
        (document.querySelector({json.dumps(selector)}) || {{}}).textContent || ''
    """).strip()


def texts(window, selector):
    return json.loads(js(window, f"""
        JSON.stringify(Array.from(document.querySelectorAll({json.dumps(selector)}))
            .map(n => n.textContent.trim()))
    """))


def chip(name, key):
    return f'.prop-chip[data-mote="{ids[name]}"][data-key="{key}"]'


def click_chip(window, name, key, *, alt=False, shift=False):
    """Click a property chip the way a person does, modifiers included --
    the chip's own handler reads event.altKey to tell 'plot this' from
    'sweep this', so .click() would always mean the first."""
    js(window, f"""
        (() => {{
          const el = document.querySelector({json.dumps(chip(name, key))});
          if (!el) throw new Error('no chip ' + {json.dumps(name + '.' + key)});
          el.dispatchEvent(new MouseEvent('click', {{
            bubbles: true, altKey: {str(alt).lower()}, shiftKey: {str(shift).lower()},
          }}));
        }})();
    """)


def curve(window, plot=0):
    """The drawn points of the first series in a plot, read back off the
    SVG path itself rather than out of the app's state -- the question is
    what got drawn, not what was computed."""
    return json.loads(js(window, f"""
        (() => {{
          const cell = document.querySelector('.plot-cell[data-plot="{plot}"]');
          if (!cell) return 'null';
          const path = cell.querySelector('path.series-path');
          if (!path) return 'null';
          return JSON.stringify(path.getAttribute('d')
            .split(/[ML]/).filter(Boolean)
            .map(pair => pair.trim().split(/\\s+/).map(Number)));
        }})();
    """) or "null")


def series_count(window, plot=0):
    return count(window, f'.plot-cell[data-plot="{plot}"] path.series-path')


def run_count():
    return get_conn().execute("SELECT COUNT(*) FROM simulation_runs").fetchone()[0]


def seed():
    """A voltage divider whose every answer has a closed form, plus the
    two things a plot has to survive: a constraint the answer breaks over
    part of the range, and a scenario to use as a baseline."""
    create_garden(GARDEN)
    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        ids["species"] = create_mote(conn, {"name": "Divider", "freedoms": "current"})
        ids["rule"] = create_mote(
            conn, {"name": "series", "equation": "current * (r_a + r_b) == voltage"}
        )
        create_relationship(conn, ids["rule"], ids["species"], "applies_to")
        # current <= 0.05 is broken above 15 V, so part of the sweep is shaded.
        ids["limit"] = create_mote(conn, {"name": "rating", "equation": "current <= 0.05"})
        create_relationship(conn, ids["limit"], ids["species"], "applies_to")

        ids["circuit"] = create_mote(
            conn, {"name": "C1", "voltage": 10.0, "r_a": 100.0, "r_b": 200.0}
        )
        create_relationship(conn, ids["circuit"], ids["species"], INSTANCE_OF)

        ids["override"] = create_mote(conn, {"r_b": 700.0})
        create_relationship(conn, ids["override"], ids["circuit"], "overrides")
        ids["scenario"] = create_mote(conn, {"name": "Stiff"})
        create_relationship(conn, ids["scenario"], ids["override"], "varies")
    finally:
        conn.close()


def run_checks(window):
    # First, and deliberately a named check rather than a bare assert:
    # everything below assumes this window is Moxie, and when that
    # stopped being true for Mora the suite died on an unattributed
    # assertion and reported "0/0 checks passed" -- true and useless.
    def moxie_loads_its_own_frontend():
        url = js(window, "location.href")
        for port, name, _dir in OCCUPIED:
            assert f":{port}/" not in url, \
                f"Moxie must not serve off {name}'s port, got {url}"
        assert js(window, "!!document.getElementById('grid')") is True, \
            f"the page in this window is not Moxie's ({url})"
        assert js(window, "!document.getElementById('dock')") is True, \
            f"this window loaded Motion's frontend, not Moxie's ({url})"
        assert wait_until(lambda: count(window, ".mote-card") >= 1), \
            "Moxie's frontend loaded but never drew the mote rail"

    check("Moxie comes up on its own port with its own frontend",
          moxie_loads_its_own_frontend)
    if not results[-1][1]:
        return  # nothing below can mean anything if this window is not Moxie

    # ---- the rail ----

    def the_rail_lists_only_motes_that_hold_numbers():
        names = texts(window, ".mote-card .mote-name")
        assert "C1" in names, f"the circuit should be listed, got {names}"
        assert "series" not in names, "a rule mote holds no plottable number"
        assert "Stiff" not in names, "a scenario mote holds no plottable number"

    check("the rail lists only motes that hold numbers",
          the_rail_lists_only_motes_that_hold_numbers)

    def an_unsolved_freedom_is_still_offered():
        """`current` has never been solved, so the mote does not carry it
        -- and it is the one curve worth drawing."""
        assert get_mote(get_conn(), ids["circuit"]).properties.get("current") is None
        assert count(window, chip("circuit", "current")) == 1, \
            "a declared freedom with no value yet must still be plottable"

    check("a freedom nothing has solved yet is still offered",
          an_unsolved_freedom_is_still_offered)

    # ---- a sweep ----

    def clicking_a_property_draws_a_curve_with_the_right_numbers():
        click_chip(window, "circuit", "current")            # y
        click_chip(window, "circuit", "voltage", alt=True)  # x
        js(window, f"""
            (() => {{
              const p = plots[0];
              p.x.lo = 0; p.x.hi = 20; p.x.steps = 200;
              scheduleRun(p);
            }})();
        """)
        assert wait_until(lambda: series_count(window) == 1), "no curve was drawn"
        assert wait_until(lambda: len(curve(window) or []) > 100), \
            "the curve was drawn but with almost no points in it"
        points = curve(window)
        assert len(points) == 201, f"a 200-step sweep draws 201 points, got {len(points)}"
        # Read the value back through the app rather than off the pixels:
        # the geometry is the renderer's business, the number is not.
        last = js(window, "JSON.stringify(plots[0].data.points[200])")
        value = list(json.loads(last)["v"].values())[0]
        assert abs(value - 20 / 300) < 1e-9, \
            f"20 V across 300 ohm is {20/300}, the plot says {value}"

    check("clicking a property draws a curve with the right numbers",
          clicking_a_property_draws_a_curve_with_the_right_numbers)

    def a_sweep_writes_nothing_and_logs_nothing():
        properties = get_mote(get_conn(), ids["circuit"]).properties
        assert "current" not in properties, \
            f"a sweep must not write a solved value, found {properties.get('current')}"
        assert run_count() == 0, "a sweep must not log a run"

    check("a sweep writes nothing and logs nothing",
          a_sweep_writes_nothing_and_logs_nothing)

    def a_broken_constraint_is_shaded_and_named():
        assert wait_until(lambda: count(window, ".violated-span") >= 1), \
            "the range where current exceeds its rating should be shaded"
        rail = texts(window, "#panel-body .stat-label")
        assert any("rating" in line for line in rail), \
            f"the broken constraint should be named in the run rail, got {rail}"

    check("a broken constraint is shaded and named",
          a_broken_constraint_is_shaded_and_named)

    # ---- knobs ----

    def a_knob_moves_the_curve_without_touching_the_garden():
        click_chip(window, "circuit", "r_b", shift=True)
        assert wait_until(lambda: count(window, ".knob") == 1), "the knob was not pinned"
        js(window, """
            (() => {
              knobs[0].value = 700;
              runAll();
            })();
        """)
        assert wait_until(lambda: abs(list(json.loads(
            js(window, "JSON.stringify(plots[0].data.points[200])"))["v"].values())[0]
            - 20 / 800) < 1e-9, timeout=10), "the curve did not follow the knob"
        assert get_mote(get_conn(), ids["circuit"]).properties["r_b"] == 200.0, \
            "a knob must never be written to the garden"

    check("a knob moves the curve without touching the garden",
          a_knob_moves_the_curve_without_touching_the_garden)

    # ---- a gap is a gap ----

    def a_point_that_cannot_solve_leaves_a_gap():
        """Sweeping r_a through -r_b makes r_a + r_b zero at exactly one
        point. The curve must break there rather than be drawn across a
        region where the model has no answer."""
        js(window, f"""
            (() => {{
              knobs.length = 0;
              const p = plots[0];
              p.x = {{ mote: {json.dumps(ids["circuit"])}, key: 'r_a',
                      lo: -400, hi: 0, steps: 200 }};
              scheduleRun(p);
            }})();
        """)
        assert wait_until(lambda: count(window, ".failed-span") >= 1, timeout=12), \
            "the point that could not solve should be hatched"
        assert wait_until(lambda: series_count(window) >= 2), \
            "the curve should be drawn as two runs with a hole between them"

    check("a point that cannot solve leaves a gap, not a line",
          a_point_that_cannot_solve_leaves_a_gap)

    # ---- the grid ----

    def the_grid_holds_two_independent_plots():
        js(window, "document.getElementById('add-plot-btn').click();")
        assert wait_until(lambda: count(window, ".plot-cell") == 2), \
            "＋ plot should add a second cell"
        click_chip(window, "circuit", "r_b")
        js(window, f"""
            (() => {{
              const p = plots[1];
              p.x = {{ mote: {json.dumps(ids["circuit"])}, key: 'voltage',
                      lo: 0, hi: 20, steps: 40 }};
              scheduleRun(p);
            }})();
        """)
        assert wait_until(lambda: series_count(window, plot=1) == 1, timeout=12), \
            "the second plot never drew"
        assert js(window, "plots[0].x.key") == "r_a", \
            "the second plot must not have changed the first plot's axis"
        assert js(window, "plots[1].data.points.length") == 41

    check("the grid holds two plots that do not interfere",
          the_grid_holds_two_independent_plots)

    # ---- saving ----

    def saving_a_plot_writes_a_specification_not_points():
        """Saves the *active* plot, which adding one above made the
        second -- so the axis written is that plot's, not the first's."""
        js(window, """
            window.prompt = () => 'I against R';
            document.getElementById('save-plot-btn').click();
        """)
        assert wait_until(lambda: any(
            PLOT_KEY in m.properties for m in list_motes(get_conn())
        ), timeout=10), "Save as mote wrote no plot mote"
        saved = [m for m in list_motes(get_conn()) if PLOT_KEY in m.properties][0]
        spec = saved.properties[PLOT_KEY]
        assert saved.properties["name"] == "I against R"
        assert "points" not in spec and "data" not in spec, \
            f"a saved plot holds a specification, not its numbers: {sorted(spec)}"
        assert spec["x"]["key"] == "voltage" and spec["x"]["steps"] == 40, \
            f"the active plot's axis should be saved, got {spec['x']}"
        assert spec["series"] and spec["series"][0]["key"] == "r_b", \
            f"its series should be saved, got {spec.get('series')}"

    check("saving a plot writes a specification, not points",
          saving_a_plot_writes_a_specification_not_points)

    # ---- last, so it sees everything the run did ----

    def the_page_raised_nothing():
        errors = js(window, "JSON.stringify(window.__moxieErrors)")
        assert json.loads(errors) == [], f"the page raised: {errors}"

    check("the page raised nothing", the_page_raised_nothing)


def drive():
    _page_loaded.wait(timeout=15)
    window = webview.windows[0]
    js(window, """
        window.__moxieErrors = [];
        window.addEventListener('error', (e) => window.__moxieErrors.push(String(e.message)));
        window.addEventListener('unhandledrejection',
          (e) => window.__moxieErrors.push('unhandled rejection: ' + e.reason));
    """)
    try:
        run_checks(window)
    finally:
        window.destroy()


def _occupy(port, directory):
    """Stand in for one of the other windows: hold the port and serve
    that app's frontend off it, so a Moxie that asks for the wrong port
    doesn't merely fail -- it loads the other app's page, exactly as Mora
    once did."""
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    try:
        server = socketserver.TCPServer(("127.0.0.1", port), handler)
    except OSError:
        print(f"note: port {port} already in use -- using the real occupant")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    seed()
    squatters = [_occupy(port, PROJECT_ROOT / directory)
                 for port, _name, directory in OCCUPIED]
    api = Api(GARDEN)
    window = webview.create_window(
        "Moxie smoke test", url=str(FRONTEND_DIR / "index.html"), js_api=api,
        width=1560, height=960,
    )
    api._window = window
    window.events.closed += api.shutdown
    window.events.loaded += _page_loaded.set

    threading.Thread(target=drive, daemon=True).start()
    # The same settings moxie/app.py::launch uses, choose_port() included:
    # a suite that picked its own port would not be testing the thing
    # that broke.
    webview.start(
        http_server=True,
        http_port=choose_port("Moxie", DEFAULT_HTTP_PORT),
        private_mode=False,
        storage_path=str(_TMP / "webview_data"),
    )
    for squatter in squatters:
        if squatter is not None:
            squatter.shutdown()

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFailed:")
        for name, _, err in failed:
            print(f"  - {name}: {err}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
