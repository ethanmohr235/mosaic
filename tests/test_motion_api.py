"""Motion's JS bridge and its payload shaping.

The simulation half of these moved here from tests/test_api.py when the
sim surface moved from Monarch to Motion -- same assertions, renamed
calls (preview_simulation -> preview, and so on). The board, lane and
node-kind tests are new, since nothing shaped a garden this way before.
"""
import pytest

from core.gardens import create_garden
from core.simulation import SimulationError, run_simulation
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
    assert result["run_id"] is None
    assert [s["step"] for s in result["steps"]] == [0, 1, 2, 3]
    assert result["steps"][0]["changes"][0]["new_value"] == pytest.approx(105.0)


def test_preview_never_writes(api):
    mote_id = _seed_compounding_rule(api)
    before = api.inspect(mote_id)
    api.preview(10)
    assert api.inspect(mote_id) == before


def test_commit_writes_the_final_step_and_returns_a_run_id(api):
    mote_id = _seed_compounding_rule(api)
    result = api.commit(3)
    assert result["run_id"] is not None
    assert api.inspect(mote_id)["properties"]["balance"] == pytest.approx(115.7625)


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


def test_commit_honours_strict_units(api):
    _seed_mismatched_units(api)
    with pytest.raises(SimulationError, match="dimensional check failed"):
        api.commit(1, strict_units=True)


def test_compare_honours_strict_units(api):
    mote_id = _seed_mismatched_units(api)
    scenario = _scenario_over(api, mote_id, current=3)
    with pytest.raises(SimulationError, match="dimensional check failed"):
        api.compare(1, "linear", None, scenario, strict_units=True)


def test_save_honours_strict_units(api):
    _seed_mismatched_units(api)
    with pytest.raises(SimulationError, match="dimensional check failed"):
        api.save(1, strict_units=True)


# ---- run history ----

def test_list_runs_and_get_run_see_a_committed_run(api):
    mote_id = _seed_compounding_rule(api)
    result = api.commit(3)

    runs = api.list_runs()
    assert [r["run_id"] for r in runs] == [result["run_id"]]
    assert runs[0]["status"] == "ok"

    run = api.get_run(result["run_id"])
    assert run == runs[0]

    changes = api.run_changes(result["run_id"])
    assert any(c["mote_id"] == mote_id and c["key"] == "balance" for c in changes)


def test_list_runs_respects_limit(api):
    _seed_compounding_rule(api)
    api.commit(1)
    api.commit(1)
    assert len(api.list_runs(limit=1)) == 1


def test_get_run_returns_none_for_an_unknown_id(api):
    assert api.get_run("nonexistent") is None


def test_list_runs_includes_a_failed_run_with_no_changes(api):
    species = api.create_mote({"name": "S", "freedoms": "a, b"})
    rule = api.create_mote({"name": "underdetermined", "equation": "a + b == 10"})
    api.create_relationship(rule["id"], species["id"], "applies_to")
    mote = api.create_mote({"name": "m"})
    _set_species(api, mote["id"], species["id"])

    # run_simulation (unlike commit/run_time_series, which never logs a
    # run that fails on its very first step) always logs failures --
    # exercised directly here since Api has no bare run_simulation wrapper.
    with pytest.raises(SimulationError):
        run_simulation(api._conn)

    [run] = api.list_runs()
    assert run["status"] == "error"
    assert "underdetermined" in run["detail"]
    assert api.run_changes(run["run_id"]) == []


def test_save_logs_without_writing_the_graph(api):
    mote_id = _seed_compounding_rule(api)
    before = api.inspect(mote_id)

    result = api.save(3)

    assert result["run_id"] is not None
    assert api.inspect(mote_id) == before          # graph untouched

    run = api.get_run(result["run_id"])
    assert run["committed"] is False

    ticks = api.run_ticks(result["run_id"])
    assert [s["step"] for s in ticks["steps"]] == [0, 1, 2]


def test_committed_run_has_committed_true(api):
    _seed_compounding_rule(api)
    result = api.commit(1)
    assert api.get_run(result["run_id"])["committed"] is True


def test_run_ticks_is_shaped_exactly_like_a_preview(api):
    """The dock cannot tell a replayed run from a fresh preview, which is
    the point -- one rendering path for both."""
    mote_id = _seed_compounding_rule(api)
    result = api.commit(3)

    ticks = api.run_ticks(result["run_id"])
    assert set(ticks) >= {"steps", "axis", "events"}
    assert ticks["axis"]["unit"] == "tick"
    balances = [
        next(c["new_value"] for c in s["changes"]
             if c["mote_id"] == mote_id and c["key"] == "balance")
        for s in ticks["steps"]
    ]
    assert balances == pytest.approx([105.0, 110.25, 115.7625])


def test_mote_history_tracks_a_property_across_committed_runs(api):
    mote_id = _seed_compounding_rule(api)
    api.commit(1)
    api.commit(1)
    assert api.mote_history(mote_id)["balance"] == pytest.approx([105.0, 110.25])


def test_mote_history_excludes_saved_runs(api):
    mote_id = _seed_compounding_rule(api)
    api.save(2)
    assert api.mote_history(mote_id) == {}


# ---- scenarios ----

def test_preview_under_a_scenario_writes_nothing(api):
    garden = _seed_ohm(api)
    scenario_id = _scenario_over(api, garden["mote"], current=10)

    plain = api.preview(1)
    assert plain["steps"][0]["changes"][0]["new_value"] == 6

    varied = api.preview(1, scenario=scenario_id)
    assert varied["steps"][0]["changes"][0]["new_value"] == 30
    assert "voltage" not in api.inspect(garden["mote"])["properties"]


def test_save_can_keep_a_scenario_run(api):
    garden = _seed_ohm(api)
    scenario_id = _scenario_over(api, garden["mote"], current=10)

    result = api.save(1, scenario=scenario_id)
    assert result["run_id"] is not None
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
    api.commit(1)

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
    assert board["runs"] == []


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


def test_lanes_from_a_logged_run_read_the_same_as_a_preview(api):
    """build_lanes takes the payload form of a step precisely so a replay
    and a preview share one path."""
    _seed_compounding_rule(api)
    result = api.save(2)
    ticks = api.run_ticks(result["run_id"])
    [lane] = api.lanes(ticks["steps"])
    assert lane["points"] == pytest.approx([100.0, 105.0, 110.25])
