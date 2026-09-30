#!/usr/bin/env python3
"""Regression smoke test for Motion, the simulation window.

Not named test_*.py, for the same reason scripts/monolith_smoke_test.py
isn't: it spawns a real pywebview window against a real WebKit/GTK
display, which the pytest suite deliberately assumes nothing about. Run
it by hand after touching motion/:

    .venv/bin/python scripts/motion_smoke_test.py

Run it alone. The pywebview window suites interfere with each other, and
two at once produce failures that are about the display and not about
the code.

What it is for is the half of Motion that Python tests cannot reach --
that pressing Run really draws lanes, that dragging the playhead really
moves the numbers on the node cards, that picking a scenario really
re-runs against it, that a comparison pane really draws a second board,
and that Solve really reaches the engine. The bridge
itself is tests/test_motion_api.py's job; what is untested anywhere else
is the wiring between the two.
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

_TMP = Path(tempfile.mkdtemp(prefix="motion-smoke-"))
db.GARDENS_DIR = _TMP / "gardens"

import webview  # noqa: E402  (after GARDENS_DIR is patched, matching app startup order)

from core.db import get_connection, initialize_database  # noqa: E402
from core.gardens import create_garden  # noqa: E402
from core.motes import create_mote, get_mote  # noqa: E402
from core.relationships import create_relationship  # noqa: E402
from core.species import INSTANCE_OF  # noqa: E402
from motion.api import Api  # noqa: E402
from motion.app import DEFAULT_HTTP_PORT  # noqa: E402
from window_launcher import choose_port  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "motion" / "frontend"
GARDEN = "smoke"

# The ports pywebview pins the other four windows to. The whole run
# happens with all four occupied, because that is the real condition --
# Motion is opened from a running Monarch, possibly alongside all of
# them -- and because it is the condition that broke for Mora once: it
# asked for a taken port, lost, and loaded the other app's page against
# its own bridge.
OCCUPIED = (
    (42001, "Monarch", "monarch"),
    (42002, "Mora", "mora"),
    (42003, "Monastery", "monastery"),
    (42005, "Monolith", "monolith"),
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


def wait_until(condition, timeout=8.0, interval=0.05):
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


def click(window, selector):
    js(window, f"document.querySelector({json.dumps(selector)}).click();")


def text(window, selector):
    return js(window, f"""
        (document.querySelector({json.dumps(selector)}) || {{}}).textContent || ''
    """).strip()


def texts(window, selector):
    return json.loads(js(window, f"""
        JSON.stringify(Array.from(document.querySelectorAll({json.dumps(selector)}))
            .map(n => n.textContent.trim()))
    """))


# The inspector's name and property keys are <input>s -- the name is
# edited in place and a key is renamed by clicking it -- so reading them
# is .value, not .textContent.

def value(window, selector):
    return js(window, f"""
        (document.querySelector({json.dumps(selector)}) || {{}}).value || ''
    """).strip()


def texts_values(window, selector):
    return json.loads(js(window, f"""
        JSON.stringify(Array.from(document.querySelectorAll({json.dumps(selector)}))
            .map(n => (n.value || '').trim()))
    """))


def computed_style(window, selector, prop):
    """A CSS property's live, resolved value -- for a background-size
    set by app.js, not the stylesheet, `.style.x` would miss anything
    set some other way, so this reads getComputedStyle instead."""
    return js(window, f"""
        (() => {{
          const el = document.querySelector({json.dumps(selector)});
          return el ? getComputedStyle(el)[{json.dumps(prop)}] : null;
        }})()
    """)


def click_text(window, selector, wanted):
    """Click the first node matching `selector` whose text contains
    `wanted` -- by what it says, which is what a person clicks, rather
    than by an index that shifts as the garden changes."""
    js(window, f"""
        (() => {{
          const wanted = {json.dumps(wanted)};
          const node = Array.from(document.querySelectorAll({json.dumps(selector)}))
            .find(n => n.textContent.includes(wanted));
          if (!node) throw new Error('no ' + {json.dumps(selector)} + ' reading ' + wanted);
          node.click();
        }})();
    """)


def press(window, selector):
    """Press a node card. The board binds mousedown rather than click --
    the same gesture starts a drag -- so `.click()` would sail past it."""
    code = """
        (() => {
          const node = document.querySelector(SEL);
          if (!node) throw new Error('no ' + SEL);
          node.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}));
          window.dispatchEvent(new MouseEvent('mouseup', {bubbles: true}));
        })();
    """.replace("SEL", json.dumps(selector))
    js(window, code)


def node_selector(name):
    return f'.board .node[data-id="{ids[name]}"]'


def seed():
    """A garden that actually simulates: a species declaring a freedom, a
    rule that compounds it, two instances, and a scenario that varies one
    of them -- so every panel in the window has something real to draw
    rather than an empty state."""
    create_garden(GARDEN)
    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        ids["species"] = create_mote(conn, {"name": "Account", "freedoms": "balance"})
        ids["rule"] = create_mote(
            conn, {"name": "Growth", "equation": "balance == prev(balance) * 1.05"}
        )
        create_relationship(conn, ids["rule"], ids["species"], "applies_to")

        ids["acct1"] = create_mote(conn, {"name": "Reserve", "balance": 100.0})
        ids["acct2"] = create_mote(conn, {"name": "Operating", "balance": 40.0})
        for key in ("acct1", "acct2"):
            create_relationship(conn, ids[key], ids["species"], INSTANCE_OF)

        # A scenario: one override, gathered by one scenario mote. Both
        # are ordinary motes -- core/scenarios.py declares no new tables.
        ids["override"] = create_mote(conn, {"balance": 1000.0})
        create_relationship(conn, ids["override"], ids["acct1"], "overrides")
        ids["scenario"] = create_mote(conn, {"name": "Windfall"})
        create_relationship(conn, ids["scenario"], ids["override"], "varies")
    finally:
        conn.close()


def balance_of(key):
    return get_mote(get_conn(), ids[key]).properties["balance"]


def run_checks(window):
    # First, and deliberately a named check rather than a bare assert:
    # everything below assumes this window is Motion, and when that
    # stopped being true for Mora the suite died on an unattributed
    # assertion and reported "0/0 checks passed" -- true and useless.
    def motion_loads_its_own_frontend():
        url = js(window, "location.href")
        for port, name, _dir in OCCUPIED:
            assert f":{port}/" not in url, \
                f"Motion must not serve off {name}'s port, got {url}"
        assert js(window, "!!document.getElementById('dock')") is True, \
            f"the page in this window is not Motion's ({url})"
        assert js(window, "!document.getElementById('graph')") is True, \
            f"this window loaded the Monarch graph, not Motion ({url})"
        assert wait_until(lambda: count(window, ".board .node") >= 4), \
            "Motion's frontend loaded but never drew the board"

    check("Motion comes up on its own port with its own frontend",
          motion_loads_its_own_frontend)
    if not results[-1][1]:
        return  # nothing below can mean anything if this window is not Motion

    def motion_opens_in_fast_mode():
        assert js(window, "document.querySelector('#mode-chips button.on').textContent") == "Fast", \
            "Motion should open directly into Fast mode"
        assert js(window, "document.getElementById('rail').classList.contains('hidden')") is True, \
            "Fast mode should hide the rail"

    check("Motion opens in Fast mode by default", motion_opens_in_fast_mode)

    # Everything below exercises the manual run/scenario/comparison
    # machinery, which only Advanced mode shows.
    click_text(window, "#mode-chips button", "Advanced")

    def no_errors():
        errors = js(window, "JSON.stringify(window.__motionErrors)")
        assert json.loads(errors) == [], f"the page raised: {errors}"

    # ---- the board ----

    def the_board_draws_a_node_per_simulable_mote():
        labels = texts(window, ".board .node-label")
        assert {"Account", "Growth", "Reserve", "Operating"} <= set(labels), \
            f"every simulable mote should be a node, got {labels}"

    check("the board draws a node per simulable mote",
          the_board_draws_a_node_per_simulable_mote)

    def override_motes_stay_off_the_board():
        """core/simulation.py excludes them from a solve, so drawing them
        would put a mote on the canvas no run will ever move."""
        assert count(window, node_selector("override")) == 0, \
            "an override mote must not be drawn as a node"
        rail = texts(window, "#scenario-list .card-label")
        assert any("Windfall" in label for label in rail), \
            f"the scenario should be a rail entry, got {rail}"

    check("an override mote is a scenario chip, not a node",
          override_motes_stay_off_the_board)

    def a_rule_node_shows_its_equation_and_a_free_node_its_kind():
        equations = texts(window, ".board .node.rule .node-eq")
        assert any("prev(balance)" in e for e in equations), \
            f"the rule node should carry its equation, got {equations}"
        assert count(window, ".board .node.free") >= 1, \
            "the species declaring `freedoms` should draw as a free node"

    check("node kinds come from the garden, not from a declared type",
          a_rule_node_shows_its_equation_and_a_free_node_its_kind)

    # ---- running ----

    def pressing_run_draws_lanes():
        click(window, "#run-btn")
        assert wait_until(lambda: count(window, "#lanes .lane") == 2), \
            f"two balances move, so two lanes, got {count(window, '#lanes .lane')}"
        labels = texts(window, "#lanes .lane-name")
        assert any("Reserve" in label and "balance" in label for label in labels), \
            f"a lane should name the value it traces, got {labels}"
        assert text(window, "#tick-label").startswith("tick 0"), \
            "a fresh run should open on tick 0, the garden before it ran"

    check("pressing Run traces every value that moved", pressing_run_draws_lanes)

    def the_axis_is_ticks_and_says_so():
        assert text(window, "#tick-axis") == "ticks", \
            "the dock must name its own axis rather than implying seconds"

    check("the timeline is counted in ticks, and labelled so", the_axis_is_ticks_and_says_so)

    def scrubbing_moves_the_numbers_on_the_cards():
        before = text(window, f'{node_selector("acct1")} .node-num')
        click(window, "#step-fwd")
        assert wait_until(lambda: text(window, "#tick-label").startswith("tick 1")), \
            "stepping forward did not move the tick"
        after = text(window, f'{node_selector("acct1")} .node-num')
        assert after != before, \
            f"the node card should show the tick's value, still {before!r}"
        assert float(after) > float(before), \
            f"a compounding balance should rise, went {before} -> {after}"

    check("stepping the dock moves the values on the node cards",
          scrubbing_moves_the_numbers_on_the_cards)

    def the_run_is_a_preview_until_solved():
        assert text(window, "#run-state") == "preview", \
            "a preview must say so"
        assert balance_of("acct1") == 100.0, \
            "a preview must not have written the graph"

    check("a run is a preview and writes nothing", the_run_is_a_preview_until_solved)

    def solving_a_recurrence_refuses_and_writes_nothing():
        # This garden is a recurrence -- prev(balance) -- and a static
        # solve has no previous step, so the engine refuses. What this
        # proves is the wiring: the button reaches api.solve and the
        # refusal comes back as a toast rather than a silent no-op.
        click(window, "#solve-btn")
        assert wait_until(lambda: "Solve failed" in text(window, "#toast")), \
            f"a refused solve should say so, toast was {text(window, '#toast')!r}"
        assert "previous value" in text(window, "#toast")
        assert balance_of("acct1") == 100.0, \
            "a refused solve must not have written the graph"

    check("Solve on a recurrence refuses and writes nothing",
          solving_a_recurrence_refuses_and_writes_nothing)

    # ---- scenarios ----

    def picking_a_scenario_re_runs_against_it():
        click_text(window, "#scenario-list .card", "Windfall")
        assert wait_until(lambda: text(window, "#run-state").startswith("scenario")), \
            "picking a scenario did not change what the window says it is answering"
        click(window, "#run-btn")
        assert wait_until(lambda: count(window, "#lanes .lane") >= 1), \
            "the scenario run drew no lanes"
        assert js(window, "document.getElementById('solve-btn').disabled") is True, \
            "a scenario is hypothetical and must not offer Solve"

    check("picking a scenario re-runs against it and refuses to solve",
          picking_a_scenario_re_runs_against_it)

    def a_scenario_run_leaves_the_graph_alone():
        assert balance_of("acct1") == 100.0, \
            "the scenario run must not have written the graph"

    check("a scenario run writes nothing", a_scenario_run_leaves_the_graph_alone)

    def comparison_pane_draws_a_second_board():
        # Right-clicking a scenario card adds it as a display-only
        # comparison pane alongside the primary board (see
        # scenarioCard/renderBoards in motion/frontend/app.js) -- this
        # replaces the old fixed-A/B Compare mode with an N-pane model.
        js(window, """
            Array.from(document.querySelectorAll('#scenario-list .card'))
              .find(n => n.textContent.includes('Windfall'))
              .dispatchEvent(new MouseEvent('contextmenu', {bubbles: true}));
        """)
        assert wait_until(lambda: count(window, ".board") == 2), \
            f"a comparison pane should draw a second board, got {count(window, '.board')}"
        badges = texts(window, ".board-badge")
        assert any("pane" in b or "run" in b for b in badges), \
            f"a comparison board should badge itself as a pane, got {badges}"
        click(window, ".board-close")
        assert wait_until(lambda: count(window, ".board") == 1), \
            "closing the comparison pane left the second board behind"

    check("a comparison pane draws and removes a second board",
          comparison_pane_draws_a_second_board)

    # ---- panels ----

    def the_rules_panel_edits_the_equation():
        click_text(window, ".rule-row", "Growth")
        assert wait_until(lambda: count(window, ".eq-edit") == 1), "the equation editor never opened"
        assert js(window, "document.querySelector('.eq-edit').readOnly") is False, \
            "Advanced mode should always allow editing the equation"
        js(window, """
            (() => {
              const box = document.querySelector('.eq-edit');
              box.value = 'balance == prev(balance) * 1.10';
              box.dispatchEvent(new Event('blur'));
            })();
        """)
        assert wait_until(
            lambda: "1.10" in get_mote(get_conn(), ids["rule"]).properties["equation"]
        ), "editing the equation did not write the rule mote"

    check("the rules panel writes an edited equation in Advanced mode", the_rules_panel_edits_the_equation)

    def the_bindings_partition_solved_from_read():
        roles = texts(window, ".binding-role")
        assert "solves" in roles, f"the panel should say what the rule solves, got {roles}"

    check("a rule's bindings say what it solves and what it reads",
          the_bindings_partition_solved_from_read)

    def clicking_a_node_opens_the_inspector():
        """The inspector is the shared floating panel now
        (frontend_shared/inspector.js), not a tab of Motion's docked one
        -- so this asks the panel, and asks for the things every window's
        inspector has: the mote's name, its properties, its
        relationships."""
        press(window, node_selector("acct2"))
        assert wait_until(lambda: count(window, ".ins:not(.hidden)") == 1), \
            "pressing a node did not open the inspector"
        assert wait_until(lambda: "Operating" in value(window, ".ins-name")), \
            "the inspector should be showing the mote that was pressed"
        assert wait_until(lambda: count(window, ".ins-body .ins-row") >= 1), \
            "the inspector should list the mote's properties"

        # A species *is* a relationship, so it is listed like any other --
        # this mote is wired to nothing else, and that one row is it.
        assert wait_until(lambda: text(window, ".ins-species") != ""), \
            "the inspector should name the mote's species"
        assert wait_until(lambda: "instance_of" in texts(window, ".ins-body .ins-rel-type")), \
            "instance_of should be listed like any other relationship"

    check("clicking a node inspects it", clicking_a_node_opens_the_inspector)

    def the_inspector_can_add_a_property():
        """The gap this whole change started from: Motion could overwrite
        the values a mote already had and had no way to add one."""
        js(window, """
            document.querySelector('.ins-add-key').value = 'smoke_added';
            document.querySelector('.ins-add .ins-input:not(.ins-add-key)').value = '42';
        """)
        click(window, ".ins-add .ins-btn")
        assert wait_until(lambda: "smoke_added" in texts_values(window, ".ins-body .ins-key")), \
            "adding a property should put a row for it in the inspector"

        stored = js(window, """
            (function() {
                const rows = document.querySelectorAll('.ins-body .ins-row');
                for (const r of rows) {
                    if (r.querySelector('.ins-key').value === 'smoke_added')
                        return r.querySelector('.ins-val input').value;
                }
                return null;
            })()
        """)
        assert stored == "42", f"the added property should hold what was typed, got {stored!r}"

    check("the inspector can add a property to a mote", the_inspector_can_add_a_property)

    def pre_flight_reports_on_roles():
        click(window, "#check-roles-btn")
        assert wait_until(lambda: text(window, "#roles-report") != ""), \
            "Check roles reported nothing at all"

    check("the pre-flight check reports on roles", pre_flight_reports_on_roles)

    def snap_to_grid_toggles_and_resizes():
        # Toggling paints the grid as a background on the *viewport*
        # (.board-surface), not the node-sized .board-world -- see
        # updateGridOverlay in app.js -- so it should already be sized
        # off default step constants before any resize.
        click(window, "#tool-snap")
        assert wait_until(lambda: count(window, ".board-surface.snap-grid") >= 1), \
            "toggling snap-to-grid did not paint the overlay"
        size = computed_style(window, ".board-surface.snap-grid", "backgroundSize")
        assert size and size != "auto", f"the grid overlay should have an explicit size, got {size!r}"

        # Right-click opens the size popover; changing a field resizes
        # the overlay live, and Reset restores the node-spacing default.
        js(window, "document.getElementById('tool-snap')"
                   ".dispatchEvent(new MouseEvent('contextmenu', {bubbles: true}));")
        assert wait_until(lambda: not js(window, "document.getElementById('grid-settings').classList.contains('hidden')")), \
            "right-clicking snap-to-grid should open its size settings"
        js(window, """
            const x = document.getElementById('grid-step-x');
            x.value = 300;
            x.dispatchEvent(new Event('input'));
        """)
        assert wait_until(lambda: "300px" in (computed_style(window, ".board-surface.snap-grid", "backgroundSize") or "")), \
            "changing the horizontal step should resize the grid live"
        click(window, "#grid-step-x-reset")
        assert wait_until(lambda: "300px" not in (computed_style(window, ".board-surface.snap-grid", "backgroundSize") or "")), \
            "the horizontal reset icon should undo a custom grid size"

    check("snap-to-grid toggles, resizes live, and resets",
          snap_to_grid_toggles_and_resizes)

    def snap_all_moves_only_unaligned_motes():
        # Nudge one mote off the grid by a few px; leave the rest wherever
        # layoutNodes seeded them (already grid-aligned, since the default
        # step comes from the same NODE_W/H + gap constants).
        js(window, f"""
            positions[{json.dumps(ids["acct1"])}] = {{ x: 17, y: 23 }};
            renderBoards();
        """)
        others_before = js(window, f"""
            JSON.stringify(positions[{json.dumps(ids["acct2"])}])
        """)
        click(window, "#grid-snap-all-btn")
        moved = js(window, f"""
            (() => {{
              const p = positions[{json.dumps(ids["acct1"])}];
              return p.x % gridStepX === 0 && p.y % gridStepY === 0;
            }})()
        """)
        assert moved is True, "Snap all should land an off-grid mote on a grid intersection"
        others_after = js(window, f"""
            JSON.stringify(positions[{json.dumps(ids["acct2"])}])
        """)
        assert others_before == others_after, \
            "Snap all should leave a mote already on the grid untouched"

    check("Snap all moves only the motes not already on the grid",
          snap_all_moves_only_unaligned_motes)

    def snap_all_avoids_overlap():
        # Both nudges round to the same cell -- (gridStepX, gridStepY) --
        # under plain rounding, so Snap all has to walk one of them out
        # to a neighboring free cell rather than stacking both on it.
        gx, gy = json.loads(js(window, "JSON.stringify([gridStepX, gridStepY])"))
        js(window, f"""
            positions[{json.dumps(ids["acct1"])}] = {{ x: {gx - 12}, y: {gy - 10} }};
            positions[{json.dumps(ids["acct2"])}] = {{ x: {gx + 8}, y: {gy + 10} }};
            renderBoards();
        """)
        click(window, "#grid-snap-all-btn")
        p1, p2 = json.loads(js(window, f"""
            JSON.stringify([positions[{json.dumps(ids["acct1"])}], positions[{json.dumps(ids["acct2"])}]])
        """))
        assert (p1["x"], p1["y"]) != (p2["x"], p2["y"]), \
            f"Snap all let two motes land on the same grid cell: {p1} and {p2}"
        for p in (p1, p2):
            assert p["x"] % gx == 0 and p["y"] % gy == 0, \
                f"a displaced mote should still land on a grid intersection, got {p}"

    check("Snap all keeps two motes headed for the same cell apart",
          snap_all_avoids_overlap)

    def autoset_matches_the_largest_box():
        # autosetGridStep() floors its search at NODE_W/NODE_H (a card
        # can render narrower/shorter than that default only in theory,
        # never in practice, but the floor is real -- match it here too).
        expected = js(window, """
            (() => {
              let w = NODE_W, h = NODE_H;
              for (const el of document.querySelectorAll('#boards .board:first-child .node')) {
                w = Math.max(w, el.offsetWidth);
                h = Math.max(h, el.offsetHeight);
              }
              return JSON.stringify([w, h]);
            })()
        """)
        expected_w, expected_h = json.loads(expected)
        gap_x, gap_y = json.loads(js(window, "JSON.stringify([COL_GAP, ROW_GAP])"))
        click(window, "#grid-autoset-btn")
        step_x, step_y = json.loads(js(window, "JSON.stringify([gridStepX, gridStepY])"))
        assert step_x == expected_w + gap_x and step_y == expected_h + gap_y, \
            f"Autoset should be the largest box plus the layout gap, got {step_x}x{step_y}, " \
            f"expected {expected_w + gap_x}x{expected_h + gap_y} (box {expected_w}x{expected_h}, gap {gap_x}x{gap_y})"
        click(window, "#tool-snap")  # leave snap off for the checks that follow

    check("Autoset sizes the grid to the largest mote box", autoset_matches_the_largest_box)

    def empty_garden_leaves_the_tools_clickable():
        """A garden with nothing in it yet draws the stage's empty state,
        and that placeholder once stretched across the whole stage --
        toolbar included -- so a new garden opened with every tool dead
        to the click. Hit-test the tools rather than trust the layout:
        .click() fires on a button no person could reach."""
        # This is about the empty-state placeholder, not the inspector --
        # which the checks above leave open, floating over the stage.
        js(window, "Inspector.close();")
        js(window, "window.__savedNodes = board.nodes; board.nodes = []; renderBoards();")
        try:
            assert count(window, ".stage-empty") == 1, "empty state did not draw"
            covered = json.loads(js(window, """
                JSON.stringify(['tool-mote', 'tool-rule', 'tool-link', 'tool-layout']
                  .filter(id => {
                    const r = document.getElementById(id).getBoundingClientRect();
                    const hit = document.elementFromPoint(r.left + r.width / 2,
                                                          r.top + r.height / 2);
                    return !hit || !hit.closest('#' + id);
                  }))
            """))
            assert not covered, f"tools covered on an empty garden: {covered}"
        finally:
            js(window, "board.nodes = window.__savedNodes; renderBoards();")

    check("an empty garden leaves the stage tools clickable",
          empty_garden_leaves_the_tools_clickable)

    check("the page raised no JavaScript errors", no_errors)


def drive():
    _page_loaded.wait(timeout=15)
    window = webview.windows[0]
    js(window, """
        window.__motionErrors = [];
        window.addEventListener('error', (e) => window.__motionErrors.push(String(e.message)));
    """)
    try:
        run_checks(window)
    finally:
        window.destroy()


def _occupy(port, directory):
    """Stand in for a running MonOSS / Mora / Monastery / Monolith: hold
    the port and serve that app's frontend off it, so a Motion that asks
    for the wrong port doesn't merely fail -- it loads the other app's
    page, exactly as Mora once did."""
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
    squatters = [_occupy(port, PROJECT_ROOT / directory / "frontend")
                 for port, _name, directory in OCCUPIED]
    api = Api(GARDEN)
    window = webview.create_window(
        "Motion smoke test", url=str(FRONTEND_DIR / "index.html"), js_api=api,
        width=1560, height=960,
    )
    api._window = window
    window.events.closed += api.shutdown
    window.events.loaded += _page_loaded.set

    threading.Thread(target=drive, daemon=True).start()
    # The same settings motion/app.py::launch uses, choose_port()
    # included: a suite that picked its own port would not be testing the
    # thing that broke.
    webview.start(
        http_server=True,
        http_port=choose_port("Motion", DEFAULT_HTTP_PORT),
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
