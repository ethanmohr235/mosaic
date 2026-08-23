import pytest

from core.motes import create_mote, get_mote, update_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO
from core.simulation import (
    SimulationError,
    get_run,
    list_runs,
    mote_history,
    run_changes,
    run_simulation,
    run_ticks,
    run_time_series,
)
from core.species import set_species

OHMS_LAW = "targets('a').voltage - targets('b').voltage == current * resistance"
KCL = "sum(sources('b').current) - sum(sources('a').current) == 0"
SOURCE_LAW = "targets('a').voltage - targets('b').voltage == voltage"


def add_rule(conn, species_id, name, equation):
    rule_id = create_mote(conn, {"name": name, "equation": equation})
    create_relationship(conn, rule_id, species_id, APPLIES_TO)
    return rule_id


def wire(conn, element, node_a, node_b):
    create_relationship(conn, element, node_a, "a")
    create_relationship(conn, element, node_b, "b")


@pytest.fixture
def divider(conn):
    """A 10 V source driving 100 ohm and 200 ohm in series.

    Terminal convention: current enters 'a' and leaves 'b'. By hand:
    total resistance 300, so I = 1/30 A, V(N1) = 10, V(N2) = 10 - 100/30
    = 20/3, V(N0) = 0.
    """
    node = create_mote(conn, {"name": "Node", "freedoms": "voltage"})
    resistor = create_mote(conn, {"name": "Resistor", "freedoms": "current"})
    source = create_mote(conn, {"name": "VoltageSource", "freedoms": "current"})
    ground = create_mote(conn, {"name": "Ground"})

    add_rule(conn, resistor, "Ohm's law", OHMS_LAW)
    add_rule(conn, node, "KCL", KCL)
    add_rule(conn, source, "Source", SOURCE_LAW)
    add_rule(conn, ground, "Ground", "voltage == 0")

    nodes = {}
    for name in ("N0", "N1", "N2"):
        nodes[name] = create_mote(conn, {"name": name})
        set_species(conn, nodes[name], node)
    set_species(conn, nodes["N0"], ground)

    v1 = create_mote(conn, {"name": "V1", "voltage": 10})
    set_species(conn, v1, source)
    r1 = create_mote(conn, {"name": "R1", "resistance": 100})
    set_species(conn, r1, resistor)
    r2 = create_mote(conn, {"name": "R2", "resistance": 200})
    set_species(conn, r2, resistor)

    wire(conn, v1, nodes["N1"], nodes["N0"])
    wire(conn, r1, nodes["N1"], nodes["N2"])
    wire(conn, r2, nodes["N2"], nodes["N0"])

    return {**nodes, "V1": v1, "R1": r1, "R2": r2}


def prop(conn, mote_id, key):
    return get_mote(conn, mote_id).properties[key]


# ---- the worked example ----

def test_voltage_divider_solves_to_hand_computed_values(conn, divider):
    run_simulation(conn)

    assert prop(conn, divider["N0"], "voltage") == pytest.approx(0.0)
    assert prop(conn, divider["N1"], "voltage") == pytest.approx(10.0)
    assert prop(conn, divider["N2"], "voltage") == pytest.approx(20 / 3)
    assert prop(conn, divider["R1"], "current") == pytest.approx(1 / 30)
    assert prop(conn, divider["R2"], "current") == pytest.approx(1 / 30)


def test_source_current_is_negative_by_the_terminal_convention(conn, divider):
    # Current enters 'a' and leaves 'b', but conventional current leaves a
    # source's positive terminal -- so the source's own current comes out
    # negative. Asserted so the convention stays deliberate.
    run_simulation(conn)
    assert prop(conn, divider["V1"], "current") == pytest.approx(-1 / 30)


def test_rerunning_gives_identical_values(conn, divider):
    run_simulation(conn)
    first = {name: get_mote(conn, mid).properties for name, mid in divider.items()}
    run_simulation(conn)
    second = {name: get_mote(conn, mid).properties for name, mid in divider.items()}
    assert first == second


def test_editing_an_input_changes_the_solution(conn, divider):
    from core.motes import update_mote

    run_simulation(conn)
    update_mote(conn, divider["R2"], {"resistance": 900})
    run_simulation(conn)
    # 10 V across 100 + 900 ohms -> 0.01 A, so N2 sits at 10 - 1 = 9 V.
    assert prop(conn, divider["R1"], "current") == pytest.approx(0.01)
    assert prop(conn, divider["N2"], "voltage") == pytest.approx(9.0)


# ---- substitution / nonlinear-in-knowns ----

def test_chained_nonlinear_formulas_resolve_by_substitution(conn):
    """Each stage's pressure feeds the next stage's temperature through a
    fractional power -- nonlinear, but only in values already known by the
    time it is evaluated."""
    stage = create_mote(conn, {"name": "Stage", "freedoms": "pressure, temperature"})
    add_rule(conn, stage, "Pressure ratio", "pressure == sources('feeds').pressure * ratio")
    add_rule(
        conn, stage, "Isentropic",
        "temperature == sources('feeds').temperature "
        "* (pressure / sources('feeds').pressure) ** 0.286",
    )

    inlet = create_mote(conn, {"name": "inlet", "pressure": 100.0, "temperature": 300.0})
    s1 = create_mote(conn, {"name": "s1", "ratio": 4.0})
    set_species(conn, s1, stage)
    create_relationship(conn, inlet, s1, "feeds")

    run_simulation(conn)
    assert prop(conn, s1, "pressure") == pytest.approx(400.0)
    assert prop(conn, s1, "temperature") == pytest.approx(300.0 * 4.0 ** 0.286)


def test_assignment_style_rules_need_no_matrix(conn):
    species = create_mote(conn, {"name": "Box", "freedoms": "area"})
    add_rule(conn, species, "Area", "area == width * height")
    box = create_mote(conn, {"name": "b", "width": 3, "height": 4})
    set_species(conn, box, species)

    run_simulation(conn)
    assert prop(conn, box, "area") == pytest.approx(12.0)


# ---- failure modes ----

def test_underdetermined_system_is_reported(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "x, y"})
    add_rule(conn, species, "one equation", "x + y == 10")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError, match="underdetermined"):
        run_simulation(conn)


def test_contradictory_rules_are_reported(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "x"})
    add_rule(conn, species, "one", "x == 1")
    add_rule(conn, species, "two", "x == 2")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError, match="contradictory"):
        run_simulation(conn)


def test_nonlinear_in_unknowns_is_reported(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "x"})
    add_rule(conn, species, "square", "x * x == 4")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError, match="could not be resolved"):
        run_simulation(conn)


def test_formula_error_names_the_rule_and_mote(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "x"})
    add_rule(conn, species, "typo", "x == missing_property")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError, match="typo"):
        run_simulation(conn)


def test_empty_garden_runs_without_error(conn):
    result = run_simulation(conn)
    assert result.values == {}
    assert result.equation_count == 0


def test_declared_unknown_with_no_equation_is_reported_not_fatal(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "x, orphan"})
    add_rule(conn, species, "r", "x == 3")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    result = run_simulation(conn)
    assert prop(conn, mote, "x") == pytest.approx(3.0)
    assert result.unsolved == [(mote, "orphan")]


# ---- solver selection ----

def test_nonlinear_rule_solves_with_the_newton_backend(conn):
    species = create_mote(conn, {"name": "Square", "freedoms": "side"})
    add_rule(conn, species, "area", "area == side * side")
    mote = create_mote(conn, {"name": "m", "area": 16.0, "side": 3.0})
    set_species(conn, mote, species)

    result = run_simulation(conn, solver="newton")
    assert prop(conn, mote, "side") == pytest.approx(4.0)
    assert result.equation_count == 1


def test_orphan_declared_unknown_is_still_reported_under_newton(conn):
    # Regression: a declared unknown no equation touches must stay a soft
    # "unsolved" report under every solver, not get swept into the
    # nonlinear solve as a phantom extra unknown.
    species = create_mote(conn, {"name": "S", "freedoms": "side, orphan"})
    add_rule(conn, species, "area", "area == side * side")
    mote = create_mote(conn, {"name": "m", "area": 16.0, "side": 3.0})
    set_species(conn, mote, species)

    result = run_simulation(conn, solver="newton")
    assert prop(conn, mote, "side") == pytest.approx(4.0)
    assert result.unsolved == [(mote, "orphan")]


def test_unknown_solver_name_is_reported(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "x"})
    add_rule(conn, species, "r", "x == 3")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError, match="no solver registered"):
        run_simulation(conn, solver="bogus")


def test_newton_solver_configuration_is_threaded_through(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "x"})
    add_rule(conn, species, "no_root", "x * x == -4")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError, match="converge|stalled"):
        run_simulation(conn, solver="newton", solver_configuration={"max_iterations": 5})


def test_time_series_accepts_a_solver_override(conn):
    species = create_mote(conn, {"name": "Square", "freedoms": "side"})
    add_rule(conn, species, "area", "area == side * side")
    mote = create_mote(conn, {"name": "m", "area": 16.0, "side": 3.0})
    set_species(conn, mote, species)

    result = run_time_series(conn, step_count=1, commit=False, solver="newton")
    assert result.failed_at is None
    assert result.steps[0].values[(mote, "side")] == pytest.approx(4.0)

    # The default backend still can't solve it -- confirms the override,
    # not some other change, is what made the newton run succeed.
    default_result = run_time_series(conn, step_count=1, commit=False)
    assert default_result.failed_at == 0


# ---- audit log ----

def test_successful_run_is_logged_with_its_changes(conn, divider):
    result = run_simulation(conn)

    cursor = conn.cursor()
    cursor.execute("SELECT run_number, status FROM simulation_runs")
    runs = [tuple(row) for row in cursor.fetchall()]
    assert runs == [(1, "ok")]

    cursor.execute("SELECT COUNT(*) AS n FROM simulation_changes WHERE run_id=?", (result.run_id,))
    assert cursor.fetchone()["n"] == len(result.changes)


def test_failed_run_is_logged_too(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "x, y"})
    add_rule(conn, species, "one equation", "x + y == 10")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError):
        run_simulation(conn)

    cursor = conn.cursor()
    cursor.execute("SELECT status, detail FROM simulation_runs")
    row = cursor.fetchone()
    assert row["status"] == "error"
    assert "underdetermined" in row["detail"]


def test_run_number_advances_across_runs(conn, divider):
    assert run_simulation(conn).run_number == 1
    assert run_simulation(conn).run_number == 2
    assert run_simulation(conn).run_number == 3


def test_change_log_records_old_and_new_values(conn):
    from core.motes import update_mote

    species = create_mote(conn, {"name": "S", "freedoms": "x"})
    add_rule(conn, species, "r", "x == width * 2")
    mote = create_mote(conn, {"name": "m", "width": 5})
    set_species(conn, mote, species)

    run_simulation(conn)
    update_mote(conn, mote, {"width": 10})
    result = run_simulation(conn)

    change = next(c for c in result.changes if c.key == "x")
    assert change.old_value == pytest.approx(10.0)
    assert change.new_value == pytest.approx(20.0)


# ---- reading the audit log back: list_runs / get_run / run_changes ----

def test_list_runs_orders_most_recent_first(conn, divider):
    first = run_simulation(conn).run_id
    second = run_simulation(conn).run_id

    runs = list_runs(conn)
    assert [r.run_id for r in runs] == [second, first]


def test_list_runs_respects_limit(conn, divider):
    run_simulation(conn)
    run_simulation(conn)
    run_simulation(conn)

    assert len(list_runs(conn, limit=2)) == 2


def test_list_runs_reports_change_count_and_status(conn, divider):
    result = run_simulation(conn)
    [run] = list_runs(conn)
    assert run.run_id == result.run_id
    assert run.status == "ok"
    assert run.change_count == len(result.changes)


def test_list_runs_includes_failed_runs_with_zero_changes(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "x, y"})
    add_rule(conn, species, "one equation", "x + y == 10")
    set_species(conn, create_mote(conn, {"name": "m"}), species)

    with pytest.raises(SimulationError):
        run_simulation(conn)

    [run] = list_runs(conn)
    assert run.status == "error"
    assert run.change_count == 0
    assert "underdetermined" in run.detail


def test_get_run_returns_none_for_missing_id(conn):
    assert get_run(conn, "nonexistent") is None


def test_get_run_matches_the_corresponding_list_runs_entry(conn, divider):
    result = run_simulation(conn)
    run = get_run(conn, result.run_id)
    assert run is not None
    assert run.run_number == result.run_number
    assert run.change_count == len(result.changes)


def test_run_changes_round_trips_old_and_new_values(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "x"})
    add_rule(conn, species, "r", "x == width * 2")
    mote = create_mote(conn, {"name": "m", "width": 5})
    set_species(conn, mote, species)

    run_simulation(conn)
    update_mote(conn, mote, {"width": 10})
    result = run_simulation(conn)

    [change] = run_changes(conn, result.run_id)
    assert change.mote_id == mote
    assert change.key == "x"
    assert change.old_value == pytest.approx(10.0)
    assert change.new_value == pytest.approx(20.0)


def test_run_changes_empty_for_a_failed_run(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "x, y"})
    add_rule(conn, species, "one equation", "x + y == 10")
    set_species(conn, create_mote(conn, {"name": "m"}), species)

    with pytest.raises(SimulationError):
        run_simulation(conn)

    [run] = list_runs(conn)
    assert run_changes(conn, run.run_id) == []


# ---- time-based simulation: run_time_series ----

def _rows(conn, table):
    cursor = conn.cursor()
    cursor.execute(f"SELECT * FROM {table}")
    return cursor.fetchall()


@pytest.fixture
def compounding(conn):
    """A single mote whose balance compounds by 5% each step -- the
    simplest possible recurrence, via prev()."""
    species = create_mote(conn, {"name": "Account", "freedoms": "balance"})
    add_rule(conn, species, "Growth", "balance == prev(balance) * 1.05")
    mote = create_mote(conn, {"name": "acct1", "balance": 100.0})
    set_species(conn, mote, species)
    return mote


def test_time_series_recurrence_produces_the_expected_sequence(conn, compounding):
    result = run_time_series(conn, step_count=4, commit=False)
    assert result.failed_at is None
    balances = [s.values[(compounding, "balance")] for s in result.steps]
    assert balances == pytest.approx([105.0, 110.25, 115.7625, 121.55062500000001])


def test_time_series_preview_never_writes(conn, compounding):
    before_props = get_mote(conn, compounding).properties
    before_runs = _rows(conn, "simulation_runs")
    before_changes = _rows(conn, "simulation_changes")

    run_time_series(conn, step_count=10, commit=False)

    assert get_mote(conn, compounding).properties == before_props
    assert _rows(conn, "simulation_runs") == before_runs
    assert _rows(conn, "simulation_changes") == before_changes


def test_time_series_commit_writes_the_final_value_and_logs_each_tick(conn, compounding):
    result = run_time_series(conn, step_count=3, commit=True)
    # step 2 (the 3rd, 0-indexed) is the final step of a 3-step run.
    assert prop(conn, compounding, "balance") == pytest.approx(result.steps[2].values[(compounding, "balance")])
    assert prop(conn, compounding, "balance") == pytest.approx(115.7625)

    runs = _rows(conn, "simulation_runs")
    assert len(runs) == 1
    assert runs[0]["status"] == "ok"
    assert runs[0]["committed"] == 1

    # One row per tick (not just the net), so the run can be scrubbed
    # through later via run_ticks() -- unlike the graph write above,
    # which only ever reflects the final cumulative value.
    changes = _rows(conn, "simulation_changes")
    assert len(changes) == 3
    assert sorted(c["step"] for c in changes) == [0, 1, 2]


def test_time_series_commit_continues_from_the_previously_committed_value(conn, compounding):
    run_time_series(conn, step_count=3, commit=True)
    run_time_series(conn, step_count=1, commit=True)
    # 115.7625 * 1.05, not 100 * 1.05 -- the second commit must build on
    # the first's result, not restart from the mote's original value.
    assert prop(conn, compounding, "balance") == pytest.approx(121.55062500000001)
    assert len(_rows(conn, "simulation_runs")) == 2


def test_time_series_committing_an_earlier_step_than_the_last_preview_works(conn, compounding):
    preview = run_time_series(conn, step_count=10, commit=False)
    run_time_series(conn, step_count=3, commit=True)
    # Committing step count 3 (steps 0,1,2) should land exactly on what
    # scrubbing to step 2 in the larger preview showed -- determinism.
    assert prop(conn, compounding, "balance") == pytest.approx(
        preview.steps[2].values[(compounding, "balance")]
    )


def test_time_series_stops_at_a_failure_but_keeps_earlier_steps(conn):
    # Two rules on the same species: "x stays whatever it was" and "x
    # must equal the step index". Both agree at step 0 (x starts at 0,
    # t is 0), so that step solves fine -- but from step 1 on they
    # contradict (prev(x) is still 0, t is 1), so the run fails there.
    species = create_mote(conn, {"name": "S", "freedoms": "x"})
    add_rule(conn, species, "stays", "x == prev(x)")
    add_rule(conn, species, "matches_t", "x == t")
    mote = create_mote(conn, {"name": "m", "x": 0.0})
    set_species(conn, mote, species)

    result = run_time_series(conn, step_count=5, commit=False)
    assert result.failed_at == 1
    assert "contradictory" in result.error
    assert len(result.steps) == 1
    assert result.steps[0].values == {(mote, "x"): 0.0}

    # Still safe to commit the one good step that was computed.
    commit_result = run_time_series(conn, step_count=1, commit=True)
    assert commit_result.run_id is not None
    assert prop(conn, mote, "x") == pytest.approx(0.0)
    assert len(_rows(conn, "simulation_runs")) == 1


def test_time_series_commit_is_a_noop_when_every_step_fails(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "a, b"})
    add_rule(conn, species, "underdetermined", "a + b == 10")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    result = run_time_series(conn, step_count=5, commit=True)
    assert result.run_id is None
    assert result.steps == []
    assert _rows(conn, "simulation_runs") == []
    assert _rows(conn, "simulation_changes") == []


def test_time_series_rejects_a_non_positive_step_count(conn, compounding):
    with pytest.raises(ValueError):
        run_time_series(conn, step_count=0, commit=False)


def test_time_series_rejects_commit_and_save_together(conn, compounding):
    with pytest.raises(ValueError):
        run_time_series(conn, step_count=1, commit=True, save=True)


# ---- saving a simulation for review without applying it ----

def test_save_logs_every_tick_without_writing_the_graph(conn, compounding):
    before = get_mote(conn, compounding).properties
    result = run_time_series(conn, step_count=3, commit=False, save=True)

    assert result.run_id is not None
    assert get_mote(conn, compounding).properties == before  # graph untouched

    runs = _rows(conn, "simulation_runs")
    assert len(runs) == 1
    assert runs[0]["committed"] == 0

    changes = _rows(conn, "simulation_changes")
    assert len(changes) == 3
    assert sorted(c["step"] for c in changes) == [0, 1, 2]


def test_save_is_a_noop_when_every_step_fails(conn):
    species = create_mote(conn, {"name": "S", "freedoms": "a, b"})
    add_rule(conn, species, "underdetermined", "a + b == 10")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    result = run_time_series(conn, step_count=5, commit=False, save=True)
    assert result.run_id is None
    assert _rows(conn, "simulation_runs") == []


def test_saved_run_is_excluded_from_mote_history(conn, compounding):
    run_time_series(conn, step_count=2, commit=False, save=True)
    assert mote_history(conn, compounding) == {}


def test_mote_history_tracks_committed_values_across_runs(conn, compounding):
    run_time_series(conn, step_count=1, commit=True)
    run_time_series(conn, step_count=1, commit=True)
    history = mote_history(conn, compounding)
    assert history["balance"] == pytest.approx([105.0, 110.25])


def test_run_changes_folds_a_multi_tick_run_to_its_net_effect(conn, compounding):
    result = run_time_series(conn, step_count=3, commit=True)
    changes = run_changes(conn, result.run_id)
    [change] = [c for c in changes if c.key == "balance"]
    assert change.old_value == pytest.approx(100.0)  # entering the run
    assert change.new_value == pytest.approx(115.7625)  # after all 3 ticks


def test_run_ticks_returns_the_per_tick_breakdown(conn, compounding):
    result = run_time_series(conn, step_count=3, commit=True)
    ticks = run_ticks(conn, result.run_id)

    assert [t.step for t in ticks] == [0, 1, 2]
    balances = [next(c.new_value for c in t.changes if c.key == "balance") for t in ticks]
    assert balances == pytest.approx([105.0, 110.25, 115.7625])


def test_run_ticks_for_a_steady_state_run_has_exactly_one_tick(conn, divider):
    result = run_simulation(conn)
    ticks = run_ticks(conn, result.run_id)
    assert [t.step for t in ticks] == [0]
    assert len(ticks[0].changes) == len(result.changes)


def test_t_is_available_in_a_time_series_rule(conn):
    species = create_mote(conn, {"name": "Ramp", "freedoms": "height"})
    add_rule(conn, species, "Linear ramp", "height == 10 - 2 * t")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    result = run_time_series(conn, step_count=3, commit=False)
    heights = [s.values[(mote, "height")] for s in result.steps]
    assert heights == pytest.approx([10.0, 8.0, 6.0])


# ---- physical property models ----

def _wire_ideal_air(conn, provider="ideal_gas"):
    """An 'Air' mote whose uses_model relationship resolves to an Ideal
    Gas property model. Returns the Air mote's id."""
    air = create_mote(conn, {"name": "Air"})
    model = create_mote(
        conn, {"name": "Ideal Air", "provider": provider, "configuration": {"gas_constant": 287.05}}
    )
    create_relationship(conn, air, model, "uses_model")
    return air


def test_property_builtin_solves_the_ideal_gas_law(conn):
    air = _wire_ideal_air(conn)

    container = create_mote(conn, {"name": "Container", "freedoms": "pressure"})
    add_rule(
        conn, container, "Ideal gas law",
        "pressure == property('gas', 'pressure', density, temperature)",
    )

    c1 = create_mote(conn, {"name": "C1", "density": 1.2, "temperature": 293.15})
    set_species(conn, c1, container)
    create_relationship(conn, c1, air, "gas")

    run_simulation(conn)

    assert prop(conn, c1, "pressure") == pytest.approx(1.2 * 287.05 * 293.15)


def test_property_builtin_reports_a_missing_model_clearly(conn):
    container = create_mote(conn, {"name": "Container", "freedoms": "pressure"})
    add_rule(
        conn, container, "Ideal gas law",
        "pressure == property('gas', 'pressure', density, temperature)",
    )
    c1 = create_mote(conn, {"name": "C1", "density": 1.2, "temperature": 293.15})
    set_species(conn, c1, container)
    # No 'gas' relationship at all -- property() has nothing to navigate to.

    with pytest.raises(SimulationError, match="gas"):
        run_simulation(conn)


def test_property_builtin_reports_an_unsupported_operation_clearly(conn):
    air = _wire_ideal_air(conn)
    container = create_mote(conn, {"name": "Container", "freedoms": "enthalpy"})
    add_rule(
        conn, container, "Bogus lookup",
        "enthalpy == property('gas', 'enthalpy', density, temperature)",
    )
    c1 = create_mote(conn, {"name": "C1", "density": 1.2, "temperature": 293.15})
    set_species(conn, c1, container)
    create_relationship(conn, c1, air, "gas")

    with pytest.raises(SimulationError, match="enthalpy"):
        run_simulation(conn)


def test_property_model_provider_is_resolved_at_most_once_per_run(conn):
    from core.ideal_gas_provider import IdealGasProvider
    from core.providers import register_provider

    instances = []

    class _CountingIdealGas(IdealGasProvider):
        def __init__(self, configuration):
            super().__init__(configuration)
            instances.append(self)

    register_provider("_counting_ideal_gas_test", _CountingIdealGas)
    air = _wire_ideal_air(conn, provider="_counting_ideal_gas_test")

    container = create_mote(conn, {"name": "Container", "freedoms": "pressure"})
    add_rule(
        conn, container, "Ideal gas law",
        "pressure == property('gas', 'pressure', density, temperature)",
    )

    # Two instances, both referencing the same Air mote -- the provider
    # should still only be constructed once for the whole run.
    for name, density, temperature in [("C1", 1.2, 293.15), ("C2", 1.0, 300.0)]:
        c = create_mote(conn, {"name": name, "density": density, "temperature": temperature})
        set_species(conn, c, container)
        create_relationship(conn, c, air, "gas")

    run_simulation(conn)
    assert len(instances) == 1
