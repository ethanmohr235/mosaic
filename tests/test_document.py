"""The console's note core (core/document.py), the worksheet's undoable
apply and line preview (core/worksheet.py), and the snapshot undo
(core/undo.py::capture/since).

The garden is a bar: bd.l = sqrt(bd.x^2 + bd.y^2) with x = 3 m and y = 4 m,
so l is 5 m -- a solved value every macro can show."""
import pytest

from core import document, modoc, undo, worksheet
from core.command_bar import CommandError
from core.motes import create_mote, get_mote, list_motes, update_mote
from core.relationships import create_relationship, list_relationships
from core.simulation import run_simulation

SHEET = "bd.x = 3 m\nbd.y = 4 m\nbd.l = sqrt(bd.x^2 + bd.y^2)\nbd.l <= 10 ! short enough"


@pytest.fixture
def bar(conn):
    worksheet.apply(conn, SHEET)
    run_simulation(conn)
    return next(m.id for m in list_motes(conn) if m.properties.get("name") == "bd")


def note(conn, source, name="Homework"):
    return create_mote(conn, {"name": name, modoc.MODOC_KEY: source})


def blocks(conn, source, note_id=None):
    note_id = note_id or note(conn, "")
    return document.render(conn, note_id, source)["blocks"]


# ---- rendering ----

def test_prose_links_values_and_refs(conn, bar):
    (para,) = blocks(conn, "See \\mote{bd}: $l$ is \\val{bd.l}.")
    kinds = [n["type"] for n in para["content"]]
    assert kinds == ["text", "link", "text", "math", "text", "value", "text"]
    link = para["content"][1]
    assert link["moteId"] == bar and link["text"] == "bd" and link["error"] is None
    assert para["content"][5]["text"].startswith("5")


def test_numbered_equations_and_refs(conn, bar):
    out = blocks(conn, "\\begin{equation}\n\\label{eq:l}\nl = \\sqrt{x^2+y^2}\n\\end{equation}\n\n"
                       "By \\ref{eq:l} and \\ref{nope}.")
    assert out[0] == {"type": "equation", "tex": "l = \\sqrt{x^2+y^2}", "number": 1, "label": "eq:l"}
    refs = [n for n in out[1]["content"] if n["type"] == "ref"]
    assert refs[0]["text"] == "Equation 1" and refs[0]["error"] is None
    assert refs[1]["error"] == "no such label"


def test_work_and_the_calc_sections(conn, bar):
    work, given, answers, checks = blocks(conn, "\\work{bd.l}\n\n\\given\n\n\\answers\n\n\\checks")
    assert work["type"] == "work" and work["steps"] and work["result"].startswith("5")
    assert work["steps"][0]["tex"] == r"\mathrm{bd}.l = \sqrt{\mathrm{bd}.x^{2} + \mathrm{bd}.y^{2}}"
    assert all(s["tex"] is None for s in work["steps"][1:])
    assert {r["ref"] for r in given["rows"]} == {"bd.x", "bd.y"}
    assert [r["ref"] for r in answers["rows"]] == ["bd.l"]
    assert checks["heading"] == "Checks" and checks["checks"][0]["passed"]


def test_a_cited_rule_and_a_table(conn, bar):
    cited, table = blocks(conn, "\\rule{short enough}\n\n\\begin{table}\n\\caption{Sizes}\n"
                                "\\begin{tabular}\nkey & value \\\\\nx & 3\n\\end{tabular}\n\\end{table}")
    assert cited["type"] == "cited" and cited["number"] == 1 and "bd.l" in cited["equation"]
    assert cited["tex"] == r"\mathrm{bd}.l \le 10"
    assert table["type"] == "table" and table["word"] == "Table" and table["number"] == 1
    assert [c[0]["text"] for c in table["header"]] == ["key", "value"]


def test_an_include_splices_another_note(conn, bar):
    note(conn, "Included text.", name="Method")
    out = blocks(conn, "Before.\n\n\\include{Method}")
    assert [b["content"][0]["text"] for b in out] == ["Before.", "Included text."]


def test_problems_and_stats(conn, bar):
    note_id = note(conn, "")
    out = document.render(conn, note_id, "\\mote{nobody} \\val{bd.zz}")
    assert {p["kind"] for p in out["problems"]} == {"unresolved_link", "unresolved_value"}
    assert out["stats"]["links"] == 1 and out["stats"]["values"] == 1


def test_a_draft_renders_without_being_saved(conn, bar):
    note_id = note(conn, "stored")
    out = document.render(conn, note_id, "typed")
    assert out["blocks"][0]["content"][0]["text"] == "typed"
    assert get_mote(conn, note_id).properties[modoc.MODOC_KEY] == "stored"


# ---- target, save, notes ----

def test_target_names_a_mote_or_a_new_note(conn, bar):
    assert document.target(conn, "bd") == document.Target(bar, "bd")
    assert document.target(conn, f"#{bar}").mote_id == bar
    assert document.target(conn, "@sel", [bar]).mote_id == bar
    assert document.target(conn, "Homework 5") == document.Target(None, "Homework 5")
    with pytest.raises(CommandError):
        document.target(conn, "@sel", [])


def test_save_is_one_undo_step(conn):
    note_id = note(conn, "first")
    _, command = document.save(conn, note_id, "second")
    assert get_mote(conn, note_id).properties[modoc.MODOC_KEY] == "second"
    command.undo()
    assert get_mote(conn, note_id).properties[modoc.MODOC_KEY] == "first"
    command.redo()
    assert get_mote(conn, note_id).properties[modoc.MODOC_KEY] == "second"


def test_saving_a_new_name_makes_the_note(conn):
    note_id, command = document.save(conn, None, "hello", name="Homework 5")
    assert get_mote(conn, note_id).properties == {"name": "Homework 5", modoc.MODOC_KEY: "hello"}
    command.undo()
    assert get_mote(conn, note_id) is None


def test_notes_lists_and_filters(conn):
    note(conn, "\\textbf{Truss} forces.", name="Homework")
    note(conn, "Lab \\mote{nobody", name="Lab")
    create_mote(conn, {"name": "plain"})
    listed = document.notes(conn)
    assert [n["name"] for n in listed] == ["Homework", "Lab"]
    assert listed[0]["first"] == "Truss forces." and listed[0]["words"] == 2
    assert [n["name"] for n in document.notes(conn, "truss")] == ["Homework"]


def test_the_figure_memo_redraws_after_a_write(conn, bar, monkeypatch):
    calls = []
    monkeypatch.setattr(document.plotview, "figure", lambda conn, ref, motes=None: calls.append(ref) or ref)
    memo = document.GardenMemo()
    memo(conn, "A")
    memo(conn, "A")
    assert calls == ["A"]
    create_mote(conn, {"name": "moved"})
    memo(conn, "A")
    assert calls == ["A", "A"]


def test_the_memo_solves_once_until_the_garden_moves(conn, bar):
    memo = document.GardenMemo()
    first = memo.explainer(conn)
    document.render(conn, "", "\\val{bd.x}", draw=memo)
    assert memo.explainer(conn) is first
    assert memo.inferred_units(conn) is memo.inferred_units(conn)
    update_mote(conn, list_motes(conn)[0].id, {"x": 7})
    assert memo.explainer(conn) is not first


# ---- the worksheet ----

def test_applying_a_worksheet_is_one_undo_step(conn):
    applied, command = worksheet.apply_undoable(conn, SHEET)
    assert applied.rules_written == 2 and command is not None
    command.undo()
    assert list_motes(conn) == [] and list_relationships(conn) == []
    command.redo()
    assert worksheet.read(conn).splitlines()[:2] == ["bd.x = 3 m", "bd.y = 4 m"]


def test_undo_brings_back_a_deleted_rule(conn, bar):
    before = {r.from_id for r in list_relationships(conn)}
    _, command = worksheet.apply_undoable(conn, "bd.x = 3 m\nbd.y = 4 m\nbd.l = sqrt(bd.x^2 + bd.y^2)")
    assert len({r.from_id for r in list_relationships(conn)}) < len(before)
    command.undo()
    assert {r.from_id for r in list_relationships(conn)} == before


def test_an_unchanged_sheet_writes_nothing_to_undo(conn, bar):
    _, command = worksheet.apply_undoable(conn, worksheet.read(conn))
    assert command is None


def test_preview_reads_each_line(conn, bar):
    lines = worksheet.preview(conn, "bd.x = 3 m\nbd.l = ?\nbd.l = sqrt(bd.x^2)\nbd.l <= 10\n"
                                    "bd.a ?= bd.b\n\nwhat is this")
    assert [p.kind for p in lines] == ["given", "unknown", "equation", "check", "check",
                                       "blank", "blank"]
    assert lines[1].value == "5 m" and lines[2].value == "5 m"
    assert lines[6].problem


def test_preview_flags_an_ambiguous_name(conn):
    create_mote(conn, {"name": "bd"})
    create_mote(conn, {"name": "bd"})
    (line,) = worksheet.preview(conn, "bd.x = 3")
    assert "2 motes" in line.problem


# ---- snapshot undo ----

def test_since_reverses_creates_updates_and_deletes(conn):
    a = create_mote(conn, {"name": "a", "x": 1})
    b = create_mote(conn, {"name": "b"})
    create_relationship(conn, a, b, "feeds")
    before = undo.capture(conn)
    from core.motes import delete_mote, delete_property, update_mote
    update_mote(conn, a, {"x": 2, "y": 3})
    delete_property(conn, a, "name")
    delete_mote(conn, b)
    c = create_mote(conn, {"name": "c"})
    command = undo.since(conn, before, "edit")
    command.undo()
    assert undo.capture(conn) == before
    command.redo()
    assert get_mote(conn, c) is not None and get_mote(conn, b) is None
    assert get_mote(conn, a).properties == {"x": 2, "y": 3}
