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


def drag(window, x1, y1, x2, y2, ctrl=False):
    """Synthetic press-move-release against the canvas, in page pixels."""
    js(window, f"""
        (function() {{
            const canvas = document.getElementById('graph');
            const steps = 12;
            function fire(type, x, y, target) {{
                target.dispatchEvent(new MouseEvent(type, {{
                    clientX: x, clientY: y, bubbles: true, cancelable: true,
                    button: 0, buttons: 1, ctrlKey: {str(ctrl).lower()},
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


def node_xy(window, id_expr):
    pos = json.loads(js(window, f"JSON.stringify(graph.screenPositionOf({id_expr}))"))
    return pos["x"], pos["y"]


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
