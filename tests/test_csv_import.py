import pytest

from core import csv_import, motes as motes_repo, relationships as rel_repo
from core.calc import quantity, parse_source_property

MESH = """\
name,mesh size [mm],stress [MPa],elements,note
coarse,10,201.5,120,first pass
fine,2.5,212,4800,
,1,213.1,19000,finest
"""


def test_parse_reads_header_units_and_values():
    parsed = csv_import.parse(MESH, "mesh.csv")
    assert parsed.ok
    assert [(c.key, c.unit) for c in parsed.columns] == [
        ("name", None), ("mesh_size", "mm"), ("stress", "MPa"), ("elements", None), ("note", None),
    ]
    coarse, fine, finest = parsed.rows
    assert coarse["mesh_size"] == 10 and coarse["stress"] == 201.5
    assert coarse["units"] == {"mesh_size": "mm", "stress": "MPa"}
    assert coarse["source"]["stress"] == {"kind": "measured", "note": "mesh.csv"}
    assert "name" not in coarse["source"]
    # An empty cell is no value at all, not a zero or a blank string.
    assert "note" not in fine and "name" not in finest


def test_import_writes_one_mote_per_row_linked_to_one_import_mote(conn):
    imported, command = csv_import.apply(conn, csv_import.parse(MESH, "mesh.csv"))
    assert len(imported.row_ids) == 3
    record = motes_repo.get_mote(conn, imported.import_id).properties
    assert record["file"] == "mesh.csv" and record["rows"] == 3

    names = [motes_repo.get_mote(conn, rid).properties["name"] for rid in imported.row_ids]
    assert names == ["coarse", "fine", "mesh.csv 3"]
    links = [r for r in rel_repo.list_relationships(conn) if r.type == csv_import.IMPORTED_FROM]
    assert {r.from_id for r in links} == set(imported.row_ids)
    assert {r.to_id for r in links} == {imported.import_id}

    fine = motes_repo.get_mote(conn, imported.row_ids[1])
    assert quantity(fine.properties, "stress").unit == "MPa"
    assert parse_source_property(fine.properties["source"])["stress"].kind == "measured"

    command.undo()
    assert motes_repo.list_motes(conn) == []
    command.redo()
    assert len(motes_repo.list_motes(conn)) == 4
    assert len(rel_repo.list_relationships(conn)) == 3


def test_a_given_name_labels_the_import_and_its_unnamed_rows(conn):
    imported, _ = csv_import.apply(conn, csv_import.parse("x\n1\n2\n", "a.csv"), name="run")
    assert motes_repo.get_mote(conn, imported.import_id).properties["name"] == "run"
    assert motes_repo.get_mote(conn, imported.row_ids[0]).properties["name"] == "run 1"


@pytest.mark.parametrize("text, problem", [
    ("", "empty"),
    ("x,y\n", "no rows"),
    ("x [furlongs per bushel]\n1\n", "is not a unit"),
    ("x,x\n1,2\n", "repeats column 1"),
    ("units\n1\n", "reserved property"),
    ("x,\n1,2\n", "has no name"),
    ("name [m]\na\n", "a name has no unit"),
    ("x\n1,2\n", "line 2: 2 cells under 1 headers"),
])
def test_a_bad_file_is_reported_and_refused(conn, text, problem):
    parsed = csv_import.parse(text, "bad.csv")
    assert not parsed.ok
    assert any(problem in p for p in parsed.problems), parsed.problems
    with pytest.raises(ValueError, match="was not imported"):
        csv_import.apply(conn, parsed)
    assert motes_repo.list_motes(conn) == []


def test_text_nan_and_scientific_notation():
    (row,) = csv_import.parse("a,b,c,d\n1e-3,nan,inf,abc\n", "f.csv").rows
    assert row["a"] == 0.001
    assert row["b"] == "nan" and row["c"] == "inf" and row["d"] == "abc"


def test_a_byte_order_mark_and_trailing_blank_lines_are_ignored():
    parsed = csv_import.parse("﻿x\n1\n\n,\n", "f.csv")
    assert parsed.ok and [c.key for c in parsed.columns] == ["x"]
    assert len(parsed.rows) == 1


def test_another_delimiter():
    (row,) = csv_import.parse("x;y\n1;2\n", "f.csv", delimiter=";").rows
    assert (row["x"], row["y"]) == (1, 2)
