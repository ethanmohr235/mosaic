#!/usr/bin/env python3
"""Regression smoke test for Monolith, the table window.

Not named test_*.py, for the same reason scripts/motion_smoke_test.py
isn't: it spawns a real pywebview window against a real WebKit/GTK
display, which the pytest suite deliberately assumes nothing about. Run
it by hand after touching monolith/:

    .venv/bin/python scripts/monolith_smoke_test.py

What it is for is the half of Monolith that Python tests cannot reach --
that the discovered columns really become a table, that a condition
built by clicking the Where bar really filters, that editing one cell
inside a three-row selection really writes all three motes, that a click
in the relationship matrix really draws an edge in the garden, and that
saving a view really leaves a mote behind. Never touches real data:
GARDENS_DIR and the webview storage_path are both a fresh temp
directory.

Every check drives the window the way a person does -- clicking the
popover's own options rather than calling the bridge -- because the
bridge is what tests/test_monolith_api.py already covers. What is
untested anywhere else is the wiring between the two.
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

_TMP = Path(tempfile.mkdtemp(prefix="monolith-smoke-"))
db.GARDENS_DIR = _TMP / "gardens"

import webview  # noqa: E402  (after GARDENS_DIR is patched, matching app startup order)

from core.db import get_connection, initialize_database  # noqa: E402
from core.gardens import create_garden  # noqa: E402
from core.graph import whole_garden  # noqa: E402
from core.motes import create_mote, get_mote  # noqa: E402
from core.query import QUERY_KEY  # noqa: E402
from core.relationships import create_relationship  # noqa: E402
from core.species import INSTANCE_OF  # noqa: E402
from monolith.api import Api  # noqa: E402
from monolith.app import DEFAULT_HTTP_PORT  # noqa: E402
from window_launcher import choose_port  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "monolith" / "frontend"
GARDEN = "smoke"

# The ports pywebview pins the other three windows to. The whole run
# happens with all three occupied, because that is the real condition --
# Monolith is opened from a running MonOSS, possibly alongside all of
# them -- and because it is the condition that broke for Mora once: it
# asked for a taken port, lost, and loaded the other app's page against
# its own bridge.
OCCUPIED = (
    (42001, "Monarch", "monarch"),
    (42002, "Mora", "mora"),
    (42003, "Monastery", "monastery"),
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


def wait_until(condition, timeout=6.0, interval=0.05):
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


def texts(window, selector):
    return json.loads(js(window, f"""
        JSON.stringify(Array.from(document.querySelectorAll({json.dumps(selector)}))
            .map(n => n.textContent.trim()))
    """))


def click_text(window, selector, text):
    """Click the first node matching `selector` whose text is exactly
    `text`. Every picker in this window is the same popover of one-line
    options, so this is how the test chooses one -- by what it says,
    which is what a person clicks, rather than by an index that shifts
    as the garden's discovered values change."""
    js(window, f"""
        (() => {{
          const wanted = {json.dumps(text)};
          const node = Array.from(document.querySelectorAll({json.dumps(selector)}))
            .find(n => n.textContent.trim() === wanted);
          if (!node) throw new Error('no ' + {json.dumps(selector)} + ' reading ' + wanted);
          node.click();
        }})();
    """)


def type_and_commit(window, text):
    """Fill the popover's one text field and press its button. `input` is
    dispatched as well as the value being set, because that is what a
    search field listens for."""
    js(window, f"""
        (() => {{
          const input = document.getElementById('pop-input');
          input.value = {json.dumps(text)};
          input.dispatchEvent(new Event('input', {{bubbles: true}}));
          document.getElementById('pop-commit').click();
        }})();
    """)


def wait_for_condition_field(window, label):
    """The Where bar redraws from Python's answer, not from the click --
    every edit re-runs the query (monolith/frontend/app.js::edit). A test
    that clicked the next button straight away would be clicking the
    chip as it was before the picker."""
    assert wait_until(
        lambda: texts(window, ".cond .cond-field") and texts(window, ".cond .cond-field")[0] == label
    ), f"the condition never became `{label}`"


def row_selector(name):
    return f'.row[data-id="{ids[name]}"]'


def cell_selector(name, column):
    return f'{row_selector(name)} .cell[data-col="{column}"]'


def seed():
    """A garden with a species, six instances carrying overlapping (not
    identical) property keys, and two relationships -- so the discovered
    columns really have to be discovered rather than assumed, and the
    matrix has something in it before the test writes more."""
    create_garden(GARDEN)
    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        ids["species"] = create_mote(conn, {"name": "Task"})

        rows = [
            ("schema", "Schema translation", {"status": "doing", "estimate": 5, "owner": "Dev Anand"}),
            ("backfill", "Backfill dry run", {"status": "doing", "estimate": 3, "owner": "Dev Anand"}),
            ("latency", "Query latency budget", {"status": "todo", "estimate": 4}),
            ("rehearsal", "Cutover rehearsal", {"status": "todo", "estimate": 4, "owner": "Joss Mbeki"}),
            ("index", "Index rebuild on shard 3", {"status": "blocked", "estimate": 6}),
            ("tiering", "Cold tiering spike", {"status": "blocked"}),
        ]
        for key, name, properties in rows:
            ids[key] = create_mote(conn, {"name": name, **properties})
            create_relationship(conn, ids[key], ids["species"], INSTANCE_OF)

        # A mote of no species and no properties at all: the table has to
        # draw an empty row rather than skipping it.
        ids["stray"] = create_mote(conn, {"name": "Unfiled thought"})

        create_relationship(conn, ids["backfill"], ids["schema"], "depends_on")
        create_relationship(conn, ids["rehearsal"], ids["backfill"], "depends_on")
    finally:
        conn.close()


def run_checks(window):
    # First, and deliberately a named check rather than a bare assert:
    # everything below assumes this window is Monolith, and when that
    # stopped being true for Mora the suite died on an unattributed
    # assertion and reported "0/0 checks passed" -- true and useless.
    def monolith_loads_its_own_frontend():
        url = js(window, "location.href")
        for port, name, _dir in OCCUPIED:
            assert f":{port}/" not in url, \
                f"Monolith must not serve off {name}'s port, got {url}"
        assert js(window, "!!document.getElementById('table')") is True, \
            f"the page in this window is not Monolith's ({url})"
        assert js(window, "!document.getElementById('graph')") is True, \
            f"this window loaded the Monarch graph, not Monolith ({url})"
        assert wait_until(lambda: count(window, ".row") >= 7), \
            "Monolith's frontend loaded but never drew the rows"

    check("Monolith comes up on its own port with its own frontend",
          monolith_loads_its_own_frontend)
    if not results[-1][1]:
        return  # nothing below can mean anything if this window is not Monolith

    def no_errors():
        errors = js(window, "JSON.stringify(window.__monolithErrors)")
        assert json.loads(errors) == [], f"the page raised: {errors}"

    # ---- discovered columns ----

    def the_table_opens_on_columns_nobody_declared():
        labels = texts(window, ".hcell")
        assert labels[0].startswith("Name"), f"name should lead the table, got {labels}"
        assert any(label.startswith("status") for label in labels), \
            f"the garden's own property keys should be columns, got {labels}"

    check("the table opens on columns discovered from the garden",
          the_table_opens_on_columns_nobody_declared)

    def the_rail_lists_every_key_and_every_relationship_type():
        keys = texts(window, ".col-toggle .label")
        assert {"status", "estimate", "owner"} <= set(keys), f"property keys missing: {keys}"
        assert "→ depends_on" in keys and "← depends_on" in keys, \
            f"both directions of every relationship type should be offered, got {keys}"
        assert "Kinds" in keys, f"the graph's own columns should be offered, got {keys}"

    check("the column rail offers property keys, both link directions and the graph's columns",
          the_rail_lists_every_key_and_every_relationship_type)

    def a_mote_with_no_properties_still_draws_a_row():
        assert count(window, row_selector("stray")) == 1, \
            "a mote carrying only a name should still be a row"
        empties = count(window, f'{row_selector("stray")} .cell.empty')
        assert empties >= 1, "its unset cells should draw as empty, not as missing"

    check("a mote with almost no properties still draws a row",
          a_mote_with_no_properties_still_draws_a_row)

    def turning_a_column_on_adds_it_to_the_table():
        before = count(window, ".hcell")
        click_text(window, ".col-toggle .label", "→ depends_on")
        assert wait_until(lambda: count(window, ".hcell") == before + 1), \
            "toggling a column in the rail did not add it to the table"
        cell = js(window, f"document.querySelector({json.dumps(cell_selector('backfill', 'rel:out:depends_on'))}).textContent")
        assert "Schema translation" in cell, \
            f"a relationship column should read the neighbour's name, got {cell!r}"

    check("a relationship column reads the motes across the edge",
          turning_a_column_on_adds_it_to_the_table)

    # ---- querying ----

    def a_condition_built_by_clicking_filters_the_table():
        click(window, "#add-cond")
        assert wait_until(lambda: count(window, ".pop-option") > 0), "the property picker never opened"
        click_text(window, ".pop-option .label", "status")
        wait_for_condition_field(window, "status")
        click(window, ".cond .cond-op")
        assert wait_until(lambda: count(window, ".pop-option") > 0), "the operator picker never opened"
        click_text(window, ".pop-option .label", "is")
        click(window, ".cond .cond-value")
        assert wait_until(lambda: count(window, ".pop-option") > 0), "the value picker never opened"
        click_text(window, ".pop-option .label", "doing")
        assert wait_until(lambda: count(window, ".row") == 2), \
            f"`status is doing` should leave 2 rows, got {count(window, '.row')}"
        footer = js(window, "document.getElementById('stat-rows').textContent")
        assert footer.startswith("2 of 8"), f"the footer should count both, got {footer!r}"

    check("a condition built by clicking the Where bar filters the table",
          a_condition_built_by_clicking_filters_the_table)

    def removing_the_condition_brings_the_rows_back():
        click(window, ".cond .cond-remove")
        assert wait_until(lambda: count(window, ".row") == 8), "removing the condition did not restore the rows"

    check("removing a condition restores the rows", removing_the_condition_brings_the_rows_back)

    def grouping_cuts_the_table_into_headers():
        click_text(window, "#group-chips button", "status")
        assert wait_until(lambda: count(window, ".group-row") == 4), \
            f"three statuses and one unset should be four headers, got {count(window, '.group-row')}"
        labels = texts(window, ".group-row .label")
        assert labels[-1] == "unset", f"the mote with no status should sort last, got {labels}"
        click_text(window, "#group-chips button", "none")
        assert wait_until(lambda: count(window, ".group-row") == 0), "ungrouping left headers behind"

    check("grouping by a discovered column cuts the table into headers",
          grouping_cuts_the_table_into_headers)

    def sorting_reorders_the_rows():
        click_text(window, ".hcell", "Name")
        assert wait_until(lambda: js(window, "document.querySelector('.row').dataset.id") == ids["backfill"]), \
            "sorting by name ascending should put Backfill first"

    check("clicking a header sorts by that column", sorting_reorders_the_rows)

    # ---- editing ----

    def editing_one_cell_writes_one_mote():
        click(window, cell_selector("latency", "property:status"))
        assert wait_until(lambda: count(window, ".pop-option") > 0), "the cell editor never opened"
        click_text(window, ".pop-option .label", "blocked")
        assert wait_until(
            lambda: get_mote(get_conn(), ids["latency"]).properties["status"] == "blocked"
        ), "editing a cell did not write the property"

    check("editing a cell writes that mote's property", editing_one_cell_writes_one_mote)

    def editing_inside_a_selection_writes_every_selected_mote():
        for name in ("schema", "backfill", "rehearsal"):
            click(window, f"{row_selector(name)} .check")
        assert wait_until(lambda: js(window, "document.getElementById('stat-sel').textContent") == "3 selected"), \
            "three checkboxes did not make a three-mote selection"
        click(window, cell_selector("schema", "property:owner"))
        assert wait_until(lambda: js(window, "!document.getElementById('pop').classList.contains('hidden')")), \
            "the cell editor never opened"
        type_and_commit(window, "Rina Kovac")

        def all_three():
            conn = get_conn()
            return all(get_mote(conn, ids[n]).properties.get("owner") == "Rina Kovac"
                       for n in ("schema", "backfill", "rehearsal"))
        assert wait_until(all_three), \
            "editing one cell inside a selection must write every selected mote"
        assert get_mote(get_conn(), ids["latency"]).properties.get("owner") is None, \
            "and must write nothing outside it"

    check("editing a cell inside a selection writes every selected mote",
          editing_inside_a_selection_writes_every_selected_mote)

    def the_bulk_bar_clears_a_property():
        click_text(window, "#bulkbar .bulk-btn", "Clear property")
        assert wait_until(lambda: count(window, ".pop-option") > 0), "the property picker never opened"
        click_text(window, ".pop-option .label", "owner")

        def none_of_three():
            conn = get_conn()
            return all("owner" not in get_mote(conn, ids[n]).properties
                       for n in ("schema", "backfill", "rehearsal"))
        assert wait_until(none_of_three), "clearing did not remove the key from every selected mote"

    check("the bulk bar clears a property across the selection", the_bulk_bar_clears_a_property)

    def bulk_linking_chains_the_selection():
        before = _relationships_of_type("blocks")
        assert before == 0
        click_text(window, "#bulkbar .bulk-btn", "Link…")
        assert wait_until(lambda: count(window, ".pop-option") >= 0)
        type_and_commit(window, "blocks")
        assert wait_until(lambda: count(window, ".pop-option") > 0), "the link-shape picker never opened"
        click_text(window, ".pop-option .label", "chain in row order")
        assert wait_until(lambda: _relationships_of_type("blocks") == 2), \
            f"chaining three selected rows should write two edges, got {_relationships_of_type('blocks')}"

    check("the bulk bar chains a selection into relationships", bulk_linking_chains_the_selection)

    def deselecting_hides_the_bulk_bar():
        click(window, "#bulk-deselect")
        assert wait_until(lambda: js(window, "document.getElementById('bulkbar').classList.contains('hidden')")), \
            "the bulk bar outlived the selection"

    check("deselecting puts the bulk bar away", deselecting_hides_the_bulk_bar)

    # ---- the matrix ----

    def the_matrix_writes_an_edge():
        click(window, "#matrix-toggle")
        assert wait_until(lambda: count(window, ".matrix-row") >= 7), "the matrix never drew its rows"
        before = _relationships_of_type("depends_on")
        order = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('.matrix-label .name')).map(n => n.textContent))
        """))
        i = order.index("Cold tiering spike")
        j = order.index("Schema translation")
        click(window, f'.matrix-cell[data-from="{i}"][data-to="{j}"]')
        assert wait_until(lambda: _relationships_of_type("depends_on") == before + 1), \
            "clicking an empty matrix cell did not write a relationship"

    check("a click in the relationship matrix writes an edge", the_matrix_writes_an_edge)

    def the_matrix_erases_the_same_edge():
        before = _relationships_of_type("depends_on")
        order = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('.matrix-label .name')).map(n => n.textContent))
        """))
        i = order.index("Cold tiering spike")
        j = order.index("Schema translation")
        cell = f'.matrix-cell[data-from="{i}"][data-to="{j}"]'
        # The edge written by the previous check reaches the grid on the
        # refetch that follows it, not on the click: clicking before the
        # cell is drawn linked would ask for the write again, not the
        # erase.
        assert wait_until(lambda: count(window, cell + ".linked") == 1), \
            "the matrix never drew the edge the previous check wrote"
        click(window, cell)
        assert wait_until(lambda: _relationships_of_type("depends_on") == before - 1), \
            "clicking a linked matrix cell did not erase the relationship"
        click(window, "#matrix-close")

    check("clicking the same cell again erases it", the_matrix_erases_the_same_edge)

    # ---- saved views ----

    def saving_a_view_leaves_a_mote_behind():
        click(window, "#add-cond")
        click_text(window, ".pop-option .label", "status")
        wait_for_condition_field(window, "status")
        click(window, ".cond .cond-value")
        click_text(window, ".pop-option .label", "blocked")
        click(window, "#save-view")
        assert wait_until(lambda: js(window, "!document.getElementById('pop').classList.contains('hidden')")), \
            "the name field never opened"
        type_and_commit(window, "At risk")
        assert wait_until(lambda: len(_view_motes()) == 1), "saving a view wrote no mote"
        view = _view_motes()[0]
        assert view.properties["name"] == "At risk"
        assert view.properties[QUERY_KEY]["conditions"], "the saved mote carries no query"
        assert wait_until(lambda: count(window, ".view") == 1), "the saved view never reached the rail"
        summary = texts(window, ".view-summary")
        assert summary == ["status is blocked"], f"the rail should describe the query, got {summary}"

    check("saving a view leaves an ordinary mote carrying the query",
          saving_a_view_leaves_a_mote_behind)

    def loading_a_view_restores_its_query():
        click(window, ".cond .cond-remove")
        assert wait_until(lambda: count(window, ".cond") == 0)
        click(window, ".view")
        assert wait_until(lambda: count(window, ".cond") == 1), "loading a view restored no condition"
        assert wait_until(lambda: js(window, "document.getElementById('view-title').textContent") == "At risk"), \
            "the topbar did not name the loaded view"

    check("loading a saved view restores its query", loading_a_view_restores_its_query)

    def the_view_mote_is_not_a_kind():
        """Carrying a query is data, like a url or a date -- the same
        line Mora draws for `modoc`."""
        click(window, "#clear-view")
        assert wait_until(lambda: count(window, ".row") == 9), \
            "the view mote should be a row of the garden like any other"

    check("the view mote is an ordinary row of the garden", the_view_mote_is_not_a_kind)

    check("the page raised no JavaScript errors", no_errors)


def _relationships_of_type(type_):
    _motes, relationships = whole_garden(get_conn())
    return len([r for r in relationships if r.type == type_])


def _view_motes():
    motes, _relationships = whole_garden(get_conn())
    return [m for m in motes if QUERY_KEY in m.properties]


def drive():
    _page_loaded.wait(timeout=15)
    window = webview.windows[0]
    js(window, """
        window.__monolithErrors = [];
        window.addEventListener('error', (e) => window.__monolithErrors.push(String(e.message)));
    """)
    try:
        run_checks(window)
    finally:
        window.destroy()


def _occupy(port, directory):
    """Stand in for a running MonOSS / Mora / Monastery: hold
    the port and serve that app's frontend off it, so a Monolith that
    asks for the wrong port doesn't merely fail -- it loads the other
    app's page, exactly as Mora once did."""
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
        "Monolith smoke test", url=str(FRONTEND_DIR / "index.html"), js_api=api,
        width=1560, height=940,
    )
    api._window = window
    window.events.closed += api.shutdown
    window.events.loaded += _page_loaded.set

    threading.Thread(target=drive, daemon=True).start()
    # The same settings monolith/app.py::launch uses, choose_port()
    # included: a suite that picked its own port would not be testing the
    # thing that broke.
    webview.start(
        http_server=True,
        http_port=choose_port("Monolith", DEFAULT_HTTP_PORT),
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
