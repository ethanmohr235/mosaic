"""The graph pane's core (core/graphview.py): the grammar of `graph`, its
rewrites, and which motes and edges a line draws."""
import pytest

from core import findtext, graphview, views
from core.command_bar import CommandError
from core.motes import create_mote
from core.relationships import create_relationship
from core.species import set_species


def run(conn, text, selection=()):
    return graphview.run(conn, graphview.parse_text(text), selection)


@pytest.fixture
def chain(conn):
    """A -> B -> C -> D, plus a species that A, B and D are instances of,
    and E on its own."""
    part = create_mote(conn, {"name": "Part"})
    ids = {n: create_mote(conn, {"name": n, "status": "open" if n in "AC" else "done"}) for n in "ABCDE"}
    for a, b in ("AB", "BC", "CD"):
        create_relationship(conn, ids[a], ids[b], "feeds")
    for n in "ABD":
        set_species(conn, ids[n], part)
    return {**ids, "Part": part}


def names(payload):
    return sorted(m["name"] for m in payload["motes"])


# ---- grammar ----

def test_lines_round_trip():
    for text in ("graph", "graph @sel --depth 3", "graph '#abc' --hide-rel instance_of,feeds",
                 "graph 'Pump P2' --view open status:open", "graph status:open or status:done"):
        line = graphview.parse_text(text)
        assert graphview.parse_text(line.text()) == line
    assert graphview.parse_text("graph #abc").text() == "graph #abc"


def test_a_bare_word_is_the_root_and_a_condition_is_not():
    line = graphview.parse_text("graph Pump status:open")
    assert line.root == "Pump" and line.conditions == ("status:open",)
    assert line.hops == 2 and graphview.parse_text("graph status:open").hops is None


@pytest.mark.parametrize("text, message", [
    ("graph A B", "one root"),
    ("graph --depth 2", "needs a root"),
    ("graph A --depth 0", "at least 1"),
    ("graph A --depth two", "whole number"),
    ("graph --wat x", "takes"),
    ("graph A --hide-rel", "needs a value"),
])
def test_a_bad_line_says_what_is_wrong(text, message):
    with pytest.raises(CommandError, match=message):
        graphview.parse_text(text)


def test_a_bad_condition_is_refused_naming_it():
    with pytest.raises(findtext.FindSyntaxError):
        graphview.parse_text("graph A status:")


def test_rewrites():
    line = graphview.parse_text("graph @sel")
    assert graphview.rewrite(line, {"depth": 1}).text() == "graph @sel --depth 3"
    assert graphview.rewrite(line, {"depth": -5}).text() == "graph @sel --depth 1"
    assert graphview.rewrite(line, {"root": None}).text() == "graph"
    assert graphview.rewrite(graphview.parse_text("graph"), {"root": "#x"}).text() == "graph #x"
    with pytest.raises(CommandError, match="no depth"):
        graphview.rewrite(graphview.parse_text("graph"), {"depth": 1})


# ---- the answer ----

def test_no_root_is_the_whole_garden(conn, chain):
    p = run(conn, "graph")
    assert names(p) == ["A", "B", "C", "D", "E", "Part"]
    assert p["root"] is None and p["depth"] is None
    assert p["shown"] == p["total"] == 6
    assert {e["type"] for e in p["edges"]} == {"feeds", "instance_of"}


def test_a_root_draws_its_neighbourhood_both_ways(conn, chain):
    p = run(conn, f"graph #{chain['C']} --depth 1 --hide-rel instance_of")
    assert names(p) == ["B", "C", "D"]
    assert p["root"] == chain["C"] and p["rootName"] == "C" and p["depth"] == 1


def test_hiding_a_type_also_stops_hops_through_it(conn, chain):
    # Through the species, D is two hops from A; with instance_of hidden it is three.
    assert "D" in names(run(conn, "graph A"))
    hidden = run(conn, "graph A --hide-rel instance_of")
    assert names(hidden) == ["A", "B", "C"]
    assert all(e["type"] == "feeds" for e in hidden["edges"])
    assert {"type": "instance_of", "hidden": True} in hidden["relTypes"]


def test_sel_is_the_primary(conn, chain):
    p = run(conn, "graph @sel --depth 1 --hide-rel instance_of", selection=(chain["A"], chain["D"]))
    assert names(p) == ["A", "B"]
    with pytest.raises(CommandError, match="@sel is empty"):
        run(conn, "graph @sel")


def test_conditions_keep_matches_and_the_root(conn, chain):
    assert names(run(conn, "graph status:open")) == ["A", "C"]
    assert names(run(conn, "graph B --depth 1 status:open --hide-rel instance_of")) == ["A", "B", "C"]
    p = run(conn, "graph type:Part")
    assert names(p) == ["A", "B", "D"] and p["edges"] == [
        e for e in p["edges"] if e["type"] == "feeds"] and len(p["edges"]) == 1


def test_a_saved_view_narrows_like_find(conn, chain):
    views.save_view(conn, "open things", findtext.parse_find("status:open").query)
    assert names(run(conn, "graph --view 'open things'")) == ["A", "C"]
    with pytest.raises(CommandError, match="no saved view"):
        run(conn, "graph --view nope")


def test_nodes_carry_name_species_and_kinds(conn, chain):
    p = run(conn, "graph")
    node = {m["name"]: m for m in p["motes"]}
    assert node["A"]["speciesId"] == chain["Part"] and node["A"]["speciesName"] == "Part"
    assert "species" in node["Part"]["kinds"]
    assert p["speciesIds"] == [chain["Part"]]


def test_unknowns_are_named(conn, chain):
    with pytest.raises(CommandError, match="no mote named"):
        run(conn, "graph Nobody")
    assert "no relationship has type 'nope'" in run(conn, "graph --hide-rel nope")["problems"]
    create_mote(conn, {"name": "A"})
    with pytest.raises(CommandError, match="2 motes are named"):
        run(conn, "graph A")
