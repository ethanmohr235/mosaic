#!/usr/bin/env python3
"""Regression smoke test for Momentum, the task window.

Not named test_*.py, for the same reason scripts/monastery_smoke_test.py
isn't: it spawns a real pywebview window against a real WebKit/GTK
display, which the pytest suite deliberately assumes nothing about. Run
it by hand after touching momentum/:

    .venv/bin/python scripts/momentum_smoke_test.py

What it is for is the half of Momentum that Python tests cannot reach --
that a card really lands in the right column, that dragging one really
rewrites a property, that dropping one on a day really schedules it into
the free slot after a meeting, that the pool really unschedules, and
that the timer really logs a session. Never touches real data:
GARDENS_DIR and the webview storage_path are both a fresh temp
directory.

Seeded relative to *today* rather than to a fixed date: the week view
draws whatever week it is opened on, and the focus queue is today's, so
a fixed seed would test an empty view on any day but one.
"""
import functools
import http.server
import json
import socketserver
import sys
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.db as db

_TMP = Path(tempfile.mkdtemp(prefix="momentum-smoke-"))
db.GARDENS_DIR = _TMP / "gardens"

import webview  # noqa: E402  (after GARDENS_DIR is patched, matching app startup order)

from core.db import get_connection, initialize_database  # noqa: E402
from core.gardens import create_garden  # noqa: E402
from core.motes import create_mote, get_mote  # noqa: E402
from core.relationships import create_relationship  # noqa: E402
from momentum.api import Api  # noqa: E402
from momentum.app import DEFAULT_HTTP_PORT  # noqa: E402
from window_launcher import choose_port  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "momentum" / "frontend"
GARDEN = "smoke"

# The ports pywebview pins the other three windows to. The whole run
# happens with all three occupied, because that is the real condition --
# Momentum is opened from a running MonOSS, possibly alongside both
# others -- and because it is the condition that broke for Mora once: it
# asked for a taken port, lost, and loaded the other app's page against
# its own bridge.
GRAPH_WINDOW_PORT = 42001
MORA_PORT = 42002
MONASTERY_PORT = 42003

MONDAY = date.today() - timedelta(days=date.today().weekday())


def day(offset):
    return MONDAY + timedelta(days=offset)


def at(offset, hour, minute=0):
    return f"{day(offset).isoformat()} {hour:02d}:{minute:02d}"


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


def drag(window, source_selector, target_selector):
    """Drive one pointer-event drag. Momentum drags cards the same way
    Monastery drags rectangles -- pointerdown on the card, pointermove
    past the threshold, pointerup over the target -- rather than the
    native HTML5 drag API, which app.js stopped using because WebKitGTK's
    support for it is not reliable enough for a card to actually move.

    `elementFromPoint` is what app.js itself asks at pointerup to find
    the drop zone, so the synthetic pointerup is dispatched at the
    target's own center rather than on the target node directly -- the
    same distinction the real gesture draws between where the node is
    and where the pointer physically ends up."""
    js(window, f"""
        (() => {{
          const source = document.querySelector({json.dumps(source_selector)});
          const target = document.querySelector({json.dumps(target_selector)});
          if (!source || !target) throw new Error('drag endpoints missing');
          const sRect = source.getBoundingClientRect();
          const tRect = target.getBoundingClientRect();
          const make = (type, x, y) => new PointerEvent(type, {{
            bubbles: true, cancelable: true, button: 0, clientX: x, clientY: y,
          }});
          source.dispatchEvent(make('pointerdown', sRect.left + 4, sRect.top + 4));
          const tx = tRect.left + tRect.width / 2, ty = tRect.top + tRect.height / 2;
          window.dispatchEvent(make('pointermove', tx, ty));
          window.dispatchEvent(make('pointerup', tx, ty));
        }})();
    """)


def card_selector(name):
    """A CSS-free way to name one card: app.js stamps every card with its
    mote id, so the test addresses cards by identity rather than by
    position, which reorders as statuses change."""
    return f'.card[data-id="{ids[name]}"]'


def seed():
    """A garden whose tasks say `status`, `estimate` and `priority`, with
    a meeting already on Monday morning -- so the free-slot search has
    something to schedule around."""
    create_garden(GARDEN)
    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        person = create_mote(conn, {"name": "Dev Anand", "role": "Data engineer"})
        ids["person"] = person

        schema = create_mote(conn, {
            "name": "Schema translation", "status": "doing", "estimate": "5h",
            "priority": "high",
            "subtasks": [{"text": "Map legacy fields", "done": True},
                         {"text": "Reconcile enums", "done": False},
                         {"text": "Peer review", "done": False}],
        })
        create_relationship(conn, schema, person, "assigned_to")
        ids["schema"] = schema

        ids["backfill"] = create_mote(conn, {
            "name": "Backfill dry run", "status": "todo", "estimate": "3h", "priority": "high",
        })
        ids["latency"] = create_mote(conn, {
            "name": "Query latency budget", "status": "todo", "estimate": "90m",
        })
        ids["signoff"] = create_mote(conn, {
            "name": "Stakeholder sign-off", "status": "done", "estimate": "1h",
        })
        ids["tiering"] = create_mote(conn, {
            "name": "Cold tiering spike", "status": "blocked", "estimate": "1d",
        })

        # Already on the calendar Monday morning: 09:00-09:30. A task
        # dropped on Monday must start at 09:30, not 09:00.
        ids["standup"] = create_mote(conn, {"name": "Standup", "date": f"{at(0, 9, 0)}/PT30M"})
        # Scheduled for today, so the focus queue has something in it.
        today_offset = date.today().weekday()
        ids["today_task"] = create_mote(conn, {
            "name": "Today's work", "status": "doing", "estimate": "2h",
            "date": f"{at(today_offset, 14, 0)}/PT2H",
        })
    finally:
        conn.close()


def run_checks(window):
    # First, and deliberately a named check rather than a bare assert:
    # everything below assumes this window is Momentum, and when that
    # stopped being true for Mora the suite died on an unattributed
    # assertion and reported "0/0 checks passed" -- true and useless.
    def momentum_loads_its_own_frontend():
        url = js(window, "location.href")
        for port, name in ((GRAPH_WINDOW_PORT, "Monarch"), (MORA_PORT, "Mora"),
                           (MONASTERY_PORT, "Monastery")):
            assert f":{port}/" not in url, \
                f"Momentum must not serve off {name}'s port, got {url}"
        assert js(window, "!!document.getElementById('columns')") is True, \
            f"the page in this window is not Momentum's ({url})"
        assert js(window, "!document.getElementById('graph')") is True, \
            f"this window loaded the Monarch graph, not Momentum ({url})"
        assert js(window, "!document.getElementById('week-grid')") is True, \
            f"this window loaded Monastery, not Momentum ({url})"
        assert wait_until(lambda: count(window, ".column") >= 4), \
            "Momentum's frontend loaded but never drew a board"

    check("Momentum comes up on its own port with its own frontend",
          momentum_loads_its_own_frontend)
    if not results[-1][1]:
        return  # nothing below can mean anything if this window is not Momentum

    # ---- the board ----

    def the_board_discovers_its_status_key():
        labels = texts(window, ".column-head .label")
        assert {"todo", "doing", "blocked", "done"} <= set(labels), \
            f"the garden's own statuses should be the columns, got {labels}"

    check("the board's columns are the garden's own statuses", the_board_discovers_its_status_key)

    def the_sidebar_offers_every_candidate_key():
        keys = texts(window, "#status-rows .name")
        assert {"status", "priority"} <= set(keys), f"candidates missing: {keys}"
        chosen = texts(window, "#status-rows .key-row.on .name")
        assert chosen == ["status"], f"status should default to `status`, got {chosen}"

    check("the sidebar offers every candidate key and marks the chosen one",
          the_sidebar_offers_every_candidate_key)

    def a_card_shows_its_estimate_and_owner():
        card = json.dumps(card_selector("schema"))
        estimate = js(window, f"document.querySelector({card} + '' ) && "
                              f"document.querySelector({card}).querySelector('.est').textContent")
        owner = js(window, f"document.querySelector({card}).querySelector('.owner').textContent")
        assert estimate == "5h", f"estimate not shown, got {estimate!r}"
        assert owner == "Dev Anand", f"owner should come from the relationship, got {owner!r}"

    check("a card shows its estimate and its owner relationship",
          a_card_shows_its_estimate_and_owner)

    def a_column_totals_its_load():
        """todo holds 3h + 90m, doing holds 5h + the 2h scheduled for
        today, blocked holds a 1d estimate -- which is eight hours of
        work, not twenty-four (core/task_candidates.py)."""
        loads = texts(window, ".column-head .load")
        assert loads[:4] == ["4.5h", "7h", "8h", "1h"], f"column loads wrong: {loads}"
        assert loads[4] == "", "a column of unestimated motes totals nothing, not zero"

    check("a column totals the work in it", a_column_totals_its_load)

    def dragging_a_card_rewrites_its_status():
        before = get_mote(get_conn(), ids["backfill"]).properties["status"]
        assert before == "todo"
        # The blocked column is third; addressing it by index rather than
        # by name keeps this working if the label ever changes case.
        drag(window, card_selector("backfill"), ".column:nth-child(3)")
        assert wait_until(
            lambda: get_mote(get_conn(), ids["backfill"]).properties["status"] == "blocked"
        ), "dragging between columns did not write the status key"

    check("dragging a card between columns rewrites the status property",
          dragging_a_card_rewrites_its_status)

    def clicking_a_card_opens_the_panel():
        click(window, card_selector("schema"))
        assert wait_until(lambda: js(window, "!document.getElementById('panel').classList.contains('hidden')")), \
            "the detail panel never opened"
        name = js(window, "document.getElementById('panel-name').textContent")
        assert name == "Schema translation", f"the panel opened on the wrong mote: {name!r}"
        subs = count(window, "#panel-subs .sub-row")
        assert subs == 3, f"the panel should list 3 subtasks, got {subs}"

    check("clicking a card opens the detail panel", clicking_a_card_opens_the_panel)

    def the_panel_shows_real_relationships():
        rels = texts(window, "#panel-rels .rel-row .type")
        assert "assigned to" in rels, f"the owner should be a real edge, got {rels}"

    check("the panel shows owners as real relationships", the_panel_shows_real_relationships)

    def a_subtask_toggles_and_persists():
        js(window, "document.querySelectorAll('#panel-subs .sub-row')[1].click();")
        assert wait_until(lambda: get_mote(get_conn(), ids["schema"]).properties["subtasks"][1]["done"] is True), \
            "toggling a subtask did not write it back"

    check("toggling a subtask writes it to the mote", a_subtask_toggles_and_persists)

    # ---- the week ----

    def the_week_view_opens():
        js(window, "document.querySelector('#tabs button[data-tab=\"days\"]').click();")
        assert wait_until(lambda: count(window, ".day") == 7), "the week never drew seven days"
        assert count(window, ".day.today") == 1, "today's column is not marked"

    check("the days view draws the current week", the_week_view_opens)

    def the_nav_arrows_step_a_week_and_today_returns():
        title_before = js(window, "document.getElementById('range-title').textContent")
        click(window, "#next")
        assert wait_until(lambda: js(window, "document.getElementById('range-title').textContent") != title_before), \
            "the next-week arrow never changed the visible range"
        assert count(window, ".day.today") == 0, "next week should not still mark today"
        click(window, "#today")
        assert wait_until(lambda: count(window, ".day.today") == 1), \
            "the today button never brought the current week back"
        assert js(window, "document.getElementById('range-title').textContent") == title_before, \
            "today should return to the same range the view opened on"
        click(window, "#prev")
        assert wait_until(lambda: count(window, ".day.today") == 0), \
            "the previous-week arrow never moved off today"
        click(window, "#today")
        assert wait_until(lambda: count(window, ".day.today") == 1)

    check("the nav arrows step between weeks and today returns to the current one",
          the_nav_arrows_step_a_week_and_today_returns)

    def the_rail_shows_and_edits_working_hours():
        assert js(window, "document.getElementById('work-start-input').value") == "09:00", \
            "the rail should default to the garden's 9-5 window"
        js(window, """
            const input = document.getElementById('work-end-input');
            input.value = '16:00';
            input.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        assert wait_until(lambda: js(window, "workHours.end") == "16:00"), \
            "editing the latest-time input never round-tripped through the bridge"

    check("the rail's working-hours inputs read and write the garden's schedule window",
          the_rail_shows_and_edits_working_hours)

    def the_pool_holds_what_is_undated():
        names = texts(window, "#pool-cards .card-name")
        assert "Query latency budget" in names, f"an undated mote should be in the pool, got {names}"

    check("the undated pool holds what has no date", the_pool_holds_what_is_undated)

    def dropping_on_a_day_finds_the_slot_after_the_meeting():
        """The standup holds Monday 09:00-09:30, so a task dropped on
        Monday must start at 09:30 -- the whole point of scheduling
        against the calendar rather than beside it."""
        drag(window, card_selector("latency"), ".day:nth-child(1)")
        assert wait_until(
            lambda: "date" in get_mote(get_conn(), ids["latency"]).properties
        ), "dropping on a day wrote nothing"
        written = get_mote(get_conn(), ids["latency"]).properties["date"]
        assert written.startswith(f"{day(0).isoformat()}T09:30"), \
            f"should land after the standup at 09:30, got {written}"

    check("dropping a mote on a day schedules it into the earliest free slot",
          dropping_on_a_day_finds_the_slot_after_the_meeting)

    def an_estimate_becomes_the_span_inside_the_value():
        """One write to one key -- the span rides in the interval, so
        there is no second property that could disagree with it."""
        written = get_mote(get_conn(), ids["latency"]).properties["date"]
        assert written.endswith("/PT1H30M"), \
            f"a 90m estimate should block out 90 minutes, got {written}"
        assert "duration" not in get_mote(get_conn(), ids["latency"]).properties

    check("an estimate becomes the span the calendar draws",
          an_estimate_becomes_the_span_inside_the_value)

    def dropping_back_on_the_pool_unschedules():
        drag(window, card_selector("latency"), "#pool")
        assert wait_until(
            lambda: "date" not in get_mote(get_conn(), ids["latency"]).properties
        ), "dropping back on the pool did not clear the date"

    check("dropping a card back on the pool unschedules it", dropping_back_on_the_pool_unschedules)

    # ---- the timer ----

    def the_focus_tab_queues_todays_work():
        js(window, "document.querySelector('#tabs button[data-tab=\"focus\"]').click();")
        assert wait_until(lambda: count(window, "#queue-cards .card") >= 1), \
            "nothing queued for today"
        names = texts(window, "#queue-cards .card-name")
        assert "Today's work" in names, f"today's scheduled mote should be queued, got {names}"

    check("the focus tab queues what is scheduled for today", the_focus_tab_queues_todays_work)

    def the_timer_runs_and_logs_a_session():
        """A running countdown writes nothing -- the same preview/commit
        split core/simulation.py keeps. Only finishing does."""
        before = _session_count()
        click(window, "#run-btn")
        assert wait_until(lambda: js(window, "document.getElementById('phase').textContent") == "in session"), \
            "the timer never started"
        assert _session_count() == before, "a running timer must not write a session"
        # Wind the clock forward rather than waiting out a real 25
        # minutes: the elapsed time is what gets logged, and this is the
        # only part of it the test cannot afford to do honestly.
        js(window, "elapsed = elapsed + 120;")
        click(window, "#finish-btn")
        assert wait_until(lambda: _session_count() == before + 1), \
            "finishing the session logged nothing"

    check("the timer logs a session only when it finishes", the_timer_runs_and_logs_a_session)

    def a_logged_session_is_an_ordinary_dated_mote():
        """Which is why it turns up in Monastery without either window
        knowing the other exists."""
        session = _latest_session()
        assert session.properties.get("date", "").endswith("/PT2M"), \
            ("a session is an ordinary interval carrying its own length; two "
             f"minutes of elapsed time should log as PT2M, got {session.properties!r}")

    check("a logged session is an ordinary dated mote", a_logged_session_is_an_ordinary_dated_mote)

    def the_session_shows_in_the_log_and_streak():
        assert wait_until(lambda: count(window, "#sessions .session") >= 1), \
            "the session log stayed empty"
        streak = js(window, "document.getElementById('streak-days').textContent")
        assert streak == "1", f"a session today should start a streak, got {streak!r}"

    check("the session log and streak reflect the logged session",
          the_session_shows_in_the_log_and_streak)

    def the_duration_is_editable_and_remembered():
        js(window, """
            const input = document.getElementById('duration-input');
            input.value = '50';
            input.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        assert wait_until(lambda: js(window, "sessionMinutes") == 50), \
            "editing the duration input never round-tripped into sessionMinutes"
        assert js(window, "document.getElementById('run-btn').textContent") == "Begin 50m", \
            "the begin button should reflect the edited duration"
        stored = js(window, "JSON.parse(window.localStorage.getItem('momentum.timer'))")
        assert stored["minutes"] == 50, f"the duration should persist to localStorage, got {stored!r}"

    check("the duration input is editable and remembered", the_duration_is_editable_and_remembered)

    def stopwatch_mode_counts_up_with_no_target():
        js(window, """
            const box = document.getElementById('stopwatch-toggle');
            box.checked = true;
            box.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        assert wait_until(lambda: js(window, "stopwatch") is True), \
            "checking the stopwatch box never flipped stopwatch mode"
        assert js(window, "document.getElementById('duration-input').disabled") is True, \
            "the duration input should be disabled while in stopwatch mode"
        before = _session_count()
        click(window, "#run-btn")
        assert wait_until(lambda: js(window, "document.getElementById('phase').textContent") == "in session"), \
            "the stopwatch never started"
        # A stopwatch has no target to hit, so it must still be running
        # well past what would have finished a countdown session.
        js(window, "elapsed = elapsed + 3000;")
        assert js(window, "running") is True, "a stopwatch must not auto-finish like a countdown does"
        click(window, "#finish-btn")
        assert wait_until(lambda: _session_count() == before + 1), \
            "stopping and logging a stopwatch session logged nothing"
        js(window, """
            const box = document.getElementById('stopwatch-toggle');
            box.checked = false;
            box.dispatchEvent(new Event('change', {bubbles: true}));
        """)

    check("stopwatch mode counts up with no target until stopped", stopwatch_mode_counts_up_with_no_target)

    def nothing_threw_along_the_way():
        errors = js(window, "JSON.stringify(window.__momentumErrors || [])")
        assert errors == "[]", f"the page threw: {errors}"

    check("no uncaught errors on the page", nothing_threw_along_the_way)


# ---- reading the garden back ----------------------------------------
#
# Every assertion above that checks a *write* reads it back through a
# fresh connection rather than through the window's bridge: the point is
# that the property really landed in the file, not that the API returned
# a hopeful dict.

def get_conn():
    conn = get_connection(GARDEN)
    initialize_database(conn)
    return conn


def _sessions():
    from core.species import instances_of, list_species

    conn = get_conn()
    try:
        species = next(
            (m for m in list_species(conn)
             if m.properties.get("name", "").casefold() == "work session"), None,
        )
        return instances_of(conn, species.id) if species else []
    finally:
        conn.close()


def _session_count():
    return len(_sessions())


def _latest_session():
    found = _sessions()
    assert found, "no session motes at all"
    return sorted(found, key=lambda m: str(m.properties.get("date", "")))[-1]


def drive():
    _page_loaded.wait(timeout=15)
    window = webview.windows[0]
    js(window, """
        window.__momentumErrors = [];
        window.addEventListener('error', (e) => window.__momentumErrors.push(String(e.message)));
    """)
    try:
        run_checks(window)
    finally:
        window.destroy()


def _occupy(port, directory):
    """Stand in for a running MonOSS / Mora / Monastery: hold the port
    and serve that app's frontend off it, so a Momentum that asks for the
    wrong port doesn't merely fail -- it loads the other app's page,
    exactly as Mora once did."""
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
        _occupy(MORA_PORT, PROJECT_ROOT / "mora" / "frontend"),
        _occupy(MONASTERY_PORT, PROJECT_ROOT / "monastery" / "frontend"),
    ]
    api = Api(GARDEN)
    window = webview.create_window(
        "Momentum smoke test", url=str(FRONTEND_DIR / "index.html"), js_api=api,
        width=1440, height=900,
    )
    api._window = window
    window.events.closed += api.shutdown
    window.events.loaded += _page_loaded.set

    threading.Thread(target=drive, daemon=True).start()
    # The same settings momentum/app.py::launch uses, choose_port()
    # included: a suite that picked its own port would not be testing the
    # thing that broke.
    webview.start(
        http_server=True,
        http_port=choose_port("Momentum", DEFAULT_HTTP_PORT),
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
