"""Moxie's bridge: choosing sweep arguments, and refusing to write.

The line worth testing hardest here is the one the module docstring
draws -- Moxie computes nothing and writes no property value, ever. A
graphing tool that could quietly change the model it graphs would be a
trap, so "the garden is untouched afterwards" gets as many tests as the
curves do.
"""
import pytest

from core.motes import create_mote, get_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO, FREEDOMS_KEY
from core.scenarios import OVERRIDES, VARIES
from core.species import set_species
from core.sweep import PLOT_KEY
from core.units import UNITS_KEY
from core.gardens import create_garden
from moxie.api import PLOT_SPECIES_NAME, Api


@pytest.fixture
def api():
    create_garden("test")
    instance = Api("test")
    yield instance
    instance.shutdown()


@pytest.fixture
def divider(api):
    """The same series circuit core/sweep.py's tests use: current is
    voltage / 300, so every number below has a closed form."""
    conn = api._conn
    species = create_mote(conn, {"name": "Divider", FREEDOMS_KEY: "current"})
    rule = create_mote(conn, {"name": "series", "equation": "current * (r_a + r_b) == voltage"})
    create_relationship(conn, rule, species, APPLIES_TO)
    circuit = create_mote(conn, {
        "name": "C1", "voltage": 10, "r_a": 100, "r_b": 200,
        UNITS_KEY: {"voltage": "V", "r_a": "ohm", "r_b": "ohm"},
    })
    set_species(conn, circuit, species)
    return {"species": species, "circuit": circuit}


def sweep_spec(divider, steps=20, **extra):
    circuit = divider["circuit"]
    spec = {
        "x": {"mote": circuit, "key": "voltage", "lo": 0, "hi": 20, "steps": steps},
        "series": [{"id": "s1", "mote": circuit, "key": "current", "case": "base"}],
    }
    spec.update(extra)
    return spec


### the board

def test_the_board_lists_only_motes_holding_numbers(api, divider):
    create_mote(api._conn, {"name": "A note", "body": "nothing numeric here"})

    names = [m["name"] for m in api.board()["motes"]]

    assert "C1" in names
    assert "A note" not in names


def test_the_board_marks_which_properties_the_solver_determines(api, divider):
    row = [m for m in api.board()["motes"] if m["name"] == "C1"][0]
    by_key = {p["key"]: p for p in row["properties"]}

    assert by_key["current"]["free"] is True
    assert by_key["voltage"]["free"] is False
    assert row["freeCount"] == 1


def test_a_freedom_nothing_has_solved_yet_is_still_plottable(api, divider):
    """The curve you most want in a garden that has never been run is the
    one nothing has written a value for. Offering only properties that
    already hold numbers would leave a fresh model able to graph only its
    own inputs."""
    row = [m for m in api.board()["motes"] if m["name"] == "C1"][0]
    current = [p for p in row["properties"] if p["key"] == "current"][0]

    assert "current" not in get_mote(api._conn, divider["circuit"]).properties
    assert current["value"] is None
    assert (current["suggestLo"], current["suggestHi"]) == (0.0, 1.0)


def test_the_board_carries_units_and_their_dimension(api, divider):
    row = [m for m in api.board()["motes"] if m["name"] == "C1"][0]
    by_key = {p["key"]: p for p in row["properties"]}

    assert by_key["voltage"]["unit"] == "V"
    assert by_key["r_a"]["dimension"] == by_key["r_b"]["dimension"]
    assert by_key["r_a"]["dimension"] != by_key["voltage"]["dimension"]


def test_the_board_suggests_a_range_around_each_value(api, divider):
    row = [m for m in api.board()["motes"] if m["name"] == "C1"][0]
    voltage = [p for p in row["properties"] if p["key"] == "voltage"][0]

    assert (voltage["suggestLo"], voltage["suggestHi"]) == (5.0, 20.0)


def test_a_boolean_is_not_offered_as_an_axis(api, divider):
    create_mote(api._conn, {"name": "Switch", "closed": True, "rating": 5})

    row = [m for m in api.board()["motes"] if m["name"] == "Switch"][0]

    assert [p["key"] for p in row["properties"]] == ["rating"]


### sweeping

def test_a_sweep_returns_a_curve_keyed_by_series_id(api, divider):
    result = api.sweep(sweep_spec(divider))

    assert len(result["points"]) == 21
    last = result["points"][-1]
    assert last["x"] == 20
    assert last["v"]["s1"] == pytest.approx(20 / 300)


def test_a_sweep_writes_nothing(api, divider):
    before = dict(get_mote(api._conn, divider["circuit"]).properties)

    api.sweep(sweep_spec(divider))

    assert get_mote(api._conn, divider["circuit"]).properties == before
    assert api._conn.execute("SELECT COUNT(*) FROM simulation_derivations").fetchone()[0] == 0


def test_a_knob_is_coerced_from_the_string_a_slider_sends(api, divider):
    """Every HTML range input hands back a string. Left uncoerced it
    would reach the solver as text and fail a long way from the cause."""
    spec = sweep_spec(divider, cases=[
        {"key": "base", "overrides": {divider["circuit"]: {"r_b": "700"}}},
    ])

    result = api.sweep(spec)

    assert result["points"][-1]["v"]["s1"] == pytest.approx(20 / 800)


def test_a_scenario_case_is_swept_as_its_own_curve(api, divider):
    conn = api._conn
    override = create_mote(conn, {"name": "cold r_b", "r_b": 300})
    create_relationship(conn, override, divider["circuit"], OVERRIDES)
    scenario = create_mote(conn, {"name": "Cold"})
    create_relationship(conn, scenario, override, VARIES)

    circuit = divider["circuit"]
    result = api.sweep({
        "x": {"mote": circuit, "key": "voltage", "lo": 0, "hi": 20, "steps": 4},
        "cases": [{"key": "base"}, {"key": "cold", "scenario": scenario}],
        "series": [
            {"id": "warm", "mote": circuit, "key": "current", "case": "base"},
            {"id": "chill", "mote": circuit, "key": "current", "case": "cold"},
        ],
    })

    last = result["points"][-1]
    assert last["v"]["warm"] == pytest.approx(20 / 300)
    assert last["v"]["chill"] == pytest.approx(20 / 400)


def test_the_scenario_appears_in_the_board_as_a_baseline(api, divider):
    conn = api._conn
    override = create_mote(conn, {"name": "cold r_b", "r_b": 300})
    create_relationship(conn, override, divider["circuit"], OVERRIDES)
    scenario = create_mote(conn, {"name": "Cold"})
    create_relationship(conn, scenario, override, VARIES)

    assert [s["name"] for s in api.board()["scenarios"]] == ["Cold"]


### cost, stated rather than capped

def test_the_plan_states_what_the_plot_will_cost(api, divider):
    spec = sweep_spec(divider, steps=200, cases=[
        {"key": "base"}, {"key": "stiff", "overrides": {divider["circuit"]: {"r_b": 700}}},
    ])

    plan = api.sweep_plan(spec)

    assert plan["cases"] == 2
    assert plan["points"] == 201
    assert plan["solves"] == 402


def test_several_series_sharing_a_case_cost_one_solve_each_point(api, divider):
    circuit = divider["circuit"]
    spec = sweep_spec(divider, steps=10)
    spec["series"].append({"id": "s2", "mote": circuit, "key": "r_b", "case": "base"})

    result = api.sweep(spec)

    assert api.sweep_plan(spec)["cases"] == 1
    assert result["solves"] == 11
    assert len(result["points"][0]["v"]) == 2


### progressive refinement

def test_the_coarse_pass_takes_every_eighth_point_and_the_last(api):
    assert api.coarse_indices(20) == [0, 8, 16, 20]
    assert api.coarse_indices(16) == [0, 8, 16]


def test_a_coarse_pass_returns_only_the_points_it_was_asked_for(api, divider):
    result = api.sweep(sweep_spec(divider), indices=[0, 10, 20])

    assert [p["i"] for p in result["points"]] == [0, 10, 20]
    assert result["solves"] == 3


def test_a_newer_generation_cancels_the_one_running(api, divider):
    api.sweep(sweep_spec(divider), generation=5)
    result = api.sweep(sweep_spec(divider), generation=2)

    assert result["cancelled"] is True
    assert result["points"] == []


def test_one_plot_in_the_grid_never_cancels_another(api, divider):
    """Each plot of the grid sweeps on its own channel. A single counter
    would mean the second plot's first frame killed the first plot's
    sweep, and in a busy grid nothing would ever finish."""
    api.sweep(sweep_spec(divider), generation=9, channel="plot-a")
    result = api.sweep(sweep_spec(divider), generation=1, channel="plot-b")

    assert result["cancelled"] is False
    assert len(result["points"]) == 21


### pinning, failure and constraints

def test_sweeping_a_freedom_is_reported_as_pinned(api, divider):
    circuit = divider["circuit"]
    result = api.sweep({
        "x": {"mote": circuit, "key": "current", "lo": 1, "hi": 2, "steps": 2},
        "series": [{"id": "s1", "mote": circuit, "key": "voltage", "case": "base"}],
    })

    assert result["pinned"] is True


def test_a_broken_constraint_is_reported_once_with_its_points(api, divider):
    conn = api._conn
    rule = create_mote(conn, {"name": "rating", "equation": "current <= 0.05"})
    create_relationship(conn, rule, divider["species"], APPLIES_TO)

    result = api.sweep(sweep_spec(divider))

    assert len(result["brokenConstraints"]) == 1
    broken = [p["x"] for p in result["points"] if p["violated"]]
    assert min(broken) == pytest.approx(16.0)


### surface and correlate

def test_a_surface_solves_a_grid(api, divider):
    circuit = divider["circuit"]
    result = api.surface({
        "x": {"mote": circuit, "key": "voltage", "lo": 0, "hi": 10, "steps": 2},
        "y": {"mote": circuit, "key": "r_b", "lo": 100, "hi": 300, "steps": 1},
        "output": {"mote": circuit, "key": "current"},
    })

    assert result["solves"] == 6
    assert result["hi"] == pytest.approx(10 / 200)


def test_a_surface_stride_takes_a_coarse_grid(api, divider):
    circuit = divider["circuit"]
    result = api.surface({
        "x": {"mote": circuit, "key": "voltage", "lo": 0, "hi": 10, "steps": 4},
        "y": {"mote": circuit, "key": "r_b", "lo": 100, "hi": 300, "steps": 4},
        "output": {"mote": circuit, "key": "current"},
    }, stride=2)

    assert result["solves"] == 9


def test_a_correlation_is_one_dot_per_mote(api, divider):
    second = create_mote(api._conn, {"name": "C2", "voltage": 30, "r_a": 100, "r_b": 200})
    set_species(api._conn, second, divider["species"])

    dots = api.correlate({"xKey": "voltage", "yKey": "current"})

    assert [d["name"] for d in dots] == ["C1", "C2"]
    assert dots[1]["y"] == pytest.approx(30 / 300)


### sensitivity

def test_sensitivity_ranks_the_knobs_that_matter(api, divider):
    circuit = divider["circuit"]
    rows = api.sensitivity({
        "x": {"mote": circuit, "key": "voltage", "lo": 0, "hi": 20, "steps": 20},
        "output": {"mote": circuit, "key": "current"},
        "knobs": [
            {"mote": circuit, "key": "r_b", "value": 200},
            {"mote": circuit, "key": "r_a", "value": 100},
        ],
    }, x=10.0)

    # current = V / (r_a + r_b): both push it down, r_b twice as hard.
    assert [r["key"] for r in rows] == ["r_b", "r_a"]
    assert rows[0]["value"] < 0
    assert abs(rows[0]["value"]) > abs(rows[1]["value"])


def test_sensitivity_writes_nothing(api, divider):
    circuit = divider["circuit"]
    before = dict(get_mote(api._conn, circuit).properties)

    api.sensitivity({
        "x": {"mote": circuit, "key": "voltage", "lo": 0, "hi": 20, "steps": 20},
        "output": {"mote": circuit, "key": "current"},
        "knobs": [{"mote": circuit, "key": "r_b", "value": 200}],
    }, x=10.0)

    assert get_mote(api._conn, circuit).properties == before


### saved plots

def test_saving_a_plot_makes_a_mote_of_the_species(api, divider):
    saved = api.save_plot("I vs V", {"mode": "sweep", "x": {"key": "voltage"}})

    mote = get_mote(api._conn, saved["id"])
    assert saved["created"] is True
    assert mote.properties["name"] == "I vs V"
    assert mote.properties[PLOT_KEY]["mode"] == "sweep"
    names = [s["name"] for s in api.board()["motes"]]
    assert PLOT_SPECIES_NAME not in names   # the species holds no numbers


def test_a_saved_plot_is_not_listed_as_something_to_plot(api, divider):
    api.save_plot("I vs V", {"mode": "sweep", "steps": 200})

    assert [m["name"] for m in api.board()["motes"]] == ["C1"]
    assert [p["name"] for p in api.board()["plots"]] == ["I vs V"]


def test_saving_over_a_plot_keeps_its_id(api, divider):
    first = api.save_plot("I vs V", {"mode": "sweep"})
    again = api.save_plot("I vs V, redone", {"mode": "surface"}, plot_id=first["id"])

    assert again == {"id": first["id"], "created": False}
    assert get_mote(api._conn, first["id"]).properties[PLOT_KEY]["mode"] == "surface"


def test_a_saved_plot_holds_the_specification_not_the_points(api, divider):
    """The whole reason to save one: reopening re-solves the garden as it
    stands, so an embedded plot cannot go stale the way a number can."""
    result = api.sweep(sweep_spec(divider))
    saved = api.save_plot("I vs V", sweep_spec(divider))

    stored = get_mote(api._conn, saved["id"]).properties[PLOT_KEY]
    assert "points" not in stored
    assert stored["x"]["key"] == "voltage"
    assert len(result["points"]) == 21


def test_deleting_a_plot_removes_it(api, divider):
    saved = api.save_plot("I vs V", {"mode": "sweep"})

    assert api.delete_plot(saved["id"]) == {"deleted": True}
    assert api.board()["plots"] == []


def test_saving_a_plot_can_be_undone(api, divider):
    saved = api.save_plot("I vs V", {"mode": "sweep"})
    assert api.undo()["undone"] is not None
    assert get_mote(api._conn, saved["id"]) is None


def test_deleting_a_plot_can_be_undone(api, divider):
    saved = api.save_plot("I vs V", {"mode": "sweep"})
    api.delete_plot(saved["id"])
    assert api.undo()["undone"] is not None
    assert get_mote(api._conn, saved["id"]).properties[PLOT_KEY]["mode"] == "sweep"
