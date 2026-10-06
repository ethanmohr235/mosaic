"""`graph --flow` (core/flowview.py): the solve, left to right."""
import pytest

from core import flowview, graphview, worksheet
from core.command_bar import CommandError
from core.motes import create_mote, list_motes
from core.relationships import create_relationship
from core.scenarios import OVERRIDES, VARIES

BEAM = """bm.b = 2 in
bm.h = 6 in
bm.M = 12000
bm.I = bm.b * bm.h^3 / 12
bm.c = bm.h / 2
bm.S = bm.I / bm.c
bm.sigma = bm.M / bm.S
bm.sigma <= 1000 ! allowable
ck.x = ?
ck.y = ?
ck.x + ck.y == bm.c
ck.x - ck.y == 1
"""


def flow(conn, command="graph --flow", **kwargs):
    return flowview.run(conn, graphview.parse_text(command), (), **kwargs)


def mote_id(conn, name):
    return next(m.id for m in list_motes(conn) if m.properties.get("name") == name)


def column_of(p, ref):
    """The column of the step whose equation determines `ref`, else of
    the stub holding it."""
    for g in p["groups"].values():
        if any(p["cards"][c].get("equation", "").startswith(ref + " ") for c in g["cards"]):
            return g["column"]
    for g in p["groups"].values():
        if ref in [r["ref"] for s in g["stubs"] for r in p["stubs"][s]["rows"]]:
            return g["column"]
    raise AssertionError(f"{ref} is not drawn")


def group_of(p, ref):
    for g in p["groups"].values():
        if any(p["cards"][c].get("equation", "").startswith(ref + " ") for c in g["cards"]):
            return g
    raise AssertionError(ref)


@pytest.fixture
def beam(conn):
    worksheet.apply(conn, BEAM)
    return conn


def test_a_chain_is_one_step_per_equation(beam):
    p = flow(beam)
    assert [column_of(p, r) for r in ("bm.I", "bm.S", "bm.sigma")] == [1, 2, 3]
    assert column_of(p, "bm.c") == 1
    assert len(p["bands"]) == 1  # ck reads bm.c, so it is one calculation
    assert [c["kind"] for c in p["bands"][0]["columns"]] == ["givens", "step", "step", "step", "step", "answers"]
    assert p["solve"]["state"] == "solved"


def test_independent_calculations_are_separate_bands(conn):
    worksheet.apply(conn, "k.N = 6\nu.a = 2\nu.b = u.a * k.N\nw.a = 3\nw.b = w.a * k.N\nw.c = w.b + 1")
    p = flow(conn)
    assert len(p["bands"]) == 2
    assert [len(b["columns"]) for b in p["bands"]] == [3, 4]  # u: one step; w: two
    # The shared constant joins nothing: each band has its own stub for it.
    givens = [s for s in p["stubs"].values() if s["name"] == "k"]
    assert len(givens) == 2 and all(s["id"].startswith("in") for s in givens)


def test_an_equation_its_probe_cannot_evaluate_reads_only_what_it_reads(conn):
    """A placeholder 1.0 puts arccos out of its domain; read at the solved
    point the angle depends on its own three inputs, not on every value."""
    worksheet.apply(conn, "p.E0 = 1 MeV\np.E1 = 11/13 * p.E0\np.EC = p.E0 - p.E1\n"
                          "p.ival = 0.284\np.angle = arccos(p.EC / (p.E0 * p.ival))\n"
                          "q.x = 2\nq.y = q.x * 3")
    p = flow(conn)
    assert len(p["bands"]) == 2
    answers = {r["ref"] for g in p["groups"].values() if g["kind"] == "answers"
               for s in g["stubs"] for r in p["stubs"][s]["rows"]}
    assert answers == {"p.angle", "q.y"}


def test_a_coupled_pair_is_one_group_with_residuals(beam):
    g = group_of(flow(beam), "ck.x + ck.y")
    assert g["coupled"] and g["equations"] == 2 and g["unknowns"] == 2
    cards = [flow(beam)["cards"][c] for c in g["cards"]]
    assert all(c["status"] == "pass" and c["offBy"] is not None for c in cards)


def test_a_substitution_step_shows_no_residual(beam):
    p = flow(beam)
    card = p["cards"][group_of(p, "bm.I")["cards"][0]]
    assert card["offBy"] is None and card["tex"].startswith(r"\mathrm{bm}.I = \frac")


def test_givens_and_answers_are_their_own_columns(beam):
    p = flow(beam)
    givens = {r["ref"] for s in p["groups"]["in0"]["stubs"] for r in p["stubs"][s]["rows"]}
    answers = {r["ref"] for s in p["groups"]["out0"]["stubs"] for r in p["stubs"][s]["rows"]}
    assert givens == {"bm.b", "bm.h", "bm.M"}
    # Nothing reads these; the check reading sigma does not make it an intermediate.
    assert answers == {"bm.sigma", "ck.x", "ck.y"}


def test_a_mote_split_over_groups_has_sibling_stubs(beam):
    p = flow(beam)
    stubs = [s for s in p["stubs"].values() if s["name"] == "bm"]
    assert len(stubs) == 5  # givens, I, c, S, answers
    assert all(len(s["siblings"]) == 4 for s in stubs)


def test_a_check_sits_after_what_it_reads_and_marks_it(beam):
    p = flow(beam)
    check = next(g for g in p["groups"].values() if g["kind"] == "check")
    assert check["column"] > column_of(p, "bm.sigma") and check["level"] == "fault"
    sigma = next(r for s in p["stubs"].values() for r in s["rows"] if r["ref"] == "bm.sigma")
    assert sigma["check"] == "fail" and sigma["source"] == "solved"
    assert p["summary"]["checks"] == 1 and p["summary"]["passed"] == 0


def test_the_given_decides_the_direction(conn):
    worksheet.apply(conn, "p.a = 2\np.b = p.a * 3")
    forward = flow(conn)
    worksheet.apply(conn, "q.b = 6\nq.a = ?\nq.a * 3 == q.b")
    backward = flow(conn)
    rows = lambda p, kind: {r["ref"] for g in p["groups"].values() if g["kind"] == kind
                            for s in g["stubs"] for r in p["stubs"][s]["rows"]}
    assert rows(forward, "givens") == {"p.a"} and rows(forward, "answers") == {"p.b"}
    assert rows(backward, "givens") == {"q.b"} and rows(backward, "answers") == {"q.a"}


def test_wires_skip_columns_through_shared_relays(beam):
    p = flow(beam)
    # bm.M is read three steps right of the givens: two relays carry it.
    carrying = [r for r in p["relays"].values() if r["ref"] == "bm.M"]
    assert len(carrying) == 2
    ports = {s["from"] for s in p["segments"]} | {s["to"] for s in p["segments"]}
    assert all(f"{c}" in ports for c in p["relays"])


def test_a_root_keeps_its_steps_either_side(beam):
    p = flow(beam, "graph bm.S --flow --depth 1")
    equations = {c["equation"].split(" ")[0] for c in p["cards"].values()}
    assert equations == {"bm.S", "bm.I", "bm.c", "bm.sigma"}
    assert p["rootName"] == "bm.S"


def test_a_value_outside_the_solve_is_refused(beam):
    other = create_mote(beam, {"name": "loose", "x": 1})
    with pytest.raises(CommandError, match="takes no part"):
        flowview.run(beam, graphview.parse_text("graph loose --flow"), ())
    assert other


def test_a_sub_calculation_folds_sideways(beam):
    p = flow(beam)
    cone = next(f for f in p["folds"] if f["kind"] == "cone" and f["steps"] == 3)
    assert not cone["folded"]
    folded = flow(beam, folds={cone["id"]: True})
    # I -> S -> sigma become one card, ranked after c (step 1): one column fewer.
    assert len(folded["bands"][0]["columns"]) == len(p["bands"][0]["columns"]) - 1
    g = next(g for g in folded["groups"].values() if g["fold"])
    assert g["fold"]["title"] == "bm.sigma ← 3 steps"
    # c is read by the coupled pair too, so it is not inside sigma's fold.
    assert column_of(folded, "bm.c") == 1


def test_a_big_block_starts_folded(conn):
    worksheet.apply(conn, "q.a = ?\nq.b = ?\nq.c = ?\nq.d = ?\n"
                          "q.a + q.b == 3\nq.b + q.c == 5\nq.c + q.d == 7\nq.a - q.d == 1")
    p = flow(conn)
    g = next(g for g in p["groups"].values() if g["equations"] == 4)
    assert g["fold"]["kind"] == "block" and g["cards"] == [g["fold"]["card"]]
    opened = flow(conn, folds={g["fold"]["id"]: False})
    assert next(g for g in opened["groups"].values() if g["equations"] == 4)["coupled"]


def test_a_failing_solve_is_drawn_as_far_as_it_got(conn):
    worksheet.apply(conn, "a.k = 2\na.z = a.k * 2\na.x = ?\na.y = ?\na.x + a.y == a.z")
    p = flow(conn)
    assert p["solve"]["state"] == "partial"
    fault = next(g for g in p["groups"].values() if g["kind"] == "fault")
    assert fault["level"] == "fault" and "a.x" in fault["health"] and "a.y" in fault["health"]
    assert column_of(p, "a.z") < fault["column"]


def test_a_scenario_is_solved_under_its_overrides(beam):
    override = create_mote(beam, {"M": 24000})
    create_relationship(beam, override, mote_id(beam, "bm"), OVERRIDES)
    case = create_mote(beam, {"name": "heavy"})
    create_relationship(beam, case, override, VARIES)
    sigma = lambda p: next(r["value"] for s in p["stubs"].values() for r in s["rows"]
                           if r["ref"] == "bm.sigma")
    assert sigma(flow(beam, run={"scenario": "heavy"})) == pytest.approx(2 * sigma(flow(beam)))


def test_the_layout_is_the_same_every_time(beam):
    assert flow(beam) == flow(beam)


# ---- the grammar ----

def test_flow_round_trips():
    line = graphview.parse_text("graph bm --flow --depth 3")
    assert line.flow and line.text() == "graph bm --flow --depth 3"
    assert graphview.rewrite(line, {"depth": 1}).text() == "graph bm --flow --depth 4"


def test_flow_refuses_hidden_relationships():
    with pytest.raises(CommandError, match="--flow draws the solve"):
        graphview.parse_text("graph --flow --hide-rel instance_of")


def test_completion_offers_flow(conn):
    assert ("--flow", "the solve, left to right") in graphview.choices(conn, "-")


def test_graph_run_hands_flow_over(beam):
    assert graphview.run(beam, graphview.parse_text("graph --flow"))["flow"]


# ---- knobs ----

def test_a_knob_moves_the_solve_and_writes_nothing(beam):
    before = {m.id: dict(m.properties) for m in list_motes(beam)}
    p = flow(beam, "graph --flow --set bm.h=12")
    assert p["sets"] == ["bm.h=12"]
    assert p["keep"] == {"command": "graph --flow --set bm.h=12 --keep", "cleared": "graph --flow"}
    assert {m.id: dict(m.properties) for m in list_motes(beam)} == before
    plain = flow(beam)
    assert plain["sets"] == [] and plain["keep"] is None
    assert p["summary"] != plain["summary"] or p["cards"] != plain["cards"]


def test_the_playheads_knobs_reach_the_flow_and_the_lines_win(beam):
    from_run = flow(beam, run={"sets": ["bm.h=12"]})
    from_line = flow(beam, "graph --flow --set bm.h=12")
    assert from_run["cards"] == from_line["cards"]
    overridden = flow(beam, "graph --flow --set bm.h=6", run={"sets": ["bm.h=12"]})
    assert overridden["cards"] == flow(beam)["cards"]
