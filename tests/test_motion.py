"""Motion's core (core/motion.py): the `sim` grammar, the run table's
rows and markers, rule cards, `why`, the undoable `solve`, the live
solve, and `set --scenario` (core/commands.py).

Two small gardens. The divider is static: current = voltage / (r_a +
r_b), the same every tick. The tank is a recurrence: level = prev(level)
+ inflow, so tick t's level is (t + 1) * inflow -- an exact series to
check a row against."""
import pytest

from core import commands, derivations, motion
from core import scenarios as scenarios_repo
from core.command_bar import CommandError
from core.motes import create_mote, get_mote, update_mote
from core.relationships import create_relationship
from core.rules import APPLIES_TO, FREEDOMS_KEY
from core.species import set_species


@pytest.fixture
def divider(conn):
    species = create_mote(conn, {"name": "Divider", FREEDOMS_KEY: "current"})
    rule = create_mote(conn, {"name": "series", "equation": "current * (r_a + r_b) == voltage"})
    create_relationship(conn, rule, species, APPLIES_TO)
    circuit = create_mote(conn, {"name": "C1", "voltage": 10, "r_a": 50, "r_b": 200})
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


def scenario(conn, name, target, **values):
    case = create_mote(conn, {"name": name})
    override = create_mote(conn, values)
    create_relationship(conn, case, override, scenarios_repo.VARIES)
    create_relationship(conn, override, target, scenarios_repo.OVERRIDES)
    return case


def sim(conn, text):
    return motion.run(conn, motion.parse_text(text))


def row(result, ref):
    return next(r for r in result.rows if r.ref == ref)


# ---- grammar ----

def test_the_line_round_trips():
    text = "sim --ticks 20 --scenario hot --vs garden --solver newton type:Divider"
    assert motion.parse_text(text).text() == text
    assert motion.parse_text("sim").tick_count == motion.DEFAULT_TICKS


@pytest.mark.parametrize("text, message", [
    ("sim --ticks 0", "1 to"),
    ("sim --ticks ten", "whole number"),
    ("sim --wat 3", "sim takes"),
    ("sim --scenario", "needs a value"),
    ("sim --solver bogus", "no solver"),
])
def test_a_bad_line_says_what_is_wrong(text, message):
    with pytest.raises(CommandError, match=message):
        motion.parse_text(text)


def test_d_toggles_vs_garden():
    line = motion.parse_text("sim --ticks 5")
    assert motion.rewrite(line, {"vs": "garden"}).text() == "sim --ticks 5 --vs garden"
    assert motion.rewrite(motion.rewrite(line, {"vs": "garden"}), {"vs": None}).text() == "sim --ticks 5"


# ---- rows ----

def test_a_recurrence_moves_tick_by_tick(conn, tank):
    result = sim(conn, "sim --ticks 5")
    level = row(result, "T1.level")
    assert level.state == motion.SOLVED
    assert level.start == 0
    assert level.series == (2, 4, 6, 8, 10)
    assert level.extremes == (0, 4)
    assert result.count == 5 and result.failed_at is None


def test_default_rows_are_solved_free_and_varied(conn, divider):
    scenario(conn, "hot", divider, voltage=30)
    result = sim(conn, "sim --ticks 3 --scenario hot")
    refs = {r.ref: r.state for r in result.rows}
    assert refs == {"C1.current": motion.SOLVED, "C1.voltage": motion.VARIED}
    assert row(result, "C1.current").series == pytest.approx((0.12, 0.12, 0.12))
    assert result.scenario == "hot"


def test_an_unsolved_freedom_is_pinned(conn):
    create_mote(conn, {"name": "Loose", "x": 3, FREEDOMS_KEY: "x"})
    result = sim(conn, "sim --ticks 2")
    assert row(result, "Loose.x").state == motion.PINNED
    assert row(result, "Loose.x").series == (3, 3)


def test_find_conditions_watch_every_number_of_the_matches(conn, divider):
    result = sim(conn, "sim --ticks 2 name:C1")
    assert {r.ref for r in result.rows} == {"C1.current", "C1.r_a", "C1.r_b", "C1.voltage"}
    assert row(result, "C1.r_a").state == motion.GIVEN


def test_vs_pairs_each_row_with_the_other_run(conn, divider):
    scenario(conn, "hot", divider, voltage=30)
    result = sim(conn, "sim --ticks 2 --scenario hot --vs garden")
    current = row(result, "C1.current")
    assert current.series == pytest.approx((0.12, 0.12))
    assert current.vs == pytest.approx((0.04, 0.04))
    assert row(result, "C1.voltage").vs_start == 10
    assert result.vs == "garden"


def test_vs_a_scenario_adds_its_varied_rows(conn, divider):
    scenario(conn, "cold", divider, r_b=450)
    result = sim(conn, "sim --ticks 1 --vs cold")
    assert row(result, "C1.r_b").vs == (450,)
    assert result.vs == "cold"


def test_a_failed_tick_is_a_marker_not_an_error(conn, divider):
    rule = create_mote(conn, {"name": "limit", "equation": "current <= 0.01"})
    create_relationship(conn, rule, get_mote(conn, divider).id, APPLIES_TO)
    result = sim(conn, "sim --ticks 3")
    kinds = {m.kind for m in result.markers}
    assert motion.CONSTRAINT in kinds
    assert [m.tick for m in result.markers if m.kind == motion.CONSTRAINT] == [0, 1, 2]


def test_an_unknown_scenario_is_refused(conn, divider):
    with pytest.raises(CommandError, match="no scenario named"):
        sim(conn, "sim --scenario nope")


def test_the_json_carries_series_and_extremes(conn, tank):
    payload = motion.run_json(sim(conn, "sim --ticks 3"))
    (level,) = [r for r in payload["rows"] if r["ref"] == "T1.level"]
    assert level["series"] == [2, 4, 6] and level["minTick"] == 0 and level["maxTick"] == 2
    assert payload["count"] == 3 and payload["command"] == "sim --ticks 3"


# ---- rule ----

def test_a_rule_card_binds_its_values(conn, divider):
    (card,) = motion.rule_cards(conn, "series")
    assert card.equation == "current * (r_a + r_b) == voltage"
    roles = {b.ref: b.role for b in card.bindings}
    assert roles["C1.current"] == "determines" and roles["C1.voltage"] == "reads"
    assert card.anchors == ("C1",)


def test_rule_on_a_component_shows_the_rules_touching_it(conn, divider):
    cards = motion.rule_cards(conn, "C1")
    assert [c.name for c in cards] == ["series"]


def test_a_rule_card_under_a_run_carries_series(conn, tank):
    (card,) = motion.rule_cards(conn, "fill", ticks=3)
    level = next(b for b in card.bindings if b.ref == "T1.level" and b.role == "determines")
    assert level.series == (2, 4, 6)


def test_rule_of_an_untouched_mote_is_refused(conn, divider):
    create_mote(conn, {"name": "Lonely"})
    with pytest.raises(CommandError, match="no rule touches"):
        motion.rule_cards(conn, "Lonely")


# ---- solve, why ----

def test_solve_writes_and_one_undo_takes_it_back(conn, divider):
    solved = motion.solve(conn)
    assert get_mote(conn, divider).properties["current"] == pytest.approx(0.04)
    assert motion.why(conn, "C1.current")["value"] == pytest.approx(0.04)
    solved.command.undo()
    assert "current" not in get_mote(conn, divider).properties
    assert derivations.snapshot(conn)["simulation_derivations"] == []
    solved.command.redo()
    assert get_mote(conn, divider).properties["current"] == pytest.approx(0.04)
    assert motion.why(conn, "C1.current")["equations"][0]["rule"] == "series"


def test_solve_refuses_a_recurrence(conn, tank):
    with pytest.raises(CommandError, match="prev"):
        motion.solve(conn)


def test_live_solve_folds_into_the_edit(conn, divider):
    result = commands.run_set(conn, ["C1", "voltage", "20"], [])
    message, combined = motion.live_solve(conn, result.command)
    assert "live: solved" in message
    assert get_mote(conn, divider).properties["current"] == pytest.approx(0.08)
    combined.undo()
    props = get_mote(conn, divider).properties
    assert props["voltage"] == 10 and "current" not in props


def test_live_solve_keeps_the_edit_when_it_cannot_solve(conn, tank):
    result = commands.run_set(conn, ["T1", "inflow", "3"], [])
    message, command = motion.live_solve(conn, result.command)
    assert "prev" in message and command is result.command


@pytest.mark.parametrize("args, expected", [
    ([], None), (["--live", "on"], True), (["--live", "off"], False)])
def test_solve_live_flag(args, expected):
    assert motion.parse_live(args) is expected


@pytest.mark.parametrize("args, expected", [
    ([], ("by", 1)), (["+5"], ("by", 5)), (["-2"], ("by", -2)), (["12"], ("to", 12))])
def test_step_grammar(args, expected):
    assert motion.parse_step(args) == expected


# ---- set --scenario, set m.k ----

def test_set_accepts_the_dotted_form(conn, divider):
    commands.run_set(conn, ["C1.voltage", "12"], [])
    assert get_mote(conn, divider).properties["voltage"] == 12


def test_set_under_a_new_scenario_creates_it_and_undoes_cleanly(conn, divider):
    result = commands.run_set(conn, ["--scenario", "hot", "C1.voltage", "30"], [])
    assert "under hot" in result.message
    (case,) = scenarios_repo.list_scenarios(conn)
    assert case.name == "hot" and case.overrides[0].properties == {"voltage": 30}
    assert get_mote(conn, divider).properties["voltage"] == 10
    result.command.undo()
    assert scenarios_repo.list_scenarios(conn) == []
    assert not any(m for m in commands.motes_repo.list_motes(conn) if m.properties.get("name") == "hot")


def test_set_under_a_scenario_reuses_its_override(conn, divider):
    case = scenario(conn, "hot", divider, voltage=30)
    commands.run_set(conn, ["--scenario", "hot", "C1", "r_a", "70"], [])
    (override,) = scenarios_repo.get_scenario(conn, case).overrides
    assert override.properties == {"voltage": 30, "r_a": 70}
    commands.run_set(conn, ["--scenario", "hot", "C1.voltage", "--clear"], [])
    (override,) = scenarios_repo.get_scenario(conn, case).overrides
    assert override.properties == {"r_a": 70}


def test_a_unit_under_a_scenario_keeps_the_targets_other_units(conn, divider):
    update_mote(conn, divider, {"units": {"voltage": "V", "r_a": "ohm"}})
    commands.run_set(conn, ["--scenario", "hot", "C1.voltage", "30", "mV"], [])
    (case,) = scenarios_repo.list_scenarios(conn)
    assert case.overrides[0].properties["units"] == {"voltage": "mV", "r_a": "ohm"}


def test_set_scenario_refuses_a_mote_that_is_not_a_scenario(conn, divider):
    with pytest.raises(CommandError, match="not a scenario"):
        commands.run_set(conn, ["--scenario", "C1", "C1.voltage", "30"], [])


# ---- scenarios, completion ----

def test_scenarios_list_overrides_beside_the_garden(conn, divider):
    scenario(conn, "hot", divider, voltage=30)
    (entry,) = motion.scenarios(conn)
    assert entry["name"] == "hot"
    assert entry["overrides"] == [{"moteId": divider, "key": "voltage", "ref": "C1.voltage",
                                   "value": 30, "garden": 10, "unit": None}]


def test_completion_offers_scenarios_after_the_flag(conn, divider):
    scenario(conn, "hot", divider, voltage=30)
    values = [r.value for r in commands.complete(conn, "sim --vs ").rows]
    assert values == ["hot", "garden"]
    assert [r.value for r in commands.complete(conn, "rule s").rows] == ["series"]
