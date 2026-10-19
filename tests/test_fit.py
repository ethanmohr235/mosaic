"""`fit`: least-squares fitting of a garden's inputs to measured points
(core/fit.py).

The divider's current is voltage / (r_a + r_b) with r_b = 200, so points
measured with r_a = 50 lie on I = V / 250, and a fit freeing r_a must
land on 50 -- an exact answer to check against, the same bargain
tests/test_plotcmd.py strikes."""
import pytest

from core import fit
from core.calc import FITTED, SOURCE_KEY
from core.command_bar import CommandError
from core.csv_import import IMPORTED_FROM
from core.motes import create_mote, get_mote, update_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO, FREEDOMS_KEY
from core.species import set_species
from core.uncertainty import UNCERTAINTY_KEY

LINE = "fit C1.current vs C1.voltage --data I vs V --free C1.r_a"


@pytest.fixture
def divider(conn):
    species = create_mote(conn, {"name": "Divider", FREEDOMS_KEY: "current"})
    rule = create_mote(conn, {"name": "series", "equation": "current * (r_a + r_b) == voltage"})
    create_relationship(conn, rule, species, APPLIES_TO)
    circuit = create_mote(conn, {"name": "C1", "voltage": 10, "r_a": 100, "r_b": 200})
    set_species(conn, circuit, species)
    return circuit


def measure(conn, points, noise=()):
    ids = []
    for i, v in enumerate(points):
        ids.append(create_mote(conn, {"name": f"m{i}", "V": v, "I": v / 250 + (noise[i] if noise else 0)}))
    return ids


def run(conn, text=LINE):
    return fit.run(conn, fit.parse_text(text))


# ---- grammar ----

def test_the_line_round_trips():
    text = 'fit C1.current vs C1.voltage --data I vs V --from bench.csv --free C1.r_a C1.r_b --scenario hot --apply'
    assert fit.parse_text(text).text() == text
    assert fit.parse_text(text).text(apply=False).endswith("--scenario hot")


@pytest.mark.parametrize("text, message", [
    ("fit C1.current C1.voltage --data I vs V --free C1.r_a", "fit what against what"),
    ("fit C1.current vs C1.voltage --free C1.r_a", "needs --data"),
    ("fit C1.current vs C1.voltage --data I V --free C1.r_a", "--data takes"),
    ("fit C1.current vs C1.voltage --data I vs V", "at least one --free"),
    ("fit C1.current vs C1.voltage --data I vs V --free C1.r_a --wat", "unknown flag"),
])
def test_a_bad_line_says_what_is_wrong(text, message):
    with pytest.raises(CommandError, match=message):
        fit.parse_text(text)


# ---- fitting ----

def test_an_exact_fit_recovers_the_input(conn, divider):
    measure(conn, [0, 5, 10, 20, 40])
    result = run(conn)
    (param,) = result.params
    assert (param.ref, param.start) == ("C1.r_a", 100.0)
    assert param.value == pytest.approx(50, rel=1e-6)
    assert result.converged and result.rms == pytest.approx(0, abs=1e-9)
    assert len(result.points) == 5 and len(result.fitted) == 5


def test_a_noisy_fit_reports_sigma_rms_and_r2(conn, divider):
    measure(conn, [2, 5, 10, 20, 40], noise=[0.0004, -0.0003, 0.0002, -0.0004, 0.0003])
    result = run(conn)
    (param,) = result.params
    assert param.value == pytest.approx(50, rel=0.05)
    assert param.sigma is not None and 0 < param.sigma < 10
    assert 0 < result.rms < 1e-3
    assert result.r2 == pytest.approx(1, abs=1e-3)


def test_nothing_is_written_without_apply(conn, divider):
    measure(conn, [5, 10, 20])
    run(conn)
    assert get_mote(conn, divider).properties["r_a"] == 100


def test_apply_writes_value_sigma_and_source_as_one_undo(conn, divider):
    measure(conn, [2, 5, 10, 20, 40], noise=[0.0004, -0.0003, 0.0002, -0.0004, 0.0003])
    result = run(conn)
    message, command = fit.apply(conn, result)
    props = get_mote(conn, divider).properties
    assert message.startswith("fitted C1.r_a = ")
    assert props["r_a"] == pytest.approx(result.params[0].value)
    assert props[UNCERTAINTY_KEY]["r_a"] == pytest.approx(result.params[0].sigma)
    assert props[SOURCE_KEY]["r_a"] == {"kind": FITTED, "note": LINE}
    command.undo()
    props = get_mote(conn, divider).properties
    assert props["r_a"] == 100 and UNCERTAINTY_KEY not in props and SOURCE_KEY not in props


def test_inputs_the_data_cannot_tell_apart_get_no_sigma(conn, divider):
    """Only r_a + r_b shows in the current, so the two can't be separated."""
    measure(conn, [2, 5, 10, 20, 40])
    result = run(conn, LINE + " C1.r_b")
    assert all(p.sigma is None for p in result.params)
    assert "can't tell" in result.message
    assert sum(p.value for p in result.params) == pytest.approx(250, rel=1e-4)


def test_from_narrows_to_one_imports_rows(conn, divider):
    bench = create_mote(conn, {"name": "bench.csv"})
    for i, row in enumerate(measure(conn, [5, 10, 20])):
        create_relationship(conn, row, bench, IMPORTED_FROM)
    create_mote(conn, {"name": "stray", "V": 10, "I": 5.0})  # not in the import
    result = run(conn, LINE + " --from bench.csv")
    assert len(result.points) == 3
    assert result.params[0].value == pytest.approx(50, rel=1e-6)
    with pytest.raises(CommandError, match="no import named"):
        run(conn, LINE + " --from other.csv")


def test_data_units_convert_into_the_models(conn, divider):
    update_mote(conn, divider, {"units": {"voltage": "V", "current": "A"}})
    for i, v in enumerate([5, 10, 20]):
        create_mote(conn, {"name": f"m{i}", "V": v, "I": v / 250 * 1000, "units": {"V": "V", "I": "mA"}})
    assert run(conn).params[0].value == pytest.approx(50, rel=1e-6)


@pytest.mark.parametrize("setup, message", [
    (lambda conn: None, "no mote carries both"),
    (lambda conn: create_mote(conn, {"name": "m", "V": 5, "I": 0.02}), None),
])
def test_too_little_data(conn, divider, setup, message):
    setup(conn)
    if message:
        with pytest.raises(CommandError, match=message):
            run(conn)
    else:
        result = run(conn)  # one point, one input: an exact fit with no σ
        assert result.params[0].value == pytest.approx(50, rel=1e-6)
        assert result.params[0].sigma is None


def test_a_free_input_with_no_number_is_refused(conn, divider):
    measure(conn, [5, 10])
    with pytest.raises(CommandError, match="no number to start"):
        run(conn, "fit C1.current vs C1.voltage --data I vs V --free C1.nope")


def test_the_curve_spans_the_data(conn, divider):
    measure(conn, [5, 10, 20])
    curve = run(conn).curve
    assert curve[0][0] == 5 and curve[-1][0] == 20
    assert curve[-1][1] == pytest.approx(20 / 250, rel=1e-6)


def test_apply_refuses_a_fit_with_no_values(conn, divider):
    result = fit.FitResult(line=fit.parse_text(LINE), message="too few points")
    with pytest.raises(CommandError, match="too few points"):
        fit.apply(conn, result)


def test_completion_offers_refs_after_vs(conn):
    """`fit m.y vs ` completes to model values -- once broken by a rename
    in core/plotcmd.py that only the console's completion reached."""
    create_mote(conn, {"name": "m", "x": 1, "y": 2})
    assert any(c.value == "m.x" for c in fit.choices(conn, "m.y vs "))
