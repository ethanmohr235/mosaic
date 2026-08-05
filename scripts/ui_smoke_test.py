#!/usr/bin/env python3
"""Regression smoke test for the MonOSS desktop UI.

Not named test_*.py on purpose: pytest's isolated `conn`-fixture suite
assumes no GUI, but this spawns a real pywebview window against a real
WebKit/GTK display, so it isn't collected automatically. Run it by hand
after UI changes:

    .venv/bin/python scripts/ui_smoke_test.py

Consolidates checks that, across the session this was written in, only
ever caught real regressions when written by hand as one-off throwaway
scripts: cross-thread sqlite access, localStorage silently unavailable
under file://, layout jitter for brand-new nodes, and a position-
persistence bug that only showed up once motes were viewed in isolation
before being connected. Never touches real data -- GARDENS_DIR and the
webview storage_path are both pointed at a fresh temp directory.
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


def js_async(window, code, var="__result", wait=1.5):
    """Fire an async IIFE that stores its outcome on `window[var]`, since
    evaluate_js does not await a returned Promise -- then poll it back."""
    js(window, f"window.{var} = undefined; ({code})();")
    time.sleep(wait)
    return js(window, f"JSON.stringify(window.{var})")


def run_checks(window):
    def create_seed_data():
        result = js_async(window, """
            async () => {
                const a = await api.create_mote({name: 'Alice', role: 'engineer'});
                const b = await api.create_mote({name: 'Bob'});
                const c = await api.create_mote({age: '42'});
                await api.create_relationship(a.id, b.id, 'knows');
                window.__seed = {aliceId: a.id, bobId: b.id, carolId: c.id};
                await refocus(a.id);
                window.__result = 'ok';
            }
        """)
        assert result == '"ok"', f"seed setup failed: {result}"

    check("create motes + relationship", create_seed_data)

    def ctrl_drag_connects_without_moving_node():
        # Alice and Bob are already rendered together (connected via the
        # seed 'knows' relationship, both within the focused neighborhood).
        js(window, "window.prompt = function() { return 'ctrl-drag-test'; };")
        geo = json.loads(js(window, """
            JSON.stringify((function() {
                const rect = document.getElementById('cy').getBoundingClientRect();
                return {
                    rect: {left: rect.left, top: rect.top},
                    a: cy.getElementById(window.__seed.aliceId).renderedPosition(),
                    b: cy.getElementById(window.__seed.bobId).renderedPosition(),
                };
            })());
        """))
        ax, ay = geo["rect"]["left"] + geo["a"]["x"], geo["rect"]["top"] + geo["a"]["y"]
        bx, by = geo["rect"]["left"] + geo["b"]["x"], geo["rect"]["top"] + geo["b"]["y"]
        pos_before = js(window, "JSON.stringify(cy.getElementById(window.__seed.aliceId).position())")

        js(window, f"""
            (function() {{
                const canvas = document.querySelector('#cy canvas:last-child');
                const steps = 12;
                function fire(type, x, y) {{
                    canvas.dispatchEvent(new MouseEvent(type, {{
                        clientX: x, clientY: y, bubbles: true, cancelable: true,
                        button: 0, buttons: 1, ctrlKey: true,
                    }}));
                }}
                fire('mousedown', {ax}, {ay});
                for (let i = 1; i <= steps; i++) {{
                    const t = i / steps;
                    fire('mousemove', {ax} + ({bx} - {ax}) * t, {ay} + ({by} - {ay}) * t);
                }}
                fire('mouseup', {bx}, {by});
            }})();
        """)
        time.sleep(1.5)

        pos_after = js(window, "JSON.stringify(cy.getElementById(window.__seed.aliceId).position())")
        assert pos_before == pos_after, f"ctrl+drag should not reposition the source node: {pos_before} -> {pos_after}"

        rels = json.loads(js_async(window, """
            async () => {
                window.__result = await api.mote_relationships(window.__seed.aliceId);
            }
        """))
        types = [r["type"] for r in rels]
        assert "ctrl-drag-test" in types, f"ctrl+drag should have created a relationship, got: {types}"

    check("ctrl+drag connects two motes without moving them", ctrl_drag_connects_without_moving_node)

    def graph_shows_label_fallback():
        js(window, "refocus(window.__seed.carolId)")
        time.sleep(1.5)
        label = js(window, "cy.nodes('.focused').data('label')")
        assert label == "age: 42", f"expected fallback label 'age: 42', got {label!r}"

    check("label falls back to first property when no name", graph_shows_label_fallback)

    def positions_stable_across_refocus():
        js(window, "refocus(window.__seed.aliceId)")
        time.sleep(1.5)
        p1 = js(window, "JSON.stringify(cy.getElementById(window.__seed.bobId).position())")
        js(window, "refocus(window.__seed.bobId)")
        time.sleep(1.5)
        js(window, "refocus(window.__seed.aliceId)")
        time.sleep(1.5)
        p2 = js(window, "JSON.stringify(cy.getElementById(window.__seed.bobId).position())")
        assert p1 == p2, f"position drifted across refocus: {p1} -> {p2}"

    check("node positions stable across refocus", positions_stable_across_refocus)

    def search_browse_and_filter():
        js(window, "document.getElementById('search-input').focus();")
        time.sleep(0.8)
        all_count = js(window, "document.querySelectorAll('.search-result-row').length")
        assert all_count == 3, f"expected 3 motes browsable, got {all_count}"

        js(window, """
            const i = document.getElementById('search-input');
            i.value = 'engineer'; i.dispatchEvent(new Event('input'));
        """)
        time.sleep(0.5)
        filtered_labels = js(window, """
            JSON.stringify(Array.from(document.querySelectorAll('.search-result-row span:first-child')).map(e => e.textContent))
        """)
        assert filtered_labels == '["Alice"]', f"property-value search failed: {filtered_labels}"

    check("search: browse all + filter by property value", search_browse_and_filter)

    def search_keyboard_nav_selects():
        js(window, """
            const i = document.getElementById('search-input');
            i.value = ''; i.dispatchEvent(new Event('input'));
            i.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowDown'}));
            i.dispatchEvent(new KeyboardEvent('keydown', {key: 'Enter'}));
        """)
        time.sleep(1.5)
        hidden = js(window, "document.getElementById('search-results').classList.contains('hidden')")
        assert hidden is True, "search results should close after Enter-selecting"

    check("search: keyboard nav + Enter selects and closes", search_keyboard_nav_selects)

    def garden_switch_isolates_data():
        js(window, "api.create_garden('smoke-second');")
        time.sleep(0.5)
        result = js_async(window, """
            async () => {
                await api.switch_garden('smoke-second');
                await loadGarden('smoke-second');
                const motes = await api.list_motes();
                window.__result = motes.length;
            }
        """)
        assert result == "0", f"new garden should start empty, got {result} motes"

        js_async(window, """
            async () => {
                await api.switch_garden('default');
                await loadGarden('default');
                window.__result = 'ok';
            }
        """)
        time.sleep(0.5)
        count = js(window, "cy.nodes().length >= 0 ? 'ok' : 'fail'")
        assert count == "ok"

    check("garden switch isolates data", garden_switch_isolates_data)

    def delete_relationship_and_mote():
        result = js_async(window, """
            async () => {
                // Delete *all* of Alice's relationships, not just one --
                // earlier checks (e.g. the ctrl+drag one) may have left
                // more than the single seeded 'knows' relationship.
                const rels = await api.mote_relationships(window.__seed.aliceId);
                for (const r of rels) {
                    await api.delete_relationship(r.id);
                }
                const remaining = await api.mote_relationships(window.__seed.aliceId);
                await api.delete_mote(window.__seed.carolId);
                const carol = await api.get_mote(window.__seed.carolId);
                window.__result = { relsLeft: remaining.length, carolGone: carol === null };
            }
        """)
        parsed = json.loads(result)
        assert parsed["relsLeft"] == 0, "relationship delete didn't take"
        assert parsed["carolGone"] is True, "mote delete didn't take"

    check("delete relationship + mote", delete_relationship_and_mote)

    def species_set_and_remove_via_real_ui():
        # Drives the actual "Set species..." button/picker/row-click flow
        # with synthetic DOM events, rather than calling api.set_species
        # directly -- a prior version of this check did the latter and
        # missed a real bug where the picker's own click handler never
        # called refocus()/openDetailPanel(), so nothing visibly updated
        # until an unrelated action (e.g. navigating away and back)
        # happened to re-render the panel.
        result = js_async(window, """
            async () => {
                const resistor = await api.create_mote({name: 'Resistor'});
                window.__seed.resistorId = resistor.id;
                window.__result = 'ok';
            }
        """)
        assert result == '"ok"', f"species setup failed: {result}"

        js(window, "refocus(window.__seed.aliceId).then(() => openDetailPanel(window.__seed.aliceId))")
        time.sleep(1.5)

        js(window, "document.getElementById('set-species-btn').click();")
        time.sleep(0.8)
        js(window, """
            const i = document.getElementById('species-picker-input');
            i.value = 'Resistor';
            i.dispatchEvent(new Event('input'));
        """)
        time.sleep(0.5)
        js(window, """
            (function() {
                const row = document.querySelector('#species-picker-results .search-result-row');
                row.dispatchEvent(new MouseEvent('mousedown', {bubbles: true, cancelable: true, button: 0}));
            })();
        """)
        time.sleep(1.5)  # no manual refocus/openDetailPanel call -- that's the point

        species_label = js(window, """
            (function() {
                const rows = document.querySelectorAll('#detail-species .rel-label');
                return rows.length > 0 ? rows[0].textContent : null;
            })()
        """)
        assert species_label == "Resistor", (
            f"Species section should update on its own after picking via the UI, got {species_label!r}"
        )

        is_diamond = js(window, "cy.getElementById(window.__seed.resistorId).hasClass('species')")
        assert is_diamond is True, "graph should pick up the new species styling without a manual refocus"

        js(window, "refocus(window.__seed.resistorId).then(() => openDetailPanel(window.__seed.resistorId))")
        time.sleep(1.5)
        instance_count_text = js(window, "document.getElementById('detail-instance-count').textContent")
        assert instance_count_text == "Instances: 1", (
            f"expected instance count text, got {instance_count_text!r}"
        )

        # Removing a species membership (via the x button) should refresh
        # the panel on its own too.
        js(window, "refocus(window.__seed.aliceId).then(() => openDetailPanel(window.__seed.aliceId))")
        time.sleep(1.5)
        js(window, "document.querySelector('#detail-species .rel-row button').click();")
        time.sleep(1.5)

        remaining = js(window, "document.querySelectorAll('#detail-species .rel-label').length")
        assert remaining == 0, "Species section should clear on its own after removing via the x button"

    check(
        "species: set/remove via real UI refreshes panel + graph without extra action",
        species_set_and_remove_via_real_ui,
    )

    def depth_control_changes_how_much_graph_is_visible():
        # A --next--> B --next--> C, plus a mote connected to nothing.
        js_async(window, """
            async () => {
                const a = await api.create_mote({name: 'depth-A'});
                const b = await api.create_mote({name: 'depth-B'});
                const c = await api.create_mote({name: 'depth-C'});
                await api.create_mote({name: 'depth-island'});
                await api.create_relationship(a.id, b.id, 'next');
                await api.create_relationship(b.id, c.id, 'next');
                window.__seed.depthA = a.id;
                window.__result = 'ok';
            }
        """)

        def visible_at(value):
            js(window, f"""
                const s = document.getElementById('depth-select');
                s.value = {json.dumps(value)};
                s.dispatchEvent(new Event('change'));
            """)
            time.sleep(1.8)
            js(window, "refocus(window.__seed.depthA)")
            time.sleep(1.8)
            return js(window, "cy.nodes().length")

        assert visible_at("1") == 2, "1 hop should show only A and its direct neighbour"
        assert visible_at("2") == 3, "2 hops should reach C"

        # 'Whole garden' has to reach the unconnected mote too -- no hop
        # count ever can, which is the reason it exists as its own setting.
        total = json.loads(js_async(window, """
            async () => { window.__result = (await api.list_motes()).length; }
        """))
        assert visible_at("all") == total, "whole garden should show every mote in the garden"

        stored = js(window, "localStorage.getItem('monoss:depth')")
        assert stored == "all", f"depth setting should persist, got {stored!r}"

    check("depth control changes how much of the graph is visible", depth_control_changes_how_much_graph_is_visible)

    def run_simulation_button_solves_and_refreshes():
        # A whole little model built through the API, then solved by
        # clicking the real button: Box(width=3, height=4) -> area 12.
        result = js_async(window, """
            async () => {
                const species = await api.create_mote({name: 'Box', unknowns: 'area'});
                const rule = await api.create_mote({
                    name: 'Area', equation: 'area == width * height',
                });
                await api.create_relationship(rule.id, species.id, 'applies_to');
                const box = await api.create_mote({name: 'b1', width: '3', height: '4'});
                await api.set_species(box.id, species.id);
                window.__seed.boxId = box.id;
                await refocus(box.id);
                await openDetailPanel(box.id);
                window.__result = 'ok';
            }
        """)
        assert result == '"ok"', f"simulation setup failed: {result}"

        js(window, "document.getElementById('run-sim-btn').click();")
        time.sleep(2.5)

        area = json.loads(js_async(window, """
            async () => {
                const mote = await api.get_mote(window.__seed.boxId);
                window.__result = mote.properties.area;
            }
        """))
        assert area == 12.0, f"expected the solver to write area=12, got {area!r}"

        toast = js(window, "document.getElementById('toast').textContent")
        assert "Solved" in toast, f"expected a success toast, got {toast!r}"

        # The panel was open across the run, so it must show the new value
        # without any further interaction.
        shown = js(window, """
            (function() {
                const rows = document.querySelectorAll('#detail-properties .detail-row');
                for (const row of rows) {
                    if (row.querySelector('span').textContent === 'area') {
                        return row.querySelector('input').value;
                    }
                }
                return null;
            })()
        """)
        assert shown == "12", f"open detail panel should refresh to show area=12, got {shown!r}"

    check("simulation: Run button solves and refreshes the panel", run_simulation_button_solves_and_refreshes)

    def unsolvable_rules_report_instead_of_crashing():
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
        time.sleep(2.5)

        toast = js(window, "document.getElementById('toast').textContent")
        assert "underdetermined" in toast, f"expected an underdetermined message, got {toast!r}"
        enabled = js(window, "!document.getElementById('run-sim-btn').disabled")
        assert enabled is True, "Run button should be re-enabled after a failed run"

    check("simulation: unsolvable garden reports instead of crashing", unsolvable_rules_report_instead_of_crashing)


def drive():
    time.sleep(3)
    window = webview.windows[0]
    try:
        run_checks(window)
    finally:
        window.destroy()


def main():
    api = Api(None)
    window = webview.create_window(
        "MonOSS smoke test", url=str(FRONTEND_DIR / "index.html"), js_api=api,
        width=1280, height=800,
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
