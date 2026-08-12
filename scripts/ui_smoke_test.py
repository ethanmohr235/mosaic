#!/usr/bin/env python3
"""Regression smoke test for the MonOSS desktop UI.

Not named test_*.py on purpose: pytest's isolated `conn`-fixture suite
assumes no GUI, but this spawns a real pywebview window against a real
WebKit/GTK display, so it isn't collected automatically. Run it by hand
after UI changes:

    .venv/bin/python scripts/ui_smoke_test.py

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
import sys
import tempfile
import threading
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import core.db as db

_TMP = Path(tempfile.mkdtemp(prefix="monoss-smoke-"))
db.GARDENS_DIR = _TMP / "gardens"

import webview  # noqa: E402  (import after GARDENS_DIR patched, matches app startup order)
from ui.api import Api  # noqa: E402

FRONTEND_DIR = PROJECT_ROOT / "ui" / "frontend"

results = []


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


def js_async(window, code, wait=1.5):
    """Fire an async IIFE that stores its outcome on `window.__result`,
    since evaluate_js does not await a returned Promise -- then poll it."""
    js(window, f"window.__result = undefined; ({code})();")
    time.sleep(wait)
    return js(window, "JSON.stringify(window.__result)")


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
        title = js(window, "document.getElementById('detail-title').textContent")
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
                const species = await api.create_mote({name: 'Resistor', unknowns: 'current'});
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
        hidden = js(window, "document.getElementById('new-instance-btn').classList.contains('hidden')")
        assert hidden is False, "a species with a template should offer New instance"

        js(window, "document.getElementById('new-instance-btn').click();")
        time.sleep(2.5)
        props = json.loads(js(window, "JSON.stringify((payload.motes.find(m => m.id === focusId) || {}).properties)"))
        assert props.get("resistance") == 100, f"instance should inherit the template, got {props}"
        assert props.get("name") == "R", f"instance should inherit the template, got {props}"

        kind = js(window, "document.getElementById('detail-type-label').textContent")
        assert kind == "Resistor", f"new instance should report its species, got {kind!r}"

    check("species template: New instance copies template properties", species_and_templating)

    def quick_add_prefills_from_template():
        js(window, "document.getElementById('quick-add-btn').click();")
        time.sleep(1.0)
        js(window, """
            const chip = document.querySelector(
                '#species-chips .chip[data-species-id=\"' + window.__seed.speciesId + '\"]');
            chip.click();
        """)
        time.sleep(1.5)
        rows = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('#quick-add-rows .prop-row'))
                .map(r => Array.from(r.querySelectorAll('input')).map(i => i.value)))
        """))
        assert ["resistance", "100"] in rows, f"picking a species should prefill from its template, got {rows}"

        js(window, """
            const rows = document.querySelectorAll('#quick-add-rows .prop-row');
            for (const r of rows) {
                const [k, v] = r.querySelectorAll('input');
                if (k.value === 'name') v.value = 'R99';
            }
            document.getElementById('create-mote-btn').click();
        """)
        time.sleep(2.5)
        name = js(window, "document.getElementById('detail-title').textContent")
        assert name == "R99", f"quick-add should create and select the new mote, got {name!r}"

    check("quick add: species chip prefills from template", quick_add_prefills_from_template)

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
                const rows = document.querySelectorAll('#detail-properties .detail-row');
                for (const r of rows) {
                    if (r.querySelector('.key').textContent === 'equation') {
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
                const rows = document.querySelectorAll('#detail-properties .detail-row');
                for (const r of rows) {
                    if (r.querySelector('.key').textContent === 'url') return !!r.querySelector('.open-link');
                }
                return 'row not found';
            })()
        """)
        assert has_open_on_url is True, f"a url property should get an Open button, got {has_open_on_url!r}"

        has_open_on_name = js(window, """
            (function() {
                const rows = document.querySelectorAll('#detail-properties .detail-row');
                for (const r of rows) {
                    if (r.querySelector('.key').textContent === 'name') return !!r.querySelector('.open-link');
                }
                return 'row not found';
            })()
        """)
        assert has_open_on_name is False, "only the url property should get an Open button"

        # Stub api.open_link so clicking Open doesn't try to launch a real
        # browser on the test machine; restored immediately after.
        js(window, """
            window.__opened = [];
            window.__origOpenLink = api.open_link.bind(api);
            api.open_link = (t) => { window.__opened.push(t); return Promise.resolve({opened: t}); };
        """)
        js(window, """
            const rows = document.querySelectorAll('#detail-properties .detail-row');
            for (const r of rows) {
                if (r.querySelector('.key').textContent === 'url') r.querySelector('.open-link').click();
            }
        """)
        time.sleep(0.5)
        opened = json.loads(js(window, "JSON.stringify(window.__opened)"))
        js(window, "api.open_link = window.__origOpenLink;")
        assert opened == ["https://example.com"], f"Open should call api.open_link with the property's value, got {opened}"

    check("detail panel: a url property gets a working Open button", url_property_gets_an_open_button)

    def export_to_calendar_button_calls_the_api_and_reports_the_result():
        # Stubbed -- the real path needs OAuth against a real Google
        # account, which has no place in an automated, offline suite.
        js(window, """
            window.__gcalCalled = false;
            window.__origExportToGoogleCalendar = api.export_to_google_calendar.bind(api);
            api.export_to_google_calendar = () => {
                window.__gcalCalled = true;
                return Promise.resolve({created: 2, updated: 1, skipped: 0, total: 3});
            };
        """)
        js(window, "document.getElementById('export-calendar-btn').click();")
        time.sleep(1.0)

        called = js(window, "window.__gcalCalled")
        assert called is True, "clicking Export to Calendar should call api.export_to_google_calendar"

        toast = js(window, "document.getElementById('toast').textContent")
        assert "2 new" in toast and "1 updated" in toast, f"expected a result summary toast, got {toast!r}"

        enabled = js(window, "!document.getElementById('export-calendar-btn').disabled")
        assert enabled is True, "the button should re-enable once export finishes"

        js(window, "api.export_to_google_calendar = window.__origExportToGoogleCalendar;")

    check("Export to Calendar button calls the API and reports the result", export_to_calendar_button_calls_the_api_and_reports_the_result)

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

    def simulation_runs_from_the_button():
        result = js_async(window, """
            async () => {
                const species = await api.create_mote({name: 'Box', unknowns: 'area'});
                const rule = await api.create_mote({name: 'Area', equation: 'area == width * height'});
                await api.create_relationship(rule.id, species.id, 'applies_to');
                const box = await api.create_mote({name: 'b1', width: '3', height: '4'});
                await api.set_species(box.id, species.id);
                window.__seed.boxId = box.id;
                await refreshMoteCache();
                await refocus(box.id);
                await openDetailPanel(box.id);
                window.__result = 'ok';
            }
        """, wait=2.0)
        assert result == '"ok"', f"simulation setup failed: {result}"

        js(window, "document.getElementById('run-sim-btn').click();")
        time.sleep(3.0)

        area = json.loads(js_async(window, """
            async () => {
                const mote = await api.get_mote(window.__seed.boxId);
                window.__result = mote.properties.area;
            }
        """))
        assert area == 12.0, f"expected the solver to write area=12, got {area!r}"
        toast = js(window, "document.getElementById('toast').textContent")
        assert "Solved" in toast, f"expected a success toast, got {toast!r}"

        shown = js(window, """
            (function() {
                const rows = document.querySelectorAll('#detail-properties .detail-row');
                for (const row of rows) {
                    if (row.querySelector('.key').textContent === 'area') {
                        return row.querySelector('input, textarea').value;
                    }
                }
                return null;
            })()
        """)
        assert shown == "12", f"open panel should refresh to show area=12, got {shown!r}"

    check("simulation: Run button solves and refreshes the panel", simulation_runs_from_the_button)

    def unsolvable_garden_reports_instead_of_crashing():
        js_async(window, """
            async () => {
                const species = await api.create_mote({name: 'Loose', unknowns: 'x, y'});
                const rule = await api.create_mote({name: 'one eq', equation: 'x + y == 10'});
                await api.create_relationship(rule.id, species.id, 'applies_to');
                const m = await api.create_mote({name: 'loose1'});
                await api.set_species(m.id, species.id);
                window.__result = 'ok';
            }
        """)
        js(window, "document.getElementById('run-sim-btn').click();")
        time.sleep(3.0)
        toast = js(window, "document.getElementById('toast').textContent")
        assert "underdetermined" in toast, f"expected an underdetermined message, got {toast!r}"
        enabled = js(window, "!document.getElementById('run-sim-btn').disabled")
        assert enabled is True, "Run button should be re-enabled after a failed run"

    check("simulation: unsolvable garden reports instead of crashing", unsolvable_garden_reports_instead_of_crashing)

    def garden_switch_isolates_data():
        result = js_async(window, """
            async () => {
                await api.create_garden('smoke-second');
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

    def gardens_modal_lists_switches_and_guards_delete():
        # Two gardens exist at this point: 'default' (current) and
        # 'smoke-second', left behind by the check above.
        js(window, "document.getElementById('garden-btn').click();")
        time.sleep(1.0)
        visible = js(window, "!document.getElementById('gardens-overlay').classList.contains('hidden')")
        assert visible is True, "clicking the garden button should open the modal"

        rows = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('.garden-row')).map(r => [
                r.querySelector('.name').textContent, r.querySelector('.danger').disabled
            ]))
        """))
        assert dict(rows) == {"default": True, "smoke-second": False}, (
            f"delete should be disabled only for the current garden, got {rows}"
        )

        # Deleting the non-current garden should work end-to-end.
        js(window, "window.confirm = () => true;")
        js(window, """
            Array.from(document.querySelectorAll('.garden-row'))
                .find(r => r.querySelector('.name').textContent === 'smoke-second')
                .querySelector('.danger').click();
        """)
        time.sleep(2.0)
        remaining = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('.garden-row')).map(r => r.querySelector('.name').textContent))
        """))
        assert remaining == ["default"], f"deleting smoke-second should remove its row, got {remaining}"

        deleted_from_disk = json.loads(js_async(window, """
            async () => { window.__result = await api.list_gardens(); }
        """))
        assert "smoke-second" not in deleted_from_disk, "the garden should actually be gone, not just hidden"

    check("gardens modal: lists gardens, guards delete, deletes a non-current garden", gardens_modal_lists_switches_and_guards_delete)

    def gardens_modal_delete_can_be_undone():
        js_async(window, """
            async () => {
                await api.create_garden('smoke-undo');
                window.__result = 'ok';
            }
        """, wait=1.0)
        js(window, "document.getElementById('garden-btn').click();")
        time.sleep(1.0)
        js(window, "window.confirm = () => true;")
        js(window, """
            Array.from(document.querySelectorAll('.garden-row'))
                .find(r => r.querySelector('.name').textContent === 'smoke-undo')
                .querySelector('.danger').click();
        """)
        time.sleep(2.0)

        after_delete = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('.garden-row')).map(r => r.querySelector('.name').textContent))
        """))
        assert "smoke-undo" not in after_delete, f"setup: delete should have removed the row, got {after_delete}"

        toast_text = js(window, "document.getElementById('toast').textContent")
        assert "Undo" in toast_text, f"deleting a garden should surface an Undo toast, got {toast_text!r}"
        js(window, "document.querySelector('#toast button').click();")
        time.sleep(2.0)

        on_disk = json.loads(js_async(window, "async () => { window.__result = await api.list_gardens(); }"))
        assert "smoke-undo" in on_disk, "Undo should bring the garden back on disk"

        rows_after_undo = json.loads(js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('.garden-row')).map(r => r.querySelector('.name').textContent))
        """))
        assert "smoke-undo" in rows_after_undo, "Undo should re-list the garden in the modal"

    check("gardens modal: a deleted garden's Undo toast brings it back", gardens_modal_delete_can_be_undone)

    def gardens_modal_esc_closes_it():
        press_key(window, "Escape")
        time.sleep(0.5)
        hidden = js(window, "document.getElementById('gardens-overlay').classList.contains('hidden')")
        assert hidden is True, "Esc should close the gardens modal"

    check("gardens modal: Esc closes it", gardens_modal_esc_closes_it)

    def gardens_modal_creates_and_switches():
        js(window, "window.prompt = () => 'smoke-third';")
        js(window, "document.getElementById('garden-btn').click();")
        time.sleep(1.0)
        js(window, "document.getElementById('new-garden-btn').click();")
        time.sleep(2.5)

        assert js(window, "currentGarden") == "smoke-third", "creating a garden should switch to it"
        modal_closed = js(window, "document.getElementById('gardens-overlay').classList.contains('hidden')")
        assert modal_closed is True, "creating a garden should close the modal"

        # Restore 'default' as current so the rest of the suite isn't
        # affected by starting a fresh garden here.
        js_async(window, """
            async () => {
                await api.switch_garden('default');
                await loadGarden('default');
                window.__result = 'ok';
            }
        """, wait=2.0)

    check("gardens modal: creating a garden switches to it and closes the modal", gardens_modal_creates_and_switches)

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

        right_click(window, 60, 60)  # empty canvas, away from any node
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
        right_click(window, 60, 60)
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

        right_click(window, 700, 550)  # empty canvas, far from P/Q
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

        right_click(window, 700, 550)
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

        js(window, "document.getElementById('detail-new-key').focus();")
        time.sleep(0.2)
        focused = js(window, "document.activeElement.id")
        assert focused == "detail-new-key", "setup: the property key field should have focus"
        js(window, """
            document.getElementById('detail-new-key').dispatchEvent(
                new KeyboardEvent('keydown', {key: 'c', ctrlKey: true, bubbles: true}));
        """)
        time.sleep(0.3)
        assert js(window, "clipboard") is None, (
            "Ctrl+C while typing in a field must not hijack normal text copy"
        )
        js(window, "document.getElementById('detail-new-key').blur();")

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
        result = js_async(window, """
            async () => {
                await api.create_garden('smoke-autofit');
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

    def no_uncaught_errors():
        errors = js(window, "JSON.stringify(window.__uiErrors || [])")
        assert errors == "[]", f"the UI logged uncaught errors: {errors}"

    check("no uncaught JS errors during the run", no_uncaught_errors)


def drive():
    time.sleep(3.5)
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
        "MonOSS smoke test", url=str(FRONTEND_DIR / "index.html"), js_api=api,
        width=1400, height=900,
    )
    api._window = window
    window.events.closed += api.shutdown

    threading.Thread(target=drive, daemon=True).start()
    webview.start(http_server=True, private_mode=False, storage_path=str(_TMP / "webview_data"))

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("\nFailed:")
        for name, _, err in failed:
            print(f"  - {name}: {err}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
