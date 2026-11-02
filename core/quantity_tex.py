# core/quantity_tex.py
"""A value's reading as TeX: what `\\val` writes into a formula.

Shared by the read pane and the `.tex` export (core/document.py fills a
formula's `\\val`s with it; core/texexport.py wraps a prose value in
`$...$`), so a value set in maths reads the same in the console and in
the PDF. No siunitx: what prints is what `calc.Quantity.text` says.
"""
import re

# Text-mode specials, as the command that prints each.
SPECIALS = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
# A `\val` inside maths that did not resolve. KaTeX in the read pane is
# allowed `\htmlClass` with this class and nothing else; the `.tex`
# preamble defines `\htmlClass` to print its body.
BROKEN_CLASS = "doc-broken"

_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
_SUPERSCRIPT_RUN_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻]+")

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
# What `calc.Quantity.text` writes: `4.00`, `4.00 in`, `4.00 ± 0.05 in`.
_QUANTITY_RE = re.compile(
    rf"^(?P<value>{_NUMBER})(?: ± (?P<sigma>{_NUMBER}))?(?: (?P<unit>\S.*))?$"
)
_SCI_RE = re.compile(r"^([-+]?[\d.]+)[eE]([-+]?\d+)$")
_UNIT_EXPONENT_RE = re.compile(r"\^(-?\d+)")


def unit_tex(unit: str) -> str:
    """A unit as the body of a `\\mathrm{}`: `m/s^2`, `N*m`, `m/s²`."""
    unit = _SUPERSCRIPT_RUN_RE.sub(lambda m: "^" + m.group(0).translate(_SUPERSCRIPTS), unit)
    unit = unit.replace("·", " ").replace("*", r"\,")
    unit = _UNIT_EXPONENT_RE.sub(r"^{\1}", unit)
    return unit.replace("%", r"\%")


def body(text: str) -> str | None:
    """A quantity reading as a maths body: `4.00 in` -> `4.00\\,\\mathrm{in}`.
    None for text that is not number-led (a date, a name)."""
    match = _QUANTITY_RE.match(text.strip())
    if match is None:
        return None
    out = _number(match.group("value"))
    if match.group("sigma"):
        out += rf"\pm {_number(match.group('sigma'))}"
    if match.group("unit"):
        out += rf"\,\mathrm{{{unit_tex(match.group('unit'))}}}"
    return out


def in_math(text: str) -> str:
    """Any value reading, to sit inside a formula: a quantity as maths,
    anything else (`2026-09-30`) as `\\text{}`."""
    return body(text) or rf"\text{{{text_escape(text)}}}"


def broken(expression: str) -> str:
    """The in-place marker for a `\\val{expression}` that did not resolve."""
    return marked(f"?{expression}")


def marked(text: str) -> str:
    """`text` set as a visible marker inside a formula: red in the pane,
    plain text in the PDF. Also what core/latex_vocab.py puts in place of
    TeX the PDF cannot set."""
    return rf"\htmlClass{{{BROKEN_CLASS}}}{{\text{{{text_escape(text)}}}}}"


def text_escape(text: str) -> str:
    return "".join(SPECIALS.get(ch, ch) for ch in text)


def _number(text: str) -> str:
    sci = _SCI_RE.match(text)
    if sci:
        return rf"{sci.group(1)}\times 10^{{{int(sci.group(2))}}}"
    return text
