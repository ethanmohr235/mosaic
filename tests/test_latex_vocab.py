"""core/latex_vocab.py: the maths a note may use, the same in the read
pane (KaTeX) and the PDF (LaTeX). The real compiles run only where
tectonic does, like test_tectonic.py's."""
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

from core import document, latex_vocab, modoc, tectonic, texexport
from core.motes import create_mote

ROOT = Path(__file__).resolve().parent.parent
needs_tectonic = pytest.mark.skipif(
    shutil.which("tectonic") is None and not os.environ.get("MONOSS_TECTONIC"),
    reason="needs tectonic and network")


def generator():
    spec = importlib.util.spec_from_file_location("latex_vocab_script", ROOT / "scripts" / "latex_vocab.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def messages(tex):
    return [d.message for d in latex_vocab.check(tex, 7)]


# ---- what is flagged ----

def test_shared_commands_and_katex_aliases_pass():
    assert messages(r"k_\infin \le \mathbb{R} + \frac{a}{b} \cancel{x} \bm{v} \R") == []


def test_a_command_the_pdf_lacks_is_flagged_with_its_line():
    (found,) = latex_vocab.check(r"\href{x}{y}", 7)
    assert (found.line, found.kind) == (7, "not_latex") and "\\href" in found.message
    assert messages(r"\fracc{1}{2}") == [r"\fracc: unknown command"]
    assert "KaTeX draws it" in messages(r"\widecheck{x}")[0]


def test_a_val_is_not_read_as_tex():
    assert messages(r"x = \val{bd.l}{m, 3} + \val[Atlas]{team}") == []


def test_unicode_in_maths_needs_a_command():
    assert messages("k_∞ ≤ α · 2 °") == []
    (message,) = messages("x + ☃")
    assert "U+2603" in message


def test_text_arguments_belong_to_the_text_font():
    assert messages(r"\text{é ☃} + \textbf{ü}") == []


def test_environments():
    assert messages(r"\begin{aligned}a&=b\end{aligned} \begin{pmatrix}1\end{pmatrix}") == []
    assert messages(r"\begin{align*}a&=b\end{align*}") == []
    (message,) = messages(r"\begin{align}a&=b\\c&=d\end{align}")
    assert "aligned" in message
    assert len(messages(r"\begin{foo}x\end{foo}")) == 1


def test_colours():
    assert messages(r"\color{red}{x} + \textcolor{#ff0000}{y} + \color[HTML]{FF0000}{z}") == []
    assert "crimson" in messages(r"\color{crimson}{x}")[0]


# ---- what the pane and the export get ----

def test_mark_puts_a_marker_where_the_problem_was():
    assert latex_vocab.mark(r"a + \href{x}{y}") == (
        r"a + \htmlClass{doc-broken}{\text{\textbackslash{}href}}{x}{y}")
    assert latex_vocab.mark("x + ☃") == r"x + \htmlClass{doc-broken}{\text{U+2603}}"


def test_a_numbered_environment_is_marked_and_set_unnumbered():
    marked = latex_vocab.mark(r"\begin{align}a&=b\end{align}")
    assert marked.endswith(r"\begin{aligned}a&=b\end{aligned}")
    assert marked.startswith(r"\htmlClass{doc-broken}")


def test_to_latex_spells_what_katex_reads():
    assert latex_vocab.to_latex("k_∞ ≤ x") == r"k_\infty  \leq  x"
    assert latex_vocab.to_latex(r"\text{a ∞}") == r"\text{a \ensuremath{\infty}}"
    assert latex_vocab.to_latex(r"\begin{gather*}a\end{gather*}") == r"\begin{gathered}a\end{gathered}"
    assert latex_vocab.to_latex(r"\color{#f80}{x}") == r"\color[HTML]{FF8800}{x}"
    assert latex_vocab.to_latex(r"\href{x}{y}") == r"\href{x}{y}"  # problems are mark()'s


def test_preamble_loads_the_packages_and_the_shims():
    pre = latex_vocab.preamble()
    assert r"\usepackage{amssymb}" in pre and r"\usepackage[normalem]{ulem}" in pre
    assert r"\providecommand{\infin}{\infty}" in pre
    assert r"\providecommand{\blueA}[1]{\textcolor[HTML]{CCFAFF}{#1}}" in pre


# ---- through a note ----

def test_a_note_reports_and_marks_it(conn):
    note = create_mote(conn, {"name": "HW", modoc.MODOC_KEY: ""})
    out = document.render(conn, note, "Intro.\n\nWe get $k_\\infin + \\href{x}{y}$.")
    assert [(p["line"], p["kind"]) for p in out["problems"]] == [(3, "not_latex")]
    (math,) = [n for n in out["blocks"][1]["content"] if n["type"] == "math"]
    assert r"k_\infin" in math["tex"] and r"\htmlClass{doc-broken}" in math["tex"]


# ---- the generated list ----

def test_the_list_matches_the_vendored_katex():
    script = generator()
    js = script.KATEX_JS.read_text(encoding="utf-8")
    vocab = latex_vocab.vocabulary()
    assert vocab.katex == script._VERSION_RE.search(js).group("version")
    assert vocab.commands <= script.katex_names(js) | set(latex_vocab.SHIMS)
    assert not vocab.commands & latex_vocab.DENIED
    assert set(latex_vocab.SHIMS) <= vocab.commands


@needs_tectonic
def test_the_list_is_current():
    data = json.loads(latex_vocab.DATA_PATH.read_text(encoding="utf-8"))
    assert generator().generate() == data, "run scripts/latex_vocab.py"


@needs_tectonic
def test_every_shim_compiles_under_the_export_preamble():
    uses = [rf"\{name}{{x}}" if body.startswith("[") else rf"\{name}"
            for name, body in latex_vocab.SHIMS.items()]
    tex = (texexport._PREAMBLE + "$" + " ".join(f"{{{u}}}" for u in uses) + "$\n"
           + texexport.escape("∞ ≤ α") + "\n\\end{document}\n")
    assert tectonic.compile_pdf(tex).startswith(b"%PDF")
