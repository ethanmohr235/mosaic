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
whole workspace. Once the Api has its `monolith` namespace (U2) it
also drives the find table: a table appears, click + j j + shift-click
selects three rows, `set @sel` writes them and one `undo` reverts them,
a header click rewrites the pane's command, an inline edit writes, and
the sorted find pane survives a relaunch. Without the namespace those
checks are skipped (and say so). U3 drives the props pane and the `p`
float; U4 drives the plot pane: `mo xie`, a solved curve the cursor
reads, zoom / log / hide rewriting the line, a knob that never writes,
`plot save` + `undo`, `plot export csv|png`, and a relaunch. Everything is driven the way a person does it -- key
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
from core.motes import create_mote, get_mote  # noqa: E402
from core.relationships import create_relationship  # noqa: E402
from core.rules import APPLIES_TO, FREEDOMS_KEY  # noqa: E402
from core.species import set_species  # noqa: E402
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
        "`:` did not move focus to the command line; focus was on " + js(window, """(() => {
            const a = document.activeElement; const pane = a && a.closest('.pane');
            return `${a ? a.tagName + '.' + a.className : 'nothing'} in ${pane ? pane.querySelector('.c-head').textContent : 'no pane'}`;
        })()""")
    js(window, f"""
        (() => {{
          const input = document.activeElement;
          input.value = {json.dumps(line)};
          input.setSelectionRange(input.value.length, input.value.length);
          input.dispatchEvent(new Event('input', {{bubbles: true}}));
        }})();
    """)
    press(window, "Enter", alt=alt)
    # The line runs asynchronously, and the command line keeps focus until
    # it has finished (it blurs then). A key pressed sooner lands in the
    # command line and is lost when it blurs -- so wait for the run to end.
    # A line that failed keeps the command line focused: that ends the wait
    # by timing out, and the next `:` finds it already there.
    wait_until(lambda: not js(window, "!!document.activeElement && !!document.activeElement.closest('#cmdline')"),
               timeout=3)


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
        # U2: a species and four instances for the find table.
        ids["chore"] = create_mote(conn, {"name": "chore"})
        for i in range(4):
            ids[f"c{i}"] = create_mote(conn, {"name": f"chore{i}", "status": "open"})
            create_relationship(conn, ids[f"c{i}"], ids["chore"], "instance_of")
        # U4: a circuit whose current is voltage / (r_a + r_b) = voltage / 300.
        divider = create_mote(conn, {"name": "Divider", FREEDOMS_KEY: "current"})
        rule = create_mote(conn, {"name": "series", "equation": "current * (r_a + r_b) == voltage"})
        create_relationship(conn, rule, divider, APPLIES_TO)
        ids["C1"] = create_mote(conn, {"name": "C1", "voltage": 10, "r_a": 100, "r_b": 200})
        set_species(conn, ids["C1"], divider)
        # U4 fit: points measured with r_a = 50, i.e. I = V / 250.
        for i, v in enumerate([5, 10, 20, 40]):
            create_mote(conn, {"name": f"meas{i}", "V": v, "I": v / 250})
    finally:
        conn.close()


def prop(mote_id, key):
    conn = get_connection(GARDEN)
    try:
        mote = get_mote(conn, mote_id)
        return mote.properties.get(key) if mote else None
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


# ---------- U2: the find table (monolith:table) ----------

TABLE_ROW = ".pane.focused .view-table .c-table-row"


def table_keys(window, selector=TABLE_ROW):
    return json.loads(js(window, f"""
        JSON.stringify(Array.from(document.querySelectorAll({json.dumps(selector)})).map(r => r.dataset.key))
    """))


def click_row(window, index, shift=False, ctrl=False, col=0):
    js(window, f"""
        (() => {{
          const n = document.querySelector('{TABLE_ROW}[data-index="{index}"] > .c-cell[data-col="{col}"]');
          n.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, button: 0}}));
          n.dispatchEvent(new MouseEvent('click', {{bubbles: true, button: 0,
            shiftKey: {json.dumps(shift)}, ctrlKey: {json.dumps(ctrl)}}}));
        }})();
    """)


def u2_checks(window):
    chores = {ids[f"c{i}"] for i in range(4)}

    def shows_table():
        type_command(window, "find type:chore")
        assert wait_until(lambda: set(table_keys(window)) >= chores, timeout=8), \
            f"`find type:chore` should list the four chores, got {table_keys(window)}"
        no_errors(window)

    check("U2: `find` shows a table", shows_table)
    if not results[-1][1]:
        return

    def selects_range():
        click_row(window, 0)
        press(window, "j")
        press(window, "j")
        click_row(window, 2, shift=True)
        chosen = table_keys(window, TABLE_ROW + ".selected")
        assert chosen == table_keys(window)[:3], f"click, j j, shift-click should select 3 rows, got {chosen}"
        assert wait_until(lambda: "+2" in js(window, "document.getElementById('topbar').textContent")), \
            "the top bar should read `sel X +2`"

    check("U2: j/k plus a shift-click select three rows", selects_range)

    def batch_set_and_undo():
        chosen = table_keys(window, TABLE_ROW + ".selected")
        type_command(window, "set @sel status done")
        assert wait_until(lambda: all(prop(i, "status") == "done" for i in chosen)), \
            f"`set @sel status done` should write all three: {[prop(i, 'status') for i in chosen]}"
        type_command(window, "undo")
        assert wait_until(lambda: all(prop(i, "status") == "open" for i in chosen)), \
            "one `undo` should revert the whole batch"

    check("U2: `set @sel` writes the selection and one undo reverts it", batch_set_and_undo)

    def header_sort():
        before = texts(window, ".pane.focused .c-head-command")
        js(window, "document.querySelector('.pane.focused .view-table .c-table-head .c-cell[data-col=\"0\"]').click()")
        assert wait_until(lambda: "--sort" in " ".join(texts(window, ".pane.focused .c-head-command"))), \
            f"a header click should rewrite the pane command: {before} → {texts(window, '.pane.focused .c-head-command')}"
        assert wait_until(lambda: count(window, ".pane.focused .view-table .c-table-head .c-cell") > 0)

    check("U2: a header click sorts by rewriting the pane command", header_sort)

    def inline_edit():
        press(window, "Escape")  # clear the selection: the edit targets one row
        keys = table_keys(window)
        target = keys[-1]
        click_row(window, len(keys) - 1)
        press(window, "Escape")
        press(window, "i")
        assert wait_until(lambda: count(window, ".view-table .c-cell-input") == 1), "`i` should open an editor"
        js(window, """
            (() => { const i = document.querySelector('.view-table .c-cell-input');
                     i.value = 'renamed chore'; i.dispatchEvent(new Event('input', {bubbles: true})); })();
        """)
        press(window, "Enter")
        assert wait_until(lambda: prop(target, "name") == "renamed chore"), \
            f"the inline edit should write the name, got {prop(target, 'name')}"
        no_errors(window)

    check("U2: an inline cell edit writes through `set`", inline_edit)


# ---------- U3: the props pane (mote:props) and the `p` float ----------

PROPS = ".pane .view-props"


def props_title(window, root=PROPS):
    got = texts(window, f"{root} .props-title")
    return got[0].lstrip("■ ").strip() if got else ""  # past the species swatch


def props_keys(window, root=PROPS):
    return texts(window, f"{root} .props-row .props-key")


def type_in_editor(window, text, root=PROPS):
    js(window, f"""
        (() => {{ const i = document.querySelector({json.dumps(root + ' .c-cell-input')});
                 i.value = {json.dumps(text)}; i.dispatchEvent(new Event('input', {{bubbles: true}})); }})();
    """)
    press(window, "Enter")


def props_cursor_to(window, key, root=PROPS):
    """`g` to the top, then j until the cursor row is `key`'s."""
    press(window, "g")
    for _ in range(40):
        current = texts(window, f"{root} .props-row.cursor .props-key")
        if current and current[0] == key:
            return True
        press(window, "j")
    return False


def u3_checks(window):
    def follows_selection():
        type_command(window, "find type:chore --sort name")
        assert wait_until(lambda: len(table_keys(window)) >= 4, timeout=8)
        click_row(window, 0)
        type_command(window, "open @sel", alt=True)
        assert wait_until(lambda: props_title(window).startswith("chore0"), timeout=8), \
            f"the props pane should show the clicked row, got {props_title(window)!r}"
        # Select another row by clicking it in the table pane.
        js(window, f"""
            (() => {{
              const n = document.querySelector('.view-table .c-table-row[data-index="1"] > .c-cell[data-col="0"]');
              n.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, button: 0}}));
              n.dispatchEvent(new MouseEvent('click', {{bubbles: true, button: 0}}));
            }})();
        """)
        assert wait_until(lambda: props_title(window).startswith("chore1")), \
            f"`open @sel` should follow the selection, got {props_title(window)!r}"
        no_errors(window)

    check("U3: `open @sel` follows the selection", follows_selection)
    if not results[-1][1]:
        return
    target = ids["c1"]

    def focus_props():
        js(window, "document.querySelector('.pane .view-props').closest('.pane')"
                   ".dispatchEvent(new MouseEvent('mousedown', {bubbles: true, button: 0}))")
        assert wait_until(lambda: count(window, ".pane.focused .view-props") == 1)

    def add_rename_and_undo():
        focus_props()
        press(window, "a")
        assert wait_until(lambda: count(window, f"{PROPS} .c-cell-input") == 1), "`a` should open an editor"
        type_in_editor(window, "color red")
        assert wait_until(lambda: prop(target, "color") == "red"), "adding a property should write it"
        assert wait_until(lambda: "color" in props_keys(window))
        assert props_cursor_to(window, "color"), f"no color row in {props_keys(window)}"
        press(window, "r")
        assert wait_until(lambda: count(window, f"{PROPS} .c-cell-input") == 1), "`r` should open an editor"
        type_in_editor(window, "hue")
        assert wait_until(lambda: prop(target, "hue") == "red" and prop(target, "color") is None), \
            "renaming should move the value"
        type_command(window, "undo")
        assert wait_until(lambda: prop(target, "color") == "red" and prop(target, "hue") is None), \
            "one undo should take back the rename"
        no_errors(window)

    check("U3: add, rename and undo from the props pane", add_rename_and_undo)

    def link_from_props():
        focus_props()
        press(window, "l")
        assert wait_until(lambda: count(window, f"{PROPS} .c-cell-input") == 1), "`l` should open an editor"
        type_in_editor(window, "blocks chore2")
        assert wait_until(lambda: any("blocks" in t for t in texts(window, f"{PROPS} .props-rel-key"))), \
            "a link row should appear"
        no_errors(window)

    check("U3: `l` links to another mote", link_from_props)

    def delete_and_follow():
        focus_props()
        assert props_cursor_to(window, "color"), f"no color row in {props_keys(window)}"
        press(window, "d")
        assert prop(target, "color") == "red", "one `d` only arms the delete"
        press(window, "d")
        assert wait_until(lambda: prop(target, "color") is None), "`d d` should delete the property"
        press(window, "G")  # the bottom rows are the links
        for _ in range(10):
            if any("blocks" in t for t in texts(window, f"{PROPS} .props-row.cursor .props-rel-key")):
                break
            press(window, "k")
        press(window, "Enter")
        assert wait_until(lambda: props_title(window).startswith("chore2")), \
            f"⏎ on a link row should walk to it, got {props_title(window)!r}"
        assert wait_until(lambda: "sel chore2" in js(window, "document.getElementById('topbar').textContent")), \
            "the top bar should name the mote walked to"
        no_errors(window)

    check("U3: `d d` deletes and ⏎ on a link walks there", delete_and_follow)

    def float_toggles():
        focus_props()
        shown = props_title(window)
        press(window, "x")  # close the props pane: the float is for layouts without one
        assert wait_until(lambda: count(window, ".pane .view-props") == 0)
        press(window, "p")
        assert wait_until(lambda: count(window, ".props-float:not([hidden]) .view-props") == 1), \
            "`p` should open the float"
        assert wait_until(lambda: props_title(window, ".props-float") == shown), \
            f"the float should show the selection, {shown!r}, got {props_title(window, '.props-float')!r}"
        press(window, "Escape")
        assert wait_until(lambda: count(window, ".props-float:not([hidden])") == 0), "`esc` should close it"
        type_command(window, "open @sel", alt=True)  # back, for the relaunch check
        assert wait_until(lambda: count(window, ".pane .view-props") == 1)
        no_errors(window)

    check("U3: `p` opens the props float and `esc` closes it", float_toggles)

    def mo_is_a_verb():
        press(window, ":")
        js(window, """
            (() => { const i = document.activeElement; i.value = 'mo t';
                     i.setSelectionRange(4, 4); i.dispatchEvent(new Event('input', {bubbles: true})); })();
        """)
        assert wait_until(lambda: texts(window, "#cmdline .c-input-mo") == ["mo"]), \
            "typing `mo ` should draw the [mo] box"
        assert texts(window, "#cmdline .cmd-prompt") == ["$"], "the command line's prompt is `$`"
        press(window, "Escape")
        type_command(window, "mo te", alt=True)
        assert wait_until(lambda: "open @sel" in headers(window)), "`mo te` should open the props view"
        no_errors(window)

    check("U3: `mo` is the verb that opens an app, drawn as the [mo] box", mo_is_a_verb)


def u3_restored_checks(window):
    def restored():
        assert wait_until(lambda: any(h == "open @sel" for h in headers(window)), timeout=10), \
            f"the props pane should survive a relaunch, got {headers(window)}"
        no_errors(window)

    check("U3: a layout with an `open @sel` pane survives a relaunch", restored)


def u2_restored_checks(window):
    def restored():
        assert wait_until(lambda: any(h.startswith("find") and "--sort" in h for h in headers(window)), timeout=10), \
            f"the sorted find pane should survive a relaunch, got {headers(window)}"
        assert wait_until(lambda: count(window, ".view-table .c-table-row") >= 4)
        no_errors(window)

    check("U2: a layout with a `find …` pane survives a relaunch", restored)


# ---------- U4: the plot pane ----------

PLOT = "plot C1.current vs C1.voltage 0:300 --steps 30"


def plot_command(window):
    """The plot pane's command, from its header."""
    return next((h for h in headers(window) if h.startswith("plot")), "")


def plot_status(window):
    return js(window, "(() => { const s = document.querySelector('.plot-status'); return s ? s.textContent : ''; })()")


def saved_plots():
    conn = get_connection(GARDEN)
    try:
        from core.motes import list_motes
        return [m.properties.get("name") for m in list_motes(conn) if "plot" in m.properties]
    finally:
        conn.close()


def u4_checks(window):
    def mo_xie_opens():
        type_command(window, "mo xie", alt=True)
        assert wait_until(lambda: count(window, ".view-plot .plot-empty") == 1), "`mo xie` should open an empty plot pane"
        no_errors(window)

    check("U4: `mo xie` opens the plot pane", mo_xie_opens)

    def draws_a_curve():
        type_command(window, PLOT)
        assert wait_until(lambda: plot_command(window) == PLOT), f"header should be the line, got {headers(window)}"
        assert wait_until(lambda: count(window, ".view-plot path.plot-series") >= 1), "no curve drawn"
        assert wait_until(lambda: "31 solves" in plot_status(window)), f"status: {plot_status(window)!r}"
        # The cursor reads the curve: x = 0 then ten steps on, x = 100 -> current 1/3.
        press(window, "l")
        press(window, "L")
        assert wait_until(lambda: "x 100" in plot_status(window) and "current 0.333" in plot_status(window)), \
            f"readout: {plot_status(window)!r}"
        no_errors(window)

    check("U4: a plot line draws the solved curve, and the cursor reads it", draws_a_curve)

    def zoom_log_hide():
        press(window, "Escape")
        press(window, "l")      # x = 0
        press(window, "z")      # mark
        press(window, "L")      # x = 100
        press(window, "z")      # zoom to 0:100
        assert wait_until(lambda: " 0:100 " in plot_command(window)), f"zoom should rewrite the range: {plot_command(window)}"
        press(window, "y")
        assert wait_until(lambda: plot_command(window).endswith("--log")), plot_command(window)
        press(window, "y")
        press(window, "t")
        assert wait_until(lambda: "--hide C1.current" in plot_command(window)), plot_command(window)
        assert wait_until(lambda: count(window, ".view-plot path.plot-series") == 0)
        press(window, "t")
        assert wait_until(lambda: "--hide" not in plot_command(window) and count(window, ".view-plot path.plot-series") >= 1)
        no_errors(window)

    check("U4: zoom, log y and hiding a series rewrite the command", zoom_log_hide)

    def knob_moves_without_writing():
        type_command(window, PLOT + " --set C1.r_a=100")
        assert wait_until(lambda: count(window, ".plot-knob") == 1), "the --set knob should be in the rail"
        press(window, "+")
        assert wait_until(lambda: "--set C1.r_a=107.5" in plot_command(window)), plot_command(window)
        assert prop(ids["C1"], "r_a") == 100, "a knob must never write to the garden"
        assert wait_until(lambda: count(window, ".plot-rail-section .plot-num") >= 1)
        no_errors(window)

    check("U4: a knob moves the curve and never writes", knob_moves_without_writing)

    def save_and_export():
        type_command(window, "plot save Load")
        assert wait_until(lambda: saved_plots() == ["Load"]), f"saved plots: {saved_plots()}"
        type_command(window, "undo")
        assert wait_until(lambda: saved_plots() == []), "undo should take the saved plot back"
        csv_path = _TMP / "plot.csv"
        png_path = _TMP / "plot.png"
        type_command(window, f"plot export csv {csv_path}")
        assert wait_until(lambda: csv_path.exists()), "csv export wrote nothing"
        assert len(csv_path.read_text().splitlines()) == 32
        type_command(window, f"plot export png {png_path}")
        assert wait_until(lambda: png_path.exists() and png_path.read_bytes().startswith(b"\x89PNG"), timeout=10), \
            "png export wrote no PNG"
        no_errors(window)

    check("U4: `plot save` writes an undoable saved plot; `plot export` writes csv and png", save_and_export)


FIT = "fit C1.current vs C1.voltage --data I vs V --free C1.r_a"


def rail_text(window):
    return js(window, "[...document.querySelectorAll('.view-plot .plot-rail')].map(r => r.textContent).join(' | ')")


def fit_checks(window):
    def fit_pane():
        type_command(window, FIT, alt=True)
        assert wait_until(lambda: FIT in headers(window)), f"headers: {headers(window)}"
        assert wait_until(lambda: count(window, ".view-plot circle.plot-point") == 4), "the four measured points"
        assert wait_until(lambda: count(window, ".view-plot path.plot-series") >= 1), "the fitted curve"
        assert wait_until(lambda: "C1.r_a" in rail_text(window) and "50" in rail_text(window)), rail_text(window)
        assert prop(ids["C1"], "r_a") == 100, "a fit pane must not write"
        no_errors(window)

    check("fit: a fit line shows the points, the fitted curve and r_a = 50", fit_pane)

    def apply_and_undo():
        press(window, "a")
        assert wait_until(lambda: abs((prop(ids["C1"], "r_a") or 0) - 50) < 1e-6), "`a` should apply the fit"
        assert prop(ids["C1"], "source") == {"r_a": {"kind": "fitted", "note": FIT}}
        type_command(window, "undo")
        assert wait_until(lambda: prop(ids["C1"], "r_a") == 100), "undo should take the fit back"
        no_errors(window)

    check("fit: `a` applies value and source as one undoable write", apply_and_undo)


def topbar(window):
    return js(window, "document.getElementById('topbar').textContent")


def sim_text(window):
    return js(window, "[...document.querySelectorAll('.view-motion .c-table')].map(t => t.textContent).join(' | ')")


def u5_checks(window):
    def mo_tion_opens_three_panes():
        type_command(window, "mo tion", alt=True)
        assert wait_until(lambda: {"scenarios", "sim", "rule @sel"} <= set(headers(window))), \
            f"headers: {headers(window)}"
        assert wait_until(lambda: "C1.current" in sim_text(window)), sim_text(window)
        assert wait_until(lambda: "tick 0/49" in topbar(window)), topbar(window)
        no_errors(window)

    check("U5: `mo tion` opens scenarios | sim | rule @sel, and the sim pane owns the playhead",
          mo_tion_opens_three_panes)

    def playhead_moves():
        press(window, "]")
        assert wait_until(lambda: "tick 10/49" in topbar(window)), topbar(window)
        press(window, ",")
        assert wait_until(lambda: "tick 9/49" in topbar(window)), topbar(window)
        type_command(window, "step 30")
        assert wait_until(lambda: "tick 30/49" in topbar(window)), topbar(window)
        assert wait_until(lambda: "@30" in sim_text(window)), sim_text(window)
        no_errors(window)

    check("U5: `] , ` and `step` move the shared playhead", playhead_moves)

    def scenario_and_vs():
        type_command(window, "set --scenario hot C1.voltage 30")
        assert prop(ids["C1"], "voltage") == 10, "a scenario edit must not touch the garden"
        assert wait_until(lambda: "hot" in js(window, "document.querySelector('.view-table') ? "
                                               "[...document.querySelectorAll('.view-table')].map(t => t.textContent).join() : ''")), \
            "the scenarios pane should list hot"
        type_command(window, "sim --scenario hot")
        assert wait_until(lambda: "varied" in sim_text(window)), sim_text(window)
        press(window, "d")
        assert wait_until(lambda: "sim --scenario hot --vs garden" in headers(window)), headers(window)
        assert wait_until(lambda: "varied" in sim_text(window) and "0.1" in sim_text(window)
                          and "0.0333" in sim_text(window)), sim_text(window)
        no_errors(window)

    check("U5: `set --scenario` writes an override; `d` compares against the garden", scenario_and_vs)

    def rule_follows_the_row():
        press(window, "g")
        press(window, "Enter")
        assert wait_until(lambda: "series" in js(window, "[...document.querySelectorAll('.motion-rule-name')]"
                                                        ".map(n => n.textContent).join()")), "the rule pane should follow C1"
        no_errors(window)

    check("U5: ⏎ on a row selects its mote, and `rule @sel` shows the rules on it", rule_follows_the_row)

    def solve_and_live():
        type_command(window, "solve")
        assert wait_until(lambda: abs((prop(ids["C1"], "current") or 0) - 10 / 300) < 1e-9), "solve should write"
        type_command(window, "undo")
        assert wait_until(lambda: prop(ids["C1"], "current") is None), "undo should take the solve back"
        type_command(window, "solve --live on")
        assert wait_until(lambda: "live" in topbar(window)), topbar(window)
        type_command(window, "set C1.voltage 20")
        assert wait_until(lambda: abs((prop(ids["C1"], "current") or 0) - 20 / 300) < 1e-9), "live should solve"
        type_command(window, "undo")
        assert wait_until(lambda: prop(ids["C1"], "voltage") == 10 and prop(ids["C1"], "current") is None), \
            "one undo takes back the edit and its live solve"
        type_command(window, "solve --live off")
        assert wait_until(lambda: "live" not in topbar(window).replace("linked", "")), topbar(window)
        no_errors(window)

    check("U5: `solve` is one undo step; `solve --live on` solves after each edit", solve_and_live)


NOTE_ID = {}


def seed_note():
    conn = get_connection(GARDEN)
    try:
        NOTE_ID["Homework"] = create_mote(conn, {
            "name": "Homework", "modoc": "The divider \\mote{C1} runs at \\val{C1.voltage}."})
    finally:
        conn.close()


def read_text(window):
    return js(window, "[...document.querySelectorAll('.view-doc .doc')].map(d => d.textContent).join(' | ')")


def focused_text(window):
    return js(window, "(() => { const t = document.querySelector('.pane.focused textarea.c-editor-text');"
                      " return t ? t.value : null; })()")


def focus_pane(window, command):
    """Click the pane whose header is `command` (mouse parity: a click
    focuses a pane)."""
    js(window, f"""(() => {{
        const pane = [...document.querySelectorAll('.pane')]
            .find(p => p.querySelector('.c-head-command').textContent === {json.dumps(command)});
        pane.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, button: 0}}));
    }})()""")


def focused_head(window):
    return js(window, "document.querySelector('.pane.focused .c-head').textContent")


def type_into_editor(window, text):
    js(window, f"""(() => {{ const t = document.querySelector('.pane.focused textarea.c-editor-text');
        t.value = {json.dumps(text)}; t.dispatchEvent(new Event('input', {{bubbles: true}})); }})()""")


def u6_checks(window):
    def mo_ra_opens_three_panes():
        seed_note()
        type_command(window, "refresh")
        type_command(window, "mo ra", alt=True)
        assert wait_until(lambda: {"notes", "edit @sel", "read @sel"} <= set(headers(window))), headers(window)
        assert "edit @sel" in focused_head(window), "mo ra focuses the editor"
        focus_pane(window, "notes")
        assert wait_until(lambda: "Homework" in js(window, "document.querySelector('.pane.focused').textContent")), \
            "the notes pane should list Homework; focused: " + focused_head(window)
        press(window, "g")
        press(window, "Enter")
        assert wait_until(lambda: "The divider C1 runs at 10" in read_text(window)), read_text(window)
        no_errors(window)

    check("U6: `mo ra` opens notes | edit @sel | read @sel, and ⏎ on a note shows it", mo_ra_opens_three_panes)

    def typing_follows_and_saves_once():
        focus_pane(window, "edit @sel")
        assert wait_until(lambda: (focused_text(window) or "").startswith("The divider")), focused_text(window)
        press(window, "i")
        type_into_editor(window, "The circuit \\mote{C1} runs at \\val{C1.voltage}.")
        assert wait_until(lambda: "The circuit C1" in read_text(window)), "the read pane should follow the draft"
        press(window, "f", ctrl=True)
        js(window, """(() => { const i = document.querySelector('.pane.focused .c-find-input');
            i.value = 'runs at'; i.dispatchEvent(new Event('input', {bubbles: true})); })()""")
        js(window, """(() => { const i = document.querySelectorAll('.pane.focused .c-find-input')[1];
            i.value = 'holds'; i.focus(); })()""")
        press(window, "Enter", ctrl=True)
        assert wait_until(lambda: "holds" in (focused_text(window) or "")), focused_text(window)
        press(window, "Escape")  # closes the find bar
        press(window, "Escape")  # stops editing: saves
        assert wait_until(lambda: "holds" in (prop(NOTE_ID["Homework"], "modoc") or "")), \
            prop(NOTE_ID["Homework"], "modoc")
        type_command(window, "undo")
        assert wait_until(lambda: (prop(NOTE_ID["Homework"], "modoc") or "").startswith("The divider")), \
            "one undo takes back the whole editing session"
        no_errors(window)

    check("U6: the read pane follows typing; find/replace; esc saves one undoable session",
          typing_follows_and_saves_once)

    def worksheet_applies_on_esc():
        type_command(window, "worksheet", alt=True)
        assert wait_until(lambda: focused_text(window) is not None)
        press(window, "i")
        type_into_editor(window, (focused_text(window) or "") + "\nzz.q = 7 m")
        assert wait_until(lambda: count(window, ".pane.focused .ws-given") >= 1)
        press(window, "Escape")
        assert wait_until(lambda: prop(_named("zz"), "q") == 7 if _named("zz") else False), "esc should apply"
        type_command(window, "undo")
        assert wait_until(lambda: _named("zz") is None), "undo takes the worksheet back"
        no_errors(window)

    check("U6: the worksheet applies on esc as one undo step", worksheet_applies_on_esc)


def _named(name):
    conn = get_connection(GARDEN)
    try:
        from core.motes import list_motes
        found = [m.id for m in list_motes(conn) if m.properties.get("name") == name]
        return found[0] if found else None
    finally:
        conn.close()


CAL = {}


def seed_calendar():
    from datetime import date
    today = date.today().isoformat()
    CAL["today"] = today
    conn = get_connection(GARDEN)
    try:
        CAL["lab"] = create_mote(conn, {"name": "Lab7", "date": f"{today}T13:00/PT1H"})
        CAL["standup"] = create_mote(conn, {"name": "Standup",
                                            "date": f"{today}T08:00/PT15M\nRRULE:FREQ=DAILY;COUNT=10"})
    finally:
        conn.close()


def click_agenda_row(window, name):
    js(window, f"""(() => {{
        const row = [...document.querySelectorAll('.view-agenda .c-table-row')]
            .find(r => r.textContent.includes({json.dumps(name)}));
        row.dispatchEvent(new MouseEvent('click', {{bubbles: true, button: 0}}));
    }})()""")


def mote_count():
    conn = get_connection(GARDEN)
    try:
        from core.motes import list_motes
        return len(list_motes(conn))
    finally:
        conn.close()


def u7_checks(window):
    def mo_nastery_opens_agenda_and_week():
        seed_calendar()
        type_command(window, "refresh")
        type_command(window, "mo nastery", alt=True)
        assert wait_until(lambda: {"agenda", "week"} <= set(headers(window))), headers(window)
        assert wait_until(lambda: "Lab7" in js(window, "document.querySelector('.view-agenda').textContent")), \
            "the agenda should list Lab7"
        assert wait_until(lambda: count(window, ".view-week .wk-event") >= 2), "the week grid should draw events"
        no_errors(window)

    check("U7: `mo nastery` opens agenda | week with today's events", mo_nastery_opens_agenda_and_week)

    def keys_move_through_set_and_undo():
        focus_pane(window, "agenda")
        click_agenda_row(window, "Lab7")
        press(window, "J")
        assert wait_until(lambda: prop(CAL["lab"], "date") == f"{CAL['today']}T14:00/PT1H"), prop(CAL["lab"], "date")
        type_command(window, "undo")
        assert wait_until(lambda: prop(CAL["lab"], "date") == f"{CAL['today']}T13:00/PT1H"), "undo moves it back"
        no_errors(window)

    check("U7: J moves an event an hour as a set line, and undo takes it back", keys_move_through_set_and_undo)

    def dragging_moves_and_resizes():
        def drag(dy, edge):
            js(window, f"""(() => {{
                const ev = [...document.querySelectorAll('.view-week .wk-event')].find(e => e.textContent === 'Lab7');
                const r = ev.getBoundingClientRect();
                const x = r.left + 4, y = {"r.bottom - 2" if edge else "r.top + 4"};
                ev.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, button: 0, clientX: x, clientY: y}}));
                window.dispatchEvent(new MouseEvent('mousemove', {{clientX: x, clientY: y + {dy}}}));
                window.dispatchEvent(new MouseEvent('mouseup', {{clientX: x, clientY: y + {dy}}}));
            }})()""")
        assert wait_until(lambda: count(window, ".view-week .wk-event") >= 2)
        drag(72, edge=False)  # two hours down
        assert wait_until(lambda: prop(CAL["lab"], "date") == f"{CAL['today']}T15:00/PT1H"), prop(CAL["lab"], "date")
        drag(36, edge=True)   # an hour longer
        assert wait_until(lambda: prop(CAL["lab"], "date") == f"{CAL['today']}T15:00/PT2H"), prop(CAL["lab"], "date")
        type_command(window, "undo")
        type_command(window, "undo")
        assert wait_until(lambda: prop(CAL["lab"], "date") == f"{CAL['today']}T13:00/PT1H"), prop(CAL["lab"], "date")
        no_errors(window)

    check("U7: dragging an event moves it, dragging its edge resizes it", dragging_moves_and_resizes)

    def a_recurring_event_asks():
        before = mote_count()
        focus_pane(window, "agenda")
        click_agenda_row(window, "Standup")
        press(window, "L")
        assert wait_until(lambda: "repeats" in js(window, "document.querySelector('.view-agenda').textContent")), \
            "a recurring event should ask first"
        press(window, "t")
        assert wait_until(lambda: mote_count() == before + 1), "t detaches this one into a mote of its own"
        assert "EXDATE" in prop(CAL["standup"], "date")
        type_command(window, "undo")
        assert wait_until(lambda: mote_count() == before and "EXDATE" not in prop(CAL["standup"], "date"))
        no_errors(window)

    check("U7: moving one occurrence of a series asks, and t detaches it (one undo)", a_recurring_event_asks)

    def paging_and_follow():
        focus_pane(window, "week")
        press(window, "]")
        assert wait_until(lambda: any(h.startswith("week 20") for h in headers(window))), headers(window)
        press(window, "t")
        assert wait_until(lambda: "week" in headers(window)), headers(window)
        focus_pane(window, "agenda")
        click_agenda_row(window, "Lab7")
        press(window, "o")
        assert wait_until(lambda: f"open #{CAL['lab']}" in headers(window)), headers(window)
        type_command(window, "month", alt=True)
        assert wait_until(lambda: count(window, ".view-month .mo-cell") == 42)
        no_errors(window)

    check("U7: [ ] page and t comes back; o follows; month draws six weeks", paging_and_follow)


GRAPH = "document.querySelector('.view-graph canvas').graph"


def graph_has(window, *mote_ids):
    return js(window, f"(() => {{ const g = {GRAPH}; return !!g && {json.dumps(list(mote_ids))}"
                      f".every(id => g.nodes.has(id)); }})()")


def graph_names(window):
    return js(window, f"(() => {{ const g = {GRAPH}; return g ? [...g.byId.values()].map(m => `${{m.name}}:${{m.id.slice(0, 6)}}`) : null; }})()")


def command_line_text(window):
    return js(window, "document.querySelector('#cmdline .c-input-native').value")


def graph_drag(window, from_id, to_id=None, dx=0, dy=0, ctrl=False):
    """Press on one node and let go on another (or `dx`,`dy` px away)."""
    js(window, f"""(() => {{
        const g = {GRAPH};
        const a = g.screenPositionOf({json.dumps(from_id)});
        const b = {json.dumps(to_id)} ? g.screenPositionOf({json.dumps(to_id)}) : {{x: a.x + {dx}, y: a.y + {dy}}};
        const opts = (p) => ({{bubbles: true, button: 0, clientX: p.x, clientY: p.y, ctrlKey: {json.dumps(ctrl)}}});
        g.canvas.dispatchEvent(new MouseEvent('mousedown', opts(a)));
        window.dispatchEvent(new MouseEvent('mousemove', opts({{x: (a.x + b.x) / 2, y: (a.y + b.y) / 2}})));
        window.dispatchEvent(new MouseEvent('mousemove', opts(b)));
        window.dispatchEvent(new MouseEvent('mouseup', opts(b)));
    }})()""")


def u8_checks(window):
    graph_pane = "graph @sel --depth 2"

    def mo_narch_follows_the_selection():
        type_command(window, "mo narch", alt=True)
        assert wait_until(lambda: {"find", graph_pane, "open @sel"} <= set(headers(window))), headers(window)
        type_command(window, "chore0")
        assert wait_until(lambda: graph_has(window, ids["c0"], ids["chore"], ids["c1"])), \
            f"the graph should draw chore0's neighbourhood, got {graph_names(window)}"
        assert not graph_has(window, ids["R1"]), "R1 is not two hops from chore0"
        no_errors(window)

    check("U8: `mo narch` opens find | graph | props, and the graph follows the selection",
          mo_narch_follows_the_selection)

    def keys_walk_select_and_rewrite_depth():
        focus_pane(window, graph_pane)
        press(window, "l")
        assert wait_until(lambda: js(window, f"{GRAPH}.cursor") == ids["chore"]), "a walk reaches the species"
        press(window, "Enter")
        assert wait_until(lambda: js(window, f"{GRAPH}.primary") == ids["chore"]), "⏎ selects the cursor"
        press(window, "]")
        assert wait_until(lambda: "graph @sel --depth 3" in headers(window)), headers(window)
        press(window, "[")
        assert wait_until(lambda: graph_pane in headers(window)), headers(window)
        no_errors(window)

    check("U8: h j k l walk, ⏎ selects, [ ] rewrite the depth", keys_walk_select_and_rewrite_depth)

    def gestures_become_commands():
        assert wait_until(lambda: graph_has(window, ids["c0"], ids["c1"])), \
            f"chore0 and chore1 should be drawn, got {graph_names(window)} under {headers(window)}"
        focus_pane(window, graph_pane)
        graph_drag(window, ids["c0"], ids["c1"], ctrl=True)
        assert wait_until(lambda: command_line_text(window) == "chore0 ->  -> chore1"), \
            f"{command_line_text(window)!r} on {graph_names(window)}, focus {focused_head(window)}"
        press(window, "Escape")
        press(window, "Escape")
        focus_pane(window, graph_pane)
        press(window, "d")
        press(window, "d")
        assert wait_until(lambda: command_line_text(window) == "delete chore"), command_line_text(window)
        press(window, "Escape")
        press(window, "Escape")
        no_errors(window)

    check("U8: ctrl+drag prefills a link, d d prefills a delete", gestures_become_commands)

    def dragging_pins_and_u_unpins():
        from console import state as console_state
        focus_pane(window, graph_pane)
        graph_drag(window, ids["c2"], dx=60, dy=40)
        assert wait_until(lambda: ids["c2"] in console_state.load(GARDEN).pins), "a dragged node is pinned"
        assert js(window, f"{GRAPH}.nodes.get({json.dumps(ids['c2'])}).pinned")
        press(window, "U")
        assert wait_until(lambda: not console_state.load(GARDEN).pins), "U unpins every node"
        press(window, "o")
        assert wait_until(lambda: f"open #{ids['chore']}" in headers(window)), headers(window)
        no_errors(window)

    check("U8: dragging a node pins it (beside the garden), U unpins, o opens", dragging_pins_and_u_unpins)


def u4_restored_checks(window):
    def restored():
        assert wait_until(lambda: plot_command(window).startswith(PLOT), timeout=10), \
            f"the plot pane should survive a relaunch, got {headers(window)}"
        assert wait_until(lambda: count(window, ".view-plot path.plot-series") >= 1)
        no_errors(window)

    check("U4: a layout with a plot pane survives a relaunch", restored)


# ---------- driving ----------

def open_window(title):
    api = make_api(GARDEN)
    loaded = threading.Event()
    window = webview.create_window(title, url=str(FRONTEND_DIR / "index.html"), js_api=api,
                                   width=1280, height=800)
    api._window = window
    window.events.closed += api.shutdown
    window.events.loaded += loaded.set
    window.has_monolith = hasattr(api, "monolith")
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
        if not second.has_monolith:
            print("SKIP  U2 find-table checks: this Api has no `monolith` namespace yet")
            return
        u2_checks(second)
        u3_checks(second)
        u4_checks(second)
        fit_checks(second)
        u5_checks(second)
        u6_checks(second)
        u7_checks(second)
        u8_checks(second)
        time.sleep(0.8)  # the sorted find pane's autosave
        third, third_loaded = open_window("Mosaic smoke test (U2 relaunch)")
        opened.append(third)
        assert third_loaded.wait(timeout=15), "the U2 relaunch never loaded"
        opened.remove(second)
        second.destroy()
        install_collector(third)
        u2_restored_checks(third)
        u3_restored_checks(third)
        u4_restored_checks(third)
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
