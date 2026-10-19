"""A rule's equation as LaTeX (core/formula_tex.py)."""
import pytest

from core import formula, formula_tex
from core.formula_tex import to_tex


@pytest.mark.parametrize("equation, tex", [
    ("delta == P * L**3 / (48 * E * I)", r"\delta = \frac{P\,L^{3}}{48\,E\,I}"),
    ("x == (a + b) * (c - d)", r"x = \left(a + b\right)\,\left(c - d\right)"),
    ("y == a - (b - c)", r"y = a - \left(b - c\right)"),
    ("y == a - b + c", r"y = a - b + c"),
    ("y == a + -b", r"y = a + \left(-b\right)"),
    ("q == -(a + b)", r"q = -\left(a + b\right)"),
    ("z == (a / b)**2", r"z = \left(\frac{a}{b}\right)^{2}"),
    ("s == sqrt(x^2 + y^2)", r"s = \sqrt{x^{2} + y^{2}}"),
    ("w == x * 2", r"w = x \cdot 2"),
    ("sigma_max == M * c / I", r"\sigma_{\mathrm{max}} = \frac{M\,c}{I}"),
    ("x_max >= 2 * R1", r"x_{\mathrm{max}} \ge 2\,R_{1}"),
    ("v == voltage", r"v = \mathit{voltage}"),
    ("v == bd.l * targets('load').F", r"v = \mathrm{bd}.l \cdot \overrightarrow{\mathrm{load}}.F"),
    # Words and dotted references are dotted, so the names do not run together.
    ("flux == density * n.v", r"\mathit{flux} = \mathit{density} \cdot \mathrm{n}.v"),
    ("xs == Gn * Gy", r"\mathit{xs} = \mathit{Gn} \cdot \mathit{Gy}"),
    ("k == sum(sources('spring').k)", r"k = \sum \overleftarrow{\mathrm{spring}}.k"),
    ("f == 1e6 * x", r"f = 10^{6}\,x"),
    ("f == 2.5e-7 * x", r"f = 2.5 \times 10^{-7}\,x"),
    ("c == 2 * pi * r", r"c = 2\,\pi\,r"),
    ("n == prev(n) * 1.5", r"n = \operatorname{prev}\left(n\right) \cdot 1.5"),
    ("h == property('fluid', 'enthalpy', p, T)",
     r"h = \operatorname{enthalpy}_{\mathrm{fluid}}\left(p, T\right)"),
    ("m == max(a, b)", r"m = \max\left(a, b\right)"),
    ("y == log10(x) + ln(x)", r"y = \log_{10}\left(x\right) + \ln\left(x\right)"),
])
def test_equations(equation, tex):
    assert to_tex(equation) == tex


def test_a_check_is_drawn_with_a_question_mark():
    assert to_tex("a == b", check=True) == r"a \stackrel{?}{=} b"
    assert to_tex("a <= b", check=True) == r"a \le b"


def test_a_bare_expression():
    assert to_tex("a / b") == r"\frac{a}{b}"


@pytest.mark.parametrize("bad", ["a ===", "import os", "a == 'text'", "0 <= x <= 1"])
def test_what_cannot_be_drawn_is_none(bad):
    assert to_tex(bad) is None


def test_a_function_mote_call_is_set_as_a_function():
    assert to_tex("y == phi(0)") == r"y = \phi\left(0\right)"
    assert to_tex("y == flux(r, 2)") == r"y = \mathit{flux}\left(r, 2\right)"


def test_every_whitelisted_call_has_a_rendering():
    """core/formula.py's whitelist decides what a formula may call; a call
    added there without a rendering here would quietly fall back to text."""
    assert set(formula_tex.CALL_TEX) == set(formula.ALLOWED_CALLS)
