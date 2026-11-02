"""core/texexport.py: a note's resolved blocks as a frozen `.tex`.

Same garden as test_document.py: a bar with x = 3 m, y = 4 m, l = 5 m."""
import base64

import pytest

from core import modoc, texexport, worksheet
from core.motes import create_mote, list_motes
from core.projects import Cover
from core.simulation import run_simulation

SHEET = "bd.x = 3 m\nbd.y = 4 m\nbd.l = sqrt(bd.x^2 + bd.y^2)\nbd.l <= 10 ! short enough"


@pytest.fixture
def bar(conn):
    worksheet.apply(conn, SHEET)
    run_simulation(conn)


def export(conn, source, cover=None):
    note = create_mote(conn, {"name": "HW", modoc.MODOC_KEY: ""})
    return texexport.export(conn, note, source=source, cover=cover)


def test_escape_and_symbols():
    assert texexport.escape("50% of a_b & #1") == r"50\% of a\_b \& \#1"
    assert texexport.escape("ΣFx") == r"\ensuremath{\Sigma}Fx"


def test_quantity_is_maths_with_a_thin_space():
    assert texexport.quantity("4.00 in") == r"$4.00\,\mathrm{in}$"
    assert texexport.quantity("5") == "$5$"
    assert texexport.quantity("1.5e-05 m/s²") == r"$1.5\times 10^{-5}\,\mathrm{m/s^{2}}$"
    assert texexport.quantity("2 ± 0.5 m") == r"$2\pm 0.5\,\mathrm{m}$"
    assert texexport.quantity("n/a") == "n/a"


def test_document_has_title_block_from_the_cover(conn, bar):
    cover = Cover(name="Homework 4", due="2026-10-09", author="Ethan", revision="r2")
    out = export(conn, "\\section{Method}\n\nHello.", cover)
    assert r"\title{Homework 4}" in out.tex and r"\author{Ethan}" in out.tex
    assert r"\date{Due 2026-10-09, Revision r2}" in out.tex
    assert r"\section*{Method}" in out.tex and out.problems == ()


def test_values_are_frozen_and_equations_numbered(conn, bar):
    out = export(conn, "Length \\val{bd.l}{m, 3}.\n\n"
                       "\\begin{equation}\nl = \\sqrt{x^2+y^2}\n\\end{equation}")
    assert r"Length $5.00\,\mathrm{m}$." in out.tex
    assert "\\begin{equation}\nl = \\sqrt{x^2+y^2}\n\\end{equation}" in out.tex


def test_a_line_break_in_an_equation_survives(conn, bar):
    out = export(conn, "\\begin{equation}\na = b \\newline c = d \\\\ e\n\\end{equation}")
    assert ("\\begin{equation}\n\\begin{gathered}\na = b \\\\ c = d \\\\ e\n"
            "\\end{gathered}\n\\end{equation}") in out.tex


def test_breaks_inside_an_inner_environment_are_left_alone(conn, bar):
    out = export(conn, "\\begin{equation}\n\\begin{aligned}\na &= b \\\\\nc &= d\n"
                       "\\end{aligned}\n\\end{equation}")
    assert ("\\begin{equation}\n\\begin{aligned} a &= b \\\\ c &= d \\end{aligned}\n"
            "\\end{equation}") in out.tex
    assert "gathered" not in out.tex


def test_bad_value_is_marked_and_reported(conn, bar):
    out = export(conn, "Oops \\val{nope.x}.")
    assert "[nope.x: error]" in out.tex
    assert any("nope.x" in p for p in out.problems)


def test_a_val_in_maths_is_frozen_into_it(conn, bar):
    out = export(conn, "The answer is $\\boxed{\\val{bd.l}{m, 3}}$.")
    assert r"The answer is $\boxed{5.00\,\mathrm{m}}$." in out.tex and out.problems == ()


def test_a_bad_value_in_maths_is_marked_and_reported(conn, bar):
    out = export(conn, "$x = \\val{nope.x}$")
    assert r"\providecommand{\htmlClass}[2]{#2}" in out.tex
    assert r"$x = \htmlClass{doc-broken}{\text{?nope.x}}$" in out.tex
    assert any("nope.x" in p for p in out.problems)


def test_the_preamble_takes_what_katex_draws(conn, bar):
    out = export(conn, "$k_\\infin \\in \\mathbb{R}$")
    assert r"\usepackage{amssymb}" in out.tex and r"\providecommand{\infin}{\infty}" in out.tex
    assert r"$k_\infin \in \mathbb{R}$" in out.tex and out.problems == ()


def test_unicode_and_katex_spellings_are_written_as_latex(conn, bar):
    out = export(conn, "Then ∞ wins: $k_∞ ≤ 1$\n\n"
                       "\\begin{equation}\n\\begin{align*}\na &= b\n\\end{align*}\n\\end{equation}")
    assert r"Then \ensuremath{\infty} wins: $k_\infty  \leq  1$" in out.tex
    assert r"\begin{aligned} a &= b \end{aligned}" in out.tex and out.problems == ()


def test_what_the_pdf_cannot_set_is_marked_and_reported(conn, bar):
    out = export(conn, "Intro.\n\n$x + \\href{a}{b}$")
    assert r"$x + \htmlClass{doc-broken}{\text{\textbackslash{}href}}{a}{b}$" in out.tex
    assert out.problems == ("line 3: \\href: links and HTML hooks don't export; write the text",)


def test_calc_sections_and_work(conn, bar):
    out = export(conn, "\\work{bd.l}\n\n\\given\n\n\\answers\n\n\\checks")
    assert r"\subsection*{" in out.tex and "bd.l" in out.tex
    assert r"$3\,\mathrm{m}$" in out.tex
    assert r"$\mathrm{bd}.l = \sqrt{\mathrm{bd}.x^{2} + \mathrm{bd}.y^{2}}$" in out.tex
    assert out.problems == ()


def test_a_cited_rule_is_typeset(conn, bar):
    out = export(conn, "\\rule{short enough}")
    assert "\\begin{equation}\n\\mathrm{bd}.l \\le 10\n\\end{equation}" in out.tex


def test_list_table_and_caption(conn, bar):
    out = export(conn, "\\begin{itemize}\n\\item one\n\\item two\n\\end{itemize}\n\n"
                       "\\begin{tabular}\na & b\n1 & 2\n\\end{tabular}")
    assert "\\begin{itemize}\n\\item one\n\\item two\n\\end{itemize}" in out.tex
    assert "\\begin{tabular}{ll}" in out.tex and "a & b" in out.tex


def test_png_image_is_written_as_a_file(conn, bar):
    from core import images
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGP4z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg==")
    images.add(conn, png, "diagram")
    out = export(conn, "\\begin{figure}\n\\image{diagram}{width=50}\n\\caption{A diagram}\n\\end{figure}")
    assert out.files["images/diagram.png"] == png
    assert r"\includegraphics[width=0.5\linewidth,keepaspectratio]{images/diagram.png}" in out.tex
    assert r"\caption{A diagram}" in out.tex


def test_missing_image_is_a_visible_problem(conn, bar):
    out = export(conn, "\\image{ghost}")
    assert "[image ghost" in out.tex and out.problems


def test_sweep_graph_becomes_pgfplots():
    fig = {"mode": "sweep", "error": None, "xLabel": "x [m]", "yLabel": "l", "yScale": "linear",
           "bands": [{"label": "length", "unit": "m", "series": ["a"]}],
           "series": [{"id": "a", "label": "l", "unit": "m", "band": 0, "ghost": [],
                       "points": [[0, 1], None, [2, 3]]}]}
    tex = texexport.sweep_axes(fig)
    assert "\\begin{axis}" in tex and "0 1 \\\\" in tex and "nan nan \\\\" in tex
    assert "ylabel={length [m]}" in tex and "\\addlegendentry{l}" in tex


def test_unit_already_in_the_label_is_not_repeated():
    fig = {"mode": "sweep", "error": None, "xLabel": "x", "yLabel": "l (m)", "yScale": "log",
           "bands": [{"label": "l (m)", "unit": "m", "series": ["a"]}],
           "series": [{"id": "a", "label": "l", "unit": "m", "band": 0, "ghost": [],
                       "points": [[1, 2]]}]}
    tex = texexport.sweep_axes(fig)
    assert "ylabel={l (m)}" in tex and "ymode=log" in tex
