# core/latex_vocab.py
"""The maths a note may use: what KaTeX draws in the read pane and what
LaTeX compiles in the PDF, as one list (docs/modoc.md -> Maths).

KaTeX takes far more than plain LaTeX does -- `\\infin`, `\\R`, `\\mathbb`,
a Unicode `∞` -- so a note that read fine in the console could fail to
compile, or compile with a symbol silently missing. Two halves close
that gap:

- **Widen the export.** `preamble()` loads the packages KaTeX's commands
  come from and defines KaTeX's own aliases (`SHIMS`); `to_latex()`
  rewrites what LaTeX spells differently (a Unicode symbol, `align*`
  inside an equation, a `#hex` colour).
- **Flag the rest.** Whatever is still outside the list -- a command
  neither side shares, a typo, a symbol with no LaTeX name, an
  environment that numbers its own lines -- is a problem (`check()`)
  and is marked where it stands (`mark()`), in the pane and in the PDF
  alike, the way an unresolved `\\val` is.

The list itself is generated, not typed: `scripts/latex_vocab.py` reads
the vendored KaTeX, asks tectonic which of its names are defined under
`preamble()`, and writes `core/data/latex_math.json`. Rerun it when
KaTeX or the preamble changes; a test fails until you do.
"""
import functools
import json
import re
from dataclasses import dataclass
from pathlib import Path

from core import modoc
from core import quantity_tex

DATA_PATH = Path(__file__).parent / "data" / "latex_math.json"

# (package, options). TeX packages tectonic fetches from its bundle, not
# Python dependencies. `normalem` keeps `\emph` italic under ulem.
PACKAGES = (
    ("amssymb", ""), ("mathtools", ""), ("amscd", ""), ("cancel", ""), ("bm", ""),
    ("xcolor", ""), ("mathrsfs", ""), ("braket", ""), ("extarrows", ""),
    ("ulem", "normalem"), ("esint", ""), ("arydshln", ""),
)

# KaTeX's own aliases (its macro table), as LaTeX. Commands KaTeX draws
# that no package defines and that would take real work to imitate are
# left out, and so flagged.
_ALIASES = {
    "infin": r"\infty", "plusmn": r"\pm", "sdot": r"\cdot", "bull": r"\bullet",
    "isin": r"\in", "exist": r"\exists", "notni": r"\not\ni",
    "sub": r"\subset", "sube": r"\subseteq", "supe": r"\supseteq",
    "N": r"\mathbb{N}", "R": r"\mathbb{R}", "Z": r"\mathbb{Z}",
    "natnums": r"\mathbb{N}", "reals": r"\mathbb{R}", "Reals": r"\mathbb{R}",
    "cnums": r"\mathbb{C}", "Complex": r"\mathbb{C}",
    "alef": r"\aleph", "alefsym": r"\aleph", "image": r"\Im", "real": r"\Re",
    "weierp": r"\wp", "thetasym": r"\vartheta", "omicron": "o",
    "Alpha": r"\mathrm{A}", "Beta": r"\mathrm{B}", "Chi": r"\mathrm{X}",
    "Epsilon": r"\mathrm{E}", "Eta": r"\mathrm{H}", "Iota": r"\mathrm{I}",
    "Kappa": r"\mathrm{K}", "Mu": r"\mathrm{M}", "Nu": r"\mathrm{N}",
    "Omicron": r"\mathrm{O}", "Rho": r"\mathrm{P}", "Tau": r"\mathrm{T}",
    "Zeta": r"\mathrm{Z}",
    "larr": r"\leftarrow", "rarr": r"\rightarrow", "uarr": r"\uparrow",
    "darr": r"\downarrow", "harr": r"\leftrightarrow", "lrarr": r"\leftrightarrow",
    "lArr": r"\Leftarrow", "Larr": r"\Leftarrow", "rArr": r"\Rightarrow",
    "Rarr": r"\Rightarrow", "uArr": r"\Uparrow", "Uarr": r"\Uparrow",
    "dArr": r"\Downarrow", "Darr": r"\Downarrow", "hArr": r"\Leftrightarrow",
    "Harr": r"\Leftrightarrow", "lrArr": r"\Leftrightarrow", "Lrarr": r"\Leftrightarrow",
    "lang": r"\langle", "rang": r"\rangle", "Dagger": r"\ddagger", "sect": r"\S",
    "clubs": r"\clubsuit", "diamonds": r"\diamondsuit", "hearts": r"\heartsuit",
    "spades": r"\spadesuit", "degree": r"{^\circ}", "gt": ">", "lt": "<",
    "dotsx": r"\ldots\,", "DOTSI": r"\relax", "DOTSB": r"\relax", "DOTSX": r"\relax",
    "ratio": r"\vcentcolon", "coloncolon": r"\dblcolon", "colonequals": r"\coloneqq",
    "coloncolonequals": r"\Coloneqq", "equalscolon": r"\eqqcolon",
    "equalscoloncolon": r"\Eqqcolon", "colonminus": r"\coloneq",
    "coloncolonminus": r"\Coloneq", "minuscolon": r"\eqcolon",
    "minuscoloncolon": r"\Eqcolon", "coloncolonapprox": r"\Colonapprox",
    "coloncolonsim": r"\Colonsim",
    "simcolon": r"\mathrel{\sim\mathrel{\mkern-1.2mu}\vcentcolon}",
    "simcoloncolon": r"\mathrel{\sim\mathrel{\mkern-1.2mu}\dblcolon}",
    "approxcolon": r"\mathrel{\approx\mathrel{\mkern-1.2mu}\vcentcolon}",
    "approxcoloncolon": r"\mathrel{\approx\mathrel{\mkern-1.2mu}\dblcolon}",
    "llbracket": r"\mathopen{[\mkern-3.2mu[}", "rrbracket": r"\mathclose{]\mkern-3.2mu]}",
    "lBrace": r"\mathopen{\{\mkern-3.2mu[}", "rBrace": r"\mathclose{]\mkern-3.2mu\}}",
    "argmin": r"\operatorname*{arg\,min}", "argmax": r"\operatorname*{arg\,max}",
    "plim": r"\operatorname*{plim}",
    "arctg": r"\operatorname{arctg}", "arcctg": r"\operatorname{arcctg}",
    "tg": r"\operatorname{tg}", "ctg": r"\operatorname{ctg}", "cotg": r"\operatorname{cotg}",
    "cosec": r"\operatorname{cosec}", "sh": r"\operatorname{sh}", "ch": r"\operatorname{ch}",
    "cth": r"\operatorname{cth}",
    "KaTeX": r"\text{KaTeX}",
}

# KaTeX's colour commands, `\blueA{x}`: name -> HTML colour.
_COLOURS = {
    "blue": "6495ED", "orange": "FFA500", "pink": "FF00AF", "red": "DF0030",
    "green": "28AE7B", "gray": "808080", "purple": "9D38BD",
    "blueA": "CCFAFF", "blueB": "80F6FF", "blueC": "63D9EA", "blueD": "11ACCD", "blueE": "0C7F99",
    "tealA": "94FFF5", "tealB": "26EDD5", "tealC": "01D1C1", "tealD": "01A995", "tealE": "208170",
    "greenA": "B6FFB0", "greenB": "8AF281", "greenC": "74CF70", "greenD": "1FAB54", "greenE": "0D923F",
    "goldA": "FFD0A9", "goldB": "FFBB71", "goldC": "FF9C39", "goldD": "E07D10", "goldE": "A75A05",
    "redA": "FCA9A9", "redB": "FF8482", "redC": "F9685D", "redD": "E84D39", "redE": "BC2612",
    "maroonA": "FFBDE0", "maroonB": "FF92C6", "maroonC": "ED5FA6", "maroonD": "CA337C",
    "maroonE": "9E034E", "purpleA": "DDD7FF", "purpleB": "C6B9FC", "purpleC": "AA87FF",
    "purpleD": "7854AB", "purpleE": "543B78", "mintA": "F5F9E8", "mintB": "EDF2DF",
    "mintC": "E0E5CC", "grayA": "F6F7F7", "grayB": "F0F1F2", "grayC": "E3E5E6",
    "grayD": "D6D8DA", "grayE": "BABEC2", "grayF": "888D93", "grayG": "626569",
    "grayH": "3B3E40", "grayI": "21242C", "kaBlue": "314453", "kaGreen": "71B307",
}

SHIMS = {**_ALIASES, **{name: f"[1]{{\\textcolor[HTML]{{{hex_}}}{{#1}}}}"
                        for name, hex_ in _COLOURS.items()}}

# KaTeX draws these, and the export could define them, but a note should
# not carry them: the pane does not trust links or raw HTML hooks.
# `\htmlClass` is the one the `\val` marker uses, and only core writes it.
DENIED = frozenset({"href", "url", "htmlClass", "htmlId", "htmlStyle", "htmlData",
                    "includegraphics"})

# xcolor's base names: what `\color{...}` may name besides a `#hex`.
XCOLOR_NAMES = frozenset({
    "red", "green", "blue", "cyan", "magenta", "yellow", "black", "gray", "white",
    "darkgray", "lightgray", "brown", "lime", "olive", "orange", "pink", "purple",
    "teal", "violet",
})

# Environments, inside an equation or `$...$`.
INNER_ENVIRONMENTS = frozenset({
    "aligned", "alignedat", "gathered", "split", "cases", "dcases", "rcases", "drcases",
    "matrix", "pmatrix", "bmatrix", "Bmatrix", "vmatrix", "Vmatrix", "matrix*",
    "pmatrix*", "bmatrix*", "Bmatrix*", "vmatrix*", "Vmatrix*", "smallmatrix",
    "subarray", "array", "CD",
})
# Same drawing, LaTeX's name for it: rewritten on export, nothing to flag.
_RENAMED_ENVIRONMENTS = {
    "align*": "aligned", "gather*": "gathered", "alignat*": "alignedat",
    "equation*": "gathered", "darray": "array",
}
# KaTeX numbers each line of these, and inside an equation LaTeX refuses
# them. Flagged, and set as the unnumbered form so a draft still compiles.
_NUMBERED_ENVIRONMENTS = {
    "align": "aligned", "gather": "gathered", "alignat": "alignedat", "equation": "gathered",
}

# Symbols KaTeX has no entry for that a value's unit can still put into a
# formula (`\mathrm{°C}`, `\mathrm{µm}`), and a middle dot typed for a
# product. The generator adds them to the map.
UNICODE_EXTRAS = {"°": r"{^\circ}", "µ": r"\mu", "·": r"\cdot"}

# Commands whose `{...}` argument is text, not maths: a Unicode letter
# there is the text font's business.
_TEXT_COMMANDS = frozenset({
    "text", "textrm", "textsf", "texttt", "textnormal", "textbf", "textmd", "textit",
    "textup", "textsl", "emph", "mbox", "hbox",
})
_COLOUR_COMMANDS = frozenset({"color", "textcolor", "colorbox", "fcolorbox"})

_TOKEN_RE = re.compile(
    r"\\(?P<env>begin|end)\s*\{(?P<envname>[^{}]*)\}"
    r"|\\(?P<name>[a-zA-Z]+)"
    r"|\\[^a-zA-Z]"
    r"|(?P<open>\{)|(?P<close>\})"
    r"|(?P<char>[^\x00-\x7f])"
)
_COLOUR_ARG_RE = re.compile(r"\s*\{(?P<colour>[^{}]*)\}")
_HEX_RE = re.compile(r"#(?P<hex>[0-9a-fA-F]{6}|[0-9a-fA-F]{3})")


@dataclass(frozen=True)
class Vocabulary:
    """The generated list. `commands` is what both sides take;
    `katex_only` what KaTeX draws and the export cannot; `unicode` a
    symbol KaTeX reads as a command, to that command (or ASCII)."""
    katex: str
    commands: frozenset[str]
    katex_only: frozenset[str]
    unicode: dict[str, str]


@functools.cache
def vocabulary() -> Vocabulary:
    data = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    return Vocabulary(data["katex"], frozenset(data["commands"]),
                      frozenset(data["katex_only"]), dict(data["unicode"]))


def preamble() -> str:
    """The `\\usepackage` and `\\providecommand` lines the export adds."""
    lines = [f"\\usepackage[{opts}]{{{name}}}" if opts else f"\\usepackage{{{name}}}"
             for name, opts in PACKAGES]
    lines.append("% KaTeX's aliases (core/latex_vocab.py::SHIMS).")
    for name, body in SHIMS.items():
        lines.append(f"\\providecommand{{\\{name}}}{body}" if body.startswith("[")
                     else f"\\providecommand{{\\{name}}}{{{body}}}")
    return "\n".join(lines) + "\n"


# --- reading a formula -------------------------------------------------

@dataclass(frozen=True)
class _Edit:
    """One place in a formula. A `problem` is flagged and marked in both
    the pane and the PDF; a `translation` is the export's alone (KaTeX
    reads the original fine). `message` is None for the second half of a
    problem already reported (an environment's `\\end`)."""
    start: int
    end: int
    problem: bool
    replacement: str
    message: str | None = None


def check(tex: str, line: int) -> list[modoc.Diagnostic]:
    """What in a formula the PDF cannot set, as the note's problems."""
    return [modoc.Diagnostic(line, "not_latex", e.message)
            for e in _scan(tex) if e.problem and e.message]


def mark(tex: str) -> str:
    """The formula with each problem replaced by a visible marker -- what
    the pane typesets and the export writes."""
    return _apply(tex, [e for e in _scan(tex) if e.problem])


def to_latex(tex: str) -> str:
    """A (marked) formula as LaTeX spells it: Unicode symbols as their
    commands, KaTeX's environment names as LaTeX's, `#hex` colours."""
    return _apply(tex, [e for e in _scan(tex) if not e.problem])


def symbol(ch: str) -> str | None:
    """A Unicode symbol as the maths command for it, or None."""
    found = vocabulary().unicode.get(ch)
    return None if found is None else _spaced(found)


def _apply(tex: str, edits: list[_Edit]) -> str:
    out, position = [], 0
    for e in edits:
        out.append(tex[position:e.start])
        out.append(e.replacement)
        position = e.end
    out.append(tex[position:])
    return "".join(out)


def _scan(tex: str) -> list[_Edit]:
    """Every edit, in order. `\\val{...}` spans are core's, not TeX, and
    are stepped over."""
    skip = modoc.math_value_spans(tex)
    edits: list[_Edit] = []
    groups: list[bool] = []  # per open brace: is it a text argument?
    text_next = False
    for m in _TOKEN_RE.finditer(tex):
        if any(start <= m.start() < end for start, end in skip):
            continue
        in_text = bool(groups) and groups[-1]
        if m.group("open"):
            groups.append(text_next or in_text)
            text_next = False
            continue
        if m.group("close"):
            if groups:
                groups.pop()
            continue
        text_next = False
        if m.group("env"):
            edits.extend(_environment(m))
        elif m.group("name"):
            name = m.group("name")
            text_next = name in _TEXT_COMMANDS
            edits.extend(_command(tex, m, name))
        elif m.group("char"):
            edits.extend(_character(m, in_text))
    return _without_overlaps(edits)


def _command(tex: str, m: re.Match, name: str) -> list[_Edit]:
    if name in _COLOUR_COMMANDS:
        return _colour(tex, m)
    if name in vocabulary().commands:
        return []
    return [_Edit(m.start(), m.end(), True, quantity_tex.marked(f"\\{name}"),
                  _command_message(name))]


def _command_message(name: str) -> str:
    if name in DENIED:
        return f"\\{name}: links and HTML hooks don't export; write the text"
    if name in vocabulary().katex_only:
        return f"\\{name}: KaTeX draws it, but LaTeX has no such command"
    return f"\\{name}: unknown command"


def _colour(tex: str, m: re.Match) -> list[_Edit]:
    """`\\color{name}`: a `#hex` is rewritten, a name xcolor lacks flagged.
    A `\\color[model]{...}` is LaTeX's own spelling and passes."""
    arg = _COLOUR_ARG_RE.match(tex, m.end())
    if arg is None:
        return []
    colour = arg.group("colour").strip()
    hex_ = _HEX_RE.fullmatch(colour)
    if hex_:
        code = hex_.group("hex").upper()
        code = "".join(c * 2 for c in code) if len(code) == 3 else code
        return [_Edit(m.start(), arg.end(), False, f"{m.group(0)}[HTML]{{{code}}}")]
    if colour in XCOLOR_NAMES:
        return []
    return [_Edit(m.start(), arg.end(), True, quantity_tex.marked(tex[m.start():arg.end()]),
                  f"{m.group(0)}{{{colour}}}: the PDF knows only #hex and "
                  f"{', '.join(sorted(XCOLOR_NAMES))}")]


def _environment(m: re.Match) -> list[_Edit]:
    which, name = m.group("env"), m.group("envname").strip()
    if name in INNER_ENVIRONMENTS:
        return []
    if name in _RENAMED_ENVIRONMENTS:
        return [_Edit(m.start(), m.end(), False, f"\\{which}{{{_RENAMED_ENVIRONMENTS[name]}}}")]
    if name in _NUMBERED_ENVIRONMENTS:
        inner = _NUMBERED_ENVIRONMENTS[name]
        if which == "end":
            return [_Edit(m.start(), m.end(), True, f"\\end{{{inner}}}")]
        return [_Edit(m.start(), m.end(), True,
                      quantity_tex.marked(m.group(0)) + f"\\begin{{{inner}}}",
                      f"\\begin{{{name}}}: KaTeX numbers each line and LaTeX refuses it "
                      f"inside an equation; use {inner}")]
    return [_Edit(m.start(), m.end(), True, quantity_tex.marked(m.group(0)),
                  f"\\begin{{{name}}}: not an environment a formula can use"
                  if which == "begin" else None)]


def _character(m: re.Match, in_text: bool) -> list[_Edit]:
    """A Unicode symbol: its command, or flagged. Inside a text argument
    the text font takes what it has (`\\text{é}`), and a symbol becomes
    `\\ensuremath`, as in prose."""
    ch = m.group("char")
    found = vocabulary().unicode.get(ch)
    if found is not None:
        command = f"\\ensuremath{{{found}}}" if in_text else _spaced(found)
        return [_Edit(m.start(), m.end(), False, command)]
    if in_text:
        return []
    code = f"U+{ord(ch):04X}"
    return [_Edit(m.start(), m.end(), True, quantity_tex.marked(code),
                  f"'{ch}' ({code}) in maths: LaTeX has no command for it; write the command")]


def _spaced(command: str) -> str:
    """A command followed by a space, so `∞x` does not become `\\inftyx`."""
    return command + " " if command.startswith("\\") else command


def _without_overlaps(edits: list[_Edit]) -> list[_Edit]:
    """A colour's argument is read past its command; a token inside that
    span must not be edited twice."""
    kept: list[_Edit] = []
    for e in sorted(edits, key=lambda e: e.start):
        if kept and e.start < kept[-1].end:
            continue
        kept.append(e)
    return kept
