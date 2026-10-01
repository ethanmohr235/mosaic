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
from monastery.app import DEFAULT_HTTP_PORT  # noqa: E402
from window_launcher import choose_port  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "monastery" / "frontend"
GARDEN = "smoke"

# The ports pywebview pins Monarch and Mora to. The whole run
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


def gesture(window, plan):
    """Drive one pointer gesture. `plan` is a JS expression returning
    {node, points}: the press lands on `node` at the first point, the
    moves and the release go to `window` -- which is where app.js listens
    for the rest of a drag, deliberately, since setPointerCapture throws
    on the synthetic pointerId this has to use.

    A PointerEvent where WebKit offers one, a MouseEvent otherwise: the
    handlers only read button and coordinates, and dispatchEvent keys on
    the type string rather than the class."""
    js(window, f"""
        (() => {{
          const plan = {plan};
          const fire = (target, type, point) => {{
            const init = {{bubbles: true, cancelable: true, button: 0, buttons: 1,
                          pointerId: 1, clientX: point[0], clientY: point[1]}};
            target.dispatchEvent(typeof PointerEvent === 'function'
              ? new PointerEvent(type, init) : new MouseEvent(type, init));
          }};
          const last = plan.points[plan.points.length - 1];
          fire(plan.node, 'pointerdown', plan.points[0]);
          plan.points.slice(1).forEach(p => fire(window, 'pointermove', p));
          // What the release lands on, asked while the drag is still up:
          // app.js takes the dragged node out of the way with
          // pointer-events during a gesture, so this is the column or
          // cell underneath, exactly as the browser sees it.
          const released = document.elementFromPoint(last[0], last[1]);
          fire(window, 'pointerup', last);
          // Real pointer input always leaves a click behind, and it goes
          // to the nearest common ancestor of where the press and the
          // release landed -- so a drag that ended somewhere else clicks
          // an ancestor, not the node that was grabbed. Leaving this out
          // was how a chooser cancelled by that very click passed here.
          let target = plan.node;
          while (released && target && !target.contains(released)) target = target.parentElement;
          fire(target || document, 'click', last);
        }})();
    """)


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
            f"Monastery must not serve off Monarch's port, got {url}"
        assert f":{MORA_PORT}/" not in url, \
            f"Monastery must not serve off Mora's port, got {url}"
        assert js(window, "!!document.getElementById('week-grid')") is True, \
            f"the page in this window is not Monastery's ({url})"
        assert js(window, "!document.getElementById('graph')") is True, \
            f"this window loaded the Monarch graph, not Monastery ({url})"
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

    def clicking_an_event_opens_the_inspector():
        js(window, """
            Array.from(document.querySelectorAll('.event'))
                .find(e => e.querySelector('.event-name').textContent === 'Schema translation').click();
        """)
        assert wait_until(lambda: js(window, "(document.querySelector('.ins .ins-name') || {}).value") == "Schema translation"), \
            "the shared inspector did not open on the clicked mote"
        assert text_of(window, ".ins .ins-species") == "Task", \
            f"the inspector should name the species, got {text_of(window, '.ins .ins-species')!r}"

    check("clicking an event opens its mote in the inspector", clicking_an_event_opens_the_inspector)

    def the_inspector_lists_properties_and_relationships():
        # Property keys/values live in <input>/<select> elements, not
        # plain text, so read their .value rather than textContent.
        keys = js(window, """
            Array.from(document.querySelectorAll('.ins .ins-key')).map(k => k.value)
        """)
        assert "status" in keys, f"properties missing 'status': {keys!r}"
        values = js(window, """
            Array.from(document.querySelectorAll('.ins .ins-row input, .ins .ins-row select, .ins .ins-row textarea'))
                .map(v => v.value)
        """)
        assert "in progress" in values, f"'in progress' value missing: {values!r}"
        assert count(window, ".ins .ins-rel") == 1, "the instance_of relationship should list"

    check("the inspector lists properties and relationships",
          the_inspector_lists_properties_and_relationships)

    def a_relationship_navigates_the_inspector():
        click(window, ".ins .ins-rel .ins-rel-other")
        assert wait_until(lambda: js(window, "(document.querySelector('.ins .ins-name') || {}).value") == "Task"), \
            "clicking the relationship did not move the inspector"

    check("clicking a relationship navigates the inspector", a_relationship_navigates_the_inspector)

    def escape_closes_the_inspector():
        js(window, "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape'}));")
        assert wait_until(
            lambda: js(window, "document.querySelector('.ins').classList.contains('hidden')")
        ), "Escape did not close the inspector"

    check("Escape closes the inspector", escape_closes_the_inspector)

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

    def dragging_an_event_moves_its_mote():
        """Standup is Monday 09:15. Dragged into Wednesday's column an
        hour lower, its `when` -- the key that placed it, not `date` --
        has to say so."""
        gesture(window, """(() => {
            const node = Array.from(document.querySelectorAll('.event'))
                .find(e => e.querySelector('.event-name').textContent === 'Standup');
            const rect = node.getBoundingClientRect();
            const column = document.querySelectorAll('.day-col')[2].getBoundingClientRect();
            const y = rect.top + rect.height / 2;
            return {node, points: [
                [rect.left + 20, y],
                [column.left + 20, y + 24],
                [column.left + 20, y + 48],
            ]};
        })()""")
        wanted = f"{day(2).isoformat()}T10:15:00/PT30M"
        assert wait_until(
            lambda: _properties(ids["standup"]).get("when") == wanted
        ), (f"a drag should rewrite the binding that placed it, got "
            f"{_properties(ids['standup'])}")
        assert wait_until(lambda: js(window, """
            (() => {
              const node = Array.from(document.querySelectorAll('.event'))
                .find(e => e.querySelector('.event-name').textContent === 'Standup');
              return node ? node.closest('.day-col').dataset.day : null;
            })()
        """) == day(2).isoformat()), "the moved event did not redraw in its new column"

    check("dragging an event moves its mote", dragging_an_event_moves_its_mote)

    def dragging_an_edge_changes_the_duration():
        """Vendor call is 09:30 for 45 minutes. Its bottom edge pulled
        down an hour makes that 105."""
        gesture(window, """(() => {
            const node = Array.from(document.querySelectorAll('.event'))
                .find(e => e.querySelector('.event-name').textContent === 'Vendor call');
            const rect = node.getBoundingClientRect();
            const x = rect.left + rect.width / 2;
            return {node, points: [
                [x, rect.bottom - 2],
                [x, rect.bottom + 24],
                [x, rect.bottom + 46],
            ]};
        })()""")
        assert wait_until(lambda: _named("Vendor call").properties.get("when", "").endswith("/PT1H45M")), \
            f"resizing should rewrite the span inside the value, got {_named('Vendor call').properties}"

    check("dragging an event's edge changes its duration",
          dragging_an_edge_changes_the_duration)

    def sweeping_empty_space_drafts_the_time_it_drew():
        gesture(window, """(() => {
            const column = document.querySelectorAll('.day-col')[1];
            const rect = column.getBoundingClientRect();
            return {node: column, points: [
                [rect.left + 20, rect.top + 14 * 48],
                [rect.left + 20, rect.top + 15 * 48],
                [rect.left + 20, rect.top + 15.5 * 48],
            ]};
        })()""")
        assert wait_until(lambda: not js(
            window, "document.getElementById('draft').classList.contains('hidden')"
        )), "sweeping empty space did not open the draft popover"
        start = js(window, "document.getElementById('draft-start').value")
        end = js(window, "document.getElementById('draft-end').value")
        assert (start, end) == ("14:00", "15:30"), \
            f"the draft should open on the range that was drawn, got {start}-{end}"
        click(window, "#draft-cancel")

    check("sweeping empty space drafts that range",
          sweeping_empty_space_drafts_the_time_it_drew)

    def dragging_an_all_day_pin_moves_the_whole_span():
        """Read-only window runs Wed to Fri. Its Wednesday pin dragged
        onto Thursday moves both ends of the span by a day -- a move is a
        shift, never a stretch."""
        gesture(window, """(() => {
            const cells = document.querySelectorAll('.allday-cell');
            const pin = Array.from(cells[2].querySelectorAll('.pin'))
                .find(p => p.querySelector('.pin-name').textContent === 'Read-only window');
            const rect = pin.getBoundingClientRect();
            const target = cells[3].getBoundingClientRect();
            const y = rect.top + rect.height / 2;
            return {node: pin, points: [
                [rect.left + 20, y],
                [target.left + 20, y],
                [target.left + target.width / 2, y],
            ]};
        })()""")
        assert wait_until(lambda: _named("Read-only window").properties.get("starts", "")
                          .startswith(day(3).isoformat())), \
            f"the span did not move, got {_named('Read-only window').properties}"
        assert _named("Read-only window").properties["starts"] == f"{day(3).isoformat()}/P3D", \
            "the span travels inside the value, so there is no end left behind to disagree"
        # The write lands before the redraw does, and the next check
        # grabs a pin off the grid -- so wait for the grid, not just the
        # garden, or that grab gets a rectangle that has already moved.
        assert wait_until(lambda: _pin_columns(window, "Read-only window") == [3, 4, 5]), \
            "the moved span did not redraw on the days it now covers"

    check("dragging an all-day pin moves the whole span",
          dragging_an_all_day_pin_moves_the_whole_span)

    def dragging_an_all_day_edge_extends_the_span():
        """Its right edge, dragged onto Sunday. `ends` is inclusive as
        people write it, so Sunday is the day it now says."""
        gesture(window, """(() => {
            const cells = document.querySelectorAll('.allday-cell');
            const pin = Array.from(cells[3].querySelectorAll('.pin'))
                .find(p => p.querySelector('.pin-name').textContent === 'Read-only window');
            const rect = pin.getBoundingClientRect();
            const target = cells[6].getBoundingClientRect();
            const y = rect.top + rect.height / 2;
            return {node: pin, points: [
                [rect.right - 3, y],
                [target.left + 10, y],
                [target.left + target.width / 2, y],
            ]};
        })()""")
        assert wait_until(lambda: _named("Read-only window").properties.get("starts")
                          == f"{day(3).isoformat()}/P4D"), \
            f"the end did not follow the edge, got {_named('Read-only window').properties}"
        assert _named("Read-only window").properties["starts"].startswith(day(3).isoformat()), \
            "resizing must not move the start it was measured from"

    check("dragging an all-day pin's edge extends its span",
          dragging_an_all_day_edge_extends_the_span)

    def sweeping_month_cells_drafts_an_all_day_range():
        click(window, "#modes button[data-view='month']")
        assert wait_until(lambda: count(window, ".month-cell") == 42)
        gesture(window, """(() => {
            const cells = Array.from(document.querySelectorAll('.month-cell'));
            const from = cells[8].getBoundingClientRect();
            const to = cells[10].getBoundingClientRect();
            return {node: cells[8], points: [
                [from.left + from.width / 2, from.top + from.height - 8],
                [to.left + 4, to.top + to.height - 8],
                [to.left + to.width / 2, to.top + to.height - 8],
            ]};
        })()""")
        assert wait_until(lambda: not js(
            window, "document.getElementById('draft').classList.contains('hidden')"
        )), "sweeping empty month cells did not open the draft popover"
        anchor = text_of(window, "#draft-anchor")
        assert "3 days" in anchor, f"the draft should carry the span that was drawn, got {anchor!r}"
        assert js(window, "document.getElementById('draft-times').classList.contains('hidden')"), \
            "a swept range of days is an all-day mote"
        click(window, "#draft-cancel")
        click(window, "#modes button[data-view='week']")
        assert wait_until(lambda: count(window, ".day-col") == 7)

    check("sweeping month cells drafts an all-day range",
          sweeping_month_cells_drafts_an_all_day_range)

    def the_draft_popover_creates_a_mote():
        before = len(all_motes())
        gesture(window, """(() => {
            const column = document.querySelectorAll('.day-col')[1];
            const rect = column.getBoundingClientRect();
            return {node: column, points: [
                [rect.left + 20, rect.top + 14 * 48],
                [rect.left + 20, rect.top + 15 * 48],
                [rect.left + 20, rect.top + 15.5 * 48],
            ]};
        })()""")
        assert wait_until(lambda: not js(
            window, "document.getElementById('draft').classList.contains('hidden')"
        )), "sweeping an empty slot did not open the draft popover"

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
            f"a new mote is written to the conventional key, got {retro.properties}"
        assert retro.properties["date"] == f"{day(1).isoformat()}T14:00:00/PT1H30M", \
            ("the draft must compose one interval carrying the drawn length, "
             f"got {retro.properties['date']!r}")
        assert "duration" not in retro.properties, \
            "a span is part of the value now, never a second property"

    check("the draft popover creates a dated mote", the_draft_popover_creates_a_mote)

    def the_draft_popover_authors_a_real_repeat_rule():
        """Structured repeat authoring (frequency/weekday/ends pills)
        used to live in Monarch's interval picker even though Monarch
        never expands one -- it moved here, the app that actually reads
        `recurring` back off core/calendar.py's dateutil expansion."""
        before = len(all_motes())
        gesture(window, """(() => {
            const column = document.querySelectorAll('.day-col')[3];
            const rect = column.getBoundingClientRect();
            return {node: column, points: [
                [rect.left + 20, rect.top + 9 * 48],
                [rect.left + 20, rect.top + 10 * 48],
                [rect.left + 20, rect.top + 10.5 * 48],
            ]};
        })()""")
        assert wait_until(lambda: not js(
            window, "document.getElementById('draft').classList.contains('hidden')"
        )), "sweeping an empty slot did not open the draft popover"

        js(window, "document.getElementById('draft-name').value = 'Repeat Test';")

        # The repeat controls are the shared interval widget's now
        # (frontend_shared/interval.js::repeatEditor) -- the same editor
        # the inspector's calendar uses, rather than a second copy living
        # only here. "+ Add a repeat rule" seeds weekly on the start's
        # own weekday; the pills below are that editor's.
        click(window, "#draft-repeat-detail .repeat-add")
        assert wait_until(lambda: count(window, "#draft-repeat-detail .interval-day-pill") == 7), \
            "adding a repeat rule did not reveal the weekday row"
        # The shared editor lists days in RRULE's own Sunday-first order,
        # so Friday is index 5. The rule is seeded on the start's own
        # weekday (a Thursday here), and this adds a second one.
        js(window, """
            const days = Array.from(
                document.querySelectorAll('#draft-repeat-detail .interval-day-pill'));
            days[5].click();
        """)
        js(window, """
            const pills = Array.from(
                document.querySelectorAll('#draft-repeat-detail .interval-pill'));
            pills.find((p) => p.textContent === 'After').click();
        """)
        js(window, "document.getElementById('draft-create').click();")
        assert wait_until(lambda: len(all_motes()) == before + 1, timeout=8.0), \
            "Create did not write a mote"

        standup = next((m for m in all_motes() if m.properties.get("name") == "Repeat Test"), None)
        assert standup is not None, "the created mote is not named Repeat Test"
        value = standup.properties.get("date", "")
        assert "RRULE:FREQ=WEEKLY" in value, f"picking Weekly should write a real RRULE, got {value!r}"
        byday = value.split("BYDAY=")[1] if "BYDAY=" in value else ""
        assert "FR" in byday, f"the weekday toggle should carry into BYDAY=, got {value!r}"
        assert "TH" in byday, f"the seeded weekday should survive, got {value!r}"
        assert "COUNT=10" in value, \
            f"the default 'ends after' count should be written, got {value!r}"

    check("the draft popover authors a real repeat rule", the_draft_popover_authors_a_real_repeat_rule)

    def the_new_mote_appears_on_the_grid():
        assert wait_until(lambda: "Retro" in event_names(window)), \
            "the created mote did not appear on the calendar"

    check("a created mote appears on the grid", the_new_mote_appears_on_the_grid)

    def deleting_one_occurrence_leaves_the_rest():
        """Daily sync recurs five times. Removing Tuesday's has to leave
        four -- and leave the mote, since one mote is the whole series."""
        js(window, """
            Array.from(document.querySelectorAll('.event'))
                .filter(e => e.querySelector('.event-name').textContent === 'Daily sync')[1]
                .querySelector('.kill').click();
        """)
        assert wait_until(lambda: not js(
            window, "document.getElementById('chooser').classList.contains('hidden')"
        )), "deleting an occurrence of a series must ask which it meant"
        click(window, "#chooser-one")

        assert wait_until(lambda: _daily_syncs(window) == 4), \
            f"four occurrences should remain, got {_daily_syncs(window)}"
        placed = _named("Daily sync").properties.get("date", "")
        assert "EXDATE" in placed, \
            f"the exception should live in the value's own rule lines, got {placed!r}"

    check("deleting one occurrence excepts it from the series",
          deleting_one_occurrence_leaves_the_rest)

    def undo_puts_the_occurrence_back():
        click(window, "#toast .toast-action")
        assert wait_until(lambda: _daily_syncs(window) == 5), \
            f"undo should restore the occurrence, got {_daily_syncs(window)}"

    check("the undo in the toast restores it", undo_puts_the_occurrence_back)

    def dragging_a_series_asks_and_then_moves_all_of_it():
        """Daily sync recurs. Dragged an hour later and answered "the
        whole series", every occurrence moves -- one mote is the series,
        so a series move is a shift of its anchor."""
        gesture(window, """(() => {
            const nodes = Array.from(document.querySelectorAll('.event'))
                .filter(e => e.querySelector('.event-name').textContent === 'Daily sync');
            const node = nodes[1];
            const rect = node.getBoundingClientRect();
            const x = rect.left + rect.width / 2;
            const y = rect.top + rect.height / 2;
            return {node, points: [[x, y], [x, y + 24], [x, y + 48]]};
        })()""")
        assert wait_until(lambda: not js(
            window, "document.getElementById('chooser').classList.contains('hidden')"
        )), "dragging one occurrence of a series must ask which it meant"
        click(window, "#chooser-all")

        wanted = f"{day(0).isoformat()}T09:00:00"
        assert wait_until(lambda: _named("Daily sync").properties.get("date", "")
                          .startswith(wanted)), \
            f"the anchor should have moved with it, got {_named('Daily sync').properties}"
        assert wait_until(lambda: _daily_syncs(window) == 5), \
            "the series should still draw five times after moving"

    check("dragging one occurrence of a series can move the whole series",
          dragging_a_series_asks_and_then_moves_all_of_it)

    def the_delete_key_asks_about_a_series_too():
        """The other way in: the keyboard, not the rectangle's × -- so
        this is not the × check twice."""
        js(window, """
            Array.from(document.querySelectorAll('.event'))
                .find(e => e.querySelector('.event-name').textContent === 'Daily sync').click();
        """)
        assert wait_until(lambda: js(window, "(document.querySelector('.ins .ins-name') || {}).value") == "Daily sync"), \
            "clicking the event did not select it"
        js(window, "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Delete'}));")
        assert wait_until(lambda: not js(
            window, "document.getElementById('chooser').classList.contains('hidden')"
        )), "the Delete key must ask which occurrence it meant"
        click(window, "#chooser-one")
        assert wait_until(lambda: _daily_syncs(window) == 4), \
            f"four occurrences should remain, got {_daily_syncs(window)}"

        assert wait_until(lambda: count(window, "#toast .toast-action") == 1)
        click(window, "#toast .toast-action")
        assert wait_until(lambda: _daily_syncs(window) == 5), "undo did not restore it"

    check("the Delete key asks about a series as well",
          the_delete_key_asks_about_a_series_too)

    def dragging_a_month_chip_moves_it_by_a_day():
        """A month cell is a day, so a chip moves by whole days -- and
        keeps the time of day it already had."""
        click(window, "#modes button[data-view='month']")
        # Wait for the chip this is about to grab, not merely for the
        # grid: the month draws as soon as the view flips, while the
        # range it asked for is still on its way back.
        assert wait_until(lambda: count(window, ".month-cell") == 42
                          and _chip_named(window, "Retro") == 1), \
            "the month view never drew the chip to drag"
        gesture(window, """(() => {
            const chip = Array.from(document.querySelectorAll('.chip'))
                .find(c => c.querySelector('.chip-name').textContent === 'Retro');
            const rect = chip.getBoundingClientRect();
            const from = chip.closest('.month-cell');
            const cells = Array.from(document.querySelectorAll('.month-cell'));
            const to = cells[cells.indexOf(from) + 1].getBoundingClientRect();
            const y = rect.top + rect.height / 2;
            return {node: chip, points: [
                [rect.left + 20, y],
                [to.left + 10, to.top + to.height / 2],
                [to.left + to.width / 2, to.top + to.height / 2],
            ]};
        })()""")
        assert wait_until(lambda: _named("Retro").properties.get("date", "")
                          .startswith(f"{day(2).isoformat()}T14:00:00")), \
            f"a chip should move by a day and keep its time, got {_named('Retro').properties}"
        click(window, "#modes button[data-view='week']")
        assert wait_until(lambda: count(window, ".day-col") == 7)

    check("dragging a month chip moves it by whole days",
          dragging_a_month_chip_moves_it_by_a_day)

    def the_delete_key_removes_the_selected_placement():
        js(window, """
            Array.from(document.querySelectorAll('.event'))
                .find(e => e.querySelector('.event-name').textContent === 'Retro').click();
        """)
        assert wait_until(lambda: js(window, "(document.querySelector('.ins .ins-name') || {}).value") == "Retro"), \
            "clicking the event did not select it"
        js(window, "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Delete'}));")
        assert wait_until(
            lambda: not any(m.properties.get("name") == "Retro" for m in all_motes())
        ), "Delete did not remove the selected mote"

        # The toast follows the redraw, so it is not up the instant the
        # mote is gone from the database.
        assert wait_until(lambda: count(window, "#toast .toast-action") == 1), \
            "a delete should offer an undo"
        click(window, "#toast .toast-action")
        assert wait_until(lambda: "Retro" in event_names(window)), \
            "undo did not put the deleted mote back on the grid"

    check("Delete removes the selected placement, and undo restores it",
          the_delete_key_removes_the_selected_placement)

    def an_outside_edit_shows_up_on_the_next_poll():
        """Nothing in MonOSS pushes -- this is the whole reason Monastery
        polls. A mote written by another process (here, this test's own
        connection, standing in for the CLI or Monarch) must
        appear without the user doing anything."""
        conn = _conn()
        try:
            create_mote(conn, {"name": "Written elsewhere", "date": at(1, 16, 0)})
        finally:
            conn.close()
        assert wait_until(lambda: "Written elsewhere" in event_names(window), timeout=12.0), \
            ("an outside write never appeared -- polling is not working. "
             + js(window, "JSON.stringify({polling: !!pollTimer, draft: !!draft, "
                          "drag: !!drag, chooser: !!chooser, focused: document.hasFocus()})"))

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


def _properties(mote_id):
    """One mote's properties, read on a connection of this test's own --
    another process's worth of access to the same file, which is the
    condition Monastery runs in anyway."""
    conn = _conn()
    try:
        mote = get_mote(conn, mote_id)
        return mote.properties if mote else {}
    finally:
        conn.close()


def _pin_columns(window, name):
    """Which all-day columns currently draw a pin for this mote."""
    cells = json.loads(js(window, """
        JSON.stringify(Array.from(document.querySelectorAll('.allday-cell'))
            .map(c => Array.from(c.querySelectorAll('.pin-name')).map(n => n.textContent)))
    """))
    return [i for i, names in enumerate(cells) if name in names]


def _chip_named(window, name):
    return js(window, f"""
        Array.from(document.querySelectorAll('.chip-name'))
            .filter(n => n.textContent === {json.dumps(name)}).length
    """)


def _named(name):
    return next(m for m in all_motes() if m.properties.get("name") == name)


def _daily_syncs(window):
    return js(window, """
        Array.from(document.querySelectorAll('.event-name'))
            .filter(n => n.textContent === 'Daily sync').length
    """)


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
        _occupy(GRAPH_WINDOW_PORT, PROJECT_ROOT / "monarch" / "frontend"),
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
        http_port=choose_port("Monastery", DEFAULT_HTTP_PORT),
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
