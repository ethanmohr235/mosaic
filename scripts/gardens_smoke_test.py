#!/usr/bin/env python3
"""Regression smoke test for the Gardens window.

Not named test_*.py, for the same reason the other window suites aren't:
it spawns a real pywebview window against a real WebKit/GTK display,
which the pytest suite deliberately assumes nothing about. Run it by
hand after touching gardens_app/:

    .venv/bin/python scripts/gardens_smoke_test.py

Run it alone. The pywebview window suites interfere with each other, and
two at once produce failures that are about the display, not the code.

The bridge itself is tests/test_gardens_api.py's job. What is untested
anywhere else is the wiring: that the list really renders every garden,
that selecting one really fills the detail pane, that the name modal
really validates before it reaches Python, that Delete really offers a
working Undo, and that "Open in" really asks for the app it names.

Nothing here touches real gardens: GARDENS_DIR and the webview storage
both point at a fresh temp directory, and the two actions that would
leave this process behind -- opening an app, opening a native file
dialog -- are stubbed in the page.
"""
import json
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.db as db

_TMP = Path(tempfile.mkdtemp(prefix="gardens-smoke-"))
db.GARDENS_DIR = _TMP / "gardens"

import webview  # noqa: E402  (after GARDENS_DIR is patched, matching app startup order)

from core.db import get_connection, initialize_database  # noqa: E402
from core.gardens import create_garden, garden_exists, list_gardens  # noqa: E402
from core.motes import create_mote  # noqa: E402
from gardens_app.api import Api  # noqa: E402
from gardens_app.app import DEFAULT_HTTP_PORT  # noqa: E402
from window_launcher import choose_port  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "gardens_app" / "frontend"

results = []
_page_loaded = threading.Event()


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


def texts(window, selector):
    return json.loads(js(window, f"""
        JSON.stringify(Array.from(document.querySelectorAll({json.dumps(selector)}))
            .map(n => n.textContent.trim()))
    """))


def text(window, selector):
    return js(window, f"""
        (document.querySelector({json.dumps(selector)}) || {{}}).textContent || ''
    """).strip()


def click(window, selector):
    js(window, f"document.querySelector({json.dumps(selector)}).click();")


def row_names(window):
    return texts(window, ".garden-row .name")


def select_row(window, name):
    js(window, f"""
        (() => {{
          const wanted = {json.dumps(name)};
          const row = Array.from(document.querySelectorAll('.garden-row'))
            .find(r => r.querySelector('.name').textContent === wanted);
          if (!row) throw new Error('no row for ' + wanted);
          row.click();
        }})();
    """)
    wait_until(lambda: text(window, "#detail-name") == name)


def answer_prompt(window, value):
    """Type into the window's own name modal and submit it -- the same
    path a person takes. The modal replaced window.prompt(), which was
    the one part of Monarch's gardens modal that was never in the design
    language."""
    js(window, f"""
        document.getElementById('prompt-input').value = {json.dumps(value)};
        document.getElementById('prompt-ok').click();
    """)


def seed():
    """Two gardens that differ in the two ways the list shows: how much
    is in them, and whether they sync."""
    create_garden("alpha")
    conn = get_connection("alpha")
    try:
        initialize_database(conn)
        create_mote(conn, {"name": "Alice"})
        create_mote(conn, {"name": "Bob"})
    finally:
        conn.close()
    create_garden("beta")


def run_checks(window):
    # Named rather than a bare assert: everything below assumes this
    # window is Gardens, and a window that quietly loaded somebody
    # else's page (window_launcher.choose_port's docstring has the story)
    # would otherwise fail as an unattributed assertion.
    def gardens_loads_its_own_frontend():
        assert wait_until(lambda: js(window, "typeof api !== 'undefined'")), "bridge never arrived"
        assert text(window, "#brand-name") == "Gardens", (
            f"loaded the wrong page: {js(window, 'location.href')}"
        )

    check("the window loads its own frontend", gardens_loads_its_own_frontend)

    # Opening an app and opening a native file dialog are the two things
    # a smoke run cannot let through: one spawns a process it can't
    # reap, the other blocks on a dialog nobody will answer.
    js(window, """
        window.__opened = [];
        api.open_app = (app, garden) => {
            window.__opened.push([app, garden]);
            return Promise.resolve({app, garden});
        };
    """)

    def the_list_shows_every_garden():
        assert wait_until(lambda: row_names(window) == ["alpha", "beta"]), (
            f"expected both gardens, got {row_names(window)}"
        )
        assert "2 gardens" in text(window, "#garden-count")

    check("the list shows every garden", the_list_shows_every_garden)

    def selecting_a_garden_fills_the_detail_pane():
        select_row(window, "alpha")
        assert text(window, "#detail-name") == "alpha"
        assert text(window, "#stat-motes") == "2", (
            f"alpha has two motes, pane says {text(window, '#stat-motes')}"
        )
        assert text(window, "#stat-rels") == "0"
        assert js(window, "!document.getElementById('detail-body').classList.contains('hidden')")

    check("selecting a garden fills the detail pane", selecting_a_garden_fills_the_detail_pane)

    def open_in_offers_every_window():
        assert texts(window, "#open-in .app-chip") == [
            "Mosaic"
        ], f"got {texts(window, '#open-in .app-chip')}"

        js(window, """
            Array.from(document.querySelectorAll('#open-in .app-chip'))
                .find(c => c.textContent.includes('Mosaic')).click();
        """)
        assert wait_until(lambda: json.loads(js(window, "JSON.stringify(window.__opened)"))), (
            "clicking an app chip should reach the bridge"
        )
        assert json.loads(js(window, "JSON.stringify(window.__opened)")) == [["console", "alpha"]]

    check("Open in offers every old window and launches the one clicked", open_in_offers_every_window)

    def creating_a_garden_adds_and_selects_it():
        click(window, "#new-btn")
        time.sleep(0.4)
        answer_prompt(window, "gamma")
        assert wait_until(lambda: "gamma" in row_names(window), timeout=6), (
            f"gamma should be listed, got {row_names(window)}"
        )
        assert garden_exists("gamma"), "gamma should exist on disk"
        assert wait_until(lambda: text(window, "#detail-name") == "gamma"), (
            "a new garden should come up selected"
        )

    check("creating a garden adds it and selects it", creating_a_garden_adds_and_selects_it)

    def a_bad_name_never_reaches_python():
        before = list_gardens()
        click(window, "#new-btn")
        time.sleep(0.4)
        answer_prompt(window, "not a valid name")
        time.sleep(0.6)
        shown = js(window, "!document.getElementById('prompt-error').classList.contains('hidden')")
        assert shown is True, "an invalid name should be explained under the field"
        assert js(window, "!document.getElementById('prompt-overlay').classList.contains('hidden')"), (
            "the modal should stay open on an invalid name"
        )
        assert list_gardens() == before, "nothing should have been created"

        # And a duplicate, which is the other thing the field knows.
        js(window, """
            document.getElementById('prompt-input').value = 'alpha';
            document.getElementById('prompt-ok').click();
        """)
        time.sleep(0.4)
        assert "already exists" in text(window, "#prompt-error")
        click(window, "#prompt-cancel")

    check("an invalid or duplicate name is caught in the field", a_bad_name_never_reaches_python)

    def renaming_moves_the_garden():
        select_row(window, "beta")
        click(window, "#rename-btn")
        time.sleep(0.4)
        answer_prompt(window, "beta-renamed")
        assert wait_until(lambda: "beta-renamed" in row_names(window), timeout=6), (
            f"the renamed garden should be listed, got {row_names(window)}"
        )
        assert "beta" not in row_names(window), "the old name should be gone"
        assert garden_exists("beta-renamed") and not garden_exists("beta")

    check("renaming moves the garden", renaming_moves_the_garden)

    def deleting_offers_a_working_undo():
        select_row(window, "gamma")
        click(window, "#delete-btn")
        time.sleep(0.4)
        click(window, "#confirm-ok")
        assert wait_until(lambda: "gamma" not in row_names(window), timeout=6), (
            "the deleted garden should leave the list"
        )
        assert not garden_exists("gamma"), "and leave the directory"

        assert "Undo" in text(window, "#toast"), (
            f"deleting should offer an undo, toast reads {text(window, '#toast')!r}"
        )
        click(window, "#toast button")
        assert wait_until(lambda: "gamma" in row_names(window), timeout=6), (
            "Undo should bring the garden back"
        )
        assert garden_exists("gamma"), "Undo should bring the file back too"

    check("deleting offers a working Undo", deleting_offers_a_working_undo)

    def cancelling_a_delete_keeps_the_garden():
        select_row(window, "gamma")
        click(window, "#delete-btn")
        time.sleep(0.4)
        click(window, "#confirm-cancel")
        time.sleep(0.4)
        assert "gamma" in row_names(window) and garden_exists("gamma")

    check("cancelling a delete keeps the garden", cancelling_a_delete_keeps_the_garden)

    def sync_starts_off_and_can_be_turned_on():
        # Reaching a real sync server is core/sync.py's job, covered with
        # a live one (tests/test_sync.py, tests/test_gardens_api.py) --
        # this only proves the window's own wiring: the button opens the
        # name modal, the URL that comes back reaches enable_sync(), and
        # the on-state actually shows what sync_status() reports for it.
        # api.enable_sync/sync_status are stubbed for exactly that reason,
        # the same way choose_sync_folder used to be stubbed for the
        # native dialog it would otherwise have popped.
        select_row(window, "alpha")
        assert js(window, "!document.getElementById('sync-off').classList.contains('hidden')"), (
            "a garden with no sync server should show the off state"
        )

        server_url = "http://smoke-token@example.test:8080/"
        js(window, """
            window.__smokeSyncDir = null;
            api.enable_sync = (name, url) => {
                window.__smokeSyncDir = url;
                return Promise.resolve({garden: name, sync_dir: url, cloned: false});
            };
            api.disable_sync = (name) => {
                window.__smokeSyncDir = null;
                return Promise.resolve({garden: name, sync_dir: null});
            };
            api.sync_status = (name) => Promise.resolve({
                device_id: 'smoke', sync_dir: window.__smokeSyncDir, local_ops: 0,
                local_ops_pushed: 0, local_ops_pending: 0, remotes: [],
            });
        """)
        click(window, "#sync-enable")
        time.sleep(0.4)
        answer_prompt(window, server_url)
        assert wait_until(
            lambda: js(window, "!document.getElementById('sync-on').classList.contains('hidden')"),
            timeout=6,
        ), "connecting to a server should switch the section to the on state"
        assert text(window, "#sync-folder") == "http://•••@example.test:8080/", (
            "the server URL should show with its token masked"
        )

        click(window, "#sync-disable")
        assert wait_until(
            lambda: js(window, "!document.getElementById('sync-off').classList.contains('hidden')"),
            timeout=6,
        ), "stopping sync should switch the section back"

    check("sync can be turned on and off per garden", sync_starts_off_and_can_be_turned_on)

    def the_page_raised_no_javascript_errors():
        errors = json.loads(js(window, "JSON.stringify(window.__errors)"))
        assert errors == [], f"page errors: {errors}"

    check("the page raised no JavaScript errors", the_page_raised_no_javascript_errors)


def drive():
    _page_loaded.wait(timeout=15)
    window = webview.windows[0]
    js(window, """
        window.__errors = [];
        window.addEventListener('error', (e) => window.__errors.push(String(e.message)));
    """)
    try:
        run_checks(window)
    finally:
        window.destroy()


def main():
    seed()
    api = Api()
    window = webview.create_window(
        "Gardens smoke test", url=str(FRONTEND_DIR / "index.html"), js_api=api,
        width=940, height=620,
    )
    api._window = window
    window.events.closed += api.shutdown
    window.events.loaded += _page_loaded.set

    threading.Thread(target=drive, daemon=True).start()
    webview.start(
        http_server=True,
        http_port=choose_port("Gardens", DEFAULT_HTTP_PORT),
        private_mode=False,
        storage_path=str(_TMP / "webview_data"),
    )

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFailed:")
        for name, _, err in failed:
            print(f"  - {name}: {err}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
