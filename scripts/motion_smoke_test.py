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
re-runs against it, that Compare really draws two boards, and that
Commit really writes the tick that was scrubbed to. The bridge itself is
tests/test_motion_api.py's job; what is untested anywhere else is the
wiring between the two.
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

# The ports pywebview pins the other five windows to. The whole run
# happens with all five occupied, because that is the real condition --
# Motion is opened from a running Monarch, possibly alongside all of
# them -- and because it is the condition that broke for Mora once: it
# asked for a taken port, lost, and loaded the other app's page against
# its own bridge.
OCCUPIED = (
    (42001, "Monarch", "monarch"),
    (42002, "Mora", "mora"),
    (42003, "Monastery", "monastery"),
    (42004, "Momentum", "momentum"),
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

    def the_run_is_a_preview_until_committed():
        assert text(window, "#run-state") == "preview", \
            "an uncommitted run must say so"
        assert balance_of("acct1") == 100.0, \
            "a preview must not have written the graph"

    check("a run is a preview and writes nothing", the_run_is_a_preview_until_committed)

    def committing_writes_the_scrubbed_tick():
        click(window, "#commit-btn")
        assert wait_until(lambda: balance_of("acct1") != 100.0), \
            "Commit did not write the graph"
        assert abs(balance_of("acct1") - 105.0) < 1e-9, \
            f"tick 1 of a 5% compounding rule is 105, got {balance_of('acct1')}"
        assert wait_until(lambda: count(window, "#run-list .card") == 1), \
            "the committed run never reached the saved-runs rail"

    check("Commit writes exactly the tick that was scrubbed to",
          committing_writes_the_scrubbed_tick)

    # ---- scenarios ----

    def picking_a_scenario_re_runs_against_it():
        click_text(window, "#scenario-list .card", "Windfall")
        assert wait_until(lambda: text(window, "#run-state").startswith("scenario")), \
            "picking a scenario did not change what the window says it is answering"
        click(window, "#run-btn")
        assert wait_until(lambda: count(window, "#lanes .lane") >= 1), \
            "the scenario run drew no lanes"
        assert js(window, "document.getElementById('commit-btn').disabled") is True, \
            "a scenario run is hypothetical and must not offer Commit"

    check("picking a scenario re-runs against it and refuses to commit",
          picking_a_scenario_re_runs_against_it)

    def a_scenario_run_leaves_the_graph_alone():
        assert abs(balance_of("acct1") - 105.0) < 1e-9, \
            "the scenario run must not have written the graph"

    check("a scenario run writes nothing", a_scenario_run_leaves_the_graph_alone)

    def compare_draws_two_boards():
        click_text(window, "#mode-chips button", "Compare")
        assert wait_until(lambda: count(window, ".board") == 2), \
            f"Compare should draw two boards, got {count(window, '.board')}"
        badges = texts(window, ".board-badge")
        assert "scenario A" in badges and "scenario B" in badges, \
            f"each board should say which side it is, got {badges}"
        click_text(window, "#mode-chips button", "Run")
        assert wait_until(lambda: count(window, ".board") == 1), \
            "leaving Compare left the second board behind"

    check("Compare draws both scenarios side by side", compare_draws_two_boards)

    # ---- panels ----

    def the_rules_panel_is_read_only_outside_build_mode():
        click_text(window, ".rule-row", "Growth")
        assert wait_until(lambda: count(window, ".eq-edit") == 1), "the equation editor never opened"
        assert js(window, "document.querySelector('.eq-edit').readOnly") is True, \
            "the equation editor must be read-only outside Build mode"

    check("the equation editor is read-only outside Build mode",
          the_rules_panel_is_read_only_outside_build_mode)

    def the_rules_panel_edits_the_equation():
        click_text(window, "#mode-chips button", "Build")
        click_text(window, ".rule-row", "Growth")
        assert wait_until(lambda: count(window, ".eq-edit") == 1), "the equation editor never opened"
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
        click_text(window, "#mode-chips button", "Run")

    check("the rules panel writes an edited equation in Build mode", the_rules_panel_edits_the_equation)

    def the_bindings_partition_solved_from_read():
        roles = texts(window, ".binding-role")
        assert "solves" in roles, f"the panel should say what the rule solves, got {roles}"

    check("a rule's bindings say what it solves and what it reads",
          the_bindings_partition_solved_from_read)

    def clicking_a_node_opens_the_inspector():
        press(window, node_selector("acct2"))
        assert wait_until(lambda: count(window, "#panel-tabs button.on") == 1
                          and texts(window, "#panel-tabs button.on") == ["Inspector"]), \
            "pressing a node did not switch the panel to the inspector"
        assert wait_until(lambda: "Operating" in text(window, "#panel-body")), \
            "pressing a node did not open it in the inspector"
        assert wait_until(lambda: count(window, ".rel-row") >= 1), \
            "the inspector should list what the mote is wired to"

    check("clicking a node inspects it", clicking_a_node_opens_the_inspector)

    def pre_flight_reports_on_roles():
        click(window, "#check-roles-btn")
        assert wait_until(lambda: text(window, "#roles-report") != ""), \
            "Check roles reported nothing at all"

    check("the pre-flight check reports on roles", pre_flight_reports_on_roles)

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
    """Stand in for a running MonOSS / Mora / Monastery / Momentum /
    Monolith: hold the port and serve that app's frontend off it, so a
    Motion that asks for the wrong port doesn't merely fail -- it loads
    the other app's page, exactly as Mora once did."""
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
