#!/usr/bin/env python3
"""Regression smoke test for the MonOSS desktop UI.

Not named test_*.py on purpose: pytest's isolated `conn`-fixture suite
assumes no GUI, but this spawns a real pywebview window against a real
WebKit/GTK display, so it isn't collected automatically. Run it by hand
after UI changes:

    .venv/bin/python scripts/monarch_smoke_test.py

Consolidates checks that, across the sessions this was written in, only
ever caught real regressions when written by hand as one-off throwaway
scripts: cross-thread sqlite access, localStorage silently unavailable
under file://, a species picker that wrote to the backend but never
refreshed, and gesture bugs that only reproduce with real mouse events.

The graph is a canvas, so nodes have no DOM elements to click. Node
interactions go through `graph.screenPositionOf(id)` and synthetic
MouseEvents at those coordinates -- the same path a real click takes.
Never touches real data: GARDENS_DIR and the webview storage_path both
point at a fresh temp directory.
"""
import json
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.db as db

_TMP = Path(tempfile.mkdtemp(prefix="monoss-smoke-"))
db.GARDENS_DIR = _TMP / "gardens"

import webview  # noqa: E402  (import after GARDENS_DIR patched, matches app startup order)
from window_launcher import choose_port  # noqa: E402
from monarch.api import Api  # noqa: E402
# Setup only: creating a garden is the Gardens window's job now
# (gardens_app/), not this bridge's, so scenarios that need a
# second garden to switch into make it through core directly.
from core.gardens import create_garden  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "monarch" / "frontend"

# Neither Monarch's 42001 (the old mora/app.py) nor Mora's 42002, so a
# smoke run can share a desktop with both apps already open.
SMOKE_HTTP_PORT = 42003

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
        if os.environ.get("MONOSS_SMOKE_TRACEBACK"):
            traceback.print_exc()


def js(window, code):
    return window.evaluate_js(code)


def js_async(window, code, wait=1.5):
    """Fire an async IIFE that stores its outcome on `window.__result`,
    since evaluate_js does not await a returned Promise -- then poll for
    it at short intervals. `wait` is a ceiling, not a fixed cost: every
    caller's IIFE ends by setting window.__result, so this returns as
    soon as that happens instead of always sleeping the worst case --
    across ~40 call sites in a 1.5s-heavy suite, that adds up."""
    js(window, f"window.__result = undefined; ({code})();")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if js(window, "window.__result !== undefined") is True:
            break
        time.sleep(0.05)
    return js(window, "JSON.stringify(window.__result)")


def wait_until(condition, timeout=5.0, interval=0.05):
    """Poll `condition()` until it returns True or `timeout` elapses.
    For waits with a clear, cheap-to-check completion signal (a button's
    disabled state, an element's visibility) -- a tighter, faster
    alternative to a flat time.sleep() sized for the worst case."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(interval)
    return False


def drag(window, x1, y1, x2, y2, ctrl=False, shift=False, button=0):
    """Synthetic press-move-release against the canvas, in page pixels.
    button=0 (left, the default) is a click/box-select/node-drag/connect
    depending on target and modifiers; button=1 is a middle-drag pan."""
    buttons = {0: 1, 1: 4, 2: 2}[button]
    js(window, f"""
        (function() {{
            const canvas = document.getElementById('graph');
            const steps = 12;
            function fire(type, x, y, target) {{
                target.dispatchEvent(new MouseEvent(type, {{
                    clientX: x, clientY: y, bubbles: true, cancelable: true,
                    button: {button}, buttons: {buttons},
                    ctrlKey: {str(ctrl).lower()}, shiftKey: {str(shift).lower()},
                }}));
            }}
            fire('mousedown', {x1}, {y1}, canvas);
            for (let i = 1; i <= steps; i++) {{
                const t = i / steps;
                fire('mousemove', {x1} + ({x2} - {x1}) * t, {y1} + ({y2} - {y1}) * t, window);
            }}
            fire('mouseup', {x2}, {y2}, window);
        }})();
    """)


def right_click(window, x, y):
    js(window, f"""
        document.getElementById('graph').dispatchEvent(new MouseEvent('contextmenu', {{
            clientX: {x}, clientY: {y}, bubbles: true, cancelable: true,
        }}));
    """)


def press_key(window, key, target="window"):
    js(window, f"""
        {target}.dispatchEvent(new KeyboardEvent('keydown', {{key: {json.dumps(key)}, bubbles: true}}));
    """)


def node_xy(window, id_expr):
    pos = json.loads(js(window, f"JSON.stringify(graph.screenPositionOf({id_expr}))"))
    return pos["x"], pos["y"]


def dblclick(window, x, y):
    """Two synthetic left clicks then a dblclick, in page pixels -- the
    sequence a real double-click delivers, so the canvas sees both
    mousedown/mouseup pairs (annotate mode's select/box logic) before
    the dblclick that adds or edits a note."""
    drag(window, x, y, x, y)
    drag(window, x, y, x, y)
    js(window, f"""
        document.getElementById('graph').dispatchEvent(new MouseEvent('dblclick', {{
            clientX: {x}, clientY: {y}, bubbles: true, cancelable: true, button: 0, detail: 2,
        }}));
    """)


def empty_canvas_point(window, clearance=70):
    """A canvas point with no node within `clearance` pixels.

    The empty-canvas context menu (New mote here / Paste) is a different
    menu from a mote's, so a check that wants it has
    to right-click somewhere a mote is not. A hardcoded corner used to
    stand in for "empty", which is only true until something moves the
    layout -- resetView() fits the graph to whatever the canvas measures,
    so a garden with a few more motes, or a canvas a few pixels shorter,
    silently puts a node under that corner and hands the check a mote's
    menu instead. This asks the graph where its nodes actually are.
    """
    point = json.loads(js(window, f"""
        (function() {{
            const canvas = document.getElementById('graph');
            const rect = canvas.getBoundingClientRect();
            const taken = [...graph.nodes.keys()]
                .map((id) => graph.screenPositionOf(id))
                .filter(Boolean);
            for (let y = rect.top + 40; y < rect.bottom - 40; y += 20) {{
                for (let x = rect.left + 40; x < rect.right - 40; x += 20) {{
                    // elementFromPoint keeps this out from under the
                    // floating panels, which sit over the canvas and would
                    // swallow a real right-click.
                    if (document.elementFromPoint(x, y) !== canvas) continue;
                    if (taken.every((p) => Math.hypot(p.x - x, p.y - y) > {clearance})) {{
                        return JSON.stringify({{x: x, y: y}});
                    }}
                }}
            }}
            return JSON.stringify(null);
        }})()
    """))
    assert point is not None, "no empty canvas point to right-click -- the graph fills the window"
    return point["x"], point["y"]


def click_menu_item(window, label):
    js(window, f"""
        Array.from(document.querySelectorAll('#context-menu .menu-item'))
            .find(i => i.textContent === {json.dumps(label)}).click();
    """)


def run_checks(window):
    def create_seed_data():
        result = js_async(window, """
            async () => {
                const a = await api.create_mote({name: 'Alice', role: 'engineer'});
                const b = await api.create_mote({name: 'Bob'});
                const c = await api.create_mote({age: '42'});
                await api.create_relationship(a.id, b.id, 'knows');
                window.__seed = {aliceId: a.id, bobId: b.id, carolId: c.id};
                await refreshMoteCache();
                await refocus(a.id);
                window.__result = 'ok';
            }
        """)
        assert result == '"ok"', f"seed setup failed: {result}"
        count = js(window, "graph.nodes.size")
        assert count == 3, f"whole garden should render all 3 motes, got {count}"

    check("create motes + relationship", create_seed_data)

    def canvas_actually_paints():
        """Guards a bug that every other check sailed straight past: the
        render loop was dead and the canvas had never painted a single
        pixel. Hit-testing works off node coordinates, not pixels, so
        clicking, dragging and connecting all passed against an entirely
        blank graph. Only sampling the canvas catches it."""
        time.sleep(1.0)
        stats = json.loads(js(window, """
            (function() {
                const c = document.getElementById('graph');
                const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;
                const colors = new Set();
                let opaque = 0;
                for (let i = 0; i < d.length; i += 400) {
                    if (d[i + 3] > 0) opaque++;
                    colors.add(d[i] + ',' + d[i + 1] + ',' + d[i + 2]);
                }
                return JSON.stringify({distinct: colors.size, opaque: opaque});
            })()
        """))
        assert stats["opaque"] > 0, f"the graph canvas is completely unpainted: {stats}"
        assert stats["distinct"] > 1, (
            f"the canvas has only the background colour -- no nodes drawn: {stats}"
        )

    check("graph canvas actually renders", canvas_actually_paints)

    def idle_cursor_is_not_a_hand():
        # Pan moved to the middle button; a "grab" hand over empty canvas
        # was left over from when plain left-drag used to pan, and wrongly
        # implied it still does.
        cursor = js(window, "getComputedStyle(document.getElementById('graph')).cursor")
        assert cursor == "default", f"idle cursor should be the plain pointer, got {cursor!r}"

    check("idle cursor is the plain pointer, not a hand", idle_cursor_is_not_a_hand)

    def graph_shows_label_fallback():
        label = js(window, """
            (function() {
                const m = payload.motes.find(m => m.id === window.__seed.carolId);
                return nodeLabel(m);
            })()
        """)
        assert label == "age: 42", f"expected fallback label 'age: 42', got {label!r}"

    check("label falls back to first property when no name", graph_shows_label_fallback)

    def click_selects_and_opens_panel():
        js(window, "graph.resetView();")
        time.sleep(1.2)
        x, y = node_xy(window, "window.__seed.bobId")
        drag(window, x, y, x, y)  # press and release without moving = click
        time.sleep(2.0)
        title = js(window, "document.querySelector('.ins-name').value")
        assert title == "Bob", f"clicking Bob should open its panel, got {title!r}"

    check("clicking a node selects it and opens the panel", click_selects_and_opens_panel)

    def dragging_a_node_moves_and_pins_it():
        js(window, "graph.resetView();")
        time.sleep(1.2)
        before = json.loads(js(window, "JSON.stringify(graph.nodes.get(window.__seed.bobId))"))
        x, y = node_xy(window, "window.__seed.bobId")
        drag(window, x, y, x + 120, y + 70)
        time.sleep(1.0)
        after = json.loads(js(window, "JSON.stringify(graph.nodes.get(window.__seed.bobId))"))
        assert after["pinned"] is True, "dragging a node should pin it"
        assert (after["x"], after["y"]) != (before["x"], before["y"]), "node should have moved"

        persisted = js(window, """
            (function() {
                const stored = JSON.parse(
                    localStorage.getItem('monoss:positions:' + currentGarden) || '{}');
                return Object.prototype.hasOwnProperty.call(stored, window.__seed.bobId);
            })()
        """)
        assert persisted is True, "a dragged node's position should persist"

    check("dragging a node moves, pins and persists it", dragging_a_node_moves_and_pins_it)

    def ctrl_drag_connects_without_moving():
        js(window, "window.prompt = function() { return 'ctrl-drag-test'; };")
        js(window, "graph.resetView();")
        time.sleep(1.2)
        before = js(window, "JSON.stringify(graph.nodes.get(window.__seed.aliceId))")
        ax, ay = node_xy(window, "window.__seed.aliceId")
        bx, by = node_xy(window, "window.__seed.carolId")
        drag(window, ax, ay, bx, by, ctrl=True)
        time.sleep(2.0)

        after = js(window, "JSON.stringify(graph.nodes.get(window.__seed.aliceId))")
        assert json.loads(before)["pinned"] == json.loads(after)["pinned"], (
            "ctrl+drag must not pin the source node -- it is a connect gesture, not a move"
        )
        rels = json.loads(js_async(window, """
            async () => {
                window.__result = await api.mote_relationships(window.__seed.aliceId);
            }
        """))
        types = [r["type"] for r in rels]
        assert "ctrl-drag-test" in types, f"ctrl+drag should create a relationship, got {types}"

    check("ctrl+drag connects two motes", ctrl_drag_connects_without_moving)

    def species_and_templating():
        result = js_async(window, """
            async () => {
                const species = await api.create_mote({name: 'Resistor', freedoms: 'current'});
                const template = await api.create_mote({name: 'R', resistance: '100'});
                await api.set_template(template.id, species.id);
                window.__seed.speciesId = species.id;
                await refreshMoteCache();
                await refocus(species.id);
                await openDetailPanel(species.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"species setup failed: {result}"

        # "New instance" appears on a species that has a template, and the
        # instance comes out pre-filled from it.
        js(window, """
            if (!document.querySelector('.ins-toggle').classList.contains('on'))
                document.querySelector('.ins-toggle').click();
        """)
        time.sleep(0.4)
        found = js(window, """
            !!Array.from(document.querySelectorAll('.ins-body .ins-ghost'))
                   .find(b => b.textContent === '+ New instance')
        """)
        assert found is True, "a species with a template should offer New instance"

        js(window, """
            Array.from(document.querySelectorAll('.ins-body .ins-ghost'))
                 .find(b => b.textContent === '+ New instance').click();
        """)
        time.sleep(2.5)
        props = json.loads(js(window, "JSON.stringify((payload.motes.find(m => m.id === focusId) || {}).properties)"))
        assert props.get("resistance") == 100, f"instance should inherit the template, got {props}"
        assert props.get("name") == "R", f"instance should inherit the template, got {props}"

        kind = js(window, "document.querySelector('.ins-species').textContent")
        assert kind == "Resistor", f"new instance should report its species, got {kind!r}"

    check("species template: New instance copies template properties", species_and_templating)

    def quick_add_creates_and_selects_a_mote():
        # Quick-add ("+ ") used to open a form panel to fill in before
        # creating -- it's Inspector-driven now (commit c2aa50f): the
        # button creates a blank mote immediately and opens its panel,
        # same as "New mote here" on the canvas. Its own throwaway garden
        # so the blank mote it leaves behind doesn't perturb the 'default'
        # garden's layout for every later check that clicks or drags
        # against computed canvas positions.
        create_garden('smoke-quickadd')
        js_async(window, """
            async () => {
                await api.switch_garden('smoke-quickadd');
                await loadGarden('smoke-quickadd');
                window.__result = 'ok';
            }
        """, wait=2.0)

        before = js(window, "graph.nodes.size")
        js(window, "document.getElementById('quick-add-btn').click();")
        time.sleep(1.5)
        after = js(window, "graph.nodes.size")
        assert after == before + 1, f"quick-add should create exactly one mote, got {before} -> {after}"
        shown = js(window, "!document.querySelector('.ins').classList.contains('hidden')")
        assert shown is True, "quick-add should select the new mote and open its panel"

        js_async(window, """
            async () => {
                await api.switch_garden('default');
                await loadGarden('default');
                window.__result = 'ok';
            }
        """, wait=2.0)

    check("quick add: creates a blank mote and opens its panel", quick_add_creates_and_selects_a_mote)

    def legend_and_relationship_filters():
        js(window, "(async () => { await refocus(window.__seed.aliceId); })()")
        time.sleep(2.0)
        rows = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#legend .filter-row'))
                .map(e => e.querySelector('.name').textContent))
        """))
        assert "Resistor" in rows, f"legend should list species, got {rows}"

        visible_before = js(window, "graph.motes.filter(m => graph.isMoteVisible(m.id)).length")
        js(window, """
            Array.from(document.querySelectorAll('#legend .filter-row'))
                .find(e => e.querySelector('.name').textContent === 'Resistor').click();
        """)
        time.sleep(0.8)
        visible_after = js(window, "graph.motes.filter(m => graph.isMoteVisible(m.id)).length")
        assert visible_after < visible_before, (
            f"hiding a species should hide its instances: {visible_before} -> {visible_after}"
        )

        stored = json.loads(js(window, "localStorage.getItem('monoss:hiddenSpecies:' + currentGarden)"))
        assert len(stored) == 1, f"hidden species should persist, got {stored}"

        # Restore, then exercise the relationship filter that motivated all
        # this -- instance_of edges touch nearly every mote.
        js(window, """
            Array.from(document.querySelectorAll('#legend .filter-row'))
                .find(e => e.querySelector('.name').textContent === 'Resistor').click();
        """)
        time.sleep(0.8)
        edges_before = js(window, "graph.relationships.filter(r => graph.isRelVisible(r)).length")
        js(window, """
            Array.from(document.querySelectorAll('#rel-filters .filter-row'))
                .find(e => e.querySelector('.name').textContent === 'instance_of').click();
        """)
        time.sleep(0.8)
        edges_after = js(window, "graph.relationships.filter(r => graph.isRelVisible(r)).length")
        assert edges_after < edges_before, (
            f"hiding instance_of should hide those edges: {edges_before} -> {edges_after}"
        )

    check("legend + relationship filters hide motes and edges", legend_and_relationship_filters)

    def search_palette_browses_and_filters():
        js(window, "openSearch();")
        time.sleep(1.0)
        all_count = js(window, "document.querySelectorAll('#search-results .search-result-row').length")
        assert all_count >= 5, f"search should browse every mote, got {all_count}"

        js(window, """
            const i = document.getElementById('search-input');
            i.value = 'engineer'; i.dispatchEvent(new Event('input'));
        """)
        time.sleep(0.8)
        labels = js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#search-results .label'))
                .map(e => e.textContent))
        """)
        assert labels == '["Alice"]', f"property-value search failed: {labels}"

    check("search: browse all + filter by property value", search_palette_browses_and_filters)

    def search_key_value_query_matches_exactly():
        js(window, """
            const i = document.getElementById('search-input');
            i.value = 'role = engineer'; i.dispatchEvent(new Event('input'));
        """)
        time.sleep(0.8)
        labels = js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#search-results .label'))
                .map(e => e.textContent))
        """)
        assert labels == '["Alice"]', f"key = value search should match on the exact property, got {labels}"

        js(window, """
            const i = document.getElementById('search-input');
            i.value = 'role = doctor'; i.dispatchEvent(new Event('input'));
        """)
        time.sleep(0.8)
        empty = js(window, "document.querySelector('#search-results .search-empty') !== null")
        assert empty is True, "key = value search should not match a different value"

    check("search: key = value matches a specific property exactly", search_key_value_query_matches_exactly)

    def search_matches_a_full_mote_id():
        """matchesQuery() is meant to be the exact counterpart of
        core/motes.py::search_motes. It used to match nodeLabel() instead
        of the id, and a label only ever shows a mote's first 8
        characters -- so pasting a whole id in found nothing here while
        the same query found it over MCP."""
        full_id = js(window, "JSON.stringify(allMotes[0].id)").strip('"')
        js(window, f"""
            const i = document.getElementById('search-input');
            i.value = {full_id!r}; i.dispatchEvent(new Event('input'));
        """)
        time.sleep(0.8)
        count = js(window, "document.querySelectorAll('#search-results .search-result-row').length")
        assert count == 1, f"a full mote id should find exactly its own mote, got {count}"

        js(window, """
            const i = document.getElementById('search-input');
            i.value = ''; i.dispatchEvent(new Event('input'));
        """)
        time.sleep(0.5)

    check("search: a full mote id matches, same as core.search_motes", search_matches_a_full_mote_id)

    def search_keyboard_nav_selects():
        js(window, """
            const i = document.getElementById('search-input');
            i.value = ''; i.dispatchEvent(new Event('input'));
            i.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown', bubbles: true}));
            i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true}));
        """)
        time.sleep(2.0)
        hidden = js(window, "document.getElementById('search-overlay').classList.contains('hidden')")
        assert hidden is True, "search should close after Enter-selecting"

    check("search: keyboard nav + Enter selects and closes", search_keyboard_nav_selects)

    def long_values_get_a_sized_textarea():
        result = js_async(window, """
            async () => {
                const rule = await api.create_mote({
                    name: 'Ohm', equation: "targets('a').voltage - targets('b').voltage == current * resistance",
                });
                window.__seed.ruleId = rule.id;
                await refreshMoteCache();
                await refocus(rule.id);
                await openDetailPanel(rule.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"rule setup failed: {result}"

        info = json.loads(js(window, """
            (function() {
                const rows = document.querySelectorAll('.ins-body .ins-row');
                for (const r of rows) {
                    if (r.querySelector('.ins-key').value === 'equation') {
                        const f = r.querySelector('textarea');
                        return JSON.stringify({tag: f ? 'textarea' : 'none', height: f ? f.clientHeight : 0});
                    }
                }
                return JSON.stringify({tag: 'not found', height: 0});
            })()
        """))
        assert info["tag"] == "textarea", f"a long equation should get a textarea, got {info}"
        # One line is ~30px; a wrapped equation must be taller or it's
        # scrolling out of sight, which is the bug this guards.
        assert info["height"] > 40, f"textarea should be sized to its content, got {info}"

    check("detail panel: long equation gets a content-sized textarea", long_values_get_a_sized_textarea)

    def url_property_gets_an_open_button():
        result = js_async(window, """
            async () => {
                const linked = await api.create_mote({name: 'linked', url: 'https://example.com'});
                window.__seed.linkedId = linked.id;
                await refreshMoteCache();
                await refocus(linked.id);
                await openDetailPanel(linked.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"url mote setup failed: {result}"

        has_open_on_url = js(window, """
            (function() {
                const rows = document.querySelectorAll('.ins-body .ins-row');
                for (const r of rows) {
                    if (r.querySelector('.ins-key').value === 'url') return !!r.querySelector('.ins-follow');
                }
                return 'row not found';
            })()
        """)
        assert has_open_on_url is True, f"a url property should get an Open button, got {has_open_on_url!r}"

        with_open = json.loads(js(window, """
            (function() {
                const rows = document.querySelectorAll('.ins-body .ins-row');
                const keys = [];
                for (const r of rows) {
                    if (r.querySelector('.ins-follow')) keys.push(r.querySelector('.ins-key').value);
                }
                return JSON.stringify(keys);
            })()
        """))
        assert with_open == ["url"], \
            f"only the url property should get an Open button, got {with_open}"

        # Stub api.open_link so clicking Open doesn't try to launch a real
        # browser on the test machine; restored immediately after.
        js(window, """
            window.__opened = [];
            window.__origOpenLink = api.inspector_open_link.bind(api);
            api.inspector_open_link = (t) => { window.__opened.push(t); return Promise.resolve({opened: t}); };
        """)
        js(window, """
            const rows = document.querySelectorAll('.ins-body .ins-row');
            for (const r of rows) {
                if (r.querySelector('.ins-key').value === 'url') r.querySelector('.ins-follow').click();
            }
        """)
        time.sleep(0.5)
        opened = json.loads(js(window, "JSON.stringify(window.__opened)"))
        js(window, "api.inspector_open_link = window.__origOpenLink;")
        assert opened == ["https://example.com"], f"Open should call api.open_link with the property's value, got {opened}"

    check("detail panel: a url property gets a working Open button", url_property_gets_an_open_button)

    def date_property_gets_a_picker_and_round_trips():
        result = js_async(window, """
            async () => {
                const dated = await api.create_mote({name: 'dated', date: '2026-08-15'});
                window.__seed.datedId = dated.id;
                await refreshMoteCache();
                await refocus(dated.id);
                await openDetailPanel(dated.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"date mote setup failed: {result}"

        # Clicking the summary button opens the calendar popup -- which
        # promotes it out to a direct child of <body> so it escapes the
        # inspector panel's backdrop-filter (see IntervalPicker.field's
        # openPanel comment). It keeps the "interval-field" class too, but
        # it is no longer a descendant of `field` once open, so its
        # contents have to be queried from the document, not from `field`.
        info = json.loads(js(window, """
            (function() {
                const rows = document.querySelectorAll('.ins-body .ins-row');
                for (const r of rows) {
                    if (r.querySelector('.ins-key').value === 'date') {
                        const field = r.querySelector('.interval-field');
                        if (!field) return JSON.stringify({found: false});
                        field.querySelector('.interval-summary-btn').click();
                        const panel = document.querySelector('.interval-panel.interval-field');
                        const allDay = panel.querySelector('.interval-time-row input[type=\"checkbox\"]');
                        const timeInput = panel.querySelector('.interval-time-row input[type=\"time\"]');
                        return JSON.stringify({
                            found: true, value: field.value, allDay: allDay.checked,
                            timeHidden: timeInput.classList.contains('hidden'),
                        });
                    }
                }
                return JSON.stringify({found: false});
            })()
        """))
        assert info["found"], "an all-day date property should render the interval picker (.interval-field)"
        assert info["value"] == "2026-08-15", f"the field should report the stored date, got {info}"
        assert info["allDay"] is True, f"a bare YYYY-MM-DD value should check All day, got {info}"
        assert info["timeHidden"] is True, f"the time input should be hidden while All day is checked, got {info}"

        # Uncheck All day and set a time -- should commit a full timed value.
        js(window, """
            const panel = document.querySelector('.interval-panel.interval-field');
            const allDay = panel.querySelector('.interval-time-row input[type=\"checkbox\"]');
            allDay.checked = false;
            allDay.dispatchEvent(new Event('change', {bubbles: true}));
            const timeInput = panel.querySelector('.interval-time-row input[type=\"time\"]');
            timeInput.value = '14:30';
            timeInput.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        time.sleep(1.0)
        updated = json.loads(js_async(window, """
            async () => { window.__result = (await api.get_mote(window.__seed.datedId)).properties.date; }
        """))
        assert updated == "2026-08-15T14:30:00", f"toggling to timed should store a full datetime, got {updated!r}"

        # Close the popup: it's rendered outside the inspector panel, so
        # leaving it open blocks the panel from re-rendering for whatever
        # mote a later check opens next (Inspector.render skips its
        # rebuild while IntervalPicker.isOpen(), to avoid yanking a field
        # out from under someone mid-edit -- see inspector.js::render).
        js(window, "IntervalPicker.closeOpen();")

    check("detail panel: date property gets a picker and round-trips all-day/timed",
          date_property_gets_a_picker_and_round_trips)

    def the_picker_follows_the_shape_not_the_key_name():
        """`date` stopped being a name the engine reads when a start
        became an ISO 8601 interval, so the picker is offered for any
        property holding one -- here under `when`, which nothing has ever
        reserved."""
        result = js_async(window, """
            async () => {
                const m = await api.create_mote({
                    name: 'interval mote', when: '2026-08-15T09:00/PT1H',
                });
                window.__seed.intervalId = m.id;
                await refreshMoteCache();
                await refocus(m.id);
                await openDetailPanel(m.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"interval mote setup failed: {result}"

        info = json.loads(js(window, """
            (function() {
                const rows = document.querySelectorAll('.ins-body .ins-row');
                for (const r of rows) {
                    if (r.querySelector('.ins-key').value === 'when') {
                        const field = r.querySelector('.interval-field');
                        if (!field) return JSON.stringify({found: false});
                        return JSON.stringify({found: true, value: field.value});
                    }
                }
                return JSON.stringify({found: false});
            })()
        """))
        assert info["found"], \
            "an interval under a non-`date` key should still render the picker"
        assert info["value"].startswith("2026-08-15T09:00"), f"the picker should show the start, got {info}"
        assert info["value"].endswith("/PT1H"), f"the picker should show the span, got {info}"

    check("detail panel: the picker follows the value's shape, not the key's name",
          the_picker_follows_the_shape_not_the_key_name)

    def editing_a_start_keeps_the_span():
        """Moving the start has to move the end with it -- the span is
        what an edit here must preserve, not the end's own clock time."""
        js(window, """
            const field = document.querySelector('.ins-body .interval-field');
            field.querySelector('.interval-summary-btn').click();
            const panel = document.querySelector('.interval-panel.interval-field');
            const timeInput = panel.querySelector('.interval-time-row input[type=\"time\"]');
            timeInput.value = '11:30';
            timeInput.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        time.sleep(1.0)
        updated = json.loads(js_async(window, """
            async () => { window.__result = (await api.get_mote(window.__seed.intervalId)).properties.when; }
        """))
        assert updated == "2026-08-15T11:30:00/PT1H", \
            f"the span must survive an edit of the start, got {updated!r}"
        js(window, "IntervalPicker.closeOpen();")

    check("detail panel: editing a start keeps the span",
          editing_a_start_keeps_the_span)

    def a_repeating_interval_gets_the_picker_with_an_editable_repeat_rule():
        """Structured repeat authoring (frequency/weekday/ends pills)
        moved to Monastery -- Monarch's picker still has to round-trip an
        existing RRULE without losing it, shown now as editable raw text
        in the repeat section (see monarch/frontend/app.js::renderRepeatDetail)."""
        result = js_async(window, """
            async () => {
                const m = await api.create_mote({
                    name: 'repeating mote', when: '2026-08-15T09:00/PT1H\\nRRULE:FREQ=WEEKLY',
                });
                window.__seed.repeatingId = m.id;
                await refreshMoteCache();
                await refocus(m.id);
                await openDetailPanel(m.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"repeating mote setup failed: {result}"

        info = json.loads(js(window, """
            (function() {
                const rows = document.querySelectorAll('.ins-body .ins-row');
                for (const r of rows) {
                    if (r.querySelector('.ins-key').value === 'when') {
                        const field = r.querySelector('.interval-field');
                        if (!field) return JSON.stringify({picker: false});
                        field.querySelector('.interval-summary-btn').click();
                        const panel = document.querySelector('.interval-panel.interval-field');
                        const editor = panel.querySelector('.interval-repeat-editor');
                        const on = Array.from(
                            panel.querySelectorAll('.interval-repeat-editor .interval-pill.on'))
                            .map((b) => b.textContent);
                        return JSON.stringify({
                            picker: true, value: field.value,
                            structured: !!editor, activePills: on,
                        });
                    }
                }
                return JSON.stringify({picker: null});
            })()
        """))
        assert info["picker"] is True, \
            f"a repeating interval should get the picker, with its rule shown and editable, got {info}"
        assert info["structured"] is True, \
            f"a stored rule should open in the structured repeat editor, got {info}"
        assert "Week" in info["activePills"], \
            f"the editor should show the stored frequency selected, got {info}"
        assert "RRULE:FREQ=WEEKLY" in info["value"], f"the rule must still be present, got {info}"
        js(window, "IntervalPicker.closeOpen();")

    check("detail panel: a repeating interval gets the picker, with an editable repeat rule",
          a_repeating_interval_gets_the_picker_with_an_editable_repeat_rule)

    def adding_a_repeat_rule_and_editing_it_writes_a_real_rrule():
        """The frequency and weekday pills are back, and shared -- the
        same editor Monastery's draft uses (frontend_shared/interval.js).
        Clicking them is what commits a real rule."""
        result = js_async(window, """
            async () => {
                const m = await api.create_mote({name: 'weekly task', due: '2026-08-17T09:00'});
                window.__seed.weeklyId = m.id;
                await refreshMoteCache();
                await refocus(m.id);
                await openDetailPanel(m.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"weekly mote setup failed: {result}"

        js(window, """
            const field = document.querySelector('.ins-body .interval-field');
            field.querySelector('.interval-summary-btn').click();
            const panel = document.querySelector('.interval-panel.interval-field');
            panel.querySelector('.interval-repeat-detail .interval-end-ghost').click();
        """)
        time.sleep(0.5)
        # "+ Add a repeat rule" seeds weekly on the start's own weekday
        # (a Monday here); clicking We and Fr adds the other two.
        js(window, """
            const panel = document.querySelector('.interval-panel.interval-field');
            const days = Array.from(panel.querySelectorAll('.interval-day-pill'));
            for (const code of ['W', 'F']) {
                const pill = days.find((d, i) => d.textContent === code && i >= 3);
                if (pill) pill.click();
            }
        """)
        time.sleep(0.6)
        updated = json.loads(js_async(window, """
            async () => { window.__result = (await api.get_mote(window.__seed.weeklyId)).properties.due; }
        """))
        assert "RRULE:FREQ=WEEKLY" in updated, f"editing the repeat text should write a real RRULE, got {updated!r}"
        assert "BYDAY=" in updated, f"the weekday pills should write a BYDAY, got {updated!r}"
        for code in ("MO", "WE", "FR"):
            assert code in updated.split("BYDAY=")[1], \
                f"BYDAY should carry every selected weekday, got {updated!r}"
        js(window, "IntervalPicker.closeOpen();")

    check("detail panel: adding a repeat rule and editing it writes a real RRULE",
          adding_a_repeat_rule_and_editing_it_writes_a_real_rrule)

    def an_until_rule_decomposes_and_survives_a_start_edit():
        """UNTIL= decomposes into the editor's "ends on" date control,
        and an edit to the start must leave the rule intact rather than
        dropping it."""
        result = js_async(window, """
            async () => {
                const m = await api.create_mote({
                    name: 'until mote',
                    deadline: '2026-08-15T09:00/PT1H\\nRRULE:FREQ=WEEKLY;UNTIL=20261231T090000Z',
                });
                window.__seed.untilId = m.id;
                await refreshMoteCache();
                await refocus(m.id);
                await openDetailPanel(m.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"until-rule mote setup failed: {result}"

        info = json.loads(js(window, """
            (function() {
                const field = document.querySelector('.ins-body .interval-field');
                field.querySelector('.interval-summary-btn').click();
                const panel = document.querySelector('.interval-panel.interval-field');
                const until = panel.querySelector('.interval-until-input');
                return JSON.stringify({ until: until ? until.value : null });
            })()
        """))
        assert info["until"] == "2026-12-31", \
            f"an UNTIL= rule should decompose into the ends-on control, got {info}"

        js(window, """
            const panel = document.querySelector('.interval-panel.interval-field');
            const timeInput = panel.querySelector('.interval-time-row input[type=\"time\"]');
            timeInput.value = '10:15';
            timeInput.dispatchEvent(new Event('change', {bubbles: true}));
        """)
        time.sleep(1.0)
        updated = json.loads(js_async(window, """
            async () => { window.__result = (await api.get_mote(window.__seed.untilId)).properties.deadline; }
        """))
        assert updated.startswith("2026-08-15T10:15:00"), f"the start should move, got {updated!r}"
        assert "RRULE:FREQ=WEEKLY;UNTIL=20261231" in updated, (
            f"the repeat-until-date should survive a start-only edit, got {updated!r}"
        )
        js(window, "IntervalPicker.closeOpen();")

    check("detail panel: an UNTIL= rule decomposes and survives a start edit",
          an_until_rule_decomposes_and_survives_a_start_edit)

    def a_rule_with_an_unsupported_param_still_shows_as_raw_text():
        """BYMONTHDAY isn't part of the FREQ/INTERVAL/BYDAY/COUNT/UNTIL
        subset decomposeRuleLines recognizes, so it has to round-trip as
        raw, editable text rather than being silently dropped -- same
        safety net every rule gets now, not a special case."""
        result = js_async(window, """
            async () => {
                const m = await api.create_mote({
                    name: 'monthday mote',
                    when: '2026-08-15T09:00/PT1H\\nRRULE:FREQ=MONTHLY;BYMONTHDAY=15',
                });
                window.__seed.monthdayId = m.id;
                await refreshMoteCache();
                await refocus(m.id);
                await openDetailPanel(m.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"BYMONTHDAY mote setup failed: {result}"

        info = json.loads(js(window, """
            (function() {
                const field = document.querySelector('.ins-body .interval-field');
                field.querySelector('.interval-summary-btn').click();
                const panel = document.querySelector('.interval-panel.interval-field');
                const raw = panel.querySelector(
                    '.interval-repeat-detail textarea, .interval-repeat-detail input[type=\"text\"]');
                return JSON.stringify({
                    fallbackShown: !!raw,
                    shows: raw ? raw.value : null,
                });
            })()
        """))
        assert info["fallbackShown"] is True, f"an unsupported param must still show as editable text, got {info}"
        assert "BYMONTHDAY=15" in (info["shows"] or ""), f"the rule has to stay visible and editable, got {info}"
        js(window, "IntervalPicker.closeOpen();")

    check("detail panel: a rule with an unsupported param (BYMONTHDAY) still shows as raw text",
          a_rule_with_an_unsupported_param_still_shows_as_raw_text)

    def the_panel_is_movable_and_resizable():
        """The floating panel's corner grip resizes it and its header
        drags it, and both survive a garden reload -- geometry is a
        preference about the panel, not about the data."""
        result = js_async(window, """
            async () => {
                const m = await api.create_mote({name: 'resize-checker'});
                window.__resizeId = m.id;
                await refreshMoteCache();
                await refocus(m.id);
                await openDetailPanel(m.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"resize mote setup failed: {result}"

        def box():
            """The panel's geometry in CSS pixels, read off its own inline
            style rather than getBoundingClientRect().

            The rect is not the same number: this window renders at a
            fractional zoom, so a panel 312 CSS pixels wide measures
            307.32, and the panel's open animation starts at
            scale(0.985) on top of that. The inline style is what the
            drag writes and what gets persisted, so it is what a claim
            about persistence should be checked against."""
            return json.loads(js(window, """
                (function() {
                    const s = document.querySelector('.ins').style;
                    return JSON.stringify({
                        w: parseFloat(s.width), h: parseFloat(s.height),
                        x: parseFloat(s.left), y: parseFloat(s.top),
                    });
                })()
            """))

        def drag_from(selector, dx, dy):
            spot = json.loads(js(window, f"""
                (function() {{
                    const r = document.querySelector('{selector}').getBoundingClientRect();
                    return JSON.stringify({{x: r.left + r.width / 2, y: r.top + r.height / 2}});
                }})()
            """))
            js(window, f"""
                (function() {{
                    const h = document.querySelector('{selector}');
                    function fire(type, x, y, target) {{
                        target.dispatchEvent(new MouseEvent(type, {{
                            clientX: x, clientY: y, bubbles: true, cancelable: true,
                        }}));
                    }}
                    fire('mousedown', {spot["x"]}, {spot["y"]}, h);
                    fire('mousemove', {spot["x"] + dx}, {spot["y"] + dy}, window);
                    fire('mouseup', {spot["x"] + dx}, {spot["y"] + dy}, window);
                }})();
            """)
            time.sleep(0.3)

        # Always put the panel back, even when an assertion fails: this
        # is the only check that moves it, and a panel parked over the
        # canvas swallows the clicks and keystrokes every later check
        # aims at the graph.
        try:
            _check_geometry(box, drag_from)
        finally:
            # Park it back at the top right rather than clearing the
            # stored geometry: with nothing stored, every later open
            # anchors the panel beside the mote that was clicked, which
            # puts it over the canvas the next check is aiming at.
            js(window, """
                localStorage.setItem('monoss:inspector:geometry', JSON.stringify({
                    x: window.innerWidth - 392 - 24, y: 58, w: 392,
                    h: Math.min(560, window.innerHeight - 90),
                }));
                Inspector.close();
            """)
            time.sleep(0.2)

    def _check_geometry(box, drag_from):
        # The grip is the bottom-right corner, so dragging it inward
        # shrinks the panel.
        before = box()
        drag_from(".ins-grip", -80, -60)
        after = box()
        assert -90 <= (after["w"] - before["w"]) <= -70, \
            f"dragging the grip 80px should shrink the panel by ~80px, got {after['w'] - before['w']}"
        assert -70 <= (after["h"] - before["h"]) <= -50, \
            f"dragging the grip 60px should shrink the panel by ~60px, got {after['h'] - before['h']}"

        # The header is the drag handle.
        moved_from = box()
        drag_from(".ins-head", -50, 40)
        moved = box()
        assert abs((moved["x"] - moved_from["x"]) + 50) < 6, \
            f"dragging the header should move the panel left by 50px, got {moved['x'] - moved_from['x']}"
        assert abs((moved["y"] - moved_from["y"]) - 40) < 6, \
            f"dragging the header should move the panel down by 40px, got {moved['y'] - moved_from['y']}"

        stored = json.loads(js(window,
            "localStorage.getItem('monoss:inspector:geometry')") or "null")
        assert stored is not None, "the panel's geometry should persist to localStorage"
        assert abs(stored["w"] - moved["w"]) < 2 and abs(stored["x"] - moved["x"]) < 2, \
            f"stored geometry should match what is on screen, got {stored} vs {moved}"

        # Reopening after a reload should restore it.
        js_async(window, """
            async () => {
                await loadGarden(currentGarden);
                await openDetailPanel(window.__resizeId);
                window.__result = 'ok';
            }
        """, wait=1.5)
        reloaded = box()
        assert abs(reloaded["w"] - moved["w"]) < 2 and abs(reloaded["x"] - moved["x"]) < 2, \
            f"panel geometry should survive a garden reload, got {reloaded} vs {moved}"

    check("the inspector is movable and resizable, and its geometry persists",
          the_panel_is_movable_and_resizable)

    def click_edge_selects_and_inspects_relationship():
        result = js_async(window, """
            async () => {
                const p = await api.create_mote({name: 'Edge-From'});
                const q = await api.create_mote({name: 'Edge-To'});
                const rel = await api.create_relationship(p.id, q.id, 'knows');
                window.__edgeIds = {p: p.id, q: q.id, rel: rel.id};
                await refreshMoteCache();
                await refocus(p.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"edge-select mote setup failed: {result}"

        mid = json.loads(js(window, """
            (function() {
                const a = graph.screenPositionOf(window.__edgeIds.p);
                const b = graph.screenPositionOf(window.__edgeIds.q);
                return JSON.stringify({x: (a.x + b.x) / 2, y: (a.y + b.y) / 2});
            })()
        """))
        drag(window, mid["x"], mid["y"], mid["x"], mid["y"])  # a plain click, no movement
        time.sleep(0.5)

        shown = js(window, "document.getElementById('detail-relationship').classList.contains('hidden')")
        assert shown is False, "clicking an edge should open the relationship panel"

        from_text = js(window, "document.getElementById('detail-rel-from').textContent")
        to_text = js(window, "document.getElementById('detail-rel-to').textContent")
        type_text = js(window, "document.getElementById('detail-rel-type-row').querySelector('.rel-type').textContent")
        assert (from_text, to_text, type_text) == ("Edge-From", "Edge-To", "knows"), \
            f"relationship panel should show its endpoints and type, got {(from_text, to_text, type_text)}"

        # Renaming reuses the same click-to-rename prompt pattern as the mote panel.
        js(window, "window.prompt = () => 'connected_to';")
        js(window, "document.getElementById('detail-rel-type-row').querySelector('.rel-type').click();")
        time.sleep(1.5)
        renamed = js(window, "document.getElementById('detail-rel-type-row').querySelector('.rel-type').textContent")
        assert renamed == "connected_to", f"renaming a relationship's type should update the panel, got {renamed!r}"

        # A plain click elsewhere on the canvas deselects it.
        drag(window, 60, 60, 60, 60)
        time.sleep(0.3)
        hidden_after_deselect = js(window, "document.getElementById('detail-panel').classList.contains('hidden')")
        assert hidden_after_deselect is True, "clicking empty canvas should close the relationship panel"

        # Clicking a mote switches cleanly to the mote panel, not the relationship one.
        p_xy = node_xy(window, "window.__edgeIds.p")
        drag(window, p_xy[0], p_xy[1], p_xy[0], p_xy[1])
        time.sleep(0.5)
        single_shown = js(window, "!document.querySelector('.ins').classList.contains('hidden')")
        rel_hidden = js(window, "document.getElementById('detail-relationship').classList.contains('hidden')")
        assert single_shown is True and rel_hidden is True, \
            "selecting a mote after a relationship should show the mote panel, not the relationship one"

        # Re-select the edge and delete it from its own panel.
        drag(window, mid["x"], mid["y"], mid["x"], mid["y"])
        time.sleep(0.5)
        js(window, "document.getElementById('delete-relationship-btn').click();")
        time.sleep(1.5)
        remaining = json.loads(js(window, "JSON.stringify(graph.relationships.map(r => r.id))"))
        rel_id = json.loads(js(window, "JSON.stringify(window.__edgeIds.rel)"))
        assert rel_id not in remaining, "deleting a relationship from its panel should remove the edge"
        panel_hidden_after_delete = js(window, "document.getElementById('detail-panel').classList.contains('hidden')")
        assert panel_hidden_after_delete is True, "deleting the selected relationship should close its panel"

    check("clicking an edge selects and inspects the relationship (rename/delete/deselect)",
          click_edge_selects_and_inspects_relationship)

    def shift_click_multiselects_relationships():
        result = js_async(window, """
            async () => {
                const p = await api.create_mote({name: 'MultiRel-P'});
                const q = await api.create_mote({name: 'MultiRel-Q'});
                const r = await api.create_mote({name: 'MultiRel-R'});
                const rel1 = await api.create_relationship(p.id, q.id, 'knows');
                const rel2 = await api.create_relationship(q.id, r.id, 'knows');
                window.__multiRel = {p: p.id, q: q.id, r: r.id, rel1: rel1.id, rel2: rel2.id};
                await refreshMoteCache();
                await refocus(p.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"multi-relationship setup failed: {result}"

        # Positions are read fresh right before each click rather than
        # cached upfront -- the force layout is still settling shortly
        # after these motes were created, so a midpoint captured too early
        # can drift out from under a later click.
        def click_edge(a_expr, b_expr, shift=False):
            mid = json.loads(js(window, f"""
                (function() {{
                    const a = graph.screenPositionOf({a_expr});
                    const b = graph.screenPositionOf({b_expr});
                    return JSON.stringify({{x: (a.x + b.x) / 2, y: (a.y + b.y) / 2}});
                }})()
            """))
            drag(window, mid["x"], mid["y"], mid["x"], mid["y"], shift=shift)

        rel1 = json.loads(js(window, "JSON.stringify(window.__multiRel.rel1)"))
        rel2 = json.loads(js(window, "JSON.stringify(window.__multiRel.rel2)"))
        p, q, r = "window.__multiRel.p", "window.__multiRel.q", "window.__multiRel.r"

        click_edge(p, q)  # plain click: select rel1
        time.sleep(0.3)
        click_edge(q, r, shift=True)  # shift+click: add rel2
        time.sleep(0.5)

        count_text = js(window, "document.getElementById('detail-relationship-multi-count').textContent")
        assert count_text == "2 relationships selected", \
            f"shift+click should build a 2-relationship selection, got {count_text!r}"
        shown = js(window, "!document.getElementById('detail-relationship-multi').classList.contains('hidden')")
        assert shown is True, "selecting 2 relationships should show the multi-relationship panel"

        selected = json.loads(js(window, "JSON.stringify([...graph.selectedRelIds])"))
        assert set(selected) == {rel1, rel2}, f"both relationships should be selected, got {selected}"

        # Shift+click the first one again to remove it, dropping back to one.
        click_edge(p, q, shift=True)
        time.sleep(0.5)
        single_shown = js(window, "!document.getElementById('detail-relationship').classList.contains('hidden')")
        assert single_shown is True, \
            "toggling back down to one relationship should show the single-relationship panel"

        # Re-select both and bulk-delete from the multi panel.
        click_edge(p, q)
        time.sleep(0.3)
        click_edge(q, r, shift=True)
        time.sleep(0.5)
        js(window, "document.getElementById('delete-relationship-selection-btn').click();")
        time.sleep(1.5)
        remaining = json.loads(js(window, "JSON.stringify(graph.relationships.map(r => r.id))"))
        assert rel1 not in remaining and rel2 not in remaining, \
            "bulk delete should remove both selected relationships"

    check("shift+click multi-selects relationships, with bulk delete", shift_click_multiselects_relationships)

    def shift_drag_box_selects_only_relationships():
        result = js_async(window, """
            async () => {
                const p = await api.create_mote({name: 'ShiftBox-P'});
                const q = await api.create_mote({name: 'ShiftBox-Q'});
                const r = await api.create_mote({name: 'ShiftBox-R'});
                const rel1 = await api.create_relationship(p.id, q.id, 'knows');
                const rel2 = await api.create_relationship(q.id, r.id, 'knows');
                window.__shiftBox = {p: p.id, q: q.id, r: r.id, rel1: rel1.id, rel2: rel2.id};
                await refreshMoteCache();
                await refocus(null);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"shift-drag relation box-select setup failed: {result}"
        ids = json.loads(js(window, "JSON.stringify(window.__shiftBox)"))

        js(window, "graph.pin(window.__shiftBox.p); graph.placeAt(window.__shiftBox.p, -5000, -5000);")
        js(window, "graph.pin(window.__shiftBox.q); graph.placeAt(window.__shiftBox.q, -4900, -5000);")
        js(window, "graph.pin(window.__shiftBox.r); graph.placeAt(window.__shiftBox.r, -4800, -5000);")
        time.sleep(0.3)
        js(window, "graph.resetView();")
        time.sleep(1.2)

        px, py = node_xy(window, "window.__shiftBox.p")
        rx, ry = node_xy(window, "window.__shiftBox.r")
        drag(window, min(px, rx) - 30, py - 30, max(px, rx) + 30, py + 30, shift=True)
        time.sleep(1.0)

        selected_rels = json.loads(js(window, "JSON.stringify([...graph.selectedRelIds])"))
        assert sorted(selected_rels) == sorted([ids["rel1"], ids["rel2"]]), (
            f"shift+drag should box-select both relationships, got {selected_rels}"
        )
        assert js(window, "graph.selectedIds.size") == 0, "shift+drag box-select should not also select motes"
        count_text = js(window, "document.getElementById('detail-relationship-multi-count').textContent")
        assert count_text == "2 relationships selected", f"expected the relationship aggregate panel, got {count_text!r}"

    check("shift+drag box-selects only relationships, with the aggregate panel", shift_drag_box_selects_only_relationships)

    def plain_drag_box_still_selects_only_motes():
        """Regression guard for the shift+drag relation box-select: plain
        drag over the exact same rectangle must still behave like ordinary
        mote box-select, now that additive mote box-select (shift+drag's
        old meaning) is gone."""
        ids = json.loads(js(window, "JSON.stringify(window.__shiftBox)"))
        px, py = node_xy(window, "window.__shiftBox.p")
        rx, ry = node_xy(window, "window.__shiftBox.r")
        drag(window, min(px, rx) - 30, py - 30, max(px, rx) + 30, py + 30)
        time.sleep(1.0)

        selected_motes = json.loads(js(window, "JSON.stringify([...graph.selectedIds])"))
        assert sorted(selected_motes) == sorted([ids["p"], ids["q"], ids["r"]]), (
            f"plain drag should box-select the motes, got {selected_motes}"
        )
        assert js(window, "graph.selectedRelIds.size") == 0, "plain drag box-select should not select any relationships"

    check("plain drag still box-selects only motes (shift+drag is relations-only)", plain_drag_box_still_selects_only_motes)

    def shift_drag_highlights_relationships_live_during_the_drag():
        result = js_async(window, """
            async () => {
                const p = await api.create_mote({name: 'LiveRel-P'});
                const q = await api.create_mote({name: 'LiveRel-Q'});
                const r = await api.create_mote({name: 'LiveRel-R'});
                const rel1 = await api.create_relationship(p.id, q.id, 'knows');
                const rel2 = await api.create_relationship(q.id, r.id, 'knows');
                window.__liveRel = {p: p.id, q: q.id, r: r.id, rel1: rel1.id, rel2: rel2.id};
                await refreshMoteCache();
                await refocus(null);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"live relation box-select setup failed: {result}"
        ids = json.loads(js(window, "JSON.stringify(window.__liveRel)"))

        # Parked far out in empty world space, like the shift+drag box-select
        # check above -- and in a *different* empty spot from that one, since
        # its three motes are still pinned where it left them. Everything the
        # suite has created with no species anchors at world (0, 0), so a box
        # drawn around three motes left near the origin catches whatever else
        # the layout has pushed through that spot: a coin flip, not a property
        # of the code under test.
        js(window, "graph.pin(window.__liveRel.p); graph.placeAt(window.__liveRel.p, 5900, -6000);")
        js(window, "graph.pin(window.__liveRel.q); graph.placeAt(window.__liveRel.q, 6000, -6000);")
        js(window, "graph.pin(window.__liveRel.r); graph.placeAt(window.__liveRel.r, 6100, -6000);")
        time.sleep(0.3)
        # A fixed camera (q at screen center, generous scale) rather than
        # resetView()'s fitToContent() -- fitting *everything* accumulated
        # by this point in the suite would zoom out far enough to compress
        # p/q/r's carefully-spaced world coordinates down to a handful of
        # screen pixels, defeating the "partial box" step below regardless
        # of margins.
        # panTarget too, not just the auto-fit: refocus() above panned the
        # camera toward the focused mote, and that animation would keep
        # dragging this fixed camera back off these three motes for as
        # long as the drag below takes.
        js(window, "graph._fitUntilAlpha = 0; graph.panTarget = null; graph.camera = {x: -18000, y: 18000, scale: 3};")
        time.sleep(0.3)

        px, py = node_xy(window, "window.__liveRel.p")
        qx, qy = node_xy(window, "window.__liveRel.q")
        rx, ry = node_xy(window, "window.__liveRel.r")
        # Midpoint of rel1 (p-q) in screen space -- the box's right edge
        # stops here, short of q itself. q is rel2's *other* endpoint too,
        # so a box reaching all the way to q would inherently graze rel2's
        # curve sample there as well, even though none of rel2 is "really"
        # covered -- stopping at the midpoint keeps the two edges' shared
        # vertex out of the box entirely.
        pq_mid_x = (px + qx) / 2

        def fire(event_type, x, y, buttons=1):
            js(window, f"""
                document.getElementById('graph').dispatchEvent(new MouseEvent({json.dumps(event_type)}, {{
                    clientX: {x}, clientY: {y}, bubbles: true, cancelable: true,
                    button: 0, buttons: {buttons}, shiftKey: true,
                }}));
            """)

        fire("mousedown", px - 30, py - 40)
        time.sleep(0.1)
        # Grow the box just far enough to cover part of rel1 (p-q) but
        # nowhere near q or rel2 (q-r).
        fire("mousemove", pq_mid_x, py + 40)
        time.sleep(0.3)
        mid_selection = json.loads(js(window, "JSON.stringify([...graph.selectedRelIds])"))
        assert mid_selection == [ids["rel1"]], (
            f"mid-drag, only rel1 should be highlighted yet, got {mid_selection}"
        )
        box_is_rel_colored = js(window, "document.getElementById('select-box').classList.contains('rel')")
        assert box_is_rel_colored is True, "the box overlay should carry the distinct relation-select color mid-drag"

        # Grow it to cover both, still without releasing the mouse.
        fire("mousemove", rx + 40, ry + 40)
        time.sleep(0.3)
        grown_selection = json.loads(js(window, "JSON.stringify([...graph.selectedRelIds])"))
        assert sorted(grown_selection) == sorted([ids["rel1"], ids["rel2"]]), (
            f"mid-drag, growing the box should add rel2 live, got {grown_selection}"
        )
        assert js(window, "graph.selectedIds.size") == 0, "relation box-select must never touch mote selection mid-drag"

        fire("mouseup", rx + 40, ry + 40)
        time.sleep(0.5)

    check("shift+drag highlights relationships live during the drag, with the distinct box color",
          shift_drag_highlights_relationships_live_during_the_drag)

    def shift_drag_over_empty_space_clears_the_relationship_selection():
        # Select rel1 first (a plain click), so there's something to clear.
        mid = json.loads(js(window, """
            (function() {
                const a = graph.screenPositionOf(window.__liveRel.p);
                const b = graph.screenPositionOf(window.__liveRel.q);
                return JSON.stringify({x: (a.x + b.x) / 2, y: (a.y + b.y) / 2});
            })()
        """))
        drag(window, mid["x"], mid["y"], mid["x"], mid["y"])
        time.sleep(0.5)
        assert js(window, "graph.selectedRelIds.size") == 1, "setup: rel1 should be selected before the empty shift+drag"

        # A shift+drag far from any mote/edge (computed via the camera
        # transform itself, so it's empty regardless of the current pan/
        # zoom) should box-select zero relationships and clear the panel,
        # not leave rel1 selected.
        far = json.loads(js(window, "JSON.stringify(graph._worldToScreen(80000, 80000))"))
        drag(window, far["x"], far["y"], far["x"] + 100, far["y"] + 80, shift=True)
        time.sleep(0.5)

        assert js(window, "graph.selectedRelIds.size") == 0, "an empty relation box-select should clear the selection"
        hidden = js(window, "document.getElementById('detail-panel').classList.contains('hidden')")
        assert hidden is True, "an empty relation box-select should close the detail panel"

    check("shift+drag over empty space clears the relationship selection",
          shift_drag_over_empty_space_clears_the_relationship_selection)

    def reserved_names_get_a_badge():
        result = js_async(window, """
            async () => {
                const other = await api.create_mote({name: 'badge-other'});
                const m = await api.create_mote(
                    {name: 'badge-checker', url: 'example.com', colour: 'teal'});
                await api.create_relationship(m.id, other.id, 'applies_to');
                await api.create_relationship(m.id, other.id, 'knows');
                window.__badgeIds = {m: m.id, other: other.id};
                await refreshMoteCache();
                await refocus(m.id);
                await openDetailPanel(m.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"reserved-badge setup failed: {result}"

        badges = json.loads(js(window, """
            (function() {
                const rows = document.querySelectorAll('.ins-body .ins-row');
                const out = {};
                for (const r of rows) {
                    out[r.querySelector('.ins-key').value] =
                        r.querySelector('.ins-key').classList.contains('reserved');
                }
                return JSON.stringify(out);
            })()
        """))
        assert badges.get("url") is True, f"a reserved property (url) should be marked, got {badges}"
        assert badges.get("colour") is False, \
            f"an ordinary property (colour) should not be marked, got {badges}"
        assert badges.get("date") is not True, \
            "`date` stopped being reserved when a start became a value shape"

        rel_badges = json.loads(js(window, """
            (function() {
                const rows = document.querySelectorAll('.ins-body .ins-rel');
                const out = {};
                for (const r of rows) {
                    const t = r.querySelector('.ins-rel-type');
                    out[t.textContent] = t.classList.contains('reserved');
                }
                return JSON.stringify(out);
            })()
        """))
        assert rel_badges.get("applies_to") is True, (
            f"a reserved relationship type (applies_to) should get a badge, got {rel_badges}"
        )
        assert rel_badges.get("knows") is False, (
            f"an ordinary relationship type (knows) should not get a badge, got {rel_badges}"
        )

    check("reserved property/relationship names get a badge, ordinary ones don't", reserved_names_get_a_badge)

    def switch_app_launcher_opens_a_sibling_app():
        # Tools no longer lists Notes/Calendar directly -- every sibling
        # app opens through the one launcher (\u2318J / switch-app-btn), see
        # app.js::LAUNCHER_APPS. Stubbed for the same reason the old
        # per-app calls were: the real one spawns a second pywebview
        # process, which an automated suite has no business starting.
        js(window, """
            window.__openAppCalls = [];
            window.__origOpenApp = api.open_app.bind(api);
            api.open_app = (appId, moteId) => {
                window.__openAppCalls.push([appId, moteId === undefined ? null : moteId]);
                return Promise.resolve({app: appId, focus: moteId});
            };
        """)
        js(window, "document.getElementById('switch-app-btn').click();")
        time.sleep(0.3)
        opened = js(window, "!document.getElementById('launcher-overlay').classList.contains('hidden')")
        assert opened is True, "the switch-app button should open the launcher"
        names = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#launcher-results .launcher-name'))
                .map(e => e.textContent))
        """))
        assert "Monastery" in names, f"the launcher should list sibling apps, got {names}"
        assert "Monarch" not in names, "the launcher should not offer to open the app you're already in"

        js(window, """
            Array.from(document.querySelectorAll('#launcher-results .launcher-card'))
                .find(c => c.querySelector('.launcher-name').textContent === 'Monastery')
                .dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true}));
        """)
        time.sleep(0.4)
        calls = json.loads(js(window, "JSON.stringify(window.__openAppCalls)"))
        assert calls == [["monastery", None]], f"picking Monastery should open it with no mote, got {calls}"
        closed = js(window, "document.getElementById('launcher-overlay').classList.contains('hidden')")
        assert closed is True, "opening an app from the launcher should close it"

        js(window, "api.open_app = window.__origOpenApp;")

    check("the app launcher (\u2318J) lists and opens sibling apps", switch_app_launcher_opens_a_sibling_app)

    def a_note_property_shows_as_a_summary():
        # Injected as JSON rather than written inline: a .modoc source is
        # backslashes and newlines all the way down, and nesting it in a
        # Python string in a JS string in an eval is three escapings deep.
        js(window, f"window.__noteSource = {json.dumps(chr(92) + 'section{A}' + chr(10) * 2 + 'line two' + chr(10))};")
        js(window, """
            window.__openAppCalls = [];
            window.__origOpenApp = api.open_app.bind(api);
            api.open_app = (appId, moteId) => {
                window.__openAppCalls.push([appId, moteId === undefined ? null : moteId]);
                return Promise.resolve({app: appId, focus: moteId});
            };
        """)
        js_async(window, """
            async () => {
                const m = await api.create_mote({name: 'Notebook 2'});
                window.__noteMote = m.id;
                await api.update_mote(window.__noteMote, {modoc: window.__noteSource});
                await refreshMoteCache();
                await refocus(window.__noteMote);
                await openDetailPanel(window.__noteMote);
                const rows = Array.from(document.querySelectorAll('.ins-body .ins-row'));
                const row = rows.find(r => r.querySelector('.ins-key').value === 'modoc');
                // .ins-val, not the row: a property's key is an input too
                // (click to rename), and it would match first.
                const field = row.querySelector('.ins-val input, .ins-val textarea');
                window.__result = {
                    value: field.value,
                    readOnly: field.readOnly,
                    hasOpen: !!row.querySelector('.ins-follow'),
                };
            }
        """, wait=3.0)
        summary = json.loads(js(window, "JSON.stringify(window.__result)"))
        assert "4 lines" in summary["value"], \
            f"the row should summarize the note, got {summary['value']!r}"
        assert summary["readOnly"] is True, "a note must not be editable from the properties list"
        assert summary["hasOpen"] is False, "Mora's window is gone: notes are written in the console"

        js(window, "api.open_app = window.__origOpenApp;")

    check("a `modoc` property renders as a read-only summary",
          a_note_property_shows_as_a_summary)

    def depth_control_limits_the_graph():
        def visible_at(value):
            js(window, f"""
                const s = document.getElementById('depth-select');
                s.value = {json.dumps(value)};
                s.dispatchEvent(new Event('change'));
            """)
            time.sleep(2.0)
            return js(window, "graph.nodes.size")

        js(window, "(async () => { await refocus(window.__seed.aliceId); })()")
        time.sleep(2.0)
        one_hop = visible_at("1")
        whole = visible_at("all")
        assert one_hop < whole, f"1 hop should show fewer motes than the whole garden: {one_hop} vs {whole}"
        stored = js(window, "localStorage.getItem('monoss:depth')")
        assert stored == "all", f"depth setting should persist, got {stored!r}"

    check("depth control limits how much of the graph is visible", depth_control_limits_the_graph)

    def garden_switch_isolates_data():
        create_garden('smoke-second')
        result = js_async(window, """
            async () => {
                await api.switch_garden('smoke-second');
                await loadGarden('smoke-second');
                const motes = await api.list_motes();
                window.__result = motes.length;
            }
        """, wait=2.5)
        assert result == "0", f"new garden should start empty, got {result} motes"

        js_async(window, """
            async () => {
                await api.switch_garden('default');
                await loadGarden('default');
                window.__result = 'ok';
            }
        """, wait=2.5)
        count = js(window, "graph.nodes.size")
        assert count > 0, "switching back should restore the original garden's motes"

    check("garden switch isolates data", garden_switch_isolates_data)

    def manage_gardens_opens_the_gardens_window():
        """Creating, deleting, importing, exporting and syncing a garden
        left this window: they are the Gardens window's (gardens_app/),
        which every app opens the same way. What is still Monarch's is
        the way in -- both entry points, the garden dropdown's and
        File's, and the Tools -> Sync… item that now deep-links to the
        same window's sync settings.

        The bridge call is stubbed rather than let through: the real one
        spawns a second pywebview process, which a smoke run has no way
        to clean up."""
        js(window, """
            window.__opened = [];
            api.open_gardens = (sync) => { window.__opened.push(sync || false); return Promise.resolve({}); };
        """)

        for menu_btn, item in [
            ("garden-btn", "garden-menu-manage"),
            ("file-btn", "file-manage-gardens"),
            ("tools-btn", "open-sync-btn"),
        ]:
            js(window, f"document.getElementById('{menu_btn}').click();")
            time.sleep(0.4)
            js(window, f"document.getElementById('{item}').click();")
            time.sleep(0.4)

        opened = json.loads(js(window, "JSON.stringify(window.__opened)"))
        assert opened == [False, False, True], (
            "the two Manage gardens… items should open the window and Tools -> Sync… "
            f"should open it on sync, got {opened}"
        )

        for gone in ["gardens-overlay", "sync-overlay"]:
            assert js(window, f"!document.getElementById('{gone}')") is True, (
                f"#{gone} should be gone from this window entirely"
            )

    check("Manage gardens… and Sync… open the shared Gardens window", manage_gardens_opens_the_gardens_window)

    def box_select_multiple_nodes():
        result = js_async(window, """
            async () => {
                const a = await api.create_mote({name: 'box-A'});
                const b = await api.create_mote({name: 'box-B'});
                const c = await api.create_mote({name: 'box-C'});
                window.__box = {a: a.id, b: b.id, c: c.id};
                await refreshMoteCache();
                await refocus(a.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"box-select setup failed: {result}"
        ids = json.loads(js(window, "JSON.stringify(window.__box)"))

        # A and B are placed together; C sits far away and must not be
        # swept up by a box drawn tightly around A and B.
        js(window, "graph.pin(window.__box.a); graph.placeAt(window.__box.a, -2000, -2000);")
        js(window, "graph.pin(window.__box.b); graph.placeAt(window.__box.b, -1900, -2000);")
        js(window, "graph.pin(window.__box.c); graph.placeAt(window.__box.c, 2000, 2000);")
        time.sleep(0.3)
        js(window, "graph.resetView();")
        time.sleep(1.2)

        ax, ay = node_xy(window, "window.__box.a")
        bx, by = node_xy(window, "window.__box.b")
        drag(window, min(ax, bx) - 30, min(ay, by) - 30, max(ax, bx) + 30, max(ay, by) + 30)
        time.sleep(1.0)

        selected = json.loads(js(window, "JSON.stringify([...graph.selectedIds])"))
        assert sorted(selected) == sorted([ids["a"], ids["b"]]), (
            f"box-select should select exactly A and B, got {selected}"
        )
        count_text = js(window, "document.getElementById('detail-multi-count').textContent")
        assert count_text == "2 motes selected", f"expected the aggregate panel, got {count_text!r}"

    check("box-select selects multiple nodes and shows the aggregate panel", box_select_multiple_nodes)

    def box_select_highlights_live_during_the_drag():
        """Motes must highlight as the box grows over them, not only once
        the mouse is released -- checked by inspecting graph.selectedIds
        mid-drag, before any mouseup fires."""
        result = js_async(window, """
            async () => {
                const p = await api.create_mote({name: 'live-P'});
                const q = await api.create_mote({name: 'live-Q'});
                window.__live = {p: p.id, q: q.id};
                await refreshMoteCache();
                await refocus(null);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"live box-select setup failed: {result}"
        ids = json.loads(js(window, "JSON.stringify(window.__live)"))

        js(window, "graph.pin(window.__live.p); graph.placeAt(window.__live.p, -3000, -3000);")
        js(window, "graph.pin(window.__live.q); graph.placeAt(window.__live.q, -2900, -2900);")
        time.sleep(0.3)
        js(window, "graph.resetView();")
        time.sleep(1.2)

        px, py = node_xy(window, "window.__live.p")
        qx, qy = node_xy(window, "window.__live.q")

        def fire(event_type, x, y, buttons=1):
            js(window, f"""
                document.getElementById('graph').dispatchEvent(new MouseEvent({json.dumps(event_type)}, {{
                    clientX: {x}, clientY: {y}, bubbles: true, cancelable: true,
                    button: 0, buttons: {buttons},
                }}));
            """)

        fire("mousedown", px - 40, py - 40)
        time.sleep(0.1)
        # Grow the box just far enough to cover P but not yet Q.
        fire("mousemove", px + 10, py + 10)
        time.sleep(0.3)
        mid_selection = json.loads(js(window, "JSON.stringify([...graph.selectedIds])"))
        assert mid_selection == [ids["p"]], (
            f"mid-drag, only P should be highlighted yet, got {mid_selection}"
        )

        # Grow it to cover both, still without releasing the mouse.
        fire("mousemove", qx + 40, qy + 40)
        time.sleep(0.3)
        grown_selection = json.loads(js(window, "JSON.stringify([...graph.selectedIds])"))
        assert sorted(grown_selection) == sorted([ids["p"], ids["q"]]), (
            f"mid-drag, growing the box should add Q live, got {grown_selection}"
        )

        fire("mouseup", qx + 40, qy + 40)
        time.sleep(0.5)

    check("box-select highlights motes live as the box grows, before mouseup", box_select_highlights_live_during_the_drag)

    def esc_deselects():
        # Left selected by the live box-select check above: P and Q, panel open.
        press_key(window, "Escape")
        time.sleep(0.5)
        size = js(window, "graph.selectedIds.size")
        assert size == 0, f"Escape should deselect once nothing else is open, got {size} still selected"
        hidden = js(window, "document.getElementById('detail-panel').classList.contains('hidden')")
        assert hidden is True, "the panel should close along with deselecting"

    check("Esc deselects", esc_deselects)

    def del_key_deletes_selection():
        ids = json.loads(js(window, "JSON.stringify(window.__box)"))
        js(window, "graph.resetView();")
        time.sleep(1.0)
        ax, ay = node_xy(window, "window.__box.a")
        bx, by = node_xy(window, "window.__box.b")
        drag(window, min(ax, bx) - 30, min(ay, by) - 30, max(ax, bx) + 30, max(ay, by) + 30)
        time.sleep(1.0)
        assert js(window, "graph.selectedIds.size") == 2, "setup: expected A and B selected"

        # A field left focused by an earlier check (a stray input never
        # explicitly blurred) makes isTypingInField() swallow this
        # keypress with no error -- blur defensively rather than let that
        # show up as a flaky failure several checks downstream.
        js(window, "document.activeElement && document.activeElement.blur();")
        js(window, "window.confirm = () => true;")
        press_key(window, "Delete")
        time.sleep(2.0)

        remaining = json.loads(js_async(window, """
            async () => { window.__result = (await api.list_motes()).map(m => m.id); }
        """))
        assert ids["a"] not in remaining and ids["b"] not in remaining, "Del should delete the selection"
        assert ids["c"] in remaining, "Del must not touch motes outside the selection"

    check("Del key deletes the selected motes", del_key_deletes_selection)

    def undo_restores_deleted_motes_and_relationships():
        result = js_async(window, """
            async () => {
                const a = await api.create_mote({name: 'undo-A'});
                const b = await api.create_mote({name: 'undo-B'});
                await api.create_relationship(a.id, b.id, 'linked-for-undo');
                window.__undoPair = {a: a.id, b: b.id};
                await refreshMoteCache();
                await refocus(null);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"undo setup failed: {result}"
        ids = json.loads(js(window, "JSON.stringify(window.__undoPair)"))

        js(window, "graph.select([window.__undoPair.a, window.__undoPair.b], {pan: false});")
        time.sleep(0.3)
        js(window, "document.activeElement && document.activeElement.blur();")
        js(window, "window.confirm = () => true;")
        press_key(window, "Delete")
        time.sleep(2.0)

        remaining = json.loads(js_async(window, "async () => { window.__result = (await api.list_motes()).map(m => m.id); }"))
        assert ids["a"] not in remaining and ids["b"] not in remaining, "setup: Del should have deleted the pair"

        toast_text = js(window, "document.getElementById('toast').textContent")
        assert "Undo" in toast_text, f"a delete should surface an Undo toast, got {toast_text!r}"

        js(window, "document.querySelector('#toast button').click();")
        time.sleep(2.0)

        restored = json.loads(js_async(window, "async () => { window.__result = await api.list_motes(); }"))
        restored_ids = [m["id"] for m in restored]
        assert ids["a"] in restored_ids and ids["b"] in restored_ids, (
            f"Undo should restore both motes with their original ids, got {restored_ids}"
        )
        restored_a = next(m for m in restored if m["id"] == ids["a"])
        assert restored_a["properties"].get("name") == "undo-A", "restored mote should keep its properties"

        rels = json.loads(js_async(window, f"""
            async () => {{ window.__result = await api.mote_relationships({json.dumps(ids["a"])}); }}
        """))
        assert any(r["type"] == "linked-for-undo" for r in rels), (
            f"Undo should also restore the relationship between them, got {rels}"
        )

    check("delete: Undo toast restores deleted motes and their relationship", undo_restores_deleted_motes_and_relationships)

    def ctrl_z_undoes_a_delete():
        result = js_async(window, """
            async () => {
                const m = await api.create_mote({name: 'ctrlz-victim'});
                window.__ctrlZId = m.id;
                await refreshMoteCache();
                await refocus(m.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"ctrl+z setup failed: {result}"
        victim_id = json.loads(js(window, "JSON.stringify(window.__ctrlZId)"))

        js(window, "graph.select(window.__ctrlZId, {pan: false});")
        time.sleep(0.3)
        js(window, "document.activeElement && document.activeElement.blur();")
        js(window, "window.confirm = () => true;")
        press_key(window, "Delete")
        time.sleep(2.0)

        remaining = json.loads(js_async(window, "async () => { window.__result = (await api.list_motes()).map(m => m.id); }"))
        assert victim_id not in remaining, "setup: Del should have deleted the mote"

        js(window, "window.dispatchEvent(new KeyboardEvent('keydown', {key: 'z', ctrlKey: true, bubbles: true}));")
        time.sleep(2.0)

        restored = json.loads(js_async(window, "async () => { window.__result = (await api.list_motes()).map(m => m.id); }"))
        assert victim_id in restored, "Ctrl+Z should undo the delete"

    check("Ctrl+Z undoes a delete", ctrl_z_undoes_a_delete)

    def middle_drag_pans():
        js(window, "graph.resetView();")
        time.sleep(1.0)
        before = json.loads(js(window, "JSON.stringify(graph.camera)"))
        drag(window, 400, 400, 500, 470, button=1)
        time.sleep(0.6)
        after = json.loads(js(window, "JSON.stringify(graph.camera)"))
        assert (before["x"], before["y"]) != (after["x"], after["y"]), (
            f"middle-drag should pan the camera: {before} -> {after}"
        )

    check("middle-drag pans the camera", middle_drag_pans)

    def context_menu_copy_paste():
        result = js_async(window, """
            async () => {
                const src = await api.create_mote({name: 'copy-src', flavor: 'vanilla'});
                window.__copySrcId = src.id;
                await refreshMoteCache();
                await refocus(src.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"copy/paste setup failed: {result}"
        src_id = json.loads(js(window, "JSON.stringify(window.__copySrcId)"))

        js(window, "graph.resetView();")
        time.sleep(1.0)
        x, y = node_xy(window, "window.__copySrcId")
        right_click(window, x, y)
        time.sleep(0.5)
        click_menu_item(window, "Copy")
        time.sleep(0.5)
        assert js(window, "clipboard !== null") is True, "Copy should populate the clipboard"

        right_click(window, *empty_canvas_point(window))
        time.sleep(0.5)
        click_menu_item(window, "Paste")
        time.sleep(2.0)

        motes = json.loads(js_async(window, "async () => { window.__result = await api.list_motes(); }"))
        pasted = [m for m in motes if m["id"] != src_id and m["properties"].get("flavor") == "vanilla"]
        assert len(pasted) == 1, f"paste should create exactly one duplicate, got {pasted}"
        assert pasted[0]["properties"]["name"] == "copy-src", (
            f"pasted mote should match the copied properties, got {pasted[0]['properties']}"
        )

    check("context menu: Copy then Paste duplicates a mote", context_menu_copy_paste)

    def copy_paste_a_mote_with_a_non_string_property():
        # Regression: copySelection/pasteAt spread a mote's *already
        # JSON-coerced* properties (numbers, bools, ...) straight into
        # create_mote, which used to unconditionally re-run json.loads()
        # on every value -- crashing with "the JSON object must be str,
        # bytes or bytearray, not int" for anything that wasn't a string.
        result = js_async(window, """
            async () => {
                const src = await api.create_mote({name: 'num-src', age: '21'});
                window.__numSrcId = src.id;
                await refreshMoteCache();
                await refocus(src.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"numeric-property copy/paste setup failed: {result}"
        src_id = json.loads(js(window, "JSON.stringify(window.__numSrcId)"))
        src_age = js(window, "moteById(window.__numSrcId).properties.age")
        assert src_age == 21, f"setup: age should already be coerced to an int, got {src_age!r}"

        js(window, "graph.resetView();")
        time.sleep(1.0)
        x, y = node_xy(window, "window.__numSrcId")
        right_click(window, x, y)
        time.sleep(0.5)
        click_menu_item(window, "Copy")
        time.sleep(0.5)
        right_click(window, *empty_canvas_point(window))
        time.sleep(0.5)
        click_menu_item(window, "Paste")
        time.sleep(2.0)

        motes = json.loads(js_async(window, "async () => { window.__result = await api.list_motes(); }"))
        pasted = [m for m in motes if m["id"] != src_id and m["properties"].get("name") == "num-src"]
        assert len(pasted) == 1, f"pasting a mote with a numeric property should not silently fail, got {pasted}"
        assert pasted[0]["properties"]["age"] == 21, (
            f"pasted numeric property should keep its type, got {pasted[0]['properties']}"
        )

    check("copy/paste a mote with a non-string property doesn't error", copy_paste_a_mote_with_a_non_string_property)

    def multi_copy_paste_preserves_relative_layout():
        result = js_async(window, """
            async () => {
                const p = await api.create_mote({name: 'pair-P'});
                const q = await api.create_mote({name: 'pair-Q'});
                window.__pair = {p: p.id, q: q.id};
                await refreshMoteCache();
                await refocus(null);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"multi-copy setup failed: {result}"

        js(window, "graph.pin(window.__pair.p); graph.placeAt(window.__pair.p, -400, -400);")
        js(window, "graph.pin(window.__pair.q); graph.placeAt(window.__pair.q, -200, -400);")  # 200 apart
        time.sleep(0.3)
        js(window, "graph.resetView();")
        time.sleep(1.0)

        js(window, "graph.select([window.__pair.p, window.__pair.q], {pan: false});")
        time.sleep(0.5)
        x, y = node_xy(window, "window.__pair.p")
        right_click(window, x, y)
        time.sleep(0.5)
        menu_text = js(window, "document.getElementById('context-menu').innerText")
        assert "Copy 2 motes" in menu_text, (
            f"right-clicking a member of a multi-selection should offer to copy the whole "
            f"selection, got {menu_text!r}"
        )
        click_menu_item(window, "Copy 2 motes")
        time.sleep(0.5)
        assert js(window, "clipboard.length") == 2, "Copy should capture both selected motes"

        right_click(window, *empty_canvas_point(window))
        time.sleep(0.5)
        paste_label = js(window, """
            Array.from(document.querySelectorAll('#context-menu .menu-item'))
                .map(i => i.textContent).find(t => t.startsWith('Paste'))
        """)
        assert paste_label == "Paste 2 motes", f"expected a pluralized paste label, got {paste_label!r}"
        click_menu_item(window, "Paste 2 motes")
        time.sleep(2.5)

        ids = json.loads(js(window, "JSON.stringify(window.__pair)"))
        pasted = json.loads(js_async(window, "async () => { window.__result = await api.list_motes(); }"))
        new_p = next(m for m in pasted if m["properties"].get("name") == "pair-P" and m["id"] != ids["p"])
        new_q = next(m for m in pasted if m["properties"].get("name") == "pair-Q" and m["id"] != ids["q"])
        pos_p = json.loads(js(window, f"JSON.stringify(graph.nodes.get({json.dumps(new_p['id'])}))"))
        pos_q = json.loads(js(window, f"JSON.stringify(graph.nodes.get({json.dumps(new_q['id'])}))"))
        gap = abs(pos_p["x"] - pos_q["x"])
        assert abs(gap - 200) < 1, f"pasted pair should keep their original 200-unit offset, got {gap}"

        selected = js(window, "graph.selectedIds.size")
        assert selected == 2, f"the pasted group should end up selected, got {selected}"

    check("multi-select copy/paste preserves relative layout", multi_copy_paste_preserves_relative_layout)

    def copy_paste_reproduces_every_relationship():
        # Regression: copy/paste used to only recreate a copied mote's
        # species relationship, silently dropping every other one --
        # including a relationship directly between two copied motes.
        result = js_async(window, """
            async () => {
                const species = await api.create_mote({name: 'Widget'});
                const a = await api.create_mote({name: 'rel-A'});
                const b = await api.create_mote({name: 'rel-B'});
                await api.create_relationship(a.id, b.id, 'knows');
                await api.set_species(a.id, species.id);
                window.__rel = {a: a.id, b: b.id, species: species.id};
                await refreshMoteCache();
                await refocus(null);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"copy-relationships setup failed: {result}"
        ids = json.loads(js(window, "JSON.stringify(window.__rel)"))

        js(window, "graph.pin(window.__rel.a); graph.placeAt(window.__rel.a, -500, -500);")
        js(window, "graph.pin(window.__rel.b); graph.placeAt(window.__rel.b, -300, -500);")
        time.sleep(0.3)
        js(window, "graph.resetView();")
        time.sleep(1.0)

        js(window, "graph.select([window.__rel.a, window.__rel.b], {pan: false});")
        time.sleep(0.3)
        ax, ay = node_xy(window, "window.__rel.a")
        right_click(window, ax, ay)
        time.sleep(0.5)
        click_menu_item(window, "Copy 2 motes")
        time.sleep(0.5)

        right_click(window, *empty_canvas_point(window))
        time.sleep(0.5)
        click_menu_item(window, "Paste 2 motes")
        time.sleep(2.5)

        motes = json.loads(js_async(window, "async () => { window.__result = await api.list_motes(); }"))
        new_a = next(m for m in motes if m["properties"].get("name") == "rel-A" and m["id"] != ids["a"])
        new_b = next(m for m in motes if m["properties"].get("name") == "rel-B" and m["id"] != ids["b"])

        rels_new_a = json.loads(js_async(window, f"""
            async () => {{ window.__result = await api.mote_relationships({json.dumps(new_a["id"])}); }}
        """))
        knows_between_copies = any(
            r["type"] == "knows" and {r["from_id"], r["to_id"]} == {new_a["id"], new_b["id"]}
            for r in rels_new_a
        )
        assert knows_between_copies, (
            f"a relationship between two copied motes should reproduce between their pasted "
            f"copies, got {rels_new_a}"
        )

        species_rel = [r for r in rels_new_a if r["type"] == "instance_of"]
        assert any(r["to_id"] == ids["species"] for r in species_rel), (
            f"a relationship to a mote outside the selection should still point at the "
            f"original, got {species_rel}"
        )

    check("copy/paste reproduces every relationship, not just species", copy_paste_reproduces_every_relationship)

    def ctrl_c_ctrl_v_round_trip():
        result = js_async(window, """
            async () => {
                const m = await api.create_mote({name: 'kbd-copy', tag: 'kbd'});
                window.__kbdId = m.id;
                await refreshMoteCache();
                await refocus(null);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"ctrl+c/v setup failed: {result}"
        src_id = json.loads(js(window, "JSON.stringify(window.__kbdId)"))

        js(window, "graph.select(window.__kbdId, {pan: false});")
        time.sleep(0.3)
        js(window, "document.activeElement && document.activeElement.blur();")
        # press_key() has no modifier support, so dispatch Ctrl+C directly.
        js(window, """
            window.dispatchEvent(new KeyboardEvent('keydown', {key: 'c', ctrlKey: true, bubbles: true}));
        """)
        time.sleep(0.5)
        assert js(window, "clipboard !== null") is True, "Ctrl+C should copy the current selection"

        js(window, """
            window.dispatchEvent(new KeyboardEvent('keydown', {key: 'v', ctrlKey: true, bubbles: true}));
        """)
        time.sleep(2.5)

        motes = json.loads(js_async(window, "async () => { window.__result = await api.list_motes(); }"))
        pasted = [m for m in motes if m["id"] != src_id and m["properties"].get("tag") == "kbd"]
        assert len(pasted) == 1, f"Ctrl+V should paste exactly one duplicate, got {pasted}"

    check("Ctrl+C / Ctrl+V copies and pastes the selection", ctrl_c_ctrl_v_round_trip)

    def ctrl_c_is_ignored_while_typing():
        js(window, "clipboard = null;")
        result = js_async(window, """
            async () => {
                const m = await api.create_mote({name: 'typing-guard'});
                window.__typingId = m.id;
                await refreshMoteCache();
                await selectMote(m.id, {pan: false});
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"typing-guard setup failed: {result}"

        js(window, "document.querySelector('.ins-add-key').focus();")
        time.sleep(0.2)
        focused = js(window, "document.activeElement.className")
        assert focused == "ins-input ins-add-key", \
            "setup: the property key field should have focus"
        js(window, """
            document.querySelector('.ins-add-key').dispatchEvent(
                new KeyboardEvent('keydown', {key: 'c', ctrlKey: true, bubbles: true}));
        """)
        time.sleep(0.3)
        assert js(window, "clipboard") is None, (
            "Ctrl+C while typing in a field must not hijack normal text copy"
        )
        js(window, "document.querySelector('.ins-add-key').blur();")

    check("Ctrl+C is ignored while typing in a field", ctrl_c_is_ignored_while_typing)

    def pin_unpin_via_menu():
        result = js_async(window, """
            async () => {
                const m = await api.create_mote({name: 'pin-test'});
                window.__pinId = m.id;
                await refreshMoteCache();
                await refocus(m.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"pin/unpin setup failed: {result}"
        js(window, "graph.resetView();")
        time.sleep(1.0)

        before_pinned = js(window, "graph.nodes.get(window.__pinId).pinned")
        x, y = node_xy(window, "window.__pinId")
        right_click(window, x, y)
        time.sleep(0.5)
        click_menu_item(window, "Unpin" if before_pinned else "Pin")
        time.sleep(0.5)
        after_pinned = js(window, "graph.nodes.get(window.__pinId).pinned")
        assert after_pinned != before_pinned, (
            f"the menu should toggle pinned state: {before_pinned} -> {after_pinned}"
        )

    check("Pin/Unpin toggles from the context menu", pin_unpin_via_menu)

    def new_mote_here_places_it_at_the_click():
        js(window, "graph.resetView();")
        time.sleep(1.2)

        x, y = 700, 400
        # The world point under the click, captured before any further
        # camera motion -- comparing against this rather than re-deriving
        # a screen position afterward means a camera that eases/auto-fits
        # while the new mote is being created can't make this check flaky.
        expected_world = json.loads(js(window, f"JSON.stringify(graph._worldAt({x}, {y}))"))
        right_click(window, x, y)
        time.sleep(0.5)
        click_menu_item(window, "New mote here")
        time.sleep(1.5)

        shown = js(window, "!document.querySelector('.ins').classList.contains('hidden')")
        assert shown is True, "New mote here should select the new mote and open its panel"
        node = json.loads(js(window, "JSON.stringify(graph.nodes.get(focusId))"))
        assert abs(node["x"] - expected_world["x"]) < 1 and abs(node["y"] - expected_world["y"]) < 1, \
            f"the new mote should land at the clicked world point {expected_world}, got {node}"
        assert node["pinned"] is True, "a mote placed by right-click should be pinned there"

    check("New mote here places the new mote at the click point", new_mote_here_places_it_at_the_click)

    def dragging_one_mote_moves_the_whole_selection():
        result = js_async(window, """
            async () => {
                const a = await api.create_mote({name: 'drag-A'});
                const b = await api.create_mote({name: 'drag-B'});
                const c = await api.create_mote({name: 'drag-C'});
                window.__drag = {a: a.id, b: b.id, c: c.id};
                await refreshMoteCache();
                await refocus(null);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"group-drag setup failed: {result}"

        js(window, "graph.pin(window.__drag.a); graph.placeAt(window.__drag.a, -400, -400);")
        js(window, "graph.pin(window.__drag.b); graph.placeAt(window.__drag.b, -200, -400);")
        js(window, "graph.pin(window.__drag.c); graph.placeAt(window.__drag.c, 2000, 2000);")
        time.sleep(0.3)
        js(window, "graph.resetView();")
        time.sleep(1.0)

        js(window, "graph.select([window.__drag.a, window.__drag.b], {pan: false});")
        time.sleep(0.3)
        before_b = json.loads(js(window, "JSON.stringify({x: graph.nodes.get(window.__drag.b).x, y: graph.nodes.get(window.__drag.b).y})"))
        before_a = json.loads(js(window, "JSON.stringify({x: graph.nodes.get(window.__drag.a).x, y: graph.nodes.get(window.__drag.a).y})"))
        ax, ay = node_xy(window, "window.__drag.a")
        drag(window, ax, ay, ax + 150, ay - 80)
        time.sleep(1.0)

        after_a = json.loads(js(window, "JSON.stringify({x: graph.nodes.get(window.__drag.a).x, y: graph.nodes.get(window.__drag.a).y})"))
        after_b = json.loads(js(window, "JSON.stringify({x: graph.nodes.get(window.__drag.b).x, y: graph.nodes.get(window.__drag.b).y})"))
        adx, ady = after_a["x"] - before_a["x"], after_a["y"] - before_a["y"]
        bdx, bdy = after_b["x"] - before_b["x"], after_b["y"] - before_b["y"]
        assert abs(adx - bdx) < 0.5 and abs(ady - bdy) < 0.5, (
            f"B should move by the same delta as the dragged A: A moved ({adx},{ady}), B moved ({bdx},{bdy})"
        )
        assert js(window, "graph.nodes.get(window.__drag.b).pinned") is True, (
            "the un-dragged group member should also end up pinned"
        )

        c_pos = json.loads(js(window, "JSON.stringify({x: graph.nodes.get(window.__drag.c).x, y: graph.nodes.get(window.__drag.c).y})"))
        assert c_pos == {"x": 2000, "y": 2000}, f"a mote outside the selection must not move, got {c_pos}"

    check("dragging one mote moves its whole multi-selection together", dragging_one_mote_moves_the_whole_selection)

    def group_pin_unpin_via_menu():
        result = js_async(window, """
            async () => {
                const a = await api.create_mote({name: 'grouppin-A'});
                const b = await api.create_mote({name: 'grouppin-B'});
                window.__grouppin = {a: a.id, b: b.id};
                await refreshMoteCache();
                await refocus(null);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"group pin/unpin setup failed: {result}"

        # placeAt() itself pins, so position first and unpin after -- the
        # reverse order would silently leave both pinned.
        js(window, "graph.placeAt(window.__grouppin.a, -300, -300); graph.placeAt(window.__grouppin.b, -100, -300);")
        js(window, "graph.unpin(window.__grouppin.a); graph.unpin(window.__grouppin.b);")
        time.sleep(0.3)
        js(window, "graph.resetView();")
        time.sleep(1.0)

        js(window, "graph.select([window.__grouppin.a, window.__grouppin.b], {pan: false});")
        time.sleep(0.3)
        ax, ay = node_xy(window, "window.__grouppin.a")
        right_click(window, ax, ay)
        time.sleep(0.5)
        menu_text = js(window, "document.getElementById('context-menu').innerText")
        assert "Pin 2 motes" in menu_text, f"expected a pluralized Pin label when both are unpinned, got {menu_text!r}"
        click_menu_item(window, "Pin 2 motes")
        time.sleep(0.5)
        pinned = json.loads(js(window, """
            JSON.stringify({a: graph.nodes.get(window.__grouppin.a).pinned, b: graph.nodes.get(window.__grouppin.b).pinned})
        """))
        assert pinned == {"a": True, "b": True}, f"Pin on a multi-selection should pin every member, got {pinned}"

        right_click(window, ax, ay)
        time.sleep(0.5)
        menu_text2 = js(window, "document.getElementById('context-menu').innerText")
        assert "Unpin 2 motes" in menu_text2, f"expected Unpin once every member is pinned, got {menu_text2!r}"
        click_menu_item(window, "Unpin 2 motes")
        time.sleep(0.5)
        unpinned = json.loads(js(window, """
            JSON.stringify({a: graph.nodes.get(window.__grouppin.a).pinned, b: graph.nodes.get(window.__grouppin.b).pinned})
        """))
        assert unpinned == {"a": False, "b": False}, f"Unpin on a multi-selection should unpin every member, got {unpinned}"

    check("Pin/Unpin on a multi-selection acts on the whole group", group_pin_unpin_via_menu)

    def multi_select_species_breakdown():
        result = js_async(window, """
            async () => {
                const species = await api.create_mote({name: 'Widget'});
                const w1 = await api.create_mote({name: 'w1'});
                const w2 = await api.create_mote({name: 'w2'});
                const loose = await api.create_mote({name: 'loose-one'});
                await api.set_species(w1.id, species.id);
                await api.set_species(w2.id, species.id);
                window.__multi = {w1: w1.id, w2: w2.id, loose: loose.id};
                await refreshMoteCache();
                await refocus(null);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"multi-select setup failed: {result}"

        js(window, """
            const ids = [window.__multi.w1, window.__multi.w2, window.__multi.loose];
            graph.select(ids, {pan: false});
            syncSelectionPanel(ids);
        """)
        time.sleep(1.5)

        rows = dict(json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('.multi-species-row')).map(r =>
                [r.querySelector('.name').textContent, r.querySelector('.count').textContent]))
        """)))
        assert rows.get("Widget") == "2", f"expected Widget:2 in the breakdown, got {rows}"
        assert rows.get("No species") == "1", f"expected No species:1 in the breakdown, got {rows}"

    check("multi-select panel shows the correct species breakdown", multi_select_species_breakdown)

    def arrow_key_navigation():
        result = js_async(window, """
            async () => {
                const center = await api.create_mote({name: 'cross-center'});
                const up = await api.create_mote({name: 'cross-up'});
                const down = await api.create_mote({name: 'cross-down'});
                const left = await api.create_mote({name: 'cross-left'});
                const right = await api.create_mote({name: 'cross-right'});
                await api.create_relationship(center.id, up.id, 'link');
                await api.create_relationship(center.id, down.id, 'link');
                await api.create_relationship(center.id, left.id, 'link');
                await api.create_relationship(center.id, right.id, 'link');
                window.__cross = {
                    center: center.id, up: up.id, down: down.id, left: left.id, right: right.id,
                };
                await refreshMoteCache();
                await refocus(center.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"arrow-nav setup failed: {result}"
        ids = json.loads(js(window, "JSON.stringify(window.__cross)"))

        js(window, "graph.pin(window.__cross.center); graph.placeAt(window.__cross.center, 0, 0);")
        js(window, "graph.pin(window.__cross.up); graph.placeAt(window.__cross.up, 0, -200);")
        js(window, "graph.pin(window.__cross.down); graph.placeAt(window.__cross.down, 0, 200);")
        js(window, "graph.pin(window.__cross.left); graph.placeAt(window.__cross.left, -200, 0);")
        js(window, "graph.pin(window.__cross.right); graph.placeAt(window.__cross.right, 200, 0);")
        time.sleep(0.3)
        js(window, "graph.resetView();")
        time.sleep(1.0)

        for key, expected in [("ArrowUp", "up"), ("ArrowDown", "down"), ("ArrowLeft", "left"), ("ArrowRight", "right")]:
            js(window, "graph.select(window.__cross.center, {pan: false});")
            time.sleep(0.3)
            press_key(window, key)
            time.sleep(1.2)
            selected = js(window, "graph.selectedId")
            assert selected == ids[expected], f"{key} should select {expected!r}, got {selected!r}"

    check("arrow keys navigate to the geometrically correct neighbour", arrow_key_navigation)

    def auto_fit_cancels_on_interaction():
        """The post-load auto-fit re-frames the camera every frame for a
        couple of seconds while the layout settles. Before this was fixed,
        dragging a node during that window looked broken -- the very next
        frame's fitToContent() would silently overwrite the camera out
        from under the user's gesture."""
        create_garden('smoke-autofit')
        result = js_async(window, """
            async () => {
                await api.switch_garden('smoke-autofit');
                const a = await api.create_mote({name: 'only'});
                window.__afId = a.id;
                await loadGarden('smoke-autofit');
                window.__result = 'ok';
            }
        """, wait=1.0)
        assert result == '"ok"', f"auto-fit setup failed: {result}"
        assert js(window, "!!graph._fitUntilAlpha") is True, (
            "test is meaningless unless the post-load auto-fit is still running"
        )

        x, y = node_xy(window, "window.__afId")
        drag(window, x, y, x + 150, y - 80)
        time.sleep(0.3)
        assert js(window, "graph._fitUntilAlpha") == 0, "a drag should cancel the post-load auto-fit immediately"

        time.sleep(2.5)  # let any would-be auto-fit finish, if it were still fighting the drag
        node = json.loads(js(window, "JSON.stringify(graph.nodes.get(window.__afId))"))
        assert node["pinned"] is True, (
            "the dragged node should stay exactly where the user put it, not get reset by auto-fit"
        )

        js_async(window, """
            async () => {
                await api.switch_garden('default');
                await loadGarden('default');
                window.__result = 'ok';
            }
        """, wait=2.0)

    check("dragging right after opening a garden isn't fought by auto-fit", auto_fit_cancels_on_interaction)

    def hidden_motes_are_excluded_from_physics():
        """Regression guard for the _step() perf fix: a filtered-out
        species' instances should drop out of the physics working set
        entirely, not just stop being drawn. Seeds a real batch (60 motes)
        in a throwaway garden so alpha is genuinely high (physics actively
        running) right after load, then hides the species immediately and
        checks a hidden instance's position/velocity is completely frozen
        across several frames -- under the old bug it would still be
        getting pushed around by repulsion/damping/anchor pull even
        though invisible."""
        create_garden('smoke-perf')
        result = js_async(window, """
            async () => {
                await api.switch_garden('smoke-perf');
                const species = await api.create_mote({name: 'Batch'});
                let sample = null;
                for (let i = 0; i < 60; i++) {
                    const m = await api.create_mote({name: 'batch-' + i});
                    await api.set_species(m.id, species.id);
                    if (i === 0) sample = m.id;
                }
                window.__batchSpeciesId = species.id;
                window.__sampleId = sample;
                await loadGarden('smoke-perf');
                window.__result = 'ok';
            }
        """, wait=8.0)
        assert result == '"ok"', f"batch setup failed: {result}"

        total = js(window, "graph.nodes.size")
        assert total == 61, f"expected 61 nodes (60 instances + the species), got {total}"
        assert js(window, "graph.alpha") > 0.5, "test is meaningless unless physics is actively running after load"

        # Hide immediately, while alpha is still high.
        js(window, """
            Array.from(document.querySelectorAll('#legend .filter-row'))
                .find(e => e.querySelector('.name').textContent === 'Batch').click();
        """)

        before = json.loads(js(window, "JSON.stringify(graph.nodes.get(window.__sampleId))"))
        time.sleep(0.5)  # several animation frames' worth
        after = json.loads(js(window, "JSON.stringify(graph.nodes.get(window.__sampleId))"))
        assert before == after, (
            f"a hidden instance's position/velocity should be completely frozen, not still simulated: "
            f"{before} -> {after}"
        )

        js_async(window, """
            async () => {
                await api.switch_garden('default');
                await loadGarden('default');
                window.__result = 'ok';
            }
        """, wait=2.0)

    check("hiding a species freezes its instances instead of still simulating them invisibly",
          hidden_motes_are_excluded_from_physics)

    def topbar_menus_open_one_at_a_time():
        js(window, "document.getElementById('file-btn').click();")
        time.sleep(0.3)
        file_open = js(window, "!document.getElementById('file-menu').classList.contains('hidden')")
        file_marked_open = js(window, "document.getElementById('file-btn').classList.contains('open')")
        assert file_open is True and file_marked_open is True, "clicking File should open its menu"

        js(window, "document.getElementById('view-btn').click();")
        time.sleep(0.3)
        file_closed = js(window, "document.getElementById('file-menu').classList.contains('hidden')")
        view_open = js(window, "!document.getElementById('view-menu').classList.contains('hidden')")
        assert file_closed is True and view_open is True, "opening View should close File -- only one menu open at a time"

        press_key(window, "Escape")
        time.sleep(0.3)
        any_open = js(window, """
            ['garden-menu', 'file-menu', 'view-menu', 'tools-menu']
                .some(id => !document.getElementById(id).classList.contains('hidden'))
        """)
        assert any_open is False, "Esc should close whichever topbar menu is open"

    check("topbar menus open one at a time, and Esc closes them", topbar_menus_open_one_at_a_time)

    def topbar_menu_closes_on_outside_click():
        js(window, "document.getElementById('tools-btn').click();")
        time.sleep(0.3)
        opened = js(window, "!document.getElementById('tools-menu').classList.contains('hidden')")
        assert opened is True, "Tools menu should be open before this check"

        # A real click (mousedown+mouseup from an actual user) fires a
        # 'click' event that bubbles to document -- drag()'s synthetic
        # mousedown/mousemove/mouseup doesn't, so this dispatches the
        # 'click' itself, same as quick-add's own outside-click check.
        js(window, """
            document.getElementById('graph').dispatchEvent(new MouseEvent('click', {
                clientX: 60, clientY: 60, bubbles: true, cancelable: true,
            }));
        """)
        time.sleep(0.3)
        closed = js(window, "document.getElementById('tools-menu').classList.contains('hidden')")
        assert closed is True, "clicking outside an open topbar menu should close it"

    check("a topbar menu closes on outside click", topbar_menu_closes_on_outside_click)

    def garden_dropdown_lists_and_switches():
        create_garden('smoke-dropdown')

        js(window, "document.getElementById('garden-btn').click();")
        time.sleep(1.0)
        rows = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#garden-menu-list .menu-item')).map(
                r => [r.querySelector('.name').textContent, r.querySelector('.check').textContent]
            ))
        """))
        assert dict(rows).get("default") == "✓", f"the current garden should be checked, got {rows}"
        assert "smoke-dropdown" in dict(rows), f"the new garden should be listed, got {rows}"

        js(window, """
            Array.from(document.querySelectorAll('#garden-menu-list .menu-item'))
                .find(r => r.querySelector('.name').textContent === 'smoke-dropdown').click();
        """)
        time.sleep(2.0)
        assert js(window, "currentGarden") == "smoke-dropdown", "clicking a garden row should switch to it"
        closed = js(window, "document.getElementById('garden-menu').classList.contains('hidden')")
        assert closed is True, "switching gardens from the dropdown should close it"

        js_async(window, """
            async () => {
                await api.switch_garden('default');
                await loadGarden('default');
                window.__result = 'ok';
            }
        """, wait=2.0)

    check("garden dropdown lists gardens and switches", garden_dropdown_lists_and_switches)

    def view_menu_toggles_the_control_panel():
        js(window, "document.getElementById('view-btn').click();")
        time.sleep(0.5)
        assert js(window, "document.getElementById('view-toggle-panel').textContent") == "Hide controls", \
            "the toggle should read 'Hide controls' while the panel is visible"

        js(window, "document.getElementById('view-toggle-panel').click();")
        time.sleep(0.3)
        hidden = js(window, "document.getElementById('control-panel').classList.contains('hidden')")
        assert hidden is True, "clicking it should hide the controls panel"
        persisted = js(window, "localStorage.getItem('monoss:controlPanel')")
        assert persisted == "hidden", "the hidden state should persist to localStorage"

        js(window, "document.getElementById('view-btn').click();")
        time.sleep(0.5)
        assert js(window, "document.getElementById('view-toggle-panel').textContent") == "Show controls", \
            "the toggle should relabel itself once the panel is hidden"

        js(window, "document.getElementById('view-toggle-panel').click();")
        time.sleep(0.3)
        visible = js(window, "!document.getElementById('control-panel').classList.contains('hidden')")
        assert visible is True, "clicking it again should show the controls panel"

    check("the View menu's Hide/Show controls toggles the controls panel and persists", view_menu_toggles_the_control_panel)

    def _set_slider(row_selector, name, value):
        """Drive one of the settings panel's sliders the way a pointer
        does: input while dragging, change on release."""
        js(window, f"""
            (function() {{
                const row = Array.from(document.querySelectorAll({json.dumps(row_selector)}))
                    .find(r => r.querySelector('.name').textContent === {json.dumps(name)});
                const slider = row.querySelector('input[type=range]');
                slider.value = {json.dumps(str(value))};
                slider.dispatchEvent(new Event('input', {{bubbles: true}}));
                slider.dispatchEvent(new Event('change', {{bubbles: true}}));
            }})();
        """)

    def view_settings_tunes_forces_per_relationship_type():
        # Its own garden: these checks assert on exact slider values and
        # stored settings, which the shared 'default' garden's accumulated
        # species/relationships would make brittle.
        create_garden('smoke-viewsettings')
        js_async(window, """
            async () => {
                await api.switch_garden('smoke-viewsettings');
                const species = await api.create_mote({name: 'Task'});
                const a = await api.create_mote({name: 'a1', status: 'open'});
                const b = await api.create_mote({name: 'b1', status: 'done'});
                const c = await api.create_mote({name: 'c1'});
                await api.set_species(a.id, species.id);
                await api.set_species(b.id, species.id);
                await api.create_relationship(a.id, b.id, 'blocks');
                window.__vs = {speciesId: species.id, aId: a.id, bId: b.id, cId: c.id};
                await loadGarden('smoke-viewsettings');
                window.__result = 'ok';
            }
        """, wait=3.0)

        js(window, "document.getElementById('open-view-settings-btn').click();")
        time.sleep(0.4)
        opened = js(window, "!document.getElementById('view-settings-panel').classList.contains('hidden')")
        assert opened is True, "the control panel's button should open the view settings panel"

        types = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#force-rel-rows .slider-row .name'))
                .map(e => e.textContent))
        """))
        assert "blocks" in types and "instance_of" in types, (
            f"every relationship type in the garden should get a force row, got {types}"
        )

        _set_slider("#force-rel-rows .slider-row", "blocks", 0.1)
        time.sleep(0.3)
        applied = js(window, "graph.forces.relStrength.blocks")
        assert abs(applied - 0.1) < 1e-9, f"the slider should reach the layout, got {applied}"
        stored = json.loads(js(window, "localStorage.getItem('monoss:viewSettings:' + currentGarden)"))
        assert abs(stored["forces"]["relStrength"]["blocks"] - 0.1) < 1e-9, (
            f"the override should persist, got {stored}"
        )

        _set_slider("#force-globals .slider-row", "Repulsion", 3000)
        time.sleep(0.3)
        repulsion = js(window, "graph.forces.repulsion")
        assert repulsion == 3000, f"a global force slider should reach the layout, got {repulsion}"

        # ...and ↺ puts the type back on the species-aware default, which
        # is an absence rather than a number.
        js(window, """
            Array.from(document.querySelectorAll('#force-rel-rows .slider-row'))
                .find(r => r.querySelector('.name').textContent === 'blocks')
                .querySelector('.reset-btn').click();
        """)
        time.sleep(0.3)
        cleared = js(window, "graph.forces.relStrength.blocks === undefined")
        assert cleared is True, "resetting a type should drop the override, not zero it"

    check("view settings: a per-type force slider reaches the layout and persists",
          view_settings_tunes_forces_per_relationship_type)

    def custom_species_color_survives_a_garden_switch():
        js(window, """
            (function() {
                const picker = document.querySelector('#species-colors input[type=color]');
                picker.value = '#ff0000';
                picker.dispatchEvent(new Event('input', {bubbles: true}));
                picker.dispatchEvent(new Event('change', {bubbles: true}));
            })();
        """)
        time.sleep(0.3)
        instance_color = js(window, "graph.colorFor(window.__vs.aId)")
        assert instance_color == "#ff0000", (
            f"a custom species color should apply to its instances, got {instance_color!r}"
        )

        js_async(window, """
            async () => {
                await api.switch_garden('default');
                await loadGarden('default');
                await api.switch_garden('smoke-viewsettings');
                await loadGarden('smoke-viewsettings');
                window.__result = 'ok';
            }
        """, wait=3.0)
        after = js(window, "graph.colorFor(window.__vs.aId)")
        assert after == "#ff0000", f"the color should be remembered per garden, got {after!r}"

    check("view settings: a custom species color applies and survives a garden switch",
          custom_species_color_survives_a_garden_switch)

    def color_by_property_value_splits_motes_by_value():
        js(window, "document.getElementById('open-view-settings-btn').click();")
        time.sleep(0.3)
        js(window, """
            (function() {
                const select = document.getElementById('color-mode-select');
                select.value = 'property';
                select.dispatchEvent(new Event('change', {bubbles: true}));
                const input = document.getElementById('color-key-input');
                input.value = 'status';
                input.dispatchEvent(new Event('input', {bubbles: true}));
            })();
        """)
        time.sleep(0.4)

        open_color = js(window, "graph.colorFor(window.__vs.aId)")
        done_color = js(window, "graph.colorFor(window.__vs.bId)")
        none_color = js(window, "graph.colorFor(window.__vs.cId)")
        assert open_color != done_color, (
            f"different property values should get different colors, both were {open_color!r}"
        )
        assert none_color == "oklch(0.62 0.02 250)", (
            f"a mote without the property should stay grey, got {none_color!r}"
        )

        hollow = js(window, """
            document.querySelector('#legend .filter-row .dot').classList.contains('hollow')
        """)
        assert hollow is True, "the legend should stop presenting itself as the color key"

        # Back to defaults for the attractor check below.
        js(window, "document.getElementById('reset-view-settings-btn').click();")
        time.sleep(0.4)
        mode = js(window, "graph.colors.mode")
        repulsion = js(window, "graph.forces.repulsion")
        assert mode == "species" and repulsion == 1500, (
            f"reset should restore every default, got mode={mode!r} repulsion={repulsion}"
        )

    check("view settings: color by property value, with a grey for motes lacking it",
          color_by_property_value_splits_motes_by_value)

    def attractor_mode_moves_a_cluster_and_blocks_editing():
        js(window, "document.getElementById('arrange-attractors-btn').click();")
        time.sleep(0.4)
        assert js(window, "graph.attractorMode") is True, "the button should enter attractor mode"

        point = json.loads(js(window, """
            JSON.stringify((graph.attractorPoints().find(p => p.id === window.__vs.speciesId)) || null)
        """))
        assert point is not None, "the species should have a draggable attractor"
        screen = json.loads(js(window, f"JSON.stringify(graph._worldToScreen({point['x']}, {point['y']}))"))

        distance_to_attractor = """
            (function() {
                const n = graph.nodes.get(window.__vs.aId);
                const a = graph._attractorPosition(window.__vs.speciesId);
                return Math.hypot(n.x - a.x, n.y - a.y);
            })()
        """
        drag(window, screen["x"], screen["y"], screen["x"] + 260, screen["y"] + 140)
        # A full second, not just "a frame": under a loaded/headless
        # compositor the canvas's rAF-driven redraw can lag well past one
        # tick, and the pixel sample below needs a frame to have actually
        # landed -- same margin the "graph canvas actually renders" check
        # above gives the same kind of sample.
        time.sleep(1.0)

        moved = json.loads(js(window, "JSON.stringify(graph.attractors[window.__vs.speciesId] || null)"))
        assert moved is not None, "dragging an attractor should move it"
        stored = json.loads(js(window, "localStorage.getItem('monoss:viewSettings:' + currentGarden)"))
        assert stored["attractors"], f"the new position should persist, got {stored}"

        # The handle is actually drawn, not just tracked -- sampled where
        # the attractor now is, while the cluster it pulls is still 260px
        # away and can't be what's painting those pixels. Same reasoning as
        # the canvas-paints check above: everything else here works off
        # coordinates and would pass over an unpainted overlay.
        ring = json.loads(js(window, """
            (function() {
                const c = document.getElementById('graph');
                const rect = c.getBoundingClientRect();
                const dpr = window.devicePixelRatio || 1;
                const a = graph._attractorPosition(window.__vs.speciesId);
                const p = graph._worldToScreen(a.x, a.y);
                const size = Math.round(40 * dpr);
                const d = c.getContext('2d').getImageData(
                    Math.round((p.x - rect.left - 20) * dpr),
                    Math.round((p.y - rect.top - 20) * dpr), size, size).data;
                const colors = new Set();
                for (let i = 0; i < d.length; i += 4) colors.add(d[i] + ',' + d[i + 1] + ',' + d[i + 2]);
                return JSON.stringify({distinct: colors.size});
            })()
        """))
        assert ring["distinct"] > 1, f"the attractor handle isn't painted: {ring}"

        # The cluster follows: measured right after the jump, then again
        # once the layout has had time to chase it.
        distance_before = js(window, distance_to_attractor)
        time.sleep(2.5)
        distance_after = js(window, distance_to_attractor)
        assert distance_after < distance_before, (
            f"instances should follow their attractor: {distance_before} -> {distance_after}"
        )

        # Read-only: clicking a mote neither selects it nor opens the panel.
        x, y = node_xy(window, "window.__vs.aId")
        drag(window, x, y, x, y)
        time.sleep(0.4)
        panel_hidden = js(window, "document.getElementById('detail-panel').classList.contains('hidden')")
        assert panel_hidden is True, "attractor mode should not select or inspect motes"

        js(window, "document.getElementById('attractor-done-btn').click();")
        time.sleep(0.3)
        assert js(window, "graph.attractorMode") is False, "Done arranging should leave the mode"

        js(window, "document.getElementById('close-view-settings').click();")
        js_async(window, """
            async () => {
                await api.switch_garden('default');
                await loadGarden('default');
                window.__result = 'ok';
            }
        """, wait=3.0)

    check("view settings: attractor mode drags a cluster and blocks editing",
          attractor_mode_moves_a_cluster_and_blocks_editing)

    def sticky_note_annotations_full_lifecycle():
        """Drives the whole annotate-mode gesture surface for real: the
        toolbar toggle, click-to-place (both anchor modes), the textarea
        overlay, a drag that must prove a mote-anchored note follows its
        mote, the × delete badge, Ctrl+Z/Ctrl+Shift+Z, and that a note
        survives its mote being deleted (core/db.py's annotations table
        has no FK on mote_id -- see its docstring)."""
        def note_editor_text():
            return js(window, "document.querySelector('.annotation-editor').value")

        def type_and_blur(text):
            js(window, f"""
                (function() {{
                    const editor = document.querySelector('.annotation-editor');
                    editor.value = {json.dumps(text)};
                    editor.dispatchEvent(new Event('input', {{bubbles: true}}));
                    editor.blur();
                }})();
            """)
            time.sleep(0.5)

        def list_notes():
            # wait=3.0: under the full suite's accumulated DOM/data, this
            # round trip is slower than in isolation and the default
            # ceiling sometimes returns before window.__result lands.
            raw = js_async(window, """
                async () => { window.__result = await api.list_annotations(); }
            """, wait=3.0)
            return json.loads(raw) if raw not in (None, "null") else []

        def wait_for_note_count(n, what):
            assert wait_until(lambda: len(list_notes()) == n, timeout=5.0), (
                f"{what}: expected {n} note(s), still {len(list_notes())} after waiting"
            )
            return list_notes()

        # A fresh mote, refocused on -- by this point in the suite the
        # graph's neighbourhood view has long since moved away from
        # window.__seed's motes (search/navigation/depth checks change
        # what's in view), so graph.nodes may no longer hold Bob at all.
        # screenPositionOf(id) returns null for anything outside the
        # current view, which node_xy then can't destructure.
        target = json.loads(js_async(window, """
            async () => {
                const m = await api.create_mote({name: 'Note-Target'});
                await refreshMoteCache();
                await refocus(m.id);
                window.__result = m;
            }
        """, wait=3.0))
        target_id_js = json.dumps(target["id"])

        def ctrl_z(shift=False):
            js(window, f"""
                document.dispatchEvent(new KeyboardEvent('keydown', {{
                    key: 'z', ctrlKey: true, shiftKey: {str(shift).lower()}, bubbles: true,
                }}));
            """)
            time.sleep(0.5)

        js(window, "document.getElementById('annotate-mode-btn').click();")
        time.sleep(0.3)
        assert js(window, "graph.annotateMode") is True, "the toolbar button should enter annotate mode"
        active_class = js(window, "document.getElementById('annotate-mode-btn').classList.contains('active')")
        assert active_class is True, "the button should show it's active"

        # World-anchored note: double-click empty canvas, type, and blur to save.
        ex, ey = empty_canvas_point(window)
        dblclick(window, ex, ey)
        time.sleep(0.4)
        assert note_editor_text() == "", "a freshly placed note should open its editor empty"
        type_and_blur("World note")

        notes = wait_for_note_count(1, "clicking empty canvas")
        world_note = notes[0]
        assert world_note["anchorMode"] == "world" and world_note["text"] == "World note", (
            f"world-anchored note should save its text, got {world_note}"
        )

        # Mote-anchored note: double-click on the target mote.
        bx, by = node_xy(window, target_id_js)
        dblclick(window, bx, by)
        time.sleep(0.4)
        type_and_blur("Note on target")

        notes = wait_for_note_count(2, "clicking a mote")
        bob_note = next(n for n in notes if n["id"] != world_note["id"])
        assert bob_note["anchorMode"] == "mote" and bob_note["moteId"] == target["id"], (
            f"clicking a mote should create a mote-anchored note on it, got {bob_note}"
        )
        assert bob_note["text"] == "Note on target", f"mote note should save its text, got {bob_note}"

        # Dragging the target mote (outside annotate mode, which owns the
        # left button) must carry the note with it.
        js(window, "document.getElementById('annotate-mode-btn').click();")
        time.sleep(0.3)
        assert js(window, "graph.annotateMode") is False, "the button should toggle annotate mode off"
        bx, by = node_xy(window, target_id_js)
        drag(window, bx, by, bx + 150, by + 90)
        time.sleep(1.0)

        js(window, "document.getElementById('annotate-mode-btn').click();")
        time.sleep(0.3)
        bob_node = json.loads(js(window, f"JSON.stringify(graph.nodes.get({target_id_js}))"))
        expected_screen = json.loads(js(window, f"""
            JSON.stringify(graph._worldToScreen(
                {bob_node["x"]} + {bob_note["x"]}, {bob_node["y"]} + {bob_note["y"]}))
        """))
        # A double-click at that resolved position should edit the note
        # (not miss into empty canvas, which would create a *third* note).
        dblclick(window, expected_screen["x"], expected_screen["y"])
        time.sleep(0.4)
        assert note_editor_text() == "Note on target", (
            "the mote-anchored note should have followed the target mote to its new position"
        )
        js(window, "document.querySelector('.annotation-editor').blur();")
        wait_for_note_count(2, "clicking the followed note should not have created a new one")

        # Delete the world note via its × badge.
        world_left = world_note["x"] - 160 / 2
        world_top = world_note["y"] - 90 / 2
        badge_screen = json.loads(js(window, f"""
            JSON.stringify(graph._worldToScreen({world_left} + 160, {world_top}))
        """))
        drag(window, badge_screen["x"], badge_screen["y"], badge_screen["x"], badge_screen["y"])
        after_delete = wait_for_note_count(1, "the × badge should delete just the world note")
        assert after_delete[0]["id"] == bob_note["id"], (
            f"the × badge should delete the world note, not the other one: {after_delete}"
        )

        # Undo the delete, then undo the creation too.
        ctrl_z()
        wait_for_note_count(2, "Ctrl+Z should undo the delete")
        ctrl_z(shift=True)
        wait_for_note_count(1, "Ctrl+Shift+Z should redo the delete")

        # A mote-anchored note survives its mote being deleted -- no FK,
        # see core/db.py's annotations table.
        js_async(window, f"""
            async () => {{
                await api.delete_mote({target_id_js});
                window.__result = 'ok';
            }}
        """)
        surviving = wait_for_note_count(1, "the annotation should survive its mote's deletion")
        assert surviving[0]["id"] == bob_note["id"], (
            f"the annotation should survive its mote's deletion, got {surviving}"
        )
        assert surviving[0]["moteId"] == bob_note["moteId"], (
            "the orphaned annotation should keep its (now dangling) moteId"
        )

        # Clean up so later checks (e.g. no_uncaught_errors) see a normal
        # graph, and leave annotate mode off.
        js_async(window, f"""
            async () => {{
                await api.delete_annotation({json.dumps(surviving[0]["id"])});
                window.__result = 'ok';
            }}
        """)
        js(window, "document.getElementById('annotate-mode-btn').click();")
        time.sleep(0.3)
        assert js(window, "graph.annotateMode") is False, "annotate mode should be off after cleanup"

    check("sticky notes: place (world + mote), follow a drag, edit, delete, undo/redo, survive mote delete",
          sticky_note_annotations_full_lifecycle)

    def note_editing_focus_and_clipboard():
        """The editor overlay matches the drawn note's zoom-scaled text
        size, a click outside it only ends the edit (no second note), and
        frontend_shared/focus.js's window-wide rules hold: a middle
        mouseup anywhere is default-prevented -- WebKitGTK's primary-
        selection paste runs on that mouseup, into whatever has focus --
        and a click on non-focusable UI blurs a focused text field."""
        def note_count():
            raw = js_async(window, """
                async () => { window.__result = (await api.list_annotations()).length; }
            """, wait=3.0)
            return json.loads(raw)

        before = note_count()
        js(window, "document.getElementById('annotate-mode-btn').click();")
        time.sleep(0.3)
        ex, ey = empty_canvas_point(window)
        dblclick(window, ex, ey)
        assert wait_until(lambda: js(window,
            "document.activeElement === document.querySelector('.annotation-editor')") is True), (
            "a freshly placed note should focus its editor"
        )
        font_px = js(window, "parseFloat(document.querySelector('.annotation-editor').style.fontSize)")
        scale = js(window, "graph.camera.scale")
        assert abs(font_px - 13 * scale) < 0.01, (
            f"the editor's font should be the note's 13 world px at zoom {scale}, got {font_px}px"
        )

        middle_up_prevented = js(window, """
            !document.getElementById('graph').dispatchEvent(new MouseEvent('mouseup', {
                bubbles: true, cancelable: true, button: 1, buttons: 0,
            }))
        """)
        assert middle_up_prevented is True, (
            "a middle mouseup must be default-prevented, or X11 pastes the primary selection into the note"
        )

        js(window, "document.querySelector('.annotation-editor').value = 'kept';")
        fx, fy = ex + 200, ey + 120
        drag(window, fx, fy, fx, fy)
        time.sleep(0.5)
        assert js(window, "document.querySelector('.annotation-editor').classList.contains('hidden')") is True, (
            "clicking outside the note should close its editor"
        )
        assert note_count() == before + 1, "clicking outside an open note must not also create a new one"

        # The same click-away rule outside annotations: a plain focused
        # <input> blurs on a mousedown over non-focusable chrome, even
        # when that element's own handler would stop the event.
        blurred = js(window, """
            (function() {
                const input = document.createElement('input');
                const pad = document.createElement('div');
                pad.addEventListener('mousedown', (e) => { e.preventDefault(); e.stopPropagation(); });
                document.body.append(input, pad);
                input.focus();
                pad.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true, button: 0 }));
                const ok = document.activeElement !== input;
                input.remove(); pad.remove();
                return ok;
            })()
        """)
        assert blurred is True, "a mousedown outside a focused field should blur it"

        js_async(window, """
            async () => {
                const notes = await api.list_annotations();
                const kept = notes.find((n) => n.text === 'kept');
                if (kept) await api.delete_annotation(kept.id);
                window.__result = 'ok';
            }
        """, wait=3.0)
        js(window, "document.getElementById('annotate-mode-btn').click();")
        time.sleep(0.3)

    check("sticky notes: editor font matches zoom, click-away ends edit, middle-click never pastes, click-away blurs",
          note_editing_focus_and_clipboard)

    def note_selection_linking_and_sizing():
        """Annotate mode's editor-style gestures: Backspace in an empty
        note deletes it; box-select, a group drag, and Delete act on
        several notes at once; ctrl+drag from a note onto a mote attaches
        it and a second one detaches it; the corner grip resizes; and a
        note grows to fit long text instead of scrolling."""
        def notes():
            raw = js_async(window, """
                async () => { window.__result = await api.list_annotations(); }
            """, wait=3.0)
            return json.loads(raw) if raw not in (None, "null") else []

        def screen_of(world_x, world_y):
            return json.loads(js(window, f"JSON.stringify(graph._worldToScreen({world_x}, {world_y}))"))

        def type_and_blur(text):
            js(window, f"""
                (function() {{
                    const editor = document.querySelector('.annotation-editor');
                    editor.value = {json.dumps(text)};
                    editor.dispatchEvent(new Event('input', {{bubbles: true}}));
                    editor.blur();
                }})();
            """)
            time.sleep(0.5)

        def key(k, target="document"):
            js(window, f"""
                {target}.dispatchEvent(new KeyboardEvent('keydown', {{
                    key: {json.dumps(k)}, bubbles: true, cancelable: true,
                }}));
            """)
            time.sleep(0.5)

        def ctrl_drag(x1, y1, x2, y2):
            drag(window, x1, y1, x2, y2, ctrl=True)
            time.sleep(0.6)

        before = len(notes())
        target = json.loads(js_async(window, """
            async () => {
                const m = await api.create_mote({name: 'Link-Target'});
                await refreshMoteCache();
                await refocus(m.id);
                window.__result = m;
            }
        """, wait=3.0))
        target_id_js = json.dumps(target["id"])
        js(window, "document.getElementById('annotate-mode-btn').click();")
        time.sleep(0.3)

        # Backspace in an empty note deletes it.
        ex, ey = empty_canvas_point(window)
        dblclick(window, ex, ey)
        assert wait_until(lambda: len(notes()) == before + 1), "double-click should add a note"
        key("Backspace", "document.querySelector('.annotation-editor')")
        assert wait_until(lambda: len(notes()) == before), "Backspace in an empty note should delete it"
        assert js(window, "document.querySelector('.annotation-editor').classList.contains('hidden')") is True

        # Two world notes side by side, placed through the api so the
        # layout is exact.
        created = json.loads(js_async(window, f"""
            async () => {{
                const c = graph._worldAt({ex}, {ey});
                const a = await api.create_sticky_note('world', c.x, c.y, null, 'A');
                const b = await api.create_sticky_note('world', c.x + 200, c.y, null, 'B');
                await Annotations.refresh();
                window.__result = [a, b];
            }}
        """, wait=3.0))
        a, b = created
        a_centre = screen_of(a["x"], a["y"])
        b_centre = screen_of(b["x"], b["y"])

        # Box-select both: drag from above-left of A to below-right of B.
        tl = screen_of(a["x"] - 100, a["y"] - 60)
        br = screen_of(b["x"] + 100, b["y"] + 60)
        drag(window, tl["x"], tl["y"], br["x"], br["y"])
        time.sleep(0.3)
        chosen = set(json.loads(js(window, "JSON.stringify(Annotations.selectedIds())")))
        assert chosen == {a["id"], b["id"]}, f"box-select should select both notes, got {chosen}"

        # Dragging one moves both, as one undo step.
        drag(window, a_centre["x"], a_centre["y"], a_centre["x"] + 60, a_centre["y"] + 40)
        time.sleep(0.6)
        by_id = {n["id"]: n for n in notes()}
        moved_a = by_id[a["id"]]["x"] - a["x"]
        moved_b = by_id[b["id"]]["x"] - b["x"]
        assert moved_a > 1 and abs(moved_a - moved_b) < 0.01, (
            f"dragging a selected note should move the whole selection together, moved {moved_a} / {moved_b}"
        )

        # ctrl+drag from A onto the mote attaches it, without moving it.
        a_now = by_id[a["id"]]
        a_screen = screen_of(a_now["x"], a_now["y"])
        mx, my = node_xy(window, target_id_js)
        ctrl_drag(a_screen["x"], a_screen["y"], mx, my)
        attached = {n["id"]: n for n in notes()}[a["id"]]
        assert attached["anchorMode"] == "mote" and attached["moteId"] == target["id"], (
            f"ctrl+drag onto a mote should attach the note to it, got {attached}"
        )
        node = json.loads(js(window, f"JSON.stringify(graph.nodes.get({target_id_js}))"))
        assert abs(node["x"] + attached["x"] - a_now["x"]) < 2, "attaching shouldn't move the note"

        # The same gesture again detaches it.
        a_screen = screen_of(node["x"] + attached["x"], node["y"] + attached["y"])
        mx, my = node_xy(window, target_id_js)
        ctrl_drag(a_screen["x"], a_screen["y"], mx, my)
        detached = {n["id"]: n for n in notes()}[a["id"]]
        assert detached["anchorMode"] == "world" and detached["moteId"] is None, (
            f"ctrl+drag onto the mote it's already on should detach it, got {detached}"
        )

        # The corner grip resizes, keeping the top-left corner put.
        b_now = {n["id"]: n for n in notes()}[b["id"]]
        corner = screen_of(b_now["x"] + 80, b_now["y"] + 45)
        drag(window, corner["x"] - 1, corner["y"] - 1, corner["x"] + 80, corner["y"] + 60)
        time.sleep(0.6)
        resized = {n["id"]: n for n in notes()}[b["id"]]
        assert resized["width"] and resized["width"] > 180, f"the grip should widen the note, got {resized}"
        assert abs((resized["x"] - resized["width"] / 2) - (b_now["x"] - 80)) < 0.5, (
            "resizing should keep the note's left edge where it was"
        )

        # Long text grows the note (drawn height > stored height) and the
        # editor never needs to scroll.
        dblclick(window, *[screen_of(resized["x"], resized["y"])[k] for k in ("x", "y")])
        time.sleep(0.4)
        long_text = "\n".join(f"line {i}" for i in range(15))
        js(window, f"""
            (function() {{
                const editor = document.querySelector('.annotation-editor');
                editor.value = {json.dumps(long_text)};
                editor.dispatchEvent(new Event('input', {{bubbles: true}}));
            }})();
        """)
        time.sleep(0.2)
        overflow = js(window, """
            (function() {
                const editor = document.querySelector('.annotation-editor');
                return editor.scrollHeight - editor.clientHeight;
            })()
        """)
        assert overflow <= 1, f"the editor should grow to fit its text, still {overflow}px to scroll"
        js(window, "document.querySelector('.annotation-editor').blur();")
        time.sleep(0.4)

        # Select both again and Delete removes them together.
        tl = screen_of(min(a["x"], b["x"]) - 400, a["y"] - 400)
        br = screen_of(max(a["x"], b["x"]) + 400, a["y"] + 400)
        drag(window, tl["x"], tl["y"], br["x"], br["y"])
        time.sleep(0.3)
        key("Delete")
        assert wait_until(lambda: len(notes()) == before), "Delete should remove every selected note"

        js(window, "document.getElementById('annotate-mode-btn').click();")
        time.sleep(0.3)
        js_async(window, f"""
            async () => {{ await api.delete_mote({target_id_js}); await refreshMoteCache(); window.__result = 'ok'; }}
        """)

    check("sticky notes: Backspace-delete, box-select/move/delete, ctrl+drag attach/detach, resize, auto-grow",
          note_selection_linking_and_sizing)

    def note_key_toggle_and_zoomed_out_glyph():
        """"A" toggles annotate mode; zoomed out past legibility a note
        collapses to a small marker (hit-testing shrinks with it), and
        double-clicking the marker zooms back in to edit it."""
        def press_a():
            js(window, """
                document.dispatchEvent(new KeyboardEvent('keydown', {key: 'a', bubbles: true, cancelable: true}));
            """)
            time.sleep(0.3)

        press_a()
        assert js(window, "graph.annotateMode") is True, "'a' should enter annotate mode"
        note = json.loads(js_async(window, """
            async () => {
                const c = graph.viewCenterWorld();
                const n = await api.create_sticky_note('world', c.x, c.y, null, 'far away');
                await Annotations.refresh();
                graph.camera.scale = 0.2;  // 13px text -> 2.6px on screen
                graph.camera.x = -c.x * 0.2; graph.camera.y = -c.y * 0.2;
                window.__result = n;
            }
        """, wait=3.0))
        time.sleep(0.3)
        edge = json.loads(js(window, f"JSON.stringify(graph._worldToScreen({note['x']} - 70, {note['y']}))"))
        centre = json.loads(js(window, f"JSON.stringify(graph._worldToScreen({note['x']}, {note['y']}))"))
        # A point inside the full note but well outside the marker misses.
        drag(window, edge["x"], edge["y"], edge["x"], edge["y"])
        time.sleep(0.2)
        assert json.loads(js(window, "JSON.stringify(Annotations.selectedIds())")) == [], (
            "a collapsed note should only be hit on its marker"
        )
        dblclick(window, centre["x"], centre["y"])
        time.sleep(0.4)
        scale = js(window, "graph.camera.scale")
        assert scale >= 1, f"editing a collapsed note should zoom in to it, scale is {scale}"
        assert js(window, "document.activeElement === document.querySelector('.annotation-editor')") is True
        js(window, "document.querySelector('.annotation-editor').blur();")
        js_async(window, f"""
            async () => {{ await api.delete_annotation({json.dumps(note['id'])}); await Annotations.refresh(); window.__result = 'ok'; }}
        """)
        press_a()
        assert js(window, "graph.annotateMode") is False, "'a' again should leave annotate mode"

    check("sticky notes: 'a' toggles annotate mode, zoomed-out notes collapse to a marker",
          note_key_toggle_and_zoomed_out_glyph)

    def group_boxes_ink_and_toolbar():
        """Annotate mode's tool bar and the two annotation kinds it adds:
        the Group tool draws a box that captures the motes inside it and
        opens its title; dragging the box by its tab carries its members
        (never pinning them) and the ink inside it, and undo puts it back;
        its walls keep members in and non-members out; dragging a member
        out in the ordinary editor drops it from the group; the swatches
        recolor a selected group; Pen strokes and the Eraser removes them."""
        def annotations(kind):
            raw = js_async(window, """
                async () => { window.__result = await api.list_annotations(); }
            """, wait=3.0)
            items = json.loads(raw) if raw not in (None, "null") else []
            return [a for a in items if a["type"] == kind]

        def key(k, **mods):
            flags = ", ".join(f"{name}: true" for name in mods)
            js(window, f"""
                document.dispatchEvent(new KeyboardEvent('keydown', {{
                    key: {json.dumps(k)}, bubbles: true, cancelable: true{', ' + flags if flags else ''}
                }}));
            """)
            time.sleep(0.3)

        def screen_of(x, y):
            p = json.loads(js(window, f"JSON.stringify(graph._worldToScreen({x}, {y}))"))
            return p["x"], p["y"]

        def node(id_js):
            return json.loads(js(window, f"JSON.stringify(graph.nodes.get({id_js}))"))

        def inside(n, g, pad=0):
            return (g["x"] - g["width"] / 2 - pad <= n["x"] <= g["x"] + g["width"] / 2 + pad
                    and g["y"] - g["height"] / 2 - pad <= n["y"] <= g["y"] + g["height"] / 2 + pad)

        # Two motes somewhere no other mote is, and a third (not grouped)
        # to test the walls against. All pinned for now, so the layout
        # can't carry one off before the box is drawn round it.
        made = json.loads(js_async(window, """
            async () => {
                const one = await api.create_mote({name: 'Group-One'});
                const two = await api.create_mote({name: 'Group-Two'});
                const out = await api.create_mote({name: 'Group-Outsider'});
                await refreshMoteCache();
                await refocus(one.id);
                const c = graph.viewCenterWorld();
                const X = c.x + 4000, Y = c.y;
                graph.placeAt(one.id, X, Y);
                graph.placeAt(two.id, X + 60, Y);
                graph.placeAt(out.id, X + 400, Y);
                graph._fitUntilAlpha = 0; graph.panTarget = null;
                graph.camera.scale = 1; graph.camera.x = -X - 30; graph.camera.y = -Y;
                window.__result = {one: one.id, two: two.id, out: out.id, X, Y};
            }
        """, wait=3.0))
        one_js, two_js, out_js = (json.dumps(made[k]) for k in ("one", "two", "out"))
        X, Y = made["X"], made["Y"]
        time.sleep(0.3)

        key("a")
        assert js(window, "graph.annotateMode") is True
        assert js(window, "document.querySelector('.annotation-toolbar').classList.contains('hidden')") is False, (
            "the tool bar should show while annotating"
        )

        # Group tool: draw a box round the two motes.
        key("g")
        assert js(window, "Annotations.currentTool()") == "group"
        assert js(window, "document.querySelector('.annotation-tool.active').title") == "Group (G)"
        x1, y1 = screen_of(X - 60, Y - 60)
        x2, y2 = screen_of(X + 120, Y + 60)
        drag(window, x1, y1, x2, y2)
        assert wait_until(lambda: len(annotations("group")) == 1), "drawing with the Group tool should add a group"
        group = annotations("group")[0]
        assert set(group["data"]["members"]) == {made["one"], made["two"]}, group["data"]
        assert js(window, "Annotations.currentTool()") == "select", "the Group tool should hand back to Select"
        assert js(window, "document.activeElement.classList.contains('annotation-group-title')") is True, (
            "a new group should open its title for typing"
        )
        js(window, """
            (function() {
                const input = document.activeElement;
                input.value = 'Pair';
                input.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter', bubbles: true, cancelable: true}));
            })();
        """)
        assert wait_until(lambda: annotations("group")[0]["text"] == "Pair"), "Enter should save the title"
        # From here on one member floats: only the box holds it.
        js(window, f"graph.unpin({two_js});")

        # Pen: a stroke inside the box, which the box will carry.
        key("p")
        sx1, sy1 = screen_of(X - 20, Y + 30)
        sx2, sy2 = screen_of(X + 80, Y + 40)
        drag(window, sx1, sy1, sx2, sy2)
        assert wait_until(lambda: len(annotations("stroke")) == 1), "a Pen drag should add a stroke"
        stroke = annotations("stroke")[0]
        assert len(stroke["data"]["points"]) >= 2 and stroke["data"]["width"] > 0, stroke
        assert js(window, "Annotations.currentTool()") == "pen", "the Pen should stay picked"

        # Move the group by its title tab.
        key("v")
        tab_x, tab_y = screen_of(X - 60 + 24, Y - 60 - 10)
        drag(window, tab_x, tab_y, tab_x + 150, tab_y)
        assert wait_until(lambda: abs(annotations("group")[0]["x"] - (group["x"] + 150)) < 1), (
            f"dragging the tab should move the group, got {annotations('group')[0]['x']} vs {group['x'] + 150}"
        )
        moved_stroke = annotations("stroke")[0]
        assert abs(moved_stroke["x"] - (stroke["x"] + 150)) < 1, "the stroke inside the box should move with it"
        time.sleep(1.0)
        moved = annotations("group")[0]
        one_now, two_now = node(one_js), node(two_js)
        assert abs(one_now["x"] - (X + 150)) < 1, f"the pinned member should move with the box: {one_now}"
        assert inside(two_now, moved), f"the floating member should stay inside the box: {two_now} {moved}"
        assert two_now["pinned"] is False, "moving a group must never pin its members"

        # Undo puts the box (and the stroke) back; the wall brings the
        # members back with it.
        key("z", ctrlKey=True)
        assert wait_until(lambda: abs(annotations("group")[0]["x"] - group["x"]) < 1), "Ctrl+Z should undo the move"
        assert abs(annotations("stroke")[0]["x"] - stroke["x"]) < 1, "the carried stroke's move is the same undo step"
        time.sleep(0.5)
        assert inside(node(one_js), group) and inside(node(two_js), group), "members should follow the box back"

        # Walls: a non-member put inside the box is pushed straight out.
        js(window, f"graph.placeAt({out_js}, {group['x']}, {group['y']});")
        time.sleep(0.3)
        assert not inside(node(out_js), group), f"a non-member should be pushed out: {node(out_js)}"

        # Recolor: select the group by its tab, click the third swatch.
        tab_x, tab_y = screen_of(X - 60 + 24, Y - 60 - 10)
        drag(window, tab_x, tab_y, tab_x, tab_y)
        assert js(window, "JSON.stringify(Annotations.selectedIds())") == json.dumps([group["id"]])
        assert js(window, "document.querySelector('.annotation-toolbar-colors').classList.contains('hidden')") is False
        swatch = js(window, "document.querySelectorAll('.annotation-swatch')[2].dataset.color")
        js(window, "document.querySelectorAll('.annotation-swatch')[2].click();")
        assert wait_until(lambda: annotations("group")[0]["color"] == swatch), "a swatch should recolor the group"
        js(window, "document.querySelector('.annotation-reset').click();")
        assert wait_until(lambda: annotations("group")[0]["color"] is None), "↺ should go back to automatic"

        # Eraser: drag across the stroke.
        key("e")
        ex1, ey1 = screen_of(X + 30, Y - 10)
        ex2, ey2 = screen_of(X + 30, Y + 70)
        drag(window, ex1, ey1, ex2, ey2)
        assert wait_until(lambda: len(annotations("stroke")) == 0), "the Eraser should remove the stroke it crosses"

        # Esc: back to Select, then (nothing selected) out of the mode.
        key("Escape")
        assert js(window, "Annotations.currentTool()") == "select"
        key("Escape")
        key("Escape")
        assert js(window, "graph.annotateMode") is False
        assert js(window, "document.querySelector('.annotation-toolbar').classList.contains('hidden')") is True

        # In the ordinary editor, dragging a member out drops it from the group.
        tx, ty = node_xy(window, two_js)
        drag(window, tx, ty, tx, ty + 250)
        assert wait_until(lambda: annotations("group")[0]["data"]["members"] == [made["one"]]), (
            f"dragging a member out should drop it: {annotations('group')[0]['data']}"
        )
        # ...and dragging it back in adds it again.
        tx, ty = node_xy(window, two_js)
        cx, cy = screen_of(group["x"] + 30, group["y"])
        drag(window, tx, ty, cx, cy)
        assert wait_until(lambda: set(annotations("group")[0]["data"]["members"]) == {made["one"], made["two"]}), (
            "dropping a mote inside a box should add it"
        )

        js_async(window, f"""
            async () => {{
                await api.delete_annotations({json.dumps([group['id']])});
                await Annotations.refresh();
                for (const id of [{one_js}, {two_js}, {out_js}]) await api.delete_mote(id);
                await refreshMoteCache();
                window.__result = 'ok';
            }}
        """, wait=3.0)

    check("annotate tool bar: group boxes hold and carry motes, pen and eraser, recolor, undo",
          group_boxes_ink_and_toolbar)

    def soon_stubs_toast_instead_of_doing_nothing():
        js(window, "document.getElementById('file-btn').click();")
        time.sleep(0.3)
        js(window, "document.querySelector('#file-menu [data-soon=\\'Export as CSV\\']').click();")
        time.sleep(0.5)

        toast = js(window, "document.getElementById('toast').textContent")
        assert "Export as CSV" in toast, f"a 'soon' stub should toast an explanation, got {toast!r}"
        closed = js(window, "document.getElementById('file-menu').classList.contains('hidden')")
        assert closed is True, "clicking a 'soon' stub should close the menu like any other action"

    check("a 'soon' stub toasts instead of silently doing nothing", soon_stubs_toast_instead_of_doing_nothing)

    def no_uncaught_errors():
        errors = js(window, "JSON.stringify(window.__uiErrors || [])")
        assert errors == "[]", f"the UI logged uncaught errors: {errors}"

    check("no uncaught JS errors during the run", no_uncaught_errors)


def drive():
    # An event fired from window.events.loaded, not a blind sleep sized
    # for the worst case -- the page is almost always ready well under
    # the old fixed 3.5s, and this still has a generous ceiling for a
    # slow first launch.
    _page_loaded.wait(timeout=15)
    window = webview.windows[0]
    # Record uncaught errors so the final check can assert on them.
    js(window, """
        window.__uiErrors = [];
        window.addEventListener('error', (e) => window.__uiErrors.push(String(e.message)));
    """)
    try:
        run_checks(window)
    finally:
        window.destroy()


def main():
    api = Api(None)
    window = webview.create_window(
        "Monarch smoke test", url=str(FRONTEND_DIR / "index.html"), js_api=api,
        width=1400, height=900,
    )
    api._window = window
    window.events.closed += api.shutdown
    window.events.loaded += _page_loaded.set

    threading.Thread(target=drive, daemon=True).start()
    # An explicit free port, never pywebview's pinned 42001. monarch/app.py
    # takes that one, so running this while the real app is open meant
    # the test's own server failed to bind and its window then loaded
    # *the running app's* page off 42001 -- the exact failure the old mora/app.py
    # documents and works around with choose_port(). The run did not blow
    # up when that happened; it quietly scored 76/81 against the wrong
    # page, which is the worse outcome. Same reasoning as
    # the old mora_smoke_test.py, and the same helper.
    webview.start(http_server=True, http_port=choose_port("Monarch smoke test", SMOKE_HTTP_PORT),
                  private_mode=False, storage_path=str(_TMP / "webview_data"))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFailed:")
        for name, _, err in failed:
            print(f"  - {name}: {err}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
