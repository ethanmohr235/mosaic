"""core/inspect.py -- what an inspector shows for one mote: the core
function the console's props pane (console/mote.py) calls."""
from core import inspect
from core.motes import create_mote
from core.relationships import create_relationship
from core.species import INSTANCE_OF, TEMPLATE_OF


def test_a_missing_mote_is_none(conn):
    assert inspect.inspect(conn, "nope") is None


def test_the_payload_types_each_value_and_lists_both_directions(conn):
    task = create_mote(conn, {"name": "task"})
    template = create_mote(conn, {"name": "tmpl", "status": "todo", "estimate": 1})
    create_relationship(conn, template, task, TEMPLATE_OF)
    for status in ("todo", "todo", "done"):
        m = create_mote(conn, {"name": f"t-{status}", "status": status})
        create_relationship(conn, m, task, INSTANCE_OF)
    beam = create_mote(conn, {"name": "beam", "status": "todo", "length": 5,
                              "units": {"length": "mm"}, "due": "2026-10-02", "done": False,
                              "owner": task})
    other = create_mote(conn, {"name": "wall"})
    create_relationship(conn, beam, task, INSTANCE_OF)
    create_relationship(conn, other, beam, "supports")

    payload = inspect.inspect(conn, beam)
    kinds = payload["kinds"]
    assert kinds["length"]["kind"] == "number" and kinds["length"]["unit"] == "mm"
    assert kinds["due"] == {"kind": "date"}
    assert kinds["done"] == {"kind": "boolean"}
    assert kinds["owner"]["kind"] == "ref" and kinds["owner"]["refLabel"] == "task"
    assert kinds["status"] == {"kind": "enum", "options": ["done", "todo"]}
    assert payload["keys"][0] != "name" and "units" == payload["keys"][-1]
    assert payload["hints"] == [{"key": "estimate", "value": 1}]
    assert payload["species"]["label"] == "task"
    directions = {(r["type"], r["direction"], r["otherLabel"]) for r in payload["relationships"]}
    assert ("instance_of", "out", "task") in directions and ("supports", "in", "wall") in directions


def test_a_freedom_nothing_wrote_is_listed_as_not_solved(conn):
    """`sim` and `plot` write nothing, so an inspector has to say a
    freedom is waiting on `solve` rather than show a blank or leave it out."""
    m = create_mote(conn, {"name": "reactor", "freedoms": ["flux", "center"], "flux": ""})
    shown = inspect.inspect(conn, m)
    assert shown["unsolved"] == ["center", "flux"]
    assert "center" in shown["keys"]   # a row even though no property holds it


def test_a_written_freedom_is_not_marked(conn):
    m = create_mote(conn, {"name": "reactor", "freedoms": ["flux"], "flux": 3.1})
    assert inspect.inspect(conn, m)["unsolved"] == []
