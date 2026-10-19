"""Function calls in documents: `\\work` shows the body with values in it,
`\\val` reads a call, and a limit is said everywhere the number is.

The flux homework is the case: `phi(0)` is a limit, and a grader needs to
see both what `phi` is and that the value at 0 came from approaching it.
"""
import pytest

from core import calc, modoc, texexport, worksheet
from core.document import render
from core.modoc_bind import notices, problems, resolve
from core.motes import create_mote, list_motes
from core.simulation import run_simulation

SHEET = """phi(r [cm]) = A * sin(B * r) / r
phi.A = 5e13
phi.B = 0.0628 1/cm
reactor.r = 10 cm
reactor.flux = phi(reactor.r)
reactor.center = phi(0)"""


@pytest.fixture
def reactor(conn):
    applied = worksheet.apply(conn, SHEET)
    assert not applied.problems, applied.problems
    run_simulation(conn)
    return next(m.id for m in list_motes(conn) if m.properties.get("name") == "reactor")


def steps(shown: calc.Work) -> list[tuple[str, str, str]]:
    return [(s.kind, s.text, s.note) for s in shown.steps]


# ---- \work ----

def test_work_writes_the_call_out_as_its_body(conn, reactor):
    assert steps(calc.work(conn, reactor, "flux", figures=4)) == [
        (calc.SYMBOLIC, "reactor.flux == phi(reactor.r)", ""),
        (calc.EXPANDED, "reactor.flux == phi.A * sin(phi.B * reactor.r) / reactor.r", ""),
        (calc.SUBSTITUTED,
         "reactor.flux == 5e13 * sin((0.0628 1/cm) * (10 cm)) / (10 cm)", ""),
        (calc.RESULT, "2.938e14", ""),
    ]


def test_a_bare_number_is_written_in_its_parameters_unit(conn, reactor):
    expanded = dict((k, t) for k, t, _n in steps(calc.work(conn, reactor, "center")))
    assert expanded[calc.EXPANDED] == "reactor.center == phi.A * sin(phi.B * (0 cm)) / (0 cm)"


def test_the_result_says_it_came_from_a_limit(conn, reactor):
    result = steps(calc.work(conn, reactor, "center", figures=3))[-1]
    assert result == (calc.RESULT, "3.14e14", "phi: limit r → 0 cm")


def test_an_equation_without_a_call_is_shown_as_before(conn):
    worksheet.apply(conn, "bd.x = 3 m\nbd.l = bd.x * 2")
    run_simulation(conn)
    bd = next(m.id for m in list_motes(conn) if m.properties.get("name") == "bd")
    assert [k for k, _t, _n in steps(calc.work(conn, bd, "l"))] == [
        calc.SYMBOLIC, calc.SUBSTITUTED, calc.RESULT]


def test_nested_calls_are_written_out_all_the_way(conn):
    worksheet.apply(conn, "double(x) = 2 * x\nquad(x) = double(double(x))\nm.r = 3\nm.y = quad(m.r)")
    run_simulation(conn)
    m = next(m.id for m in list_motes(conn) if m.properties.get("name") == "m")
    expanded = dict((k, t) for k, t, _n in steps(calc.work(conn, m, "y")))
    assert expanded[calc.EXPANDED] == "m.y == 2 * (2 * m.r)"
    assert expanded[calc.SUBSTITUTED] == "m.y == 2 * (2 * 3)"


# ---- \val ----

def resolved(conn, source: str):
    note = create_mote(conn, {"name": "HW", modoc.MODOC_KEY: source})
    document = modoc.parse(source)
    return note, document, resolve(conn, note, document)


def test_val_reads_a_call_and_notes_the_limit(conn, reactor):
    _note, document, resolution = resolved(conn, "At the center, \\val{phi(0)}{3}.")
    (value,) = resolution.values.values()
    assert value.text == "3.14e14" and value.error is None
    assert value.note == "phi: limit r → 0 cm"
    assert [n.message for n in notices(document, resolution)] == [
        "\\val{phi(0)}: phi: limit r → 0 cm"]


def test_a_limit_is_a_notice_never_a_problem(conn, reactor):
    note, document, resolution = resolved(conn, "\\val{phi(0)}\n\n\\work{reactor.center}")
    assert problems(document, resolution) == []
    payload = render(conn, note, "\\val{phi(0)}")
    assert payload["problems"] == [] and len(payload["notices"]) == 1


def test_val_away_from_the_singularity_has_no_note(conn, reactor):
    _note, document, resolution = resolved(conn, "\\val{phi(10)}")
    assert notices(document, resolution) == []


# ---- .tex ----

def test_the_tex_export_says_the_limit_and_sets_the_call(conn, reactor):
    note = create_mote(conn, {"name": "HW", modoc.MODOC_KEY: ""})
    bundle = texexport.export(conn, note, source="\\work{reactor.center}")
    assert r"\phi\left(0\right)" in bundle.tex
    assert r"\emph{(phi: limit r \ensuremath{\to} 0 cm)}" in bundle.tex
    assert bundle.problems == ()
