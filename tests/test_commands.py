"""core/commands.py -- the console's merged grammar: verb-first
precedence, the quote/#id escape, `@sel`, completion and the
reserved-name warning."""
import pytest

from core import commands
from core import motes as motes_repo
from core.command_bar import CommandError
from core.commands import APP, STATEMENT, VERB, complete, expand_sel, parse
from core.reserved import RESERVED_VERBS, reserved_words


# ---- parse ----------------------------------------------------------------

def test_a_verb_first_token_is_a_verb():
    parsed = parse("layout save two")
    assert (parsed.kind, parsed.head, parsed.args) == (VERB, "layout", ("save", "two"))


def test_mo_and_a_suffix_is_an_app():
    parsed = parse("  mo tion  ")
    assert (parsed.kind, parsed.head, parsed.args, parsed.text) == (APP, "tion", (), "mo tion")
    assert parse("mo").head == ""


def test_a_bare_suffix_is_an_ordinary_name():
    assert parse("tion").kind == STATEMENT


def test_a_verb_wins_over_a_mote_of_the_same_name():
    # verb-first: `log -> R1` is the log verb with arguments, not a
    # relationship, even though it would parse as one.
    assert parse("log -> R1").kind == VERB


@pytest.mark.parametrize("line", ['"log" -> R1', "'log'", "#abc123 -> R1", '"tion".x = 1'])
def test_a_quoted_first_token_or_an_id_is_never_a_verb(line):
    parsed = parse(line)
    assert parsed.kind == STATEMENT
    assert parsed.text == line


@pytest.mark.parametrize("line", ["R1.x = 5 ohm", "log.x = 1", "Log", "cdx", "R1 -> R2", ""])
def test_everything_else_is_a_statement(line):
    # `log.x` is one token that isn't a reserved word; matching is exact
    # and case-sensitive, so `Log` is a mote name.
    assert parse(line).kind == STATEMENT


def test_verb_arguments_tokenize_like_a_shell():
    assert parse('cd "my garden"').args == ("my garden",)
    with pytest.raises(CommandError):
        parse('cd "unclosed')


def test_reserved_words_are_the_verbs_and_mo():
    assert reserved_words() == set(RESERVED_VERBS)
    assert list(RESERVED_VERBS) == [
        "mo", "find", "set", "open", "rename", "unlink", "template",
        "plot", "fit", "sim", "rule", "scenarios", "solve", "why", "step", "play", "pause",
        "edit", "read", "notes", "worksheet", "cd", "layout", "refresh", "undo", "redo", "log"]


def test_resolve_view():
    assert commands.resolve_view("") == ("", "")
    assert commands.resolve_view("log") == ("log", "log")
    with pytest.raises(CommandError):
        commands.resolve_view("tion")  # not ported yet: no view answers it
    with pytest.raises(CommandError):
        commands.resolve_view("R1.x = 1")


# ---- @sel -----------------------------------------------------------------

def test_expand_sel_uses_the_primary():
    assert expand_sel("@sel.x = 5", ["aaa", "bbb"]) == "#aaa.x = 5"
    assert expand_sel("R1 -> @sel", ["aaa"]) == "R1 -> #aaa"


def test_expand_sel_leaves_quotes_and_longer_words_alone():
    assert expand_sel('R1.note = "@sel" @selection', ["aaa"]) == 'R1.note = "@sel" @selection'


def test_expand_sel_with_an_empty_selection_is_an_error():
    with pytest.raises(CommandError, match="@sel"):
        expand_sel("@sel.x = 1", [])
    assert expand_sel("R1.x = 1", []) == "R1.x = 1"


# ---- the reserved-name warning --------------------------------------------

def test_creating_a_mote_named_like_a_verb_warns(conn):
    result = commands.run_statement(conn, '"log".x = 1')
    assert result.ok and result.created
    assert '"log" (quoted)' in result.message
    assert f"#{result.mote_id[:8]}" in result.message


def test_creating_a_mote_named_like_a_verb_warns_but_a_suffix_does_not(conn):
    assert "console word" in commands.run_statement(conn, "'mo'").message
    assert "console word" not in commands.run_statement(conn, "tion").message


def test_ordinary_or_existing_motes_get_no_warning(conn):
    assert "console word" not in commands.run_statement(conn, "R1.x = 1").message
    commands.run_statement(conn, '"log"')
    # Found, not created: already warned when it was made.
    assert "console word" not in commands.run_statement(conn, '"log".y = 2').message


# ---- completion -------------------------------------------------------------

def _values(completion):
    return [row.value for row in completion.rows]


def test_first_token_lists_verbs(conn):
    result = complete(conn, "r")
    assert [r.value for r in result.rows] == ["rename", "rule", "read", "refresh", "redo"]
    assert (result.ghost, result.replace_from) == ("ename", 0)


def test_after_mo_lists_matching_apps_then_dim_apps(conn):
    result = complete(conn, "mo r")
    rows = result.rows
    assert [r.value for r in rows if not r.dim] == ["ra"]
    dim = [r.value for r in rows if r.dim]
    assert dim == ["nolith", "te", "xie", "tion", "nastery", "narch", "saic"]
    assert rows[0].description == "Mora — mora:edit"
    assert result.hint == "mo ‹app›"
    assert (result.ghost, result.replace_from) == ("a", 3)


def test_an_app_you_can_open_outranks_one_that_is_not_available(conn, monkeypatch):
    from core import commands as commands_module
    monkeypatch.setattr(commands_module, "CONSOLE_APPS", (
        commands_module.ConsoleApp("te", "Inspector"),
        commands_module.ConsoleApp("tion", "Motion", default_view="motion:state"),
    ))
    result = complete(conn, "mo t")
    assert [r.value for r in result.rows if not r.dim][:2] == ["tion", "te"]
    assert result.ghost == "ion"


def test_an_empty_line_lists_every_verb_and_mo_lists_every_app(conn):
    result = complete(conn, "")
    assert _values(result) == list(RESERVED_VERBS)
    assert result.ghost == ""
    apps = [a.suffix for a in commands.CONSOLE_APPS if a.default_view or a.legacy]
    apps += [a.suffix for a in commands.CONSOLE_APPS if not (a.default_view or a.legacy)]
    assert _values(complete(conn, "mo ")) == apps


def test_first_token_also_offers_mote_names_but_not_reserved_ones(conn):
    motes_repo.create_mote(conn, {"name": "rotor"})
    motes_repo.create_mote(conn, {"name": "redo"})
    values = [r.value for r in complete(conn, "ro").rows if not r.dim]
    assert values == ["rotor"]
    assert "redo" not in [r.value for r in complete(conn, "re").rows if not r.description]


def test_a_dotted_or_capitalized_first_token_completes_as_a_statement(conn):
    motes_repo.create_mote(conn, {"name": "R1", "resistance": 5})
    result = complete(conn, "R1.re")
    assert _values(result) == ["resistance"]
    assert (result.hint, result.ghost, result.replace_from) == ("", "sistance", 3)

    result = complete(conn, "R")
    assert _values(result) == ["R1"]
    assert result.replace_from == 0


def test_statement_completion_mid_line_uses_the_cursor(conn):
    motes_repo.create_mote(conn, {"name": "Pump"})
    line = "R1 -> Pu = x"
    result = complete(conn, line, cursor=8)
    assert _values(result) == ["Pump"]
    assert (result.ghost, result.replace_from) == ("mp", 6)


def test_cd_completes_garden_names(conn):
    result = complete(conn, "cd wo", gardens=["home", "work", "world"], current_garden="work")
    assert _values(result) == ["work", "world"]
    assert [r.current for r in result.rows] == [True, False]
    assert (result.hint, result.ghost, result.replace_from) == ("cd ‹garden›", "rk", 3)


def test_layout_completes_ops_then_names(conn):
    ops = complete(conn, "layout ", layouts=["two", "three"])
    assert _values(ops) == ["save", "load", "list", "rm"]
    assert ops.hint == "layout save|load|list|rm ‹name›"
    assert ops.replace_from == 7

    names = complete(conn, "layout load t", layouts=["two", "three", "one"])
    assert _values(names) == ["two", "three"]
    assert (names.ghost, names.replace_from) == ("wo", 12)

    assert _values(complete(conn, "layout list ", layouts=["two"])) == []


def test_nothing_completes_after_an_app_suffix_or_argumentless_verb(conn):
    assert complete(conn, "tion ").rows == ()
    result = complete(conn, "undo ")
    assert result.rows == () and result.hint == "undo"


# ---- U2: find and set --------------------------------------------------------

def test_find_and_set_resolve_and_complete(conn):
    from core import motes as motes_repo
    motes_repo.create_mote(conn, {"name": "a", "status": "open"})
    motes_repo.create_mote(conn, {"name": "b", "status": "done"})
    assert commands.resolve_view("find status:open") == ("monolith:table", "find status:open")

    def values(line):
        return [row.value for row in commands.complete(conn, line).rows]

    assert values("find st") == ["status:"]
    assert values("find status:o") == ["status:open"]
    assert values("find --") == ["--sort", "--group", "--cols", "--view"]
    assert "species" not in values("find --sort ")  # it would read back as `type`
    assert values("set ") == ["@sel", "a", "b"]
    assert values("set @sel st") == ["status"]
    assert values("set @sel status d") == ["done"]


def test_u3_verbs_complete_targets_then_their_arguments(conn):
    from core import motes as motes_repo
    from core.relationships import create_relationship
    a = motes_repo.create_mote(conn, {"name": "a", "status": "open"})
    b = motes_repo.create_mote(conn, {"name": "b"})
    create_relationship(conn, a, b, "blocks")

    def values(line):
        return [row.value for row in commands.complete(conn, line).rows]

    assert values("open ") == ["@sel", "a", "b"]
    assert values("rename a st") == ["status"]
    assert values("unlink a ") == ["blocks"]
    assert values("unlink a blocks ") == ["a", "b"]
    assert values("template ") == ["a", "b"]
    assert commands.resolve_view("open @sel") == ("mote:props", "open @sel")
