#!/usr/bin/env python3
"""Regression smoke test for Monastery, the calendar window.

Not named test_*.py, for the same reason scripts/mora_smoke_test.py
isn't: it spawns a real pywebview window against a real WebKit/GTK
display, which the pytest suite deliberately assumes nothing about. Run
it by hand after touching monastery/:

    .venv/bin/python scripts/monastery_smoke_test.py

What it is for is the half of Monastery that Python tests cannot reach --
that a mote really lands in the right day column, that an overlapping
pair really sits side by side, that a binding toggle really empties the
grid, that clicking an event really opens the panel, and that the draft
popover really writes a mote. Never touches real data: GARDENS_DIR and
the webview storage_path are both a fresh temp directory.

Seeded relative to *today* rather than to a fixed date: the week view
draws whatever week it is opened on, so a fixed seed would test an empty
grid on any day but one.
"""
import functools
import http.server
import json
import socketserver
import sys
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.db as db

_TMP = Path(tempfile.mkdtemp(prefix="monastery-smoke-"))
db.GARDENS_DIR = _TMP / "gardens"

import webview  # noqa: E402  (after GARDENS_DIR is patched, matching app startup order)

from core.db import get_connection, initialize_database  # noqa: E402
from core.gardens import create_garden  # noqa: E402
from core.motes import create_mote, get_mote, list_motes  # noqa: E402
from core.relationships import create_relationship  # noqa: E402
from monastery.api import Api  # noqa: E402
from monastery.app import choose_port  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "monastery" / "frontend"
GARDEN = "smoke"

# The ports pywebview pins the graph window and Mora to. The whole run
# happens with both occupied, because that is the real condition --
# Monastery is opened from a running MonOSS, possibly alongside Mora --
# and because it is the condition that broke for Mora once: it asked for
# a taken port, lost, and loaded the other app's page against its own
# bridge.
GRAPH_WINDOW_PORT = 42001
MORA_PORT = 42002

MONDAY = date.today() - timedelta(days=date.today().weekday())


def day(offset):
    return MONDAY + timedelta(days=offset)


def at(offset, hour, minute=0):
    d = day(offset)
    return f"{d.isoformat()}T{hour:02d}:{minute:02d}:00"


results = []
_page_loaded = threading.Event()
ids = {}


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
        if condition():
            return True
        time.sleep(interval)
    return False


def text_of(window, selector):
    return js(window, f"(document.querySelector({json.dumps(selector)}) || {{}}).textContent || ''")


def click(window, selector):
    js(window, f"document.querySelector({json.dumps(selector)}).click();")


def count(window, selector):
    return js(window, f"document.querySelectorAll({json.dumps(selector)}).length")


def seed():
    """A week's worth of motes, dated three different ways -- which is
    the case Monastery exists for. A one-key reader would show only the
    `date` ones."""
    create_garden(GARDEN)
    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        meeting = create_mote(conn, {"name": "Meeting"})
        task = create_mote(conn, {"name": "Task"})
        ids["meeting_species"] = meeting
        ids["task_species"] = task

        # Two that overlap on Monday morning, so lane packing has
        # something to pack.
        standup = create_mote(conn, {"name": "Standup", "when": at(0, 9, 15), "duration": 30})
        create_relationship(conn, standup, meeting, "instance_of")
        ids["standup"] = standup

        overlap = create_mote(conn, {"name": "Vendor call", "when": at(0, 9, 30), "duration": 45})
        create_relationship(conn, overlap, meeting, "instance_of")

        schema = create_mote(conn, {"name": "Schema translation", "starts": at(0, 10, 0),
                                    "ends": at(0, 12, 30), "status": "in progress"})
        create_relationship(conn, schema, task, "instance_of")
        ids["schema"] = schema

        # An all-day span across three days, and a bare due date.
        create_mote(conn, {"name": "Read-only window",
                           "starts": day(2).isoformat(), "ends": day(4).isoformat()})
        create_mote(conn, {"name": "Runbook rewrite", "due": day(4).isoformat()})

        # Recurring, so expansion is visible on the grid.
        create_mote(conn, {"name": "Daily sync", "date": at(0, 8, 0),
                           "duration": 15, "recurrence": "FREQ=DAILY;COUNT=5"})

        # Never placed: no date at all, and a template that has one.
        create_mote(conn, {"name": "Rina Kovac", "role": "Staff engineer"})
        template = create_mote(conn, {"name": "Meeting template", "when": at(1, 14, 0)})
        create_relationship(conn, template, meeting, "template_of")
    finally:
        conn.close()


def run_checks(window):
    # First, and deliberately a named check rather than a bare assert:
    # everything below assumes this window is Monastery, and when that
    # stopped being true for Mora the suite died on an unattributed
    # assertion and reported "0/0 checks passed" -- true and useless.
    def monastery_loads_its_own_frontend():
        url = js(window, "location.href")
        assert f":{GRAPH_WINDOW_PORT}/" not in url, \
            f"Monastery must not serve off the graph window's port, got {url}"
        assert f":{MORA_PORT}/" not in url, \
            f"Monastery must not serve off Mora's port, got {url}"
        assert js(window, "!!document.getElementById('week-grid')") is True, \
            f"the page in this window is not Monastery's ({url})"
        assert js(window, "!document.getElementById('graph')") is True, \
            f"this window loaded the MonOSS graph, not Monastery ({url})"
        assert js(window, "!document.getElementById('editor')") is True, \
            f"this window loaded Mora, not Monastery ({url})"
        assert wait_until(lambda: count(window, ".day-col") == 7), \
            "Monastery's frontend loaded but never drew a week"

    check("Monastery comes up on its own port with its own frontend",
          monastery_loads_its_own_frontend)
    if not results[-1][1]:
        return  # nothing below can mean anything if this window is not Monastery

    def the_week_opens_on_today():
        assert wait_until(lambda: count(window, ".day-head.today") == 1), \
            "today's column is not marked"
        assert count(window, "#now-line") == 1, "the now-line should be drawn in today's column"

    check("the week view opens on the current week", the_week_opens_on_today)

    def every_binding_places_its_motes():
        names = event_names(window)
        assert "Standup" in names, f"`when` did not place: {names}"
        assert "Schema translation" in names, f"`starts` did not place: {names}"
        assert "Daily sync" in names, f"`date` did not place: {names}"

    check("motes place under every date-shaped property, not just `date`",
          every_binding_places_its_motes)

    def the_sidebar_lists_each_binding():
        keys = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#binding-rows .toggle-name'))
                .map(n => n.textContent))
        """))
        assert set(keys) >= {"when", "starts", "due", "date"}, f"bindings missing: {keys}"

    check("the sidebar lists every binding it found", the_sidebar_lists_each_binding)

    def an_all_day_mote_pins_across_its_whole_span():
        pins = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('.allday-cell'))
                .map(c => Array.from(c.querySelectorAll('.pin-name')).map(n => n.textContent)))
        """))
        spanned = [i for i, names in enumerate(pins) if "Read-only window" in names]
        assert spanned == [2, 3, 4], \
            f"a Wed-to-Fri span should pin on all three days, got columns {spanned}"

    check("an all-day span pins on every day it covers", an_all_day_mote_pins_across_its_whole_span)

    def a_recurring_mote_expands_across_the_week():
        occurrences = js(window, """
            Array.from(document.querySelectorAll('.event-name'))
                .filter(n => n.textContent === 'Daily sync').length
        """)
        assert occurrences == 5, f"COUNT=5 should draw five occurrences, got {occurrences}"

    check("a recurrence expands into separate occurrences", a_recurring_mote_expands_across_the_week)

    def overlapping_events_sit_side_by_side():
        """Standup 09:15-09:45 and Vendor call 09:30-10:15 overlap, so
        neither may take the full column width."""
        widths = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('.event'))
                .filter(e => ['Standup', 'Vendor call']
                    .includes(e.querySelector('.event-name').textContent))
                .map(e => e.style.width))
        """))
        assert len(widths) == 2, f"expected both overlapping events drawn, got {widths}"
        assert all("100.00%" not in w for w in widths), \
            f"overlapping events should share the column, got widths {widths}"

    check("overlapping events are packed into lanes", overlapping_events_sit_side_by_side)

    def a_template_is_never_drawn():
        assert "Meeting template" not in event_names(window), \
            "a template holds starting values, not real data -- it must not draw"

    check("a dated template stays off the grid", a_template_is_never_drawn)

    def the_status_bar_counts_undated_motes():
        text = text_of(window, "#stat-undated")
        assert text.startswith("3 "), \
            f"the person and the two species motes have no date, got {text!r}"

    check("the status bar counts motes without a date", the_status_bar_counts_undated_motes)

    def turning_a_binding_off_removes_its_motes():
        js(window, """
            Array.from(document.querySelectorAll('#binding-rows .toggle-row'))
                .find(r => r.querySelector('.toggle-name').textContent === 'when').click();
        """)
        assert wait_until(lambda: "Standup" not in event_names(window)), \
            "turning `when` off did not remove its motes"
        assert "Schema translation" in event_names(window), \
            "turning `when` off must not remove motes placed by `starts`"

    check("a binding toggle filters the grid", turning_a_binding_off_removes_its_motes)

    def turning_it_back_on_restores_them():
        js(window, """
            Array.from(document.querySelectorAll('#binding-rows .toggle-row'))
                .find(r => r.querySelector('.toggle-name').textContent === 'when').click();
        """)
        assert wait_until(lambda: "Standup" in event_names(window)), \
            "turning `when` back on did not restore its motes"

    check("a binding toggle restores the grid", turning_it_back_on_restores_them)

    def hiding_a_species_filters_the_grid():
        js(window, """
            Array.from(document.querySelectorAll('#species-rows .toggle-row'))
                .find(r => r.querySelector('.toggle-name').textContent === 'Task').click();
        """)
        assert wait_until(lambda: "Schema translation" not in event_names(window)), \
            "hiding the Task species did not remove its instances"
        assert "Standup" in event_names(window), "hiding Task must not hide Meetings"
        js(window, """
            Array.from(document.querySelectorAll('#species-rows .toggle-row'))
                .find(r => r.querySelector('.toggle-name').textContent === 'Task').click();
        """)
        assert wait_until(lambda: "Schema translation" in event_names(window))

    check("a species toggle filters the grid", hiding_a_species_filters_the_grid)

    def clicking_an_event_opens_the_panel():
        js(window, """
            Array.from(document.querySelectorAll('.event'))
                .find(e => e.querySelector('.event-name').textContent === 'Schema translation').click();
        """)
        assert wait_until(lambda: text_of(window, "#panel-name") == "Schema translation"), \
            f"the panel did not open, showing {text_of(window, '#panel-name')!r}"
        assert text_of(window, "#panel-kind-text") == "Task", \
            f"the panel should name the species, got {text_of(window, '#panel-kind-text')!r}"

    check("clicking an event opens its mote in the panel", clicking_an_event_opens_the_panel)

    def the_panel_says_which_property_placed_it():
        placed = text_of(window, "#panel-placed")
        assert "starts" in placed, f"the panel must name the binding, got {placed!r}"

    check("the panel says which property placed the mote", the_panel_says_which_property_placed_it)

    def the_panel_lists_properties_and_relationships():
        props = text_of(window, "#panel-props")
        assert "status" in props and "in progress" in props, f"properties missing: {props!r}"
        assert count(window, "#panel-rels .rel-row") == 1, "the instance_of relationship should list"

    check("the panel lists properties and relationships",
          the_panel_lists_properties_and_relationships)

    def a_relationship_navigates_the_panel():
        click(window, "#panel-rels .rel-row")
        assert wait_until(lambda: text_of(window, "#panel-name") == "Task"), \
            f"clicking the relationship did not move the panel, got {text_of(window, '#panel-name')!r}"

    check("clicking a relationship navigates the panel", a_relationship_navigates_the_panel)

    def escape_closes_the_panel():
        js(window, "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));")
        assert wait_until(
            lambda: js(window, "document.getElementById('panel').classList.contains('hidden')")
        ), "Escape did not close the panel"

    check("Escape closes the detail panel", escape_closes_the_panel)

    def the_month_view_draws_a_full_grid():
        click(window, "#modes button[data-view='month']")
        assert wait_until(lambda: count(window, ".month-cell") == 42), \
            f"the month grid should be six weeks, got {count(window, '.month-cell')} cells"
        assert count(window, ".month-cell.today") == 1, "today is not marked in the month view"
        assert count(window, ".chip") > 0, "the month view drew no events"

    check("the month view draws six weeks with events", the_month_view_draws_a_full_grid)

    def clicking_a_day_number_returns_to_that_week():
        js(window, """
            document.querySelector('.month-cell.today .month-num').click();
        """)
        assert wait_until(lambda: not js(
            window, "document.getElementById('week-view').classList.contains('hidden')"
        )), "clicking a day number did not return to the week view"
        assert wait_until(lambda: count(window, ".day-head.today") == 1)

    check("a month day number opens that week", clicking_a_day_number_returns_to_that_week)

    def navigating_away_and_back_lands_on_today():
        click(window, "#next")
        assert wait_until(lambda: count(window, ".day-head.today") == 0), \
            "next week should not contain today"
        click(window, "#today")
        assert wait_until(lambda: count(window, ".day-head.today") == 1), \
            "Today did not return to the current week"

    check("next / Today navigate the week", navigating_away_and_back_lands_on_today)

    def the_draft_popover_creates_a_mote():
        before = len(all_motes())
        js(window, """
            const col = document.querySelectorAll('.day-col')[1];
            const rect = col.getBoundingClientRect();
            col.dispatchEvent(new MouseEvent('click', {
                bubbles: true, clientX: rect.left + 20, clientY: rect.top + 14 * 48,
            }));
        """)
        assert wait_until(lambda: not js(
            window, "document.getElementById('draft').classList.contains('hidden')"
        )), "clicking an empty slot did not open the draft popover"

        js(window, """
            document.getElementById('draft-name').value = 'Retro';
            Array.from(document.querySelectorAll('.species-chip'))
                .find(c => c.textContent.includes('Meeting')).click();
            document.getElementById('draft-create').click();
        """)
        assert wait_until(lambda: len(all_motes()) == before + 1, timeout=8.0), \
            "Create did not write a mote"

        retro = next((m for m in all_motes() if m.properties.get("name") == "Retro"), None)
        assert retro is not None, "the created mote is not named Retro"
        assert "date" in retro.properties, \
            f"a new mote must be dated with the reserved key, got {retro.properties}"
        assert retro.properties["date"].startswith(day(1).isoformat()), \
            f"the mote landed on the wrong day: {retro.properties['date']}"

    check("the draft popover creates a dated mote", the_draft_popover_creates_a_mote)

    def the_new_mote_appears_on_the_grid():
        assert wait_until(lambda: "Retro" in event_names(window)), \
            "the created mote did not appear on the calendar"

    check("a created mote appears on the grid", the_new_mote_appears_on_the_grid)

    def an_outside_edit_shows_up_on_the_next_poll():
        """Nothing in MonOSS pushes -- this is the whole reason Monastery
        polls. A mote written by another process (here, this test's own
        connection, standing in for the CLI or the graph window) must
        appear without the user doing anything."""
        conn = _conn()
        try:
            create_mote(conn, {"name": "Written elsewhere", "date": at(1, 16, 0)})
        finally:
            conn.close()
        assert wait_until(lambda: "Written elsewhere" in event_names(window), timeout=12.0), \
            "an outside write never appeared -- polling is not working"

    check("an edit from another process appears on the next poll",
          an_outside_edit_shows_up_on_the_next_poll)

    def no_uncaught_errors():
        errors = js(window, "JSON.stringify(window.__monasteryErrors || [])")
        assert errors == "[]", f"Monastery logged uncaught errors: {errors}"

    check("no uncaught JS errors during the run", no_uncaught_errors)


def event_names(window):
    return json.loads(js(window, """
        JSON.stringify(Array.from(
            document.querySelectorAll('.event-name, .pin-name, .chip-name')
        ).map(n => n.textContent))
    """))


def _conn():
    conn = get_connection(GARDEN)
    initialize_database(conn)
    return conn


def all_motes():
    conn = _conn()
    try:
        return list_motes(conn)
    finally:
        conn.close()


def drive():
    _page_loaded.wait(timeout=15)
    window = webview.windows[0]
    js(window, """
        window.__monasteryErrors = [];
        window.addEventListener('error', (e) => window.__monasteryErrors.push(String(e.message)));
    """)
    try:
        run_checks(window)
    finally:
        window.destroy()


def _occupy(port, directory):
    """Stand in for a running MonOSS / Mora: hold the port and serve that
    app's frontend off it, so a Monastery that asks for the wrong port
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
    squatters = [
        _occupy(GRAPH_WINDOW_PORT, PROJECT_ROOT / "ui" / "frontend"),
        _occupy(MORA_PORT, PROJECT_ROOT / "mora" / "frontend"),
    ]
    api = Api(GARDEN)
    window = webview.create_window(
        "Monastery smoke test", url=str(FRONTEND_DIR / "index.html"), js_api=api,
        width=1440, height=900,
    )
    api._window = window
    window.events.closed += api.shutdown
    window.events.loaded += _page_loaded.set

    threading.Thread(target=drive, daemon=True).start()
    # The same settings monastery/app.py::launch uses, choose_port()
    # included: a suite that picked its own port would not be testing the
    # thing that broke.
    webview.start(
        http_server=True,
        http_port=choose_port(),
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
