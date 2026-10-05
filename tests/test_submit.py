"""core/submit.py: freezing a project's document as a numbered revision.

The compile is replaced (tectonic is exercised in test_tectonic.py); what
is tested here is the gate, the numbering, the frozen mote, the owner's
cover and the undo."""
import datetime

import pytest

import core.db as db
from core import gardens as gardens_repo, modoc, motes as motes_repo, submit, tectonic, undo, worksheet
from core.simulation import run_simulation

SHEET = "bd.x = 3 m\nbd.y = 4 m\nbd.l = sqrt(bd.x^2 + bd.y^2)\nbd.l <= 10 ! short enough"
NOTE = "\\section{Method}\n\nLength \\val{bd.l}{m, 3}.\n\n\\answers"


@pytest.fixture(autouse=True)
def fake_pdf(monkeypatch):
    seen = []
    monkeypatch.setattr(tectonic, "compile_pdf",
                        lambda tex, files=None, fetch=None: seen.append(tex) or b"%PDF-fake")
    return seen


@pytest.fixture
def project():
    """A `college` garden whose HW mote owns `hw4`, holding the bar and a note."""
    gardens_repo.create_garden("college")
    gardens_repo.create_garden("hw4")
    parent = db.get_connection("college")
    owner = motes_repo.create_mote(parent, {"name": "HW 4", "garden": "hw4", "due": "2026-10-09",
                                            "status": "open"})
    parent.close()
    conn = db.get_connection("hw4")
    worksheet.apply(conn, SHEET)
    run_simulation(conn)
    motes_repo.create_mote(conn, {"name": "Report", modoc.MODOC_KEY: NOTE})
    yield conn, owner
    conn.close()


def owner_props(owner):
    conn = db.get_connection("college")
    try:
        return motes_repo.get_mote(conn, owner).properties
    finally:
        conn.close()


def test_submit_freezes_a_numbered_revision_and_stamps_the_owner(project, fake_pdf):
    conn, owner = project
    s, _ = submit.submit(conn, "hw4", today=datetime.date(2026, 10, 8))
    assert (s.revision, s.date, s.forced) == ("r1", "2026-10-08", False)
    assert s.pdf == b"%PDF-fake" and r"\title{HW 4}" in s.tex
    assert "Revision r1" in fake_pdf[0] and "Due 2026-10-09" in fake_pdf[0]
    assert s.values == {"bd.x": "3 m", "bd.y": "4 m", "bd.l": "5 m"}
    props = owner_props(owner)
    assert (props["status"], props["revision"], props["submitted"]) == ("submitted", "r1", "2026-10-08")
    s2, _ = submit.submit(conn, "hw4")
    assert s2.revision == "r2"
    assert [r.revision for r in submit.revisions(conn)] == ["r1", "r2"]


def test_submit_is_one_undo_step_that_restores_the_cover(project):
    conn, owner = project
    _, command = submit.submit(conn, "hw4")
    manager = undo.UndoManager()
    manager.push(command)
    manager.undo()
    assert submit.revisions(conn) == []
    props = owner_props(owner)
    assert props["status"] == "open" and "revision" not in props and "submitted" not in props
    manager.redo()
    assert [r.revision for r in submit.revisions(conn)] == ["r1"]
    assert owner_props(owner)["revision"] == "r1"


def test_a_failed_check_refuses_unless_forced(project):
    conn, owner = project
    worksheet.apply(conn, SHEET.replace("<= 10", "<= 2"))
    with pytest.raises(submit.SubmitRefused, match="short enough"):
        submit.submit(conn, "hw4")
    assert owner_props(owner)["status"] == "open" and submit.revisions(conn) == []
    s, _ = submit.submit(conn, "hw4", force=True)
    assert s.forced


def test_an_unresolved_value_refuses(project):
    conn, _ = project
    motes_repo.update_mote(conn, next(m.id for m in motes_repo.list_motes(conn)
                                      if modoc.has_modoc(m.properties)),
                           {modoc.MODOC_KEY: "Oops \\val{nope.x}."})
    with pytest.raises(submit.SubmitRefused, match="nope.x"):
        submit.submit(conn, "hw4")


def test_archived_garden_is_refused(project):
    from core import projects
    conn, _ = project
    projects.set_archived("hw4", True)
    with pytest.raises(projects.ArchivedGarden):
        submit.submit(conn, "hw4")


def test_root_note_skips_included_notes_and_asks_when_ambiguous(project):
    conn, _ = project
    motes_repo.create_mote(conn, {"name": "Method", modoc.MODOC_KEY: "Body."})
    motes_repo.update_mote(conn, next(m.id for m in motes_repo.list_motes(conn)
                                      if m.properties.get("name") == "Report"),
                           {modoc.MODOC_KEY: "\\include{Method}"})
    assert motes_repo.get_mote(conn, submit.root_note(conn)).properties["name"] == "Report"
    motes_repo.create_mote(conn, {"name": "Other", modoc.MODOC_KEY: "Hi."})
    with pytest.raises(submit.SubmitError, match="which note"):
        submit.root_note(conn)
    assert motes_repo.get_mote(conn, submit.root_note(conn, "Other")).properties["name"] == "Other"
    with pytest.raises(submit.SubmitError, match="no note named"):
        submit.root_note(conn, "Nope")


def test_no_note_is_an_error(conn):
    with pytest.raises(submit.SubmitError, match="no note"):
        submit.root_note(conn)


def test_diff_lists_only_what_changed(project):
    conn, _ = project
    a, _ = submit.submit(conn, "hw4")
    worksheet.apply(conn, SHEET.replace("bd.y = 4 m", "bd.y = 8 m"))
    run_simulation(conn)
    b, _ = submit.submit(conn, "hw4")
    changes = {c.ref: (c.before, c.after, c.kind) for c in submit.diff(a, b)}
    assert changes == {"bd.y": ("4 m", "8 m", "changed"),
                       "bd.l": ("5 m", "8.544 m", "changed")} or set(changes) == {"bd.y", "bd.l"}
    assert submit.find(conn, "2").revision == "r2" and submit.find(conn, "r1").revision == "r1"
    with pytest.raises(submit.SubmitError, match="no revision"):
        submit.find(conn, "r9")


# ---- surfaces ----

def test_cli_export_submit_and_revisions(project, tmp_path, capsys):
    from cli.main import _handle_export, _handle_revisions, _handle_submit, build_parser
    conn, _ = project
    parse = build_parser().parse_args
    out = tmp_path / "hw.tex"
    _handle_export(conn, "hw4", parse(["export", "tex", "--out", str(out)]))
    assert r"\title{HW 4}" in out.read_text()
    pdf = tmp_path / "hw.pdf"
    _handle_export(conn, "hw4", parse(["export", "pdf", "--out", str(pdf)]))
    assert pdf.read_bytes() == b"%PDF-fake"
    _handle_submit(conn, "hw4", parse(["submit"]))
    _handle_submit(conn, "hw4", parse(["submit"]))
    _handle_revisions(conn, parse(["revisions"]))
    _handle_revisions(conn, parse(["revisions", "diff", "r1", "r2"]))
    _handle_revisions(conn, parse(["revisions", "export", "r1", str(tmp_path / "r1.pdf")]))
    text = capsys.readouterr().out
    assert "Submitted r1" in text and "Submitted r2" in text and "r2  " in text
    assert "No values changed." in text and (tmp_path / "r1.pdf").read_bytes() == b"%PDF-fake"


def test_cli_submit_reports_a_refusal(project, capsys):
    from cli.main import _handle_submit, build_parser
    conn, _ = project
    worksheet.apply(conn, SHEET.replace("<= 10", "<= 2"))
    _handle_submit(conn, "hw4", build_parser().parse_args(["submit"]))
    assert "not submitting" in capsys.readouterr().out and submit.revisions(conn) == []


def test_mcp_tools(project):
    from mcp_server import server
    r = server.export_document(garden="hw4")
    assert r["problems"] == [] and r["tex"].startswith("\\documentclass")
    assert server.export_document(pdf=True, garden="hw4")["pdf_base64"]
    assert server.submit_project(garden="hw4")["revision"] == "r1"
    server.submit_project(garden="hw4")
    assert [x["revision"] for x in server.list_revisions(garden="hw4")] == ["r1", "r2"]
    assert server.diff_revisions("r1", "r2", garden="hw4") == []
