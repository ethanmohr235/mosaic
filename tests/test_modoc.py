"""The .modoc language -- parsing only.

Pure strings in, dataclasses out: core/modoc.py takes no connection, and
the point of that split is that the whole grammar is testable without a
garden. What the macros *mean* is tests/test_modoc_bind.py's job.
"""
import pytest

from core import modoc
from core.modoc import (
    ABOUT,
    Equation,
    Graph,
    Heading,
    ItemList,
    Link,
    MODOC_KEY,
    Math,
    Paragraph,
    Styled,
    Text,
    Value,
    anchor,
    export_file,
    graph_key,
    graphs,
    has_modoc,
    import_file,
    links,
    math_spans,
    parse,
    parse_inline,
    read_source,
    value_key,
    values,
    word_count,
    write_source,
)
from core.motes import create_mote, get_mote
from core.relationships import create_relationship


def inline(text):
    nodes, diagnostics = parse_inline(text)
    return nodes


def kinds(document):
    return [type(b).__name__ for b in document.blocks]


# ---- blocks ----

def test_paragraphs_split_on_blank_lines():
    document = parse("one\ntwo\n\nthree")
    assert kinds(document) == ["Paragraph", "Paragraph"]
    assert document.blocks[0].content == [Text("one two")]
    assert document.blocks[1].line == 4


def test_sections_and_subsections_carry_a_level():
    document = parse("\\section{A}\n\\subsection{B}")
    assert [b.level for b in document.blocks] == [1, 2]
    assert document.blocks[0].content == [Text("A")]


def test_a_heading_may_contain_inline_macros():
    document = parse("\\section{Status of \\mote{atlas}{Atlas}}")
    assert document.blocks[0].content == [Text("Status of "), Link("atlas", "Atlas", 1)]


def test_itemize_becomes_one_block_of_items():
    document = parse("\\begin{itemize}\n\\item one\n\\item two\n\\end{itemize}")
    assert document.blocks == [ItemList([[Text("one")], [Text("two")]], 1)]


def test_a_wrapped_item_continues_the_previous_one():
    document = parse("\\begin{itemize}\n\\item one\ncontinued\n\\end{itemize}")
    assert document.blocks[0].items == [[Text("one continued")]]


def test_equation_keeps_its_source_for_the_math_reader():
    document = parse("\\begin{equation}\nC = \\alpha n\n\\end{equation}")
    assert document.blocks == [Equation("C = \\alpha n", 1)]


def test_comments_are_dropped_but_escaped_percents_survive():
    document = parse("real % not real\n100\\% sure")
    assert document.blocks[0].content == [Text("real 100% sure")]


def test_graph_is_block_level_only():
    document = parse("inline \\graph{Load curve} stays prose")
    assert graphs(document) == []
    assert isinstance(document.blocks[0], Paragraph)


def test_graph_names_a_saved_view():
    document = parse("\\graph{Load curve}\n\n\\graph{7f3c1a02}")
    assert graphs(document) == [Graph("Load curve", 1), Graph("7f3c1a02", 3)]


def test_graph_still_reads_a_name_with_spaces_and_punctuation():
    """A view is named by a person, not by a grammar. Whatever is between
    the braces is the name -- the old `... over t` / `... by status`
    shapes carried meaning here, and nothing does any more."""
    assert graphs(parse("\\graph{  Current vs. R (bench 2)  }"))[0].view == \
        "Current vs. R (bench 2)"


# ---- inline ----

def test_mote_link_with_and_without_a_label():
    assert inline("\\mote{abc}") == [Link("abc", None, 1)]
    assert inline("\\mote{abc}{Alice}") == [Link("abc", "Alice", 1)]


def test_val_takes_an_expression_and_an_optional_anchor():
    assert inline("\\val{due}") == [Value("due", None, 1)]
    assert inline("\\val[Atlas]{count(targets('a'))}") == [
        Value("count(targets('a'))", "Atlas", 1)
    ]


def test_styles_and_math():
    assert inline("\\textbf{a}\\emph{b}\\texttt{c}$d$") == [
        Styled("bold", "a"), Styled("emph", "b"), Styled("code", "c"), Math("d"),
    ]


def test_escapes_produce_literal_text_in_one_run():
    assert inline("50\\% of \\{a\\} \\$5") == [Text("50% of {a} $5")]


# ---- diagnostics ----

def test_an_unknown_command_is_reported_but_still_renders():
    document = parse("a \\bogus b")
    assert [d.kind for d in document.diagnostics] == ["unknown_command"]
    assert document.blocks[0].content == [Text("a \\bogus b")]


def test_an_unclosed_environment_is_reported_and_still_yields_its_items():
    document = parse("\\begin{itemize}\n\\item one")
    assert [d.kind for d in document.diagnostics] == ["unterminated"]
    assert document.blocks == [ItemList([[Text("one")]], 1)]


def test_a_stray_end_is_reported():
    document = parse("\\end{itemize}")
    assert [d.kind for d in document.diagnostics] == ["stray_environment"]


def test_an_empty_graph_is_reported_and_still_a_block():
    document = parse("\\graph{}")
    assert [d.kind for d in document.diagnostics] == ["bad_graph"]
    assert graphs(document) == [Graph("", 1)]


def test_a_graph_anchor_is_reported_as_the_old_grammar_it_is():
    """`\\graph[Atlas]{...}` used to say which mote to evaluate the
    expression against. A saved view names its own motes, so there is
    nothing left to anchor -- and saying so beats ignoring it."""
    document = parse("\\graph[Atlas]{Load curve}")
    assert [d.kind for d in document.diagnostics] == ["graph_anchor"]
    assert graphs(document) == [Graph("Load curve", 1)]


def test_diagnostics_carry_the_line_they_came_from():
    document = parse("fine\n\nalso fine\n\n\\bogus")
    assert document.diagnostics[0].line == 5


# ---- reading a document back ----

def test_links_and_values_are_collected_from_every_block_kind():
    document = parse(
        "\\section{\\mote{a}}\n\npara \\val{x}\n\n"
        "\\begin{itemize}\n\\item \\mote{b} \\val{y}\n\\end{itemize}"
    )
    assert [n.ref for n in links(document)] == ["a", "b"]
    assert [n.expression for n in values(document)] == ["x", "y"]


def test_identical_macros_share_a_resolution_key():
    document = parse("\\val{due} again \\val{due} but \\val[other]{due}")
    first, second, third = values(document)
    assert value_key(first) == value_key(second) != value_key(third)


def test_two_graphs_of_one_view_share_a_resolution_key():
    """The same figure twice is one sweep, not two -- see
    core/modoc_bind.py::resolve."""
    document = parse("\\graph{Load curve}\n\n\\graph{Load curve}\n\n\\graph{Other}")
    first, second, third = graphs(document)
    assert graph_key(first) == graph_key(second) != graph_key(third)


def test_word_count_counts_rendered_words_not_markup():
    document = parse("% ignored\none \\textbf{two} \\val{due} three")
    assert word_count(document) == 4


def test_word_count_counts_a_link_by_what_it_shows():
    assert word_count(parse("\\mote{abc} and \\mote{abc}{Rina Kovac}")) == 4


def test_word_count_ignores_punctuation_stranded_by_a_macro():
    assert word_count(parse("See \\mote{abc} . ; ,")) == 2


# ---- math ----

@pytest.mark.parametrize("source,expected", [
    ("\\alpha n", [("text", "\u03b1 n")]),
    ("r^{2}", [("text", "r"), ("sup", "2")]),
    ("x_i + 1", [("text", "x"), ("sub", "i"), ("text", " + 1")]),
    # A symbol name that is a prefix of a longer command must not be
    # substituted inside it: `\left(` is not `\u2264ft(`.
    ("\\left(a\\right)", [("text", "\\left(a\\right)")]),
    ("\\theta \\le \\infty", [("text", "\u03b8 \u2264 \u221e")]),
])
def test_math_spans(source, expected):
    assert [(s.kind, s.text) for s in math_spans(source)] == expected


# ---- the graph side ----

def test_has_modoc_only_counts_a_string():
    assert has_modoc({MODOC_KEY: "\\section{A}"})
    assert not has_modoc({MODOC_KEY: 3})
    assert not has_modoc({"name": "A"})


def test_read_source_is_empty_for_an_unannotated_or_missing_mote(conn):
    mote_id = create_mote(conn, {"name": "A"})
    assert read_source(conn, mote_id) == ""
    assert read_source(conn, "nope") == ""


def test_write_source_touches_only_the_modoc_property(conn):
    mote_id = create_mote(conn, {"name": "A", "status": "active"})
    write_source(conn, mote_id, "\\section{A}")
    assert get_mote(conn, mote_id).properties == {
        "name": "A", "status": "active", MODOC_KEY: "\\section{A}",
    }


def test_write_source_refuses_a_mote_that_does_not_exist(conn):
    with pytest.raises(LookupError):
        write_source(conn, "nope", "text")


def test_a_note_with_no_about_reads_from_itself(conn):
    note = create_mote(conn, {MODOC_KEY: "x"})
    assert anchor(conn, note) == (note, [])


def test_one_about_moves_the_anchor(conn):
    note = create_mote(conn, {MODOC_KEY: "x"})
    subject = create_mote(conn, {"name": "Atlas"})
    create_relationship(conn, note, subject, ABOUT)
    assert anchor(conn, note) == (subject, [])


def test_several_abouts_fall_back_to_the_note_and_say_so(conn):
    note = create_mote(conn, {MODOC_KEY: "x"})
    for name in ("Atlas", "Borealis"):
        create_relationship(conn, note, create_mote(conn, {"name": name}), ABOUT)
    anchored, diagnostics = anchor(conn, note)
    assert anchored == note
    assert [d.kind for d in diagnostics] == ["ambiguous_anchor"]


def test_export_and_import_round_trip_the_source_exactly(conn, tmp_path):
    source = "% a note\n\\section{A}\n\nDue \\val{due}.\n"
    mote_id = create_mote(conn, {"name": "A"})
    write_source(conn, mote_id, source)

    path = export_file(conn, mote_id, tmp_path / "a.modoc")
    assert path.read_text() == source
    assert read_source(conn, import_file(conn, path)) == source


def test_import_without_a_target_names_the_new_mote_after_the_file(conn, tmp_path):
    path = tmp_path / "field-spec.modoc"
    path.write_text("\\section{Spec}")
    from core.motes import get_mote as fetch

    assert fetch(conn, import_file(conn, path)).properties["name"] == "field-spec"


def test_export_refuses_a_mote_that_does_not_exist(conn, tmp_path):
    with pytest.raises(LookupError):
        export_file(conn, "nope", tmp_path / "a.modoc")


def test_a_diagnostic_inside_a_list_points_at_its_own_item(conn):
    document = parse("\\begin{itemize}\n\\item fine\n\\item \\bogus\n\\end{itemize}")
    assert [d.line for d in document.diagnostics] == [3]


# ---- the calc macros ----
#
# Grammar only: which block each line makes and how its argument reads.
# What goes *in* a section is core/calc.py's, tested there.

def test_work_takes_the_value_to_show():
    block = modoc.parse("\\work{bd.l}").blocks[0]
    assert isinstance(block, modoc.Work)
    assert block.target == "bd.l"
    assert block.format.empty


def test_work_takes_a_unit_and_figures_in_a_second_brace():
    block = modoc.parse("\\work{bd.l}{in, 4}").blocks[0]
    assert block.target == "bd.l"
    assert block.format == modoc.Format("in", 4)


def test_work_with_no_argument_is_a_diagnostic():
    document = modoc.parse("\\work{}")
    assert not document.blocks
    assert document.diagnostics[0].kind == "bad_work"


@pytest.mark.parametrize("kind", modoc.SECTION_COMMANDS)
def test_a_section_command_takes_nothing(kind):
    block = modoc.parse(f"\\{kind}").blocks[0]
    assert isinstance(block, modoc.Section)
    assert block.kind == kind
    assert block.format.empty


def test_a_sections_only_brace_is_its_format():
    """There is no other argument it could be -- what goes in a given list
    comes from the garden, not from the note."""
    block = modoc.parse("\\answers{in, 3}").blocks[0]
    assert block.format == modoc.Format("in", 3)


def test_a_second_brace_on_a_section_is_a_diagnostic():
    document = modoc.parse("\\answers{in}{3}")
    assert document.diagnostics[0].kind == "unexpected_argument"


def test_a_calc_block_breaks_the_paragraph_around_it():
    blocks = modoc.parse("before\n\\given\nafter").blocks
    assert [type(b).__name__ for b in blocks] == ["Paragraph", "Section", "Paragraph"]


def test_a_calc_command_is_not_reported_as_unknown():
    assert modoc.parse("\\given\n\\work{bd.l}").diagnostics == []


@pytest.mark.parametrize("text, expected", [
    ("in, 3", modoc.Format("in", 3)),
    ("3, in", modoc.Format("in", 3)),
    ("3", modoc.Format(None, 3)),
    ("in", modoc.Format("in", None)),
    ("m/s", modoc.Format("m/s", None)),
    ("", modoc.Format()),
    (None, modoc.Format()),
    ("  in ,  4 ", modoc.Format("in", 4)),
])
def test_parse_format_tells_a_unit_from_a_figure_count(text, expected):
    """Neither has to be labelled: there is no unit named `3` and no figure
    count spelled `in`."""
    assert modoc.parse_format(text) == expected


def test_val_takes_a_format_in_its_second_brace():
    node = modoc.parse("The bar is \\val{bd.l}{in, 4} long.").blocks[0].content[1]
    assert isinstance(node, modoc.Value)
    assert node.expression == "bd.l"
    assert node.format == modoc.Format("in", 4)


def test_val_keeps_its_ref_and_its_format_apart():
    node = modoc.parse("\\val[other]{l}{in}").blocks[0].content[0]
    assert (node.ref, node.expression, node.format) == ("other", "l", modoc.Format("in"))


def test_two_vals_asking_for_different_units_do_not_share_a_key():
    """The format is part of what a macro asks, so they must resolve
    separately."""
    a, b = modoc.values(modoc.parse("\\val{l}{in} \\val{l}{m}"))
    assert modoc.value_key(a) != modoc.value_key(b)


def test_two_identical_vals_still_share_a_key():
    a, b = modoc.values(modoc.parse("\\val{l}{in} and \\val{l}{in}"))
    assert modoc.value_key(a) == modoc.value_key(b)
