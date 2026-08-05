import pytest

from core.motes import create_mote, get_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO
from core.simulation import SimulationError, run_simulation
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
    node = create_mote(conn, {"name": "Node", "unknowns": "voltage"})
    resistor = create_mote(conn, {"name": "Resistor", "unknowns": "current"})
    source = create_mote(conn, {"name": "VoltageSource", "unknowns": "current"})
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
    stage = create_mote(conn, {"name": "Stage", "unknowns": "pressure, temperature"})
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
    species = create_mote(conn, {"name": "Box", "unknowns": "area"})
    add_rule(conn, species, "Area", "area == width * height")
    box = create_mote(conn, {"name": "b", "width": 3, "height": 4})
    set_species(conn, box, species)

    run_simulation(conn)
    assert prop(conn, box, "area") == pytest.approx(12.0)


# ---- failure modes ----

def test_underdetermined_system_is_reported(conn):
    species = create_mote(conn, {"name": "S", "unknowns": "x, y"})
    add_rule(conn, species, "one equation", "x + y == 10")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError, match="underdetermined"):
        run_simulation(conn)


def test_contradictory_rules_are_reported(conn):
    species = create_mote(conn, {"name": "S", "unknowns": "x"})
    add_rule(conn, species, "one", "x == 1")
    add_rule(conn, species, "two", "x == 2")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError, match="contradictory"):
        run_simulation(conn)


def test_nonlinear_in_unknowns_is_reported(conn):
    species = create_mote(conn, {"name": "S", "unknowns": "x"})
    add_rule(conn, species, "square", "x * x == 4")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    with pytest.raises(SimulationError, match="could not be resolved"):
        run_simulation(conn)


def test_formula_error_names_the_rule_and_mote(conn):
    species = create_mote(conn, {"name": "S", "unknowns": "x"})
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
    species = create_mote(conn, {"name": "S", "unknowns": "x, orphan"})
    add_rule(conn, species, "r", "x == 3")
    mote = create_mote(conn, {"name": "m"})
    set_species(conn, mote, species)

    result = run_simulation(conn)
    assert prop(conn, mote, "x") == pytest.approx(3.0)
    assert result.unsolved == [(mote, "orphan")]


# ---- audit log ----

def test_successful_run_is_logged_with_its_changes(conn, divider):
    result = run_simulation(conn)

    cursor = conn.cursor()
    cursor.execute("SELECT tick, status FROM simulation_runs")
    runs = [tuple(row) for row in cursor.fetchall()]
    assert runs == [(1, "ok")]

    cursor.execute("SELECT COUNT(*) AS n FROM simulation_changes WHERE run_id=?", (result.run_id,))
    assert cursor.fetchone()["n"] == len(result.changes)


def test_failed_run_is_logged_too(conn):
    species = create_mote(conn, {"name": "S", "unknowns": "x, y"})
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


def test_tick_advances_across_runs(conn, divider):
    assert run_simulation(conn).tick == 1
    assert run_simulation(conn).tick == 2
    assert run_simulation(conn).tick == 3


def test_change_log_records_old_and_new_values(conn):
    from core.motes import update_mote

    species = create_mote(conn, {"name": "S", "unknowns": "x"})
    add_rule(conn, species, "r", "x == width * 2")
    mote = create_mote(conn, {"name": "m", "width": 5})
    set_species(conn, mote, species)

    run_simulation(conn)
    update_mote(conn, mote, {"width": 10})
    result = run_simulation(conn)

    change = next(c for c in result.changes if c.key == "x")
    assert change.old_value == pytest.approx(10.0)
    assert change.new_value == pytest.approx(20.0)
