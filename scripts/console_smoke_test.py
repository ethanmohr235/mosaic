#!/usr/bin/env python3
"""Regression smoke test for the console (Mosaic's one tiled window).

Not named test_*.py, for the same reason scripts/monolith_smoke_test.py
isn't: it opens a real pywebview window on a real WebKit/GTK display,
which the pytest suite deliberately assumes nothing about. Run it by
hand after touching console/:

    .venv/bin/python scripts/console_smoke_test.py

What it covers is the half the Python tests can't reach: that the shell
boots on the bridge without a JavaScript error, that `^w v` and a
divider drag really split and resize panes, that the view picker places
a view, that a statement typed at the command line lands in the log and
selects its mote, that a saved layout and the history come back after a
reload and after a relaunch on a fresh Api, and that `cd` swaps the
whole workspace. Everything is driven the way a person does it -- key
events, clicks and drags -- because tests/test_console_api.py already
covers the bridge on its own.

Never touches real data: GARDENS_DIR and the webview storage_path are
both a fresh temp directory.
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

_TMP = Path(tempfile.mkdtemp(prefix="console-smoke-"))
db.GARDENS_DIR = _TMP / "gardens"

import webview  # noqa: E402  (after GARDENS_DIR is patched, matching app startup order)

from core.db import get_connection, initialize_database  # noqa: E402
from core.gardens import create_garden  # noqa: E402
from core.motes import create_mote  # noqa: E402
from window_launcher import choose_port  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "console" / "frontend"
DEFAULT_HTTP_PORT = 42009
GARDEN = "smoke"
OTHER_GARDEN = "smoke-b"

ids = {}
results = []
_loaded = threading.Event()

ERROR_COLLECTOR = """
    window.__consoleErrors = window.__consoleErrors || [];
    if (!window.__consoleErrorsInstalled) {
      window.__consoleErrorsInstalled = true;
      window.addEventListener('error', (e) => window.__consoleErrors.push(String(e.message)));
      window.addEventListener('unhandledrejection',
        (e) => window.__consoleErrors.push('unhandled: ' + String(e.reason && e.reason.message || e.reason)));
      const original = console.error.bind(console);
      console.error = (...args) => { window.__consoleErrors.push(args.map(String).join(' ')); original(...args); };
    }
"""


def make_api(garden: str):
    """The real root Api. A fresh instance is what a relaunch gets."""
    from console.api import Api
    return Api(garden)


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


def texts(window, selector):
    return json.loads(js(window, f"""
        JSON.stringify(Array.from(document.querySelectorAll({json.dumps(selector)}))
            .map(n => n.textContent.trim()))
    """))


def press(window, key, ctrl=False, shift=False, alt=False):
    """A keydown on whatever has focus, as the keyboard would send it."""
    js(window, f"""
        (document.activeElement || document.body).dispatchEvent(new KeyboardEvent('keydown', {{
          key: {json.dumps(key)}, ctrlKey: {json.dumps(ctrl)}, shiftKey: {json.dumps(shift)},
          altKey: {json.dumps(alt)}, bubbles: true, cancelable: true }}));
    """)


def tile(window, key):
    """`^w` then `key`."""
    press(window, "w", ctrl=True)
    press(window, key, shift=key in "<>")


def type_command(window, line, alt=False):
    """`:` to the command line, type `line`, ⏎."""
    press(window, ":")
    assert wait_until(lambda: js(window, "document.activeElement.classList.contains('c-input-native')"
                                         " && !!document.activeElement.closest('#cmdline')")), \
        "`:` did not move focus to the command line"
    js(window, f"""
        (() => {{
          const input = document.activeElement;
          input.value = {json.dumps(line)};
          input.setSelectionRange(input.value.length, input.value.length);
          input.dispatchEvent(new Event('input', {{bubbles: true}}));
        }})();
    """)
    press(window, "Enter", alt=alt)


def pane_grows(window):
    return json.loads(js(window, """
        JSON.stringify(Array.from(document.querySelectorAll('.split > .pane'))
            .map(p => parseFloat(p.style.flexGrow || p.style.flex)))
    """))


def headers(window):
    return texts(window, ".pane .c-head-command")


def log_texts(window):
    return texts(window, ".view-log .c-table-row")


def no_errors(window):
    errors = json.loads(js(window, "JSON.stringify(window.__consoleErrors || [])"))
    assert errors == [], f"the page raised: {errors}"


def seed():
    """Two gardens. `R1` exists in the first so the statement the test
    types names a mote the garden already has."""
    for name in (GARDEN, OTHER_GARDEN):
        create_garden(name)
    conn = get_connection(GARDEN)
    try:
        initialize_database(conn)
        ids["R1"] = create_mote(conn, {"name": "R1"})
    finally:
        conn.close()


# ---------- first launch ----------

def first_launch_checks(window):
    def boots():
        assert wait_until(lambda: count(window, ".pane") == 1, timeout=10), \
            f"expected one pane on first launch, got {count(window, '.pane')}"
        assert wait_until(lambda: headers(window) == ["log"]), \
            f"the first pane should be the log, got {headers(window)}"
        assert texts(window, "#topbar .breadcrumb") == [f"/ {GARDEN}"]
        assert count(window, ".pane-error") == 0, texts(window, ".pane-error")
        no_errors(window)

    check("the console boots on its garden with one log pane", boots)
    if not results[-1][1]:
        return False

    def splits():
        tile(window, "v")
        assert wait_until(lambda: count(window, ".pane") == 2), "`^w v` did not make a second pane"
        assert count(window, ".split.row") == 1, "`^w v` should split side by side"
        assert count(window, ".pane.focused .picker") == 1, "the new pane should be focused on the picker"

    check("`^w v` splits the focused pane", splits)

    def picks_log():
        js(window, "document.querySelector('.pane.focused .picker-row[data-view=\"log\"]').click()")
        assert wait_until(lambda: headers(window) == ["log", "log"]), \
            f"picking `log` should place it, got {headers(window)}"

    check("picking `log` in the view picker places it", picks_log)

    def drags():
        before = pane_grows(window)
        js(window, """
            (() => {
              const d = document.querySelector('.divider.row');
              const r = d.getBoundingClientRect();
              const x = r.left + r.width / 2, y = r.top + r.height / 2;
              d.dispatchEvent(new MouseEvent('mousedown', {clientX: x, clientY: y, button: 0, bubbles: true}));
              window.dispatchEvent(new MouseEvent('mousemove', {clientX: x - 150, clientY: y, bubbles: true}));
              window.__guideShown = !document.querySelector('.resize-guide').hidden;
              window.dispatchEvent(new MouseEvent('mouseup', {clientX: x - 150, clientY: y, bubbles: true}));
            })();
        """)
        assert js(window, "window.__guideShown") is True, "the resize guide never appeared while dragging"
        assert wait_until(lambda: pane_grows(window)[0] < before[0] - 0.05), \
            f"dragging the divider left should shrink the left pane: {before} → {pane_grows(window)}"

    check("dragging a divider resizes the panes", drags)

    def keyboard_resize():
        before = pane_grows(window)
        tile(window, ">")
        assert wait_until(lambda: abs(pane_grows(window)[1] - (before[1] + 0.05)) < 0.002), \
            f"`^w >` should grow the focused pane by 5%: {before} → {pane_grows(window)}"
        assert "%" in js(window, "document.querySelector('.resize-guide').textContent")

    check("`^w >` resizes the focused pane by 5%", keyboard_resize)

    def statement_lands_in_log():
        type_command(window, "R1.x = 5")
        assert wait_until(lambda: any("R1.x = 5" in t for t in log_texts(window))), \
            f"the statement never reached the log: {log_texts(window)}"
        selected = f'.view-log .c-table-row.selected[data-key="{ids["R1"]}"]'
        assert wait_until(lambda: count(window, selected) >= 1), "the statement did not select R1"
        assert wait_until(lambda: "sel " in js(window, "document.getElementById('topbar').textContent")), \
            "the top bar should show the selection"

    check("a statement lands in the log and selects its mote", statement_lands_in_log)

    def saves_layout():
        type_command(window, "layout save two")
        assert wait_until(lambda: texts(window, "#topbar .tab.active") == ["1:two"]), \
            f"`layout save two` should show an active tab, got {texts(window, '#topbar .tab')}"

    check("`layout save two` saves and activates the layout", saves_layout)

    def dirty_marks():
        tile(window, "<")
        assert wait_until(lambda: texts(window, "#topbar .tab.active") == ["1:two*"]), \
            "resizing after a save should mark the layout unsaved"
        assert "unsaved layout" in js(window, "document.getElementById('topbar').textContent")
        tile(window, ">")
        assert wait_until(lambda: texts(window, "#topbar .tab.active") == ["1:two"]), \
            "resizing back should clear the unsaved mark"

    check("drifting from the saved layout marks it unsaved", dirty_marks)
    check("the page raised no JavaScript errors", lambda: no_errors(window))
    return True


def history_has(window, command):
    press(window, ":")
    press(window, "ArrowUp")
    found = wait_until(lambda: command in texts(window, "#popup .c-popup-row .col-1"))
    rows = texts(window, "#popup .c-popup-row .col-1")
    press(window, "Escape")
    press(window, "Escape")
    return found, rows


def restored_checks(window, label):
    def restored():
        assert wait_until(lambda: count(window, ".pane") == 2, timeout=10), \
            f"{label}: expected the two-pane tree back, got {count(window, '.pane')} panes"
        assert wait_until(lambda: headers(window) == ["log", "log"]), headers(window)
        assert texts(window, "#topbar .tab.active") == ["1:two"], texts(window, "#topbar .tab")
        found, rows = history_has(window, "layout save two")
        assert found, f"{label}: history should hold `layout save two`, got {rows}"
        no_errors(window)

    check(f"the layout and history survive a {label}", restored)


def garden_checks(window):
    def cd_swaps():
        type_command(window, f"cd {OTHER_GARDEN}")
        assert wait_until(lambda: texts(window, "#topbar .breadcrumb") == [f"/ {OTHER_GARDEN}"]), \
            "`cd` did not change the breadcrumb"
        assert wait_until(lambda: count(window, ".pane") == 1), "the other garden should start on one pane"
        assert count(window, "#topbar .tab") == 0, "the other garden has no saved layouts"
        type_command(window, f"cd {GARDEN}")
        assert wait_until(lambda: texts(window, "#topbar .tab.active") == ["1:two"]), \
            "`cd` back should bring the first garden's layouts back"
        assert wait_until(lambda: count(window, ".pane") == 2), "and its two panes"
        no_errors(window)

    check("`cd` swaps the workspace to another garden and back", cd_swaps)


# ---------- driving ----------

def open_window(title):
    api = make_api(GARDEN)
    loaded = threading.Event()
    window = webview.create_window(title, url=str(FRONTEND_DIR / "index.html"), js_api=api,
                                   width=1280, height=800)
    api._window = window
    window.events.closed += api.shutdown
    window.events.loaded += loaded.set
    return window, loaded


def install_collector(window):
    js(window, ERROR_COLLECTOR)


def reload(window):
    js(window, "window.__beforeReload = true; setTimeout(() => location.reload(), 0);")
    assert wait_until(lambda: js(window, "window.__beforeReload !== true && !!document.querySelector('.pane')"),
                      timeout=10), "the page never came back from the reload"
    install_collector(window)


def drive(first, first_loaded):
    opened = [first]
    first_loaded.wait(timeout=15)
    install_collector(first)
    try:
        if not first_launch_checks(first):
            return
        time.sleep(0.8)  # let the debounced working-tree autosave land
        reload(first)
        restored_checks(first, "reload")

        # A fresh window on a fresh Api is the relaunch. The first window
        # closes only once the second is up, so the GUI loop keeps running.
        second, second_loaded = open_window("Mosaic smoke test (relaunch)")
        opened.append(second)
        assert second_loaded.wait(timeout=15), "the relaunched window never loaded"
        opened.remove(first)
        first.destroy()
        install_collector(second)
        restored_checks(second, "relaunch on a fresh Api")
        garden_checks(second)
        check("the relaunched page raised no JavaScript errors", lambda: no_errors(second))
    finally:
        for window in opened:
            window.destroy()


def main():
    seed()
    window, loaded = open_window("Mosaic smoke test")
    threading.Thread(target=drive, args=(window, loaded), daemon=True).start()
    # The same settings console/app.py uses, choose_port() included.
    webview.start(
        http_server=True,
        http_port=choose_port("console", DEFAULT_HTTP_PORT),
        private_mode=False,
        storage_path=str(_TMP / "webview_data"),
    )
    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFailed:")
        for name, _, err in failed:
            print(f"  - {name}: {err}")
    sys.exit(1 if failed or not results else 0)


if __name__ == "__main__":
    main()
