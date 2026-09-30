"""Motion's JS bridge and its payload shaping.

The simulation half of these moved here from tests/test_api.py when the
sim surface moved from Monarch to Motion -- same assertions, renamed
calls (preview_simulation -> preview, and so on). The board, lane and
node-kind tests are new, since nothing shaped a garden this way before.
"""
import pytest

from core.gardens import create_garden
from core.simulation import SimulationError
from motion import payload as payload_repo
from motion.api import Api


@pytest.fixture
def api():
    create_garden("test")
    instance = Api("test")
    yield instance
    instance.shutdown()


def _seed_compounding_rule(api):
    """Mirrors tests/test_simulation.py's `compounding` fixture, but
    through the JS-facing Api rather than core.simulation directly."""
    species = api.create_mote({"name": "Account", "freedoms": "balance"})
    rule = api.create_mote({"name": "Growth", "equation": "balance == prev(balance) * 1.05"})
    api.create_relationship(rule["id"], species["id"], "applies_to")
    mote = api.create_mote({"name": "acct1", "balance": 100.0})
    _set_species(api, mote["id"], species["id"])
    return mote["id"]


def _set_species(api, mote_id, species_id):
    """Motion's bridge has no set_species of its own -- species are
    Monarch's business, and this window only ever reads them. The
    relationship is the whole mechanism (core/species.py), so the tests
    draw it directly rather than growing the bridge a method the window
    would never call."""
    api.create_relationship(mote_id, species_id, "instance_of")


def _seed_ohm(api):
    """A resistor whose voltage is solved from current and resistance."""
    species = api.create_mote({"name": "Resistor", "freedoms": "voltage"})
    rule = api.create_mote({"name": "Ohm", "equation": "voltage == current * resistance"})
    api.create_relationship(rule["id"], species["id"], "applies_to")
    mote = api.create_mote({"name": "R1", "current": "2", "resistance": "3"})
    _set_species(api, mote["id"], species["id"])
    return {"mote": mote["id"], "species": species["id"], "rule": rule["id"]}


def _scenario_over(api, target_id, **properties):
    override = api.create_mote({str(k): str(v) for k, v in properties.items()})
    api.create_relationship(override["id"], target_id, "overrides")
    scenario = api.create_mote({"name": "High Load"})
    api.create_relationship(scenario["id"], override["id"], "varies")
    return scenario["id"]


# ---- running ----

def test_preview_returns_a_step_per_requested_count(api):
    _seed_compounding_rule(api)
    result = api.preview(4)
    assert result["failed_at"] is None
    assert result["error"] is None
    assert [s["step"] for s in result["steps"]] == [0, 1, 2, 3]
    assert result["steps"][0]["changes"][0]["new_value"] == pytest.approx(105.0)


def test_preview_never_writes(api):
    mote_id = _seed_compounding_rule(api)
    before = api.inspect(mote_id)
    api.preview(10)
    assert api.inspect(mote_id) == before


def test_solve_writes_back_and_reports_what_changed(api):
    garden = _seed_ohm(api)
    result = api.solve()

    assert result["error"] is None
    assert result["summary"].startswith("Solved 1 value")
    assert [(c["key"], c["new_value"]) for c in result["changes"]] == [("voltage", 6)]
    assert result["equation_count"] >= 0
    assert result["unsolved"] == []
    assert api.inspect(garden["mote"])["properties"]["voltage"] == 6


def test_solve_reports_a_failure_as_an_error_and_writes_nothing(api):
    species = api.create_mote({"name": "S", "freedoms": "a, b"})
    rule = api.create_mote({"name": "underdetermined", "equation": "a + b == 10"})
    api.create_relationship(rule["id"], species["id"], "applies_to")
    mote = api.create_mote({"name": "m"})
    _set_species(api, mote["id"], species["id"])

    result = api.solve()
    assert "underdetermined" in result["error"]
    assert "a" not in api.inspect(mote["id"])["properties"]


def test_solve_refuses_a_recurrence_rather_than_advancing_it(api):
    """prev() needs a previous step, which only a preview has. Solving is
    one-shot, so a recurrence is refused -- never quietly stepped."""
    mote_id = _seed_compounding_rule(api)
    result = api.solve()
    assert "previous value" in result["error"]
    assert api.inspect(mote_id)["properties"]["balance"] == 100.0


def test_preview_reports_a_failure_without_raising(api):
    species = api.create_mote({"name": "S", "freedoms": "a, b"})
    rule = api.create_mote({"name": "underdetermined", "equation": "a + b == 10"})
    api.create_relationship(rule["id"], species["id"], "applies_to")
    mote = api.create_mote({"name": "m"})
    _set_species(api, mote["id"], species["id"])

    result = api.preview(5)
    assert result["failed_at"] == 0
    assert "underdetermined" in result["error"]
    assert result["steps"] == []


def test_every_run_payload_carries_the_axis_it_is_counted_in(api):
    """The dock never assumes seconds -- see motion/payload.tick_axis."""
    _seed_compounding_rule(api)
    result = api.preview(3)
    assert result["axis"] == {"unit": "tick", "label": "ticks", "count": 3}


def _seed_mismatched_units(api):
    """A rule that adds a resistance to a current -- dimensionally wrong,
    but solvable as bare numbers. Mirrors the `mismatched` fixture in
    tests/test_units_simulation.py, through the JS-facing Api."""
    species = api.create_mote({"name": "Element", "freedoms": "total"})
    rule = api.create_mote({"name": "Bad sum", "equation": "total == resistance + current"})
    api.create_relationship(rule["id"], species["id"], "applies_to")
    mote = api.create_mote({
        "name": "E", "resistance": "1", "current": "2",
        "units": {"resistance": "ohm", "current": "A", "total": "ohm"},
    })
    _set_species(api, mote["id"], species["id"])
    return mote["id"]


def test_strict_units_is_advisory_by_default(api):
    _seed_mismatched_units(api)
    result = api.preview(1)
    assert result["dimension_warnings"]


def test_preview_honours_strict_units(api):
    _seed_mismatched_units(api)
    with pytest.raises(SimulationError, match="dimensional check failed"):
        api.preview(1, strict_units=True)


def test_solve_honours_strict_units(api):
    mote_id = _seed_mismatched_units(api)
    result = api.solve(strict_units=True)
    assert "dimensional check failed" in result["error"]
    assert "total" not in api.inspect(mote_id)["properties"]


def test_compare_honours_strict_units(api):
    mote_id = _seed_mismatched_units(api)
    scenario = _scenario_over(api, mote_id, current=3)
    with pytest.raises(SimulationError, match="dimensional check failed"):
        api.compare(1, "linear", None, scenario, strict_units=True)


# ---- scenarios ----

def test_preview_under_a_scenario_writes_nothing(api):
    garden = _seed_ohm(api)
    scenario_id = _scenario_over(api, garden["mote"], current=10)

    plain = api.preview(1)
    assert plain["steps"][0]["changes"][0]["new_value"] == 6

    varied = api.preview(1, scenario=scenario_id)
    assert varied["steps"][0]["changes"][0]["new_value"] == 30
    assert "voltage" not in api.inspect(garden["mote"])["properties"]


def test_compare_runs_two_scenarios_side_by_side(api):
    garden = _seed_ohm(api)
    scenario_id = _scenario_over(api, garden["mote"], current=10)

    result = api.compare(1, "linear", None, scenario_id)
    assert result["a"]["scenario"] is None
    assert result["b"]["scenario"] == scenario_id
    assert result["a"]["steps"][0]["changes"][0]["new_value"] == 6
    assert result["b"]["steps"][0]["changes"][0]["new_value"] == 30
    # Neither side is written: both are ordinary previews.
    assert "voltage" not in api.inspect(garden["mote"])["properties"]


def test_preview_reports_constraint_violations(api):
    garden = _seed_ohm(api)
    limit = api.create_mote({"name": "Too big", "equation": "voltage <= 1"})
    api.create_relationship(limit["id"], garden["species"], "applies_to")

    result = api.preview(1)
    assert len(result["constraint_violations"]) == 1
    assert "Too big" in result["constraint_violations"][0]
    assert result["steps"][0]["changes"][0]["new_value"] == 6   # advisory only
    # ...and the same violation reaches the timeline as a mark to jump to.
    assert [e["tick"] for e in result["events"]] == [0]
    assert result["events"][0]["kind"] == "constraint"


# ---- explanation and dependencies ----

def test_why_explains_a_solved_value(api):
    garden = _seed_ohm(api)
    api.solve()

    explanation = api.why(garden["mote"], "voltage")
    assert explanation["value"] == 6
    assert explanation["method"] == "substitution"
    assert [e["rule_name"] for e in explanation["equations"]] == ["Ohm"]
    assert {i["key"] for i in explanation["inputs"]} == {"current", "resistance"}
    # A stored input has nothing behind it, which is where the panel stops.
    assert all(i["derivation"] is None for i in explanation["inputs"])


def test_why_before_any_run_returns_a_note_rather_than_failing(api):
    garden = _seed_ohm(api)
    explanation = api.why(garden["mote"], "voltage")
    assert explanation["note"]
    assert explanation["equations"] == []


def test_why_follows_the_latest_solve(api):
    garden = _seed_ohm(api)
    api.solve()
    api.update_mote(garden["mote"], {"current": "10"})
    api.solve()
    assert api.why(garden["mote"], "voltage")["value"] == 30


def test_a_preview_or_failed_solve_leaves_why_on_the_last_solve(api):
    garden = _seed_ohm(api)
    api.solve()
    api.preview(2)
    assert api.why(garden["mote"], "voltage")["value"] == 6


def test_inspector_why_prints_the_note_for_an_underived_value(api):
    garden = _seed_ohm(api)
    api.solve()
    assert api.inspector_why(garden["mote"], "current") == {
        "lines": ["not determined by the last solve -- set directly, or unchanged since"]
    }
    lines = api.inspector_why(garden["mote"], "voltage")["lines"]
    assert lines[0].startswith("substitution")
    assert any("Ohm" in line for line in lines)


def test_dependencies_answers_without_a_run(api):
    garden = _seed_ohm(api)
    deps = api.dependencies(garden["mote"], "voltage")

    assert deps["free"] is True
    assert deps["determinable"] is True
    assert deps["producers"] == ["Ohm"]
    assert {n["key"]: n["status"] for n in deps["needs"]} == {
        "current": "stored", "resistance": "stored"
    }


def test_dependencies_reports_a_value_that_is_not_free(api):
    garden = _seed_ohm(api)
    deps = api.dependencies(garden["mote"], "current")

    assert deps["free"] is False
    assert deps["producers"] == []
    assert deps["consumers"] == ["Ohm"]
    assert [e["key"] for e in deps["impact"]] == ["voltage"]


def test_check_roles_reports_wiring_and_scope(api):
    whole = api.create_mote({"name": "W1", "roles": '["left"]'})
    report = api.check_roles()

    assert report["motes_checked"] == 1
    assert "'left'" in report["problems"][0]

    part = api.create_mote({"name": "Left"})
    api.create_relationship(whole["id"], part["id"], "left")
    assert api.check_roles()["problems"] == []


# ---- the board ----

def test_board_answers_the_whole_window_in_one_call(api):
    _seed_ohm(api)
    board = api.board()
    assert board["garden"] == "test"
    assert {n["label"] for n in board["nodes"]} == {"Resistor", "Ohm", "R1"}
    assert [r["name"] for r in board["rules"]] == ["Ohm"]
    assert "linear" in board["solvers"]
    assert "runs" not in board


def test_solvers_come_from_the_registry_not_a_fixed_menu(api):
    """The seam a future integrator arrives through: registering it in
    core/solver_registry.py is the whole change on this side."""
    from core import solvers as solvers_repo
    assert api.board()["solvers"] == solvers_repo.list_solvers()


# ---- check_equation_names ----

def test_check_equation_names_offers_an_unconnected_mote(api):
    a = api.create_mote({"name": "A"})
    b = api.create_mote({"name": "B", "x": 5})
    result = api.check_equation_names(a["id"], "y == B.x")
    assert result == [{"name": "B", "candidates": [{"id": b["id"], "label": "B"}]}]


def test_check_equation_names_empty_once_connected(api):
    a = api.create_mote({"name": "A"})
    b = api.create_mote({"name": "B", "x": 5})
    api.create_relationship(a["id"], b["id"], "relates_to")
    assert api.check_equation_names(a["id"], "y == B.x") == []


def test_a_rule_mote_is_a_rule_node_and_carries_its_equation(api):
    garden = _seed_ohm(api)
    node = next(n for n in api.board()["nodes"] if n["id"] == garden["rule"])
    assert node["kind"] == payload_repo.RULE
    assert node["equation"] == "voltage == current * resistance"


def test_a_mote_declaring_freedoms_is_a_free_node(api):
    garden = _seed_ohm(api)
    node = next(n for n in api.board()["nodes"] if n["id"] == garden["species"])
    assert node["kind"] == payload_repo.FREE
    assert node["freedoms"] == ["voltage"]


def test_a_scenarios_targets_are_varied_nodes_only_under_that_scenario(api):
    garden = _seed_ohm(api)
    scenario_id = _scenario_over(api, garden["mote"], current=10)

    plain = next(n for n in api.board()["nodes"] if n["id"] == garden["mote"])
    assert plain["kind"] == payload_repo.FIXED

    varied = next(
        n for n in api.board(scenario=scenario_id)["nodes"] if n["id"] == garden["mote"]
    )
    assert varied["kind"] == payload_repo.VARIED


def test_override_and_template_motes_never_reach_the_board(api):
    """core/simulation.py leaves both out of a solve, so drawing them
    would put motes on the canvas no run will ever move."""
    garden = _seed_ohm(api)
    override = api.create_mote({"name": "Override", "current": "10"})
    api.create_relationship(override["id"], garden["mote"], "overrides")
    template = api.create_mote({"name": "Template"})
    api.create_relationship(template["id"], garden["species"], "template_of")

    ids = {n["id"] for n in api.board()["nodes"]}
    assert override["id"] not in ids
    assert template["id"] not in ids


def test_applies_to_is_drawn_as_a_governs_edge(api):
    garden = _seed_ohm(api)
    edge = next(
        e for e in api.board()["edges"]
        if e["from"] == garden["rule"] and e["to"] == garden["species"]
    )
    assert edge["kind"] == payload_repo.GOVERNS
    assert edge["type"] == "applies_to"


def test_a_rules_bindings_partition_what_it_solves_from_what_it_reads(api):
    garden = _seed_ohm(api)
    rule = api.board()["rules"][0]
    solved = {b["key"] for b in rule["bindings"] if b["role"] == "determines"}
    read = {b["key"] for b in rule["bindings"] if b["role"] == "reads"}
    assert solved == {"voltage"}
    assert read == {"current", "resistance"}
    assert all(b["moteId"] == garden["mote"] for b in rule["bindings"])


def test_inspect_returns_a_motes_state_and_neighbourhood(api):
    garden = _seed_ohm(api)
    detail = api.inspect(garden["mote"])
    assert detail["label"] == "R1"
    assert detail["properties"]["current"] == 2
    assert {r["type"] for r in detail["relationships"]} == {"instance_of"}


def test_inspect_returns_none_for_a_missing_mote(api):
    assert api.inspect("nope") is None


# ---- lanes ----

def test_lanes_trace_each_value_a_run_moved(api):
    mote_id = _seed_compounding_rule(api)
    result = api.preview(3)
    [lane] = api.lanes(result["steps"])

    assert lane["moteId"] == mote_id
    assert lane["key"] == "balance"
    # Point 0 is the value before the run, so a lane shows where it started.
    assert lane["points"] == pytest.approx([100.0, 105.0, 110.25, 115.7625])
    assert lane["min"] == pytest.approx(100.0)
    assert lane["max"] == pytest.approx(115.7625)


def test_a_value_that_never_moves_gets_no_lane(api):
    """A flat line is a constant, not a trace -- the inspector already
    shows it."""
    _seed_ohm(api)
    result = api.preview(2)
    assert {lane["key"] for lane in api.lanes(result["steps"])} == {"voltage"}


def test_lanes_are_capped_and_ordered_by_how_far_the_value_moved(api):
    species = api.create_mote({"name": "S", "freedoms": "small, big"})
    small = api.create_mote({"name": "Small", "equation": "small == prev(small) + 1"})
    big = api.create_mote({"name": "Big", "equation": "big == prev(big) + 100"})
    api.create_relationship(small["id"], species["id"], "applies_to")
    api.create_relationship(big["id"], species["id"], "applies_to")
    mote = api.create_mote({"name": "m", "small": 0.0, "big": 0.0})
    _set_species(api, mote["id"], species["id"])

    result = api.preview(3)
    lanes = api.lanes(result["steps"])
    assert [lane["key"] for lane in lanes] == ["big", "small"]
    assert len(api.lanes(result["steps"], limit=1)) == 1
