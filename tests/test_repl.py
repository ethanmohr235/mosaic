"""The interactive front end, driven the way a person drives it.

cli/main.py and cli/repl.py are two ways to type the same commands, and
they now share cli/output.py for everything about how a result is
printed -- so what's worth testing here is the half that is genuinely
different: line parsing, the `id=`/`from=`/`to=` argument style, and the
current-garden state a one-shot argparse run doesn't have.
"""
import pytest

from cli.repl import parse_properties, repl
from core.gardens import create_garden, garden_exists
from core.motes import create_mote, get_mote, list_motes
from core.relationships import list_relationships
from core.rules import APPLIES_TO
from core.species import set_species


@pytest.fixture
def run_repl(monkeypatch, capsys):
    """Feed the REPL a script of lines and hand back everything it
    printed. 'exit' is appended so the loop always terminates -- a test
    that hangs waiting on input() is worse than one that fails."""
    def run(lines: list[str], garden: str = "test") -> str:
        if not garden_exists(garden):
            create_garden(garden)
        script = iter([*lines, "exit"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(script))
        repl(garden)
        return capsys.readouterr().out
    return run


# ---- line parsing ----

def test_parse_properties_decodes_json_typed_values():
    props = parse_properties(["age=21", "active=true", "tags=[1,2]", "name=Ethan"])
    assert props == {"age": 21, "active": True, "tags": [1, 2], "name": "Ethan"}


def test_parse_properties_keeps_an_unparseable_value_as_a_string():
    assert parse_properties(["phone=12a45"]) == {"phone": "12a45"}


def test_parse_properties_ignores_a_token_with_no_equals():
    assert parse_properties(["stray", "key=value"]) == {"key": "value"}


# ---- motes ----

def test_mote_create_and_list_round_trip(run_repl, conn):
    out = run_repl(['mote create name=Alice age=30'])
    assert "Mote created with id=" in out

    motes = list_motes(conn)
    assert len(motes) == 1
    # age=30 arrives as an int, not the string "30" -- the REPL's own
    # JSON-where-possible parsing, unlike argparse's --properties JSON.
    assert motes[0].properties == {"name": "Alice", "age": 30}


def test_mote_get_reports_a_missing_mote_without_raising(run_repl, conn):
    assert "No mote found." in run_repl(["mote get id=nonexistent"])


def test_mote_get_requires_an_id(run_repl, conn):
    assert "Error: 'id' is required" in run_repl(["mote get"])


def test_mote_update_sets_properties(run_repl, conn):
    mote_id = create_mote(conn, {"name": "Alice"})
    run_repl([f"mote update id={mote_id} age=31"])
    assert get_mote(conn, mote_id).properties == {"name": "Alice", "age": 31}


def test_mote_update_rejects_an_id_with_no_properties(run_repl, conn):
    mote_id = create_mote(conn, {"name": "Alice"})
    assert "Error: no properties provided" in run_repl([f"mote update id={mote_id}"])


def test_mote_delete_removes_it(run_repl, conn):
    mote_id = create_mote(conn, {"name": "Alice"})
    assert "Mote deleted" in run_repl([f"mote delete id={mote_id}"])
    assert get_mote(conn, mote_id) is None


# ---- relationships ----

def test_rel_create_and_list(run_repl, conn):
    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Reactor"})

    out = run_repl([f"rel create from={a} to={b} type=works_on", "rel list"])

    assert "Relationship created" in out
    rels = list_relationships(conn)
    assert [(r.from_id, r.to_id, r.type) for r in rels] == [(a, b, "works_on")]


def test_rel_create_requires_from_to_and_type(run_repl, conn):
    a = create_mote(conn, {"name": "Alice"})
    out = run_repl([f"rel create from={a} type=works_on"])
    assert "Error: 'from', 'to', and 'type' are required" in out
    assert list_relationships(conn) == []


def test_rel_update_retypes_a_relationship(run_repl, conn):
    a = create_mote(conn, {"name": "Alice"})
    b = create_mote(conn, {"name": "Reactor"})
    run_repl([
        f"rel create from={a} to={b} type=works_on",
        "rel list",
    ])
    rel_id = list_relationships(conn)[0].id

    run_repl([f"rel update id={rel_id} type=leads"])
    assert list_relationships(conn)[0].type == "leads"


# ---- gardens ----

def test_garden_use_switches_the_prompt_and_the_connection(run_repl, conn):
    create_mote(conn, {"name": "InTest"})
    create_garden("other")

    out = run_repl(["garden use other", "mote list"])

    assert "Switched to garden: other" in out
    # The listing is of `other`, which is empty -- not of `test`.
    assert "InTest" not in out


def test_garden_use_reports_a_missing_garden(run_repl, conn):
    assert "No such garden: 'nope'" in run_repl(["garden use nope"])


def test_garden_create_and_delete_and_restore(run_repl, conn):
    out = run_repl([
        "garden create throwaway",
        "garden delete throwaway",
        "garden restore throwaway",
    ])
    assert "Garden created: throwaway" in out
    assert "Garden deleted: throwaway" in out
    assert "Garden restored: throwaway" in out
    assert garden_exists("throwaway")


# ---- shared output with cli/main.py ----

def _wire_solvable_container(conn):
    """The same smallest solvable garden tests/test_cli.py builds, so the
    two front ends are exercised against identical data."""
    air = create_mote(conn, {"name": "Air"})
    model = create_mote(
        conn, {"name": "Ideal Air", "provider": "ideal_gas", "configuration": {"gas_constant": 287.05}}
    )
    from core.relationships import create_relationship
    create_relationship(conn, air, model, "uses_model")

    container = create_mote(conn, {"name": "Container", "unknowns": "pressure"})
    rule = create_mote(
        conn,
        {"name": "Ideal gas law", "equation": "pressure == property('gas', 'pressure', density, temperature)"},
    )
    create_relationship(conn, rule, container, APPLIES_TO)
    c1 = create_mote(conn, {"name": "C1", "density": 1.2, "temperature": 293.15})
    set_species(conn, c1, container)
    create_relationship(conn, c1, air, "gas")
    return c1


def test_simulate_runs_and_prints_changes(run_repl, conn):
    c1 = _wire_solvable_container(conn)
    out = run_repl(["simulate"])
    assert "Solved 1 value" in out
    assert c1[:8] in out and "pressure:" in out


def test_simulate_history_then_show_prints_the_same_run(run_repl, conn):
    _wire_solvable_container(conn)
    history = run_repl(["simulate", "simulate history"])
    run_id = [line for line in history.splitlines() if line.startswith("[")][0].split("]")[0].lstrip("[")

    out = run_repl([f"simulate show id={run_id}"])
    assert f"Run {run_id}" in out
    assert "status=ok" in out
    assert "applied to graph" in out


def test_model_list_and_show_match_the_argparse_front_end(run_repl, conn):
    _wire_solvable_container(conn)

    out = run_repl(["model list", "model show id=" + _model_id(conn)])
    assert "Ideal Air (provider=ideal_gas)" in out
    assert "Model: Ideal Air" in out
    assert "pressure(density, temperature) -> pressure" in out


def test_model_show_requires_an_id(run_repl, conn):
    assert "Error: 'id' is required" in run_repl(["model show"])


def _model_id(conn) -> str:
    return next(m.id for m in list_motes(conn) if m.properties.get("provider") == "ideal_gas")


# ---- loop behavior ----

def test_unknown_command_does_not_end_the_session(run_repl, conn):
    out = run_repl(["frobnicate", "mote create name=Alice"])
    assert "Unknown command: frobnicate" in out
    assert "Mote created with id=" in out


def test_an_error_is_reported_and_the_loop_continues(run_repl, conn):
    # An unparseable line (unbalanced quote) raises inside shlex, which
    # the loop has to survive rather than exit on.
    out = run_repl(['mote create name="unclosed', "mote create name=Bob"])
    assert "Error:" in out
    assert "Mote created with id=" in out


def test_help_lists_every_command_group(run_repl, conn):
    out = run_repl(["help"])
    for group in ("garden", "mote", "rel", "model", "simulate"):
        assert group in out
