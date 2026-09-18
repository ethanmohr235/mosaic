"""Sweeping: the same garden solved once per point of a range.

The assertions here are real numbers rather than smoke, because the
gardens are built small enough to have an exact answer. `divider` is a
two-resistor series circuit whose current is V/300 for any V, so a sweep
of the source voltage has a closed form the test can check point by
point -- which is the only way to tell "it solved 41 times" apart from
"it solved once and drew a line".

What a sweep must *not* do gets as much room as what it must: no
write-back, no run-log row, no scenario mote left behind.
"""
import pytest

from core import sweep as sweep_repo
from core.motes import create_mote, get_mote
from core.relationships import create_relationship
from core.rules import FREEDOMS_KEY, APPLIES_TO
from core.scenarios import OVERRIDES, VARIES
from core.simulation import SimulationError, run_simulation
from core.species import set_species
from core.sweep import Axis, Case, run_correlation, run_surface, run_sweep


def add_rule(conn, target_id, name, equation):
    rule_id = create_mote(conn, {"name": name, "equation": equation})
    create_relationship(conn, rule_id, target_id, APPLIES_TO)
    return rule_id


@pytest.fixture
def divider(conn):
    """A source across two resistances in series: current == voltage /
    (r_a + r_b), with r_a + r_b = 300. Every answer below is that."""
    species = create_mote(conn, {"name": "Divider", FREEDOMS_KEY: "current"})
    add_rule(conn, species, "series", "current * (r_a + r_b) == voltage")
    circuit = create_mote(conn, {"name": "C1", "voltage": 10, "r_a": 100, "r_b": 200})
    set_species(conn, circuit, species)
    return {"species": species, "circuit": circuit}


def axis_of(divider, lo=0.0, hi=20.0, steps=20):
    return Axis(mote_id=divider["circuit"], key="voltage", lo=lo, hi=hi, steps=steps)


### the curve

def test_every_point_is_its_own_solve(conn, divider):
    result = run_sweep(
        conn, axis_of(divider), [Case("base")], [(divider["circuit"], "current")]
    )

    assert len(result.points) == 21
    assert result.solves == 21
    for point in result.points:
        value = point.values[("base", divider["circuit"], "current")]
        assert value == pytest.approx(point.x / 300)


def test_an_axis_includes_both_ends_of_its_range(divider):
    axis = axis_of(divider, lo=100, hi=200, steps=4)
    assert axis.values() == [100, 125, 150, 175, 200]


def test_a_sweep_reads_a_given_as_readily_as_a_freedom(conn, divider):
    """r_a is nobody's freedom -- it is a constant of the model. Reading
    it back is still the right answer (a flat line), and a caller should
    not have to know which kind of property it asked for."""
    result = run_sweep(
        conn, axis_of(divider), [Case("base")], [(divider["circuit"], "r_a")]
    )
    assert all(
        p.values[("base", divider["circuit"], "r_a")] == 100 for p in result.points
    )


### what it leaves behind

def test_a_sweep_writes_nothing_to_the_garden(conn, divider):
    before = dict(get_mote(conn, divider["circuit"]).properties)

    run_sweep(conn, axis_of(divider), [Case("base")], [(divider["circuit"], "current")])

    assert get_mote(conn, divider["circuit"]).properties == before
    assert "current" not in before


def test_a_sweep_logs_no_run(conn, divider):
    run_sweep(conn, axis_of(divider), [Case("base")], [(divider["circuit"], "current")])

    rows = conn.execute("SELECT COUNT(*) FROM simulation_runs").fetchone()[0]
    assert rows == 0


def test_a_sweep_does_not_disturb_an_ordinary_run(conn, divider):
    """The economy of reading the garden once is only safe if nothing a
    sweep does leaks into the next real solve."""
    run_sweep(conn, axis_of(divider), [Case("base")], [(divider["circuit"], "current")])

    run_simulation(conn)
    assert get_mote(conn, divider["circuit"]).properties["current"] == pytest.approx(1 / 30)


### cases

def test_cases_are_solved_at_every_point(conn, divider):
    cases = [Case("base"), Case("stiff", overrides={divider["circuit"]: {"r_b": 700}})]

    result = run_sweep(conn, axis_of(divider), cases, [(divider["circuit"], "current")])

    assert result.solves == 42
    assert result.case_count == 2
    last = result.points[-1]
    assert last.values[("base", divider["circuit"], "current")] == pytest.approx(20 / 300)
    assert last.values[("stiff", divider["circuit"], "current")] == pytest.approx(20 / 800)


def test_several_outputs_share_one_solve_per_case(conn, divider):
    """The economy the whole cost model rests on: two curves reading two
    properties of one case cost one solve per point, not two."""
    outputs = [(divider["circuit"], "current"), (divider["circuit"], "r_b")]

    result = run_sweep(conn, axis_of(divider), [Case("base")], outputs)

    assert result.solves == 21
    assert len(result.points[0].values) == 2


def test_an_override_is_never_written_down(conn, divider):
    run_sweep(
        conn, axis_of(divider),
        [Case("stiff", overrides={divider["circuit"]: {"r_b": 700}})],
        [(divider["circuit"], "current")],
    )
    assert get_mote(conn, divider["circuit"]).properties["r_b"] == 200


### scenarios as baselines

def test_a_scenario_case_sweeps_the_perturbed_garden(conn, divider):
    override = create_mote(conn, {"name": "cold r_b", "r_b": 300})
    create_relationship(conn, override, divider["circuit"], OVERRIDES)
    scenario = create_mote(conn, {"name": "Cold"})
    create_relationship(conn, scenario, override, VARIES)

    result = run_sweep(
        conn, axis_of(divider),
        [Case("base"), Case("cold", scenario_id=scenario)],
        [(divider["circuit"], "current")],
    )

    last = result.points[-1]
    assert last.values[("base", divider["circuit"], "current")] == pytest.approx(20 / 300)
    assert last.values[("cold", divider["circuit"], "current")] == pytest.approx(20 / 400)


def test_an_unknown_scenario_is_refused_rather_than_ignored(conn, divider):
    with pytest.raises(SimulationError, match="no scenario"):
        run_sweep(
            conn, axis_of(divider), [Case("ghost", scenario_id="nope")],
            [(divider["circuit"], "current")],
        )


### failure is a point, not an end

def test_a_failing_point_does_not_stop_the_sweep(conn):
    """Sweeping r_a to where r_a + r_b is zero divides by zero at exactly
    one point. The other points are still answers."""
    species = create_mote(conn, {"name": "Divider", FREEDOMS_KEY: "current"})
    add_rule(conn, species, "series", "current == voltage / (r_a + r_b)")
    circuit = create_mote(conn, {"name": "C1", "voltage": 10, "r_a": 1, "r_b": -2})
    set_species(conn, circuit, species)

    result = run_sweep(
        conn, Axis(mote_id=circuit, key="r_a", lo=0.0, hi=4.0, steps=4),
        [Case("base")], [(circuit, "current")],
    )

    assert len(result.points) == 5
    assert result.failures == 1
    failed = [p for p in result.points if p.errors][0]
    assert failed.x == pytest.approx(2.0)
    assert failed.values == {}
    assert len(result.points[-1].values) == 1


### pinning a freedom

def test_sweeping_a_freedom_reports_it_as_pinned(conn, divider):
    result = run_sweep(
        conn, Axis(mote_id=divider["circuit"], key="current", lo=1.0, hi=2.0, steps=2),
        [Case("base")], [(divider["circuit"], "voltage")],
    )
    assert result.pinned is True


def test_sweeping_a_given_is_not_pinning(conn, divider):
    result = run_sweep(
        conn, axis_of(divider), [Case("base")], [(divider["circuit"], "current")]
    )
    assert result.pinned is False


### progressive passes and cancellation

def test_indices_restrict_the_sweep_to_a_coarse_pass(conn, divider):
    result = run_sweep(
        conn, axis_of(divider), [Case("base")], [(divider["circuit"], "current")],
        indices=[0, 10, 20],
    )

    assert [p.index for p in result.points] == [0, 10, 20]
    assert result.solves == 3


def test_a_cancelled_sweep_returns_what_it_computed(conn, divider):
    seen = {"n": 0}

    def should_cancel():
        seen["n"] += 1
        return seen["n"] > 3

    result = run_sweep(
        conn, axis_of(divider), [Case("base")], [(divider["circuit"], "current")],
        should_cancel=should_cancel,
    )

    assert result.cancelled is True
    assert len(result.points) == 3


### surface

def test_a_surface_solves_the_grid(conn, divider):
    result = run_surface(
        conn,
        Axis(mote_id=divider["circuit"], key="voltage", lo=0, hi=10, steps=2),
        Axis(mote_id=divider["circuit"], key="r_b", lo=100, hi=300, steps=1),
        (divider["circuit"], "current"),
    )

    assert result.solves == 6
    corner = [c for c in result.cells if c.x == 10 and c.y == 300][0]
    assert corner.value == pytest.approx(10 / 400)
    assert result.span[1] == pytest.approx(10 / 200)


def test_a_surface_stride_takes_a_coarse_grid(conn, divider):
    result = run_surface(
        conn,
        Axis(mote_id=divider["circuit"], key="voltage", lo=0, hi=10, steps=4),
        Axis(mote_id=divider["circuit"], key="r_b", lo=100, hi=300, steps=4),
        (divider["circuit"], "current"),
        stride=2,
    )

    assert result.solves == 9
    assert {c.col for c in result.cells} == {0, 2, 4}


### correlation

def test_a_correlation_is_one_solve_and_one_dot_per_mote(conn, divider):
    second = create_mote(conn, {"name": "C2", "voltage": 30, "r_a": 100, "r_b": 200})
    set_species(conn, second, divider["species"])

    dots = run_correlation(conn, "voltage", "current")

    assert [d.name for d in dots] == ["C1", "C2"]
    assert dots[1].x == 30
    assert dots[1].y == pytest.approx(30 / 300)


def test_a_correlation_skips_motes_missing_either_property(conn, divider):
    create_mote(conn, {"name": "A note"})

    dots = run_correlation(conn, "voltage", "current")

    assert [d.name for d in dots] == ["C1"]


def test_a_correlation_writes_nothing(conn, divider):
    run_correlation(conn, "voltage", "current")

    assert "current" not in get_mote(conn, divider["circuit"]).properties
    assert conn.execute("SELECT COUNT(*) FROM simulation_runs").fetchone()[0] == 0


### the axis itself

def test_an_axis_needs_a_range(divider):
    with pytest.raises(ValueError, match="range"):
        Axis(mote_id=divider["circuit"], key="voltage", lo=5, hi=5, steps=4)


def test_an_axis_needs_at_least_one_step(divider):
    with pytest.raises(ValueError, match="step"):
        Axis(mote_id=divider["circuit"], key="voltage", lo=0, hi=5, steps=0)


### constraints, as the honest form of a limit line

def test_a_sweep_reports_where_the_answer_breaks_a_constraint(conn, divider):
    """A constraint is checked at every point anyway, so the range where
    the model breaks its own rule costs nothing extra to know. Here the
    current may not exceed 0.05 A, which 20 V across 300 ohm does."""
    add_rule(conn, divider["species"], "rating", "current <= 0.05")

    result = run_sweep(
        conn, axis_of(divider), [Case("base")], [(divider["circuit"], "current")]
    )

    broken = [p for p in result.points if p.violations]
    assert broken, "the high end of the range should break the rating"
    assert min(p.x for p in broken) == pytest.approx(16.0)
    assert all(p.x < 16 for p in result.points if not p.violations)
    assert len(result.broken_constraints) == 1
    assert "rating" in result.broken_constraints[0]


def test_a_satisfied_constraint_shades_nothing(conn, divider):
    add_rule(conn, divider["species"], "rating", "current <= 5")

    result = run_sweep(
        conn, axis_of(divider), [Case("base")], [(divider["circuit"], "current")]
    )

    assert result.broken_constraints == []


### sweeping off a snapshot, so a window need not hold its connection

def test_a_sweep_can_run_from_a_garden_read_in_advance(conn, divider):
    ground = sweep_repo.read_garden(conn)

    result = run_sweep(
        ground, axis_of(divider), [Case("base")], [(divider["circuit"], "current")]
    )

    assert result.points[-1].values[("base", divider["circuit"], "current")] == pytest.approx(20 / 300)


def test_a_snapshot_sweep_sees_the_garden_as_it_was_read(conn, divider):
    """The point of reading up front: the sweep is answering about the
    garden it was handed, so an edit landing mid-sweep cannot half-apply."""
    ground = sweep_repo.read_garden(conn)
    from core.motes import update_mote
    update_mote(conn, divider["circuit"], {"r_b": 700})

    result = run_sweep(
        ground, axis_of(divider), [Case("base")], [(divider["circuit"], "current")]
    )

    assert result.points[-1].values[("base", divider["circuit"], "current")] == pytest.approx(20 / 300)


def test_a_snapshot_must_carry_the_scenarios_the_sweep_uses(conn, divider):
    ground = sweep_repo.read_garden(conn)

    with pytest.raises(SimulationError, match="was not read with this garden"):
        run_sweep(
            ground, axis_of(divider), [Case("cold", scenario_id="whatever")],
            [(divider["circuit"], "current")],
        )
