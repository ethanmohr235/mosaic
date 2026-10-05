"""core/planner.py: plan my week, worked, and planned vs. actual.

Dates are fixed. NOW is Monday 2026-10-05 08:00; work hours 9-17, so a day
holds 8 hours and a 2 h block."""
from datetime import datetime, timedelta

import pytest

from core import interval, planner, undo
from core.command_bar import CommandError
from core.motes import create_mote, get_mote, list_motes, update_mote
from core.relationships import list_relationships

NOW = datetime(2026, 10, 5, 8, 0)


def work(conn, name, estimate, due, **extra):
    return create_mote(conn, {"name": name, "estimate": estimate, "due": due, **extra})


def blocks(conn):
    return sorted((m for m in list_motes(conn) if planner.is_block(m)),
                  key=lambda m: m.properties["when"])


def span(mote):
    found = interval.parse(mote.properties["when"])
    return found.start, found.end


def run(conn, *args, now=NOW):
    return planner.run_verb(conn, "plan", args, now)


def test_durations():
    assert planner.parse_duration(3) == timedelta(hours=3)
    assert planner.parse_duration("1h30m") == timedelta(minutes=90)
    assert planner.parse_duration("90m") == timedelta(minutes=90)
    assert planner.parse_duration("2.5") == timedelta(hours=2.5)
    assert planner.parse_duration("PT45M") == timedelta(minutes=45)
    assert planner.parse_duration("soon") is None and planner.parse_duration(0) is None
    assert planner.parse_duration(True) is None


def test_plan_is_a_preview_until_applied(conn):
    work(conn, "HW4", 3, "2026-10-09")
    message, wrote, command = run(conn)
    assert "would place" in message and "HW4" in message and "`plan --apply`" in message
    assert wrote == 0 and command is None and blocks(conn) == []


def test_blocks_are_split_and_placed_earliest_first(conn):
    hw = work(conn, "HW4", 3, "2026-10-09")
    message, wrote, command = run(conn, "--apply")
    made = blocks(conn)
    assert [(span(b)[0].hour, (span(b)[1] - span(b)[0]).seconds // 3600) for b in made] == [(9, 2), (11, 1)]
    assert [span(b)[0].day for b in made] == [5, 5]            # a 2 h block, then the last hour after it
    assert all(b.properties["kind"] == "planned" for b in made)
    assert {r.to_id for r in list_relationships(conn, type_="for")} == {hw}
    assert message.startswith("placed 2 blocks (3h)")


def test_earliest_deadline_goes_first_and_busy_time_is_respected(conn):
    work(conn, "Later", 2, "2026-10-12")
    work(conn, "Sooner", 2, "2026-10-06")
    create_mote(conn, {"name": "Lecture", "starts": "2026-10-05T09:00/PT2H"})
    run(conn, "--apply")
    starts = {b.properties["name"]: span(b)[0] for b in blocks(conn)}
    assert starts["Work: Sooner"] == datetime(2026, 10, 5, 11, 0)   # after the lecture
    assert starts["Work: Later"] == datetime(2026, 10, 5, 13, 0)    # after Sooner


def test_a_block_never_ends_after_the_due(conn):
    work(conn, "Quick", 2, "2026-10-05T10:00")     # due at 10:00 -- an hour after work starts
    message, _, _ = run(conn, "--apply")
    assert blocks(conn) == [] or all(span(b)[1] <= datetime(2026, 10, 5, 10, 0) for b in blocks(conn))
    assert "short 1h: Quick" in message


def test_what_does_not_fit_is_reported_short(conn):
    work(conn, "Huge", 20, "2026-10-06")            # two days of 8 h = 16 h
    message, _, _ = run(conn, "--apply")
    assert "short 4h: Huge (due 2026-10-06)" in message
    assert sum((span(b)[1] - span(b)[0] for b in blocks(conn)), timedelta()) == timedelta(hours=16)


def test_skipped_motes_are_listed_never_guessed(conn):
    create_mote(conn, {"name": "Exam", "due": "2026-10-08"})
    work(conn, "Old", 2, "2026-10-01")
    work(conn, "Done", 2, "2026-10-08", status="done")
    work(conn, "Submitted", 2, "2026-10-08", status="submitted")
    message, wrote, _ = run(conn, "--apply")
    assert "Exam (no estimate)" in message and "Old (overdue)" in message
    assert "Done" not in message and "Submitted" not in message and wrote == 0


def test_replanning_does_not_double_book(conn):
    work(conn, "HW4", 3, "2026-10-09")
    run(conn, "--apply")
    again, wrote, _ = run(conn, "--apply")
    assert wrote == 0 and "nothing to place" in again and len(blocks(conn)) == 2


def test_apply_is_one_undo_step_and_makes_the_species_once(conn):
    work(conn, "HW4", 3, "2026-10-09")
    work(conn, "HW5", 2, "2026-10-10")
    _, _, command = run(conn, "--apply")
    species = [m for m in list_motes(conn) if m.properties.get("name") == "Work block"]
    assert len(species) == 1
    manager = undo.UndoManager()
    manager.push(command)
    manager.undo()
    assert blocks(conn) == [] and not [m for m in list_motes(conn) if m.properties.get("name") == "Work block"]
    assert list_relationships(conn, type_="for") == []
    manager.redo()
    assert len(blocks(conn)) == 3


def test_options(conn):
    work(conn, "HW4", 4, "2026-10-09")
    run(conn, "--apply", "--block", "1h", "--hours", "13-17", "--from", "2026-10-06")
    made = blocks(conn)
    assert len(made) == 4 and all(span(b)[1] - span(b)[0] == timedelta(hours=1) for b in made)
    assert min(span(b)[0] for b in made) == datetime(2026, 10, 6, 13, 0)
    for bad in (["--days", "0"], ["--hours", "17-9"], ["--block", "5m"], ["--nope"], ["--from", "x"], ["--days"]):
        with pytest.raises(CommandError):
            run(conn, *bad)


def test_today_starts_after_now(conn):
    work(conn, "HW4", 2, "2026-10-09")
    run(conn, "--apply", now=datetime(2026, 10, 5, 13, 10))
    assert span(blocks(conn)[0])[0] == datetime(2026, 10, 5, 13, 30)


def test_worked_logs_an_actual_block_and_budget_adds_up(conn):
    hw = work(conn, "HW4", 5, "2026-10-09")
    message, wrote, command = planner.run_verb(conn, "worked", ("HW4", "90m"), NOW)
    assert "logged 1.5h on HW4" in message and wrote == 1
    (block,) = blocks(conn)
    assert block.properties["kind"] == "actual" and span(block)[1] == NOW
    run(conn, "--apply")                            # plans only what is left: 3.5 h
    (row,) = planner.report(conn, now=NOW)
    assert (row.name, row.estimate, row.done) == ("HW4", timedelta(hours=5), timedelta(minutes=90))
    assert row.planned == timedelta(hours=3.5) and row.left == timedelta(0)
    assert "est 5h" in row.text() and "done 1.5h" in row.text() and "planned 3.5h" in row.text()
    # marking a planned block done moves its hours from planned to done
    planned = next(b for b in blocks(conn) if b.properties["kind"] == "planned")
    update_mote(conn, planned.id, {"status": "done"})
    (row,) = planner.report(conn, now=NOW)
    assert row.done > timedelta(minutes=90) and row.planned < timedelta(hours=3.5)
    assert planner.run_verb(conn, "budget", ("HW4",), NOW)[0].startswith("HW4: est 5h")


def test_worked_on_a_date_and_errors(conn):
    work(conn, "HW4", 5, "2026-10-09")
    planner.run_verb(conn, "worked", ("HW4", "2h", "2026-10-02"), NOW)
    assert span(blocks(conn)[0])[0] == datetime(2026, 10, 2, 9, 0)
    for bad in (("HW4",), ("HW4", "lots"), ("Nope", "1h"), ("HW4", "1h", "never")):
        with pytest.raises(CommandError):
            planner.run_verb(conn, "worked", bad, NOW)
    assert planner.run_verb(conn, "budget", (), NOW)[0].startswith("HW4:")


def test_budget_with_nothing(conn):
    assert planner.run_verb(conn, "budget", (), NOW)[0] == "nothing estimated or logged yet"


def test_console_verbs_and_follow(conn):
    from console.api import Api
    hw = work(conn, "HW4", 2, "2026-10-30")
    api = Api("test")
    try:
        assert "would place" in api.run("plan")["message"]
        done = api.run("plan --apply")
        assert done["ok"] and done["wrote"]
        assert "planned 2h" in api.run("budget")["message"]
        assert api.run("worked HW4 30m")["wrote"]
        assert api.run("worked HW4 lots")["effect"] == "error"
        block = next(m for m in list_motes(conn) if planner.is_block(m))
        assert api.monastery.follow(block.id)["command"] == f"open #{hw}"
        api.run("undo")
        api.run("undo")
        assert blocks(conn) == []
    finally:
        api.shutdown()


def test_cli_and_mcp(conn, capsys):
    from cli.main import _handle_planner, build_parser
    from mcp_server import server
    work(conn, "HW4", 2, "2030-10-09")
    parse = build_parser().parse_args
    _handle_planner(conn, parse(["plan"]))
    _handle_planner(conn, parse(["worked", "HW4", "30m"]))
    _handle_planner(conn, parse(["budget"]))
    _handle_planner(conn, parse(["worked", "Nope", "30m"]))
    out = capsys.readouterr().out
    assert "would place" in out and "logged 0.5h" in out and "HW4: est 2h" in out and "Error:" in out
    assert server.plan_week(garden="test")["applied"] is False
    applied = server.plan_week(apply=True, garden="test")
    assert applied["written"] >= 1
    assert server.time_budget("HW4", garden="test")[0]["estimate"] == 2.0
    assert server.log_time("HW4", "1h", garden="test")["logged"].startswith("logged 1h")
