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

    container = create_mote(conn, {"name": "Container", "freedoms": "pressure"})
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


# ---- species / folders / portals / library ----
#
# The charter (docs/architecture.md, "What belongs on which surface")
# says the CLI gets everything scriptable. These existed only in the app
# or only in MCP before.

def test_species_template_and_new_instance(run_repl, conn):
    species = create_mote(conn, {"name": "Resistor"})
    template = create_mote(conn, {"name": "R", "resistance": 100})

    out = run_repl([
        f"species template id={template} species={species}",
        f"species new species={species} name=R1",
        "species list",
    ])

    assert "is now the template for" in out
    assert "Instance created with id=" in out
    assert species in out

    instances = [m for m in list_motes(conn) if m.properties.get("name") == "R1"]
    assert instances and instances[0].properties["resistance"] == 100, "should inherit the template"


def test_species_set_of_and_instances(run_repl, conn):
    species = create_mote(conn, {"name": "Person"})
    alice = create_mote(conn, {"name": "Alice"})

    out = run_repl([
        f"species set id={alice} species={species}",
        f"species of id={alice}",
        f"species instances id={species}",
    ])

    assert "is now an instance of" in out
    assert out.count(species) >= 1 and out.count(alice) >= 1


def test_species_requires_its_arguments(run_repl, conn):
    assert "'species' is required" in run_repl(["species set id=abc"])


def test_folder_set_members_and_of(run_repl, conn):
    folder = create_mote(conn, {"name": "Thermodynamics"})
    note = create_mote(conn, {"name": "Entropy"})

    out = run_repl([
        f"folder set id={note} folder={folder}",
        "folder list",
        f"folder members id={folder}",
        f"folder of id={note}",
    ])

    assert "filed under" in out
    assert out.count(folder) >= 2 and out.count(note) >= 1
    assert list_relationships(conn)[0].type == "in_folder"


def test_mote_search_finds_by_text_and_by_key_value(run_repl, conn):
    create_mote(conn, {"name": "Alice", "role": "engineer"})
    create_mote(conn, {"name": "Bob", "role": "doctor"})

    assert "Alice" in run_repl(["mote search engineer"])
    assert "Bob" not in run_repl(["mote search engineer"])
    assert "Alice" in run_repl(["mote search role = engineer"])


def test_library_save_list_and_use(run_repl, conn):
    mote = create_mote(conn, {"name": "Standup", "duration": 15})

    saved = run_repl([f"library save id={mote}"])
    entry_id = saved.split("id=")[-1].split()[0]

    listed = run_repl(["library list"])
    assert "Standup" in listed and entry_id in listed

    run_repl([f"library use id={entry_id} name=Monday"])
    copies = [m for m in list_motes(conn) if m.properties.get("name") == "Monday"]
    assert copies and copies[0].properties["duration"] == 15


def test_portal_create_and_resolve(run_repl, conn):
    create_garden("elsewhere")
    from core.db import get_connection, initialize_database
    other = get_connection("elsewhere")
    try:
        initialize_database(other)
        target = create_mote(other, {"name": "Reactor"})
    finally:
        other.close()

    created = run_repl([f"portal create garden=elsewhere mote={target}"])
    assert "Portal created" in created
    portal_id = created.split("id=")[-1].split()[0]

    resolved = run_repl([f"portal resolve id={portal_id}"])
    assert "Garden: elsewhere" in resolved
    assert "Reactor" in resolved


def test_portal_create_requires_a_target(run_repl, conn):
    assert "'mote' is required" in run_repl(["portal create garden=elsewhere"])
