#!/usr/bin/env python3
"""Regression smoke test for Mora, the .modoc editor window.

Not named test_*.py, for the same reason scripts/ui_smoke_test.py isn't:
it spawns a real pywebview window against a real WebKit/GTK display,
which the pytest suite deliberately assumes nothing about. Run it by
hand after touching mora/:

    .venv/bin/python scripts/mora_smoke_test.py

What it is for is the half of Mora that Python tests cannot reach --
that the rendered pane really builds from the payload, that a `\\mote`
link really moves the data panel, that the stepper really flips Real to
Projected, and that Save really writes only the note. Never touches real
data: GARDENS_DIR and the webview storage_path are both a fresh temp
directory.
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

_TMP = Path(tempfile.mkdtemp(prefix="mora-smoke-"))
db.GARDENS_DIR = _TMP / "gardens"

import webview  # noqa: E402  (after GARDENS_DIR is patched, matching app startup order)

from core.db import get_connection, initialize_database  # noqa: E402
from core.gardens import create_garden  # noqa: E402
from core.modoc import write_source  # noqa: E402
from core.motes import create_mote, get_mote  # noqa: E402
from core.relationships import create_relationship  # noqa: E402
from mora.api import Api  # noqa: E402
from mora.app import choose_port  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "mora" / "frontend"
GARDEN = "smoke"

# The port pywebview pins the *graph* window to (its DEFAULT_HTTP_PORT,
# whenever private_mode is off). The whole run happens with it occupied,
# because that is the real condition -- Mora is opened from a running
# MonOSS -- and because it is the condition that broke: Mora asked for
# the same port, lost, and loaded the graph window's page instead.
GRAPH_WINDOW_PORT = 42001

NOTE = r"""% a note about the migration
\section{Atlas Migration}

Owned by \mote{Rina Kovac}, due \val{due}, \val{count(targets('contains'))} tasks
in scope and \val{sum(targets('contains').progress)} points logged.

\graph{targets('contains') by status}

\subsection{Throughput}
\begin{itemize}
\item \mote{Task 0} is \textbf{ahead}.
\item \mote{Nowhere} is not a mote.
\end{itemize}

\graph{sum(targets('contains').progress) over t}

\begin{equation}
C = \alpha n + \beta n r^{2}
\end{equation}

Broken on purpose: \val{nonesuch}.
"""

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


def seed():
    """A project of four tasks whose progress a rule advances, so every
    macro in NOTE has something real to resolve against."""
    create_garden(GARDEN)
    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        species = create_mote(conn, {"name": "Task", "freedoms": "progress"})
        rule = create_mote(conn, {"name": "Grow", "equation": "progress == prev(progress) + 10"})
        create_relationship(conn, rule, species, "applies_to")
        create_mote(conn, {"name": "Rina Kovac", "role": "Staff engineer"})

        project = create_mote(
            conn, {"name": "Atlas Migration", "due": "2026-09-30", "status": "active"}
        )
        for index, status in enumerate(("todo", "todo", "in progress", "done")):
            task = create_mote(
                conn, {"name": f"Task {index}", "status": status, "progress": 10 * index}
            )
            create_relationship(conn, task, species, "instance_of")
            create_relationship(conn, project, task, "contains")
        write_source(conn, project, NOTE)
        ids["project"] = project
    finally:
        conn.close()


def run_checks(window):
    # First, and deliberately a named check rather than a bare assert:
    # everything below assumes this window is Mora, and when that stopped
    # being true the suite died on an unattributed assertion at the top
    # of the run and reported "0/0 checks passed" -- a true result and a
    # useless one.
    def mora_loads_its_own_frontend_not_the_graph_windows():
        url = js(window, "location.href")
        assert f":{GRAPH_WINDOW_PORT}/" not in url, \
            f"Mora must not serve off the graph window's port, got {url}"
        assert js(window, "!!document.getElementById('editor')") is True, \
            f"the page in this window is not Mora's ({url})"
        assert js(window, "!document.getElementById('graph')") is True, \
            f"this window loaded the MonOSS graph, not Mora ({url})"
        assert wait_until(
            lambda: js(window, "!!document.querySelector('#rendered h1')")
        ), "Mora's frontend loaded but never rendered the note"

    check("Mora comes up on its own port with its own frontend",
          mora_loads_its_own_frontend_not_the_graph_windows)
    if not results[-1][1]:
        return  # nothing below can mean anything if this window is not Mora

    def the_note_opens_and_renders():
        assert wait_until(lambda: text_of(window, "#rendered h1") == "Atlas Migration"), \
            f"heading never rendered, got {text_of(window, '#rendered h1')!r}"
        assert text_of(window, "#note-title") == "Atlas Migration"

    check("the note opens and its heading renders", the_note_opens_and_renders)

    def values_resolve_as_chips():
        chips = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#rendered .val'))
                .map(n => n.textContent))
        """))
        assert "2026-09-30" in chips, f"\\val{{due}} did not resolve: {chips}"
        assert "4" in chips, f"\\val{{count(...)}} did not resolve: {chips}"
        assert "60" in chips, f"\\val{{sum(...)}} did not resolve: {chips}"

    check("\\val resolves against the garden", values_resolve_as_chips)

    def a_broken_value_is_marked_not_hidden():
        broken = js(window, "document.querySelectorAll('#rendered .val.broken').length")
        assert broken == 1, f"expected exactly one broken \\val, got {broken}"

    check("a broken \\val renders visibly broken", a_broken_value_is_marked_not_hidden)

    def links_resolve_and_unresolved_ones_are_marked():
        links = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#rendered .mote-link'))
                .map(n => [n.textContent, n.classList.contains('broken')]))
        """))
        assert ["Rina Kovac", False] in links, f"name link did not resolve: {links}"
        assert ["Nowhere", True] in links, f"unresolved link was not marked: {links}"

    check("\\mote resolves by name, and says so when it can't", links_resolve_and_unresolved_ones_are_marked)

    def both_graph_forms_draw():
        bars = js(window, "document.querySelectorAll('#rendered .figure .bar-row').length")
        line = js(window, "document.querySelectorAll('#rendered .figure svg path').length")
        assert bars == 3, f"the `by status` graph should have three bars, got {bars}"
        assert line == 2, f"the `over t` graph should draw a fill and a line, got {line}"

    check("both \\graph forms draw", both_graph_forms_draw)

    def math_renders_symbols_and_superscripts():
        display = text_of(window, "#rendered .display-math")
        assert "α" in display and "β" in display, f"symbols not substituted: {display!r}"
        sup = text_of(window, "#rendered .display-math sup")
        assert sup == "2", f"superscript not rendered, got {sup!r}"

    check("display math substitutes symbols and raises exponents", math_renders_symbols_and_superscripts)

    def the_panel_starts_on_the_note_mote():
        assert wait_until(lambda: text_of(window, "#panel-name") == "Atlas Migration"), \
            f"panel opened on {text_of(window, '#panel-name')!r}"
        rows = js(window, "document.querySelectorAll('#panel-rels .entry-row').length")
        assert rows == 4, f"the project's four `contains` relationships should list, got {rows}"

    check("the data panel starts on the note's own mote", the_panel_starts_on_the_note_mote)

    def the_note_source_is_not_shown_as_a_property():
        keys = js(window, "document.getElementById('panel-props').textContent")
        assert "modoc" not in keys, "the note's own source should not be listed as a property"

    check("the panel hides the note's `modoc` property", the_note_source_is_not_shown_as_a_property)

    def clicking_a_link_moves_the_panel_and_back_returns():
        js(window, """
            Array.from(document.querySelectorAll('#rendered .mote-link'))
                .find(n => n.textContent === 'Rina Kovac').click();
        """)
        assert wait_until(lambda: text_of(window, "#panel-name") == "Rina Kovac"), \
            f"panel did not follow the link, showing {text_of(window, '#panel-name')!r}"
        assert js(window, "!document.getElementById('panel-back').classList.contains('hidden')")
        click(window, "#panel-back")
        assert wait_until(lambda: text_of(window, "#panel-name") == "Atlas Migration"), \
            "back-to-note-mote did not return"

    check("a \\mote link navigates the panel, and back returns", clicking_a_link_moves_the_panel_and_back_returns)

    def linked_in_text_counts_references():
        entries = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#panel-linked .entry-row'))
                .map(n => n.textContent))
        """))
        assert any("Rina Kovac" in e and "×1" in e for e in entries), \
            f"linked-in-text list is wrong: {entries}"

    check("'Linked in text' lists every reference with a count", linked_in_text_counts_references)

    def the_status_bar_counts_the_note():
        assert "mote links" in text_of(window, "#stat-links")
        assert text_of(window, "#stat-values").startswith("4 "), \
            f"expected 4 values, got {text_of(window, '#stat-values')!r}"
        assert text_of(window, "#problems-btn").startswith("2 "), \
            f"expected 2 problems, got {text_of(window, '#problems-btn')!r}"

    check("the status bar counts links, values and problems", the_status_bar_counts_the_note)

    def stepping_projects_without_writing():
        before = get_mote_progress()
        click(window, "#tick-up")
        click(window, "#tick-up")
        assert wait_until(lambda: text_of(window, "#tick-value") == "t+2")
        assert wait_until(lambda: text_of(window, "#tick-label") == "projected"), \
            f"label did not flip, got {text_of(window, '#tick-label')!r}"
        chips = js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#rendered .val'))
                .map(n => n.textContent))
        """)
        assert "140" in json.loads(chips), f"values did not move with t: {chips}"
        assert get_mote_progress() == before, "a preview must not write to the graph"

    check("the t+N stepper projects and writes nothing", stepping_projects_without_writing)

    def stepping_back_to_zero_says_real():
        click(window, "#tick-down")
        click(window, "#tick-down")
        assert wait_until(lambda: text_of(window, "#tick-label") == "real")
        assert js(window, "document.getElementById('apply').disabled") is True, \
            "Apply must be unavailable at t+0 -- there is nothing to apply"

    check("t+0 reads as Real and disables Apply", stepping_back_to_zero_says_real)

    def editing_updates_the_rendered_pane():
        js(window, """
            const editor = document.getElementById('editor');
            editor.value = editor.value.replace('Atlas Migration}', 'Atlas Migration II}');
            editor.dispatchEvent(new Event('input'));
        """)
        assert wait_until(lambda: text_of(window, "#rendered h1") == "Atlas Migration II"), \
            "the rendered pane did not follow the edit"
        assert text_of(window, "#dirty") == "edited", "the dirty pill did not change"

    check("typing re-renders and marks the note edited", editing_updates_the_rendered_pane)

    def saving_writes_only_the_note():
        before = dict(get_mote(_conn(), ids["project"]).properties)
        click(window, "#save")
        assert wait_until(lambda: text_of(window, "#dirty") == "synced"), "save did not clear the dirty pill"
        after = dict(get_mote(_conn(), ids["project"]).properties)
        assert "Atlas Migration II" in after["modoc"], "the note was not written"
        del before["modoc"], after["modoc"]
        assert before == after, f"save touched other properties: {before} -> {after}"

    check("Save writes the note and nothing else", saving_writes_only_the_note)

    def problems_show_even_when_nothing_is_linked():
        js(window, """
            const editor = document.getElementById('editor');
            window.__saved = editor.value;
            editor.value = '\\\\bogus with no links at all.';
            editor.dispatchEvent(new Event('input'));
        """)
        assert wait_until(lambda: text_of(window, "#problems-btn").startswith("1 ")), \
            f"a note with no links still has problems, got {text_of(window, '#problems-btn')!r}"
        assert "Nothing linked" in text_of(window, "#panel-linked")
        assert "unknown_command" in text_of(window, "#panel-problems")
        js(window, """
            const editor = document.getElementById('editor');
            editor.value = window.__saved;
            editor.dispatchEvent(new Event('input'));
        """)
        assert wait_until(lambda: text_of(window, "#problems-btn").startswith("2 "))

    check("Problems report even when the note links nothing", problems_show_even_when_nothing_is_linked)

    def the_mode_switcher_hides_panes():
        click(window, "#modes button[data-mode='preview']")
        assert js(window, "document.getElementById('source-pane').classList.contains('hidden')")
        click(window, "#modes button[data-mode='source']")
        assert js(window, "document.getElementById('preview-pane').classList.contains('hidden')")
        click(window, "#modes button[data-mode='split']")
        assert js(window, "!document.getElementById('source-pane').classList.contains('hidden')")

    check("Source / Split / Read switch which panes are shown", the_mode_switcher_hides_panes)

    def the_source_pane_is_highlighted():
        commands = js(window, "document.querySelectorAll('#highlight .hl-cmd').length")
        comments = js(window, "document.querySelectorAll('#highlight .hl-comment').length")
        assert commands > 5, f"commands are not highlighted, found {commands}"
        assert comments >= 1, f"the leading %% comment is not highlighted, found {comments}"

    check("the source pane highlights commands and comments", the_source_pane_is_highlighted)

    def no_uncaught_errors():
        errors = js(window, "JSON.stringify(window.__moraErrors || [])")
        assert errors == "[]", f"Mora logged uncaught errors: {errors}"

    check("no uncaught JS errors during the run", no_uncaught_errors)


def _conn():
    conn = get_connection(GARDEN)
    initialize_database(conn)
    return conn


def get_mote_progress():
    conn = _conn()
    try:
        return sorted(
            m.properties.get("progress")
            for m in [get_mote(conn, r) for r in _task_ids(conn)]
        )
    finally:
        conn.close()


def _task_ids(conn):
    from core.relationships import list_relationships

    return [r.to_id for r in list_relationships(conn, from_id=ids["project"], type_="contains")]


def drive():
    _page_loaded.wait(timeout=15)
    window = webview.windows[0]
    js(window, """
        window.__moraErrors = [];
        window.addEventListener('error', (e) => window.__moraErrors.push(String(e.message)));
    """)
    try:
        run_checks(window)
    finally:
        window.destroy()


def _occupy_graph_window_port():
    """Stand in for a running MonOSS: hold 42001 and serve ui/frontend
    off it, so a Mora that asks for the wrong port doesn't merely fail --
    it loads the graph window's page, exactly as it did in the bug."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(PROJECT_ROOT / "ui" / "frontend")
    )
    try:
        server = socketserver.TCPServer(("127.0.0.1", GRAPH_WINDOW_PORT), handler)
    except OSError:
        print(f"note: port {GRAPH_WINDOW_PORT} already in use -- using the real occupant")
        return None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    seed()
    squatter = _occupy_graph_window_port()
    api = Api(GARDEN, ids["project"])
    window = webview.create_window(
        "Mora smoke test", url=str(FRONTEND_DIR / "index.html"), js_api=api,
        width=1440, height=900,
    )
    api._window = window
    window.events.closed += api.shutdown
    window.events.loaded += _page_loaded.set

    threading.Thread(target=drive, daemon=True).start()
    # The same settings mora/app.py::launch uses, choose_port() included:
    # a suite that picked its own port would not be testing the thing
    # that broke.
    webview.start(
        http_server=True,
        http_port=choose_port(),
        private_mode=False,
        storage_path=str(_TMP / "webview_data"),
    )
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
