#!/usr/bin/env python3
"""Regression smoke test for Mora, the .modoc editor window.

Not named test_*.py, for the same reason scripts/monarch_smoke_test.py isn't:
it spawns a real pywebview window against a real WebKit/GTK display,
which the pytest suite deliberately assumes nothing about. Run it by
hand after touching mora/:

    .venv/bin/python scripts/mora_smoke_test.py

What it is for is the half of Mora that Python tests cannot reach --
that the rendered pane really builds from the payload, that a `\\mote`
link really moves the data panel, that a `\\graph` really draws the
Moxie view it names, and that Save really writes only the note. Never touches real
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
from core.motes import create_mote, get_mote, search_motes, update_mote  # noqa: E402
from core.relationships import create_relationship  # noqa: E402
from core.species import set_species  # noqa: E402
from core.sweep import PLOT_KEY  # noqa: E402
from mora.api import Api  # noqa: E402
from mora.app import DEFAULT_HTTP_PORT  # noqa: E402
from window_launcher import choose_port  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "mora" / "frontend"
GARDEN = "smoke"

# The port pywebview pins the *graph* window to (its DEFAULT_HTTP_PORT,
# whenever private_mode is off). The whole run happens with it occupied,
# because that is the real condition -- Mora is opened from a running
# MonOSS -- and because it is the condition that broke: Mora asked for
# the same port, lost, and loaded Monarch's page instead.
GRAPH_WINDOW_PORT = 42001

NOTE = r"""% a note about the migration
\section{Atlas Migration}

Owned by \mote{Rina Kovac}, due \val{due}, \val{count(targets('contains'))} tasks
in scope and \val{sum(targets('contains').progress)} points logged.

\graph{Capacity by team}

\subsection{Throughput}
Cost grows as $r^{2}$, with a shear modulus of $G = \frac{E}{2(1+\nu)}$.

\begin{itemize}
\item \mote{Task 0} is \textbf{ahead}.
\item \mote{Nowhere} is not a mote.
\end{itemize}

\begin{equation}
\begin{aligned}
C &= \alpha n + \beta n r^{2} \\
\lambda &= \frac{4G^2 \nu}{E - 6G\nu}
\end{aligned}
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
    macro in NOTE has something real to resolve against -- plus a crew
    whose capacity follows its size, and a Moxie view saved over it, so
    the note's `\\graph` has a real figure to draw.

    The growth rule is written against `t` rather than `prev(progress)`
    so that it can be *swept*: a sweep has no previous tick to read, and
    one rule that cannot solve stops every solve in the garden, saved
    view included."""
    create_garden(GARDEN)
    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        species = create_mote(conn, {"name": "Task", "freedoms": "progress"})
        rule = create_mote(conn, {"name": "Grow", "equation": "progress == start + 10 * (t + 1)"})
        create_relationship(conn, rule, species, "applies_to")
        create_mote(conn, {"name": "Rina Kovac", "role": "Staff engineer"})

        project = create_mote(
            conn, {"name": "Atlas Migration", "due": "2026-09-30", "status": "active"}
        )
        for index, status in enumerate(("todo", "todo", "in progress", "done")):
            task = create_mote(
                conn,
                {"name": f"Task {index}", "status": status,
                 "progress": 10 * index, "start": 10 * index},
            )
            create_relationship(conn, task, species, "instance_of")
            create_relationship(conn, project, task, "contains")
        write_source(conn, project, NOTE)
        ids["project"] = project
        ids["view"] = _seed_view(conn)
    finally:
        conn.close()


def _seed_view(conn):
    """A crew whose capacity is twelve points a head, and the saved Moxie
    plot of one against the other. Written as the `plot` property of an
    ordinary mote, which is exactly what core/plotview.py::save_plot writes --
    the point being that Mora reads a real saved view, not a shape
    invented for this test."""
    crew_species = create_mote(conn, {"name": "Crew", "freedoms": "capacity"})
    rule = create_mote(conn, {"name": "Throughput", "equation": "capacity == team * 12"})
    create_relationship(conn, rule, crew_species, "applies_to")
    crew = create_mote(conn, {
        "name": "Platform crew", "team": 4,
        "units": {"capacity": "1", "team": "1"},
    })
    set_species(conn, crew, crew_species)
    # Kept by id rather than looked up by name later: the note's own
    # \subsection is called Throughput too, and search_motes() matches
    # property values, so a name lookup would find the note and write an
    # `equation` onto the project.
    ids["throughput_rule"] = rule
    return create_mote(conn, {"name": "Capacity by team", PLOT_KEY: {
        "mode": "sweep",
        "x": {"mote": crew, "key": "team", "lo": 1.0, "hi": 8.0, "steps": 200},
        "series": [{"mote": crew, "key": "capacity", "label": "Platform crew.capacity"}],
        "knobs": [],
    }})


def run_checks(window):
    # First, and deliberately a named check rather than a bare assert:
    # everything below assumes this window is Mora, and when that stopped
    # being true the suite died on an unattributed assertion at the top
    # of the run and reported "0/0 checks passed" -- a true result and a
    # useless one.
    def mora_loads_its_own_frontend_not_the_graph_windows():
        url = js(window, "location.href")
        assert f":{GRAPH_WINDOW_PORT}/" not in url, \
            f"Mora must not serve off Monarch's port, got {url}"
        assert js(window, "!!document.getElementById('editor')") is True, \
            f"the page in this window is not Mora's ({url})"
        assert js(window, "!document.getElementById('graph')") is True, \
            f"this window loaded the Monarch graph, not Mora ({url})"
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

    def a_saved_moxie_view_draws_in_the_note():
        """`\\graph{Capacity by team}` names a view saved in Moxie, and what
        arrives here is the figure re-solved against the garden as it
        stands -- drawn by the very renderer Moxie uses
        (frontend_shared/plot.js), not a second, weaker one."""
        # `typeof`, not `window.Plot`: plot.js declares `const Plot` at
        # the top level of a classic script, which is in scope for every
        # script after it but is not a property of window.
        assert js(window, "typeof Plot !== 'undefined'"), "plot.js did not load"
        errors = js(window, "document.querySelectorAll('#rendered .figure-error').length")
        assert errors == 0, f"the figure did not resolve: {text_of(window, '#rendered .figure-error')!r}"
        title = text_of(window, "#rendered .figure-head span")
        assert title == "Capacity by team", f"the figure is not titled by its view: {title!r}"
        paths = js(window, "document.querySelectorAll('#rendered .figure-body path.series-path').length")
        assert paths == 1, f"expected one drawn curve, got {paths}"

    check("a saved Moxie view draws in the note", a_saved_moxie_view_draws_in_the_note)

    def the_figure_is_the_garden_now_not_the_points_it_was_saved_with():
        """The whole reason the macro names a view instead of describing
        one: change the model and the figure follows. Capacity is twelve
        a head, so doubling the rate doubles the curve's last value."""
        def last_y():
            return js(window, """
                (() => {
                  const block = payload.blocks.find(b => b.type === 'graph');
                  const points = block.series[0].points.filter(Boolean);
                  return points[points.length - 1][1];
                })()
            """)
        before = last_y()
        conn = _conn()
        try:
            update_mote(conn, ids["throughput_rule"], {"equation": "capacity == team * 24"})
        finally:
            conn.close()
        click(window, "#refresh-btn")
        assert wait_until(lambda: last_y() == before * 2), \
            f"the figure did not follow the garden: {before} -> {last_y()}"

    check("a figure re-solves rather than replaying saved points",
          the_figure_is_the_garden_now_not_the_points_it_was_saved_with)

    def math_is_typeset_by_katex():
        """The `.katex-html` layer specifically, not the whole node: KaTeX
        emits a hidden MathML copy beside it, so textContent on the parent
        reads every symbol twice."""
        assert js(window, "!!window.katex"), "katex.min.js did not load"
        assert js(window, "!document.querySelector('#rendered .math-raw')"), \
            "math fell back to raw source, so KaTeX threw"
        assert js(window, "!document.querySelector('#rendered .katex-error')"), \
            "KaTeX reported a parse error in the sample note"

        display = text_of(window, "#rendered .display-math .katex-html")
        assert "α" in display and "λ" in display, f"symbols not typeset: {display!r}"
        # A real fraction box -- the thing the substitution renderer this
        # replaced could not build at all.
        fracs = js(window, "document.querySelectorAll('#rendered .display-math .mfrac').length")
        assert fracs == 1, f"expected one typeset fraction in the equation, got {fracs}"
        rows = js(window, "document.querySelectorAll('#rendered .display-math .vlist-t').length")
        assert rows > 0, "the aligned environment did not lay out"

        inline = js(window, "document.querySelectorAll('#rendered p .math .katex').length")
        assert inline == 2, f"expected two inline formulas typeset, got {inline}"

    check("math is typeset by KaTeX, inline and display", math_is_typeset_by_katex)

    def clicking_a_link_opens_the_shared_inspector():
        """Mora has no docked data panel any more (frontend_shared/inspector.js
        is the one editing surface across all six windows) -- a `\\mote` link
        in the rendered text is now the only way into a mote from here, and
        it opens the same floating inspector every other window uses."""
        js(window, """
            Array.from(document.querySelectorAll('#rendered .mote-link'))
                .find(n => n.textContent === 'Rina Kovac').click();
        """)
        assert wait_until(lambda: js(window, "document.querySelectorAll('.ins:not(.hidden)').length") == 1), \
            "clicking a \\mote link did not open the inspector"
        assert wait_until(lambda: js(window, "document.querySelector('.ins-name').value") == "Rina Kovac"), \
            "the inspector should be showing the mote that was clicked"
        js(window, "Inspector.close();")

    check("a \\mote link opens the shared inspector", clicking_a_link_opens_the_shared_inspector)

    def the_status_bar_counts_the_note():
        assert "mote links" in text_of(window, "#stat-links")
        assert text_of(window, "#stat-values").startswith("4 "), \
            f"expected 4 values, got {text_of(window, '#stat-values')!r}"
        assert text_of(window, "#problems-btn").startswith("2 "), \
            f"expected 2 problems, got {text_of(window, '#problems-btn')!r}"

    check("the status bar counts links, values and problems", the_status_bar_counts_the_note)

    def the_footer_offers_no_clock():
        """Mora shows the garden as it stands. The `t+N` stepper and the
        Apply button that committed its projection are gone -- moving a
        garden through time is Motion's window, and a note editor holding
        that lever was the wrong place for it."""
        for gone in ("stepper", "tick-value", "tick-label", "apply"):
            assert js(window, f"!document.getElementById({json.dumps(gone)})"), \
                f"#{gone} is still in the footer"
        assert js(window, "typeof api.apply_step === 'undefined'"), \
            "the bridge still exposes apply_step"

    check("the footer offers no clock", the_footer_offers_no_clock)

    def editing_updates_the_rendered_pane():
        js(window, """
            const editor = document.getElementById('editor');
            editor.value = editor.value.replace('Atlas Migration}', 'Atlas Migration II}');
            editor.dispatchEvent(new Event('input'));
        """)
        # Read the pill first: autosave clears it back to "synced" a
        # second or so after the last keystroke, so asking about it
        # after waiting on the render would be a race.
        assert text_of(window, "#dirty") == "edited", "the dirty pill did not change"
        assert wait_until(lambda: text_of(window, "#rendered h1") == "Atlas Migration II"), \
            "the rendered pane did not follow the edit"

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

    def autosave_writes_without_pressing_save():
        """The point of autosave: nobody touched #save here."""
        before = dict(get_mote(_conn(), ids["project"]).properties)
        js(window, """
            const editor = document.getElementById('editor');
            editor.value = editor.value.replace('Atlas Migration II}', 'Atlas Migration III}');
            editor.dispatchEvent(new Event('input'));
        """)
        assert wait_until(
            lambda: "Atlas Migration III" in get_mote(_conn(), ids["project"]).properties["modoc"]
        ), "autosave did not write the note"
        assert wait_until(lambda: text_of(window, "#dirty") == "synced"), \
            "autosave did not clear the dirty pill"
        after = dict(get_mote(_conn(), ids["project"]).properties)
        del before["modoc"], after["modoc"]
        assert before == after, f"autosave touched other properties: {before} -> {after}"

    check("autosave writes the note with no click on Save", autosave_writes_without_pressing_save)

    def problems_show_even_when_nothing_is_linked():
        js(window, """
            const editor = document.getElementById('editor');
            window.__saved = editor.value;
            editor.value = '\\\\bogus with no links at all.';
            editor.dispatchEvent(new Event('input'));
        """)
        assert wait_until(lambda: text_of(window, "#problems-btn").startswith("1 ")), \
            f"a note with no links still has problems, got {text_of(window, '#problems-btn')!r}"
        click(window, "#problems-btn")
        assert wait_until(lambda: "unknown_command" in text_of(window, "#problems-popover")), \
            "the problems popover did not list the diagnostic"
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

    def new_note_opens_a_fresh_page_and_shows_up_in_the_switcher():
        js(window, "window.prompt = () => 'Field notes';")
        click(window, "#notes-btn")
        assert wait_until(
            lambda: not js(window, "document.getElementById('notes-overlay').classList.contains('hidden')")
        ), "the notes window did not open"
        click(window, "#notes-new-btn")
        assert wait_until(lambda: text_of(window, "#note-title") == "Field notes"), \
            f"the new note never became current, got {text_of(window, '#note-title')!r}"
        assert js(window, "document.getElementById('editor').value") == "", \
            "a freshly created note should open on an empty page"
        found = search_motes(_conn(), "Field notes")
        assert any(m.properties.get("modoc") == "" for m in found), \
            "the new mote was not written with an empty modoc property"
        click(window, "#notes-btn")
        assert wait_until(lambda: "Field notes" in text_of(window, "#notes-search-results")), \
            "the new note did not appear in the notes window"
        js(window, "document.dispatchEvent(new KeyboardEvent('keydown', {key: 'Escape', bubbles: true}))")
        assert wait_until(
            lambda: js(window, "document.getElementById('notes-overlay').classList.contains('hidden')")
        ), "Escape did not close the notes window"

    check("+ New note… opens a fresh page and lists it in the notes window",
          new_note_opens_a_fresh_page_and_shows_up_in_the_switcher)

    def the_garden_switcher_lists_the_current_garden():
        assert text_of(window, "#brand-sub") == "MonOSS", \
            f"the brand subheading should be static, got {text_of(window, '#brand-sub')!r}"
        assert text_of(window, "#garden-name") == GARDEN, \
            f"the garden switcher should show the current garden, got {text_of(window, '#garden-name')!r}"
        click(window, "#garden-btn")
        assert wait_until(
            lambda: GARDEN in text_of(window, "#garden-menu")
        ), "the garden switcher did not list the current garden"
        click(window, "#garden-btn")  # close it again

    check("the garden switcher shows the current garden, not the brand subheading",
          the_garden_switcher_lists_the_current_garden)

    def no_uncaught_errors():
        errors = js(window, "JSON.stringify(window.__moraErrors || [])")
        assert errors == "[]", f"Mora logged uncaught errors: {errors}"

    check("no uncaught JS errors during the run", no_uncaught_errors)


def _conn():
    conn = get_connection(GARDEN)
    initialize_database(conn)
    return conn


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
    """Stand in for a running MonOSS: hold 42001 and serve monarch/frontend
    off it, so a Mora that asks for the wrong port doesn't merely fail --
    it loads Monarch's page, exactly as it did in the bug."""
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(PROJECT_ROOT / "monarch" / "frontend")
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
        http_port=choose_port("Mora", DEFAULT_HTTP_PORT),
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
