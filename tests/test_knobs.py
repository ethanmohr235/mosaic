"""Knobs (core/knobs.py): `--set m.k=value [unit]` on a pane's line, values
to try that are never written.

The divider's current is voltage / (r_a + r_b), so a knob's effect is an
exact number; the tank's level is (t + 1) * inflow, an exact time series."""
import pytest

from core import commands, fit, graphview, knobs, motion, plotcmd
from core import scenarios as scenarios_repo
from core.command_bar import CommandError, run_command
from core.knobs import KnobSpec
from core.motes import create_mote, get_mote, list_motes
from core.plotcmd import _Garden
from core.relationships import create_relationship
from core.rules import APPLIES_TO, FREEDOMS_KEY
from core.species import set_species


@pytest.fixture
def divider(conn):
    species = create_mote(conn, {"name": "Divider", FREEDOMS_KEY: "current"})
    rule = create_mote(conn, {"name": "series", "equation": "current * (r_a + r_b) == voltage"})
    create_relationship(conn, rule, species, APPLIES_TO)
    circuit = create_mote(conn, {
        "name": "C1", "voltage": 10, "r_a": 50, "r_b": 200,
        "units": {"voltage": "V", "r_a": "ohm", "r_b": "ohm", "current": "A"}})
    set_species(conn, circuit, species)
    return circuit


@pytest.fixture
def tank(conn):
    species = create_mote(conn, {"name": "Tank", FREEDOMS_KEY: "level"})
    rule = create_mote(conn, {"name": "fill", "equation": "level == prev(level) + inflow"})
    create_relationship(conn, rule, species, APPLIES_TO)
    t1 = create_mote(conn, {"name": "T1", "level": 0, "inflow": 2})
    set_species(conn, t1, species)
    return t1


def snapshot(conn):
    return {m.id: dict(m.properties) for m in list_motes(conn)}


# ---- the grammar ----

@pytest.mark.parametrize("text, spec", [
    ("bd.E=200e9", KnobSpec("bd.E", 200e9)),
    ("bd.E=200GPa", KnobSpec("bd.E", 200.0, "GPa")),
    ("bd.E=200 GPa", KnobSpec("bd.E", 200.0, "GPa")),
    ("C1.r_a=-0.5 kohm", KnobSpec("C1.r_a", -0.5, "kohm")),
    ("#ab12.x=3", KnobSpec("#ab12.x", 3.0)),
])
def test_parse_set_reads_a_number_and_an_optional_unit(text, spec):
    assert knobs.parse_set(text) == spec


@pytest.mark.parametrize("text", ["bd.E", "bd.E=", "=5", "bd.E=abc", "bd.E=5 flurbs", "bd.E=true"])
def test_parse_set_refuses_what_is_not_a_number(text):
    with pytest.raises(CommandError):
        knobs.parse_set(text)


def test_an_assignment_round_trips():
    for text in ("a.b=1", "a.b=0.25", "a.b=200 GPa", "a.b=2e+20"):
        spec = knobs.parse_set(text)
        assert knobs.parse_set(spec.assignment) == spec


def test_words_are_flag_then_assignment():
    specs = (KnobSpec("a.b", 1.0), KnobSpec("a.c", 2.0, "m"))
    assert knobs.words(specs) == ["--set", "a.b=1", "--set", "a.c=2 m"]


# ---- meeting the garden ----

def test_a_typed_unit_is_converted_into_the_properties_own(conn, divider):
    (knob,) = knobs.resolve(_Garden(conn), [KnobSpec("C1.r_a", 0.1, "kohm")])
    assert (knob.mote, knob.key, knob.unit) == (divider, "r_a", "ohm")
    assert knob.value == pytest.approx(100.0)


def test_no_unit_means_the_properties_own(conn, divider):
    (knob,) = knobs.resolve(_Garden(conn), [KnobSpec("C1.r_a", 75.0)])
    assert knob.value == 75.0 and knob.unit == "ohm"


def test_a_unit_of_the_wrong_dimension_is_refused(conn, divider):
    with pytest.raises(CommandError, match="r_a"):
        knobs.resolve(_Garden(conn), [KnobSpec("C1.r_a", 5.0, "m")])


def test_a_unit_on_a_property_with_none_declared_is_refused(conn, tank):
    with pytest.raises(CommandError, match="declares no unit"):
        knobs.resolve(_Garden(conn), [KnobSpec("T1.inflow", 5.0, "m")])


def test_an_unknown_name_is_refused(conn, divider):
    with pytest.raises(CommandError, match="no mote named"):
        knobs.resolve(_Garden(conn), [KnobSpec("Z9.x", 1.0)])


def test_overrides_later_knob_wins(conn, divider):
    resolved = knobs.resolve(_Garden(conn), [KnobSpec("C1.r_a", 1.0), KnobSpec("C1.r_a", 2.0)])
    assert knobs.overrides(resolved) == {divider: {"r_a": 2.0}}


def test_overlay_changes_nothing_it_was_given():
    base = {"a": {"x": 1}, "b": {"y": 2}}
    over = {"a": {"x": 9}}
    out = scenarios_repo.overlay(base, over)
    assert out == {"a": {"x": 9}, "b": {"y": 2}}
    assert base == {"a": {"x": 1}, "b": {"y": 2}} and over == {"a": {"x": 9}}


# ---- the panes take them ----

def test_sim_runs_under_a_knob_and_writes_nothing(conn, tank):
    before = snapshot(conn)
    plain = motion.run(conn, motion.parse_text("sim --ticks 3"))
    tried = motion.run(conn, motion.parse_text("sim --ticks 3 --set T1.inflow=5"))
    level = lambda run: next(r for r in run.rows if r.ref == "T1.level").series  # noqa: E731
    assert level(plain) == (2.0, 4.0, 6.0)
    assert level(tried) == (5.0, 10.0, 15.0)
    assert snapshot(conn) == before


def test_a_knob_row_reads_as_varied_and_starts_at_the_knob(conn, tank):
    run = motion.run(conn, motion.parse_text("sim --ticks 2 --set T1.inflow=5"))
    row = next(r for r in run.rows if r.ref == "T1.inflow")
    assert (row.state, row.start) == (motion.VARIED, 5.0)


def test_the_vs_run_is_the_plain_garden_not_the_knobbed_one(conn, tank):
    run = motion.run(conn, motion.parse_text("sim --ticks 2 --vs garden --set T1.inflow=5"))
    row = next(r for r in run.rows if r.ref == "T1.level")
    assert row.series == (5.0, 10.0) and row.vs == (2.0, 4.0)


def test_the_sim_line_keeps_its_knobs_when_rewritten(conn, tank):
    line = motion.parse_text("sim --ticks 4 --set 'T1.inflow=5'")
    assert line.sets == (KnobSpec("T1.inflow", 5.0),)
    again = motion.parse_text(line.text())
    assert again == line
    assert motion.rewrite(line, {"vs": "garden"}).sets == line.sets


def test_rule_cards_follow_the_runs_knobs(conn, tank):
    (card,) = motion.rule_cards(conn, "fill", ticks=2, sets=["T1.inflow=5"])
    level = next(b for b in card.bindings if b.key == "level")
    assert level.series == (5.0, 10.0)


def test_fit_starts_from_a_knob_and_holds_the_rest(conn, divider):
    line = fit.parse_text("fit C1.current vs C1.voltage --data I vs V --free C1.r_a --set C1.r_b=200")
    assert line.sets == (KnobSpec("C1.r_b", 200.0),)
    assert fit.parse_text(line.text()) == line


def test_a_fit_under_a_knob_moves_the_answer(conn, divider):
    for v in (2.0, 4.0, 6.0, 8.0):
        create_mote(conn, {"name": f"m{v}", "V": v, "I": v / 250})
    base = "fit C1.current vs C1.voltage --data I vs V --free C1.r_a"
    plain = fit.run(conn, fit.parse_text(base))
    held = fit.run(conn, fit.parse_text(base + " --set C1.r_b=100"))
    assert plain.params[0].value == pytest.approx(50.0, rel=1e-3)
    assert held.params[0].value == pytest.approx(150.0, rel=1e-3)
    assert get_mote(conn, divider).properties["r_a"] == 50


def test_graph_takes_a_knob_only_with_flow():
    assert graphview.parse(["--flow", "--set", "C1.r_a=10"]).sets == (KnobSpec("C1.r_a", 10.0),)
    with pytest.raises(CommandError, match="--flow"):
        graphview.parse(["--set", "C1.r_a=10"])
    line = graphview.parse_text("graph --flow --set 'C1.r_a=10 ohm'")
    assert graphview.parse_text(line.text()) == line


def test_the_plot_takes_a_unit(conn, divider):
    resolved = plotcmd.resolve(conn, plotcmd.parse_text("plot C1.current vs C1.voltage 1:10 --set 'C1.r_a=0.1 kohm'").line)
    (knob,) = resolved.knobs
    assert knob.value == pytest.approx(100.0) and knob.unit == "ohm"
    assert get_mote(conn, divider).properties["r_a"] == 50


# ---- the payload ----

def test_line_payload_offers_keep_only_while_there_are_knobs():
    line = motion.parse_text("sim --ticks 4 --set T1.inflow=5")
    payload = knobs.line_payload(line)
    assert payload["sets"] == ["T1.inflow=5"]
    assert payload["keep"] == {"command": "sim --ticks 4 --set T1.inflow=5 --keep",
                               "cleared": "sim --ticks 4"}
    assert knobs.line_payload(motion.parse_text("sim")) == {"sets": [], "keep": None}


# ---- the way out ----

def test_keep_writes_every_knob_as_one_undo_step(conn, divider):
    from core.undo import UndoManager
    stack = UndoManager()
    resolved = knobs.resolve(_Garden(conn), [KnobSpec("C1.r_a", 0.1, "kohm"), KnobSpec("C1.r_b", 300.0)])
    message, command = knobs.keep(conn, resolved)
    stack.push(command)
    props = get_mote(conn, divider).properties
    assert (props["r_a"], props["r_b"]) == (pytest.approx(100.0), 300.0)
    assert props["units"]["r_a"] == "ohm"  # the unit is the property's own, unchanged
    assert "C1.r_a" in message and "C1.r_b" in message
    stack.undo()
    props = get_mote(conn, divider).properties
    assert (props["r_a"], props["r_b"]) == (50, 200)


def test_keep_with_no_knobs_writes_nothing(conn, divider):
    assert knobs.keep(conn, ()) == ("no knobs to keep", None)


# ---- set and the statement are one writer ----

def test_set_and_a_statement_write_the_same_motes(conn):
    a = create_mote(conn, {"name": "A"})
    b = create_mote(conn, {"name": "B"})
    run_command(conn, "A.x = 5 mm")
    commands.run_set(conn, ["B", "x", "5", "mm"], [])
    assert get_mote(conn, a).properties["x"] == get_mote(conn, b).properties["x"] == 5
    assert get_mote(conn, a).properties["units"] == get_mote(conn, b).properties["units"] == {"x": "mm"}
