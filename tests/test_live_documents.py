"""A document reads the garden as a solve of it would leave it.

`sim` and `plot` write nothing, and a note that read only stored values
showed nothing (or last week's number) until someone ran `solve`. Since
2026-10-19 `\\val`, `\\work`, `\\given`, `\\answers`, `\\checks` and
`\\verification` read one live solve (core/simulation.py::live_garden).
What earns a test: a value nobody wrote is there, in the unit a written
solve would infer; an edit shows without a re-solve; a value the solve
cannot find says "not solved" rather than showing an older one; and the
garden itself is untouched.
"""
import pytest

from core import calc, modoc, worksheet
from core.modoc_bind import problems, resolve
from core.motes import create_mote, get_mote, list_motes, update_mote
from core.simulation import live_garden, run_simulation

SHEET = "bd.x = 3 m\nbd.y = 4 m\nbd.l = sqrt(bd.x^2 + bd.y^2)\nbd.l <= 10"


def named(conn, name: str):
    return next(m for m in list_motes(conn) if m.properties.get("name") == name)


def values(conn, source: str) -> list:
    note = create_mote(conn, {"name": "HW", modoc.MODOC_KEY: source})
    document = modoc.parse(source)
    resolution = resolve(conn, note, document)
    return [resolution.values[modoc.value_key(n)] for n in modoc.values(document)]


@pytest.fixture
def bar(conn):
    applied = worksheet.apply(conn, SHEET)
    assert not applied.problems
    return named(conn, "bd").id


def test_a_value_nobody_solved_is_there(conn, bar):
    assert "l" not in get_mote(conn, bar).properties   # never `solve`d
    (value,) = values(conn, "\\val{bd.l}")
    assert value.text == "5 m" and value.error is None


def test_an_edit_shows_without_a_resolve(conn, bar):
    run_simulation(conn)
    update_mote(conn, bar, {"x": 6, "y": 8})
    (value,) = values(conn, "\\val{bd.l}")
    assert value.text == "10 m"
    assert get_mote(conn, bar).properties["l"] == 5   # stored stays stale


def test_an_expression_reads_the_live_value_too(conn, bar):
    (value,) = values(conn, "\\val{bd.l * 2}")
    assert value.value == pytest.approx(10)


def test_a_value_the_solve_cannot_find_says_not_solved(conn, bar):
    run_simulation(conn)
    worksheet.apply(conn, SHEET + "\nbd.k = ?")    # a freedom nothing determines
    update_mote(conn, bar, {"k": 42})               # an old value it must not show
    (value,) = values(conn, "\\val{bd.k}")
    assert value.value is None
    assert "not solved" in value.error and "no equation" in value.error


def test_a_broken_garden_says_why(conn, bar):
    run_simulation(conn)
    worksheet.apply(conn, SHEET.replace("sqrt(bd.x^2 + bd.y^2)", "sqrt(bd.x^2 + bd.y^2) / 0"))
    (value,) = values(conn, "\\val{bd.l}")
    assert "the garden does not solve" in value.error


def test_not_solved_is_a_problem_of_the_note(conn, bar):
    worksheet.apply(conn, SHEET + "\nbd.k = ?")
    note = create_mote(conn, {"name": "HW", modoc.MODOC_KEY: ""})
    document = modoc.parse("\\val{bd.k}")
    assert any("not solved" in p.message for p in problems(document, resolve(conn, note, document)))


def test_answers_and_checks_are_live(conn, bar):
    live = live_garden(conn)
    (answer,) = calc.answers(conn, live)
    assert (answer.key, answer.quantity.value) == ("l", pytest.approx(5))
    (check,) = calc.checks(conn, live=live)
    assert check.passed


def test_work_shows_the_inferred_unit(conn):
    worksheet.apply(conn, "m.d = 3 m\nm.t = 2 s\nm.v = m.d / m.t")
    shown = calc.work(conn, named(conn, "m").id, "v")
    assert shown.quantity.unit == "m*s^-1"


def test_reading_live_writes_nothing(conn, bar):
    before = {m.id: m.properties for m in list_motes(conn)}
    values(conn, "\\val{bd.l} \\val{bd.l * 2}")
    after = {m.id: m.properties for m in list_motes(conn) if m.properties.get("name") != "HW"}
    assert after == before
