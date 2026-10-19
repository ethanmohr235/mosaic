# core/texexport.py
"""A note as a self-contained `.tex` file, every value frozen.

The source is `core/document.py::render` -- the same resolved blocks the
read pane draws -- so the PDF and the pane cannot disagree about what a
note says. This module only *writes*: it never reads the garden, never
solves, and never raises on a bad macro. A value that did not resolve is
printed as a visible marker in the text and returned in `problems`, which
is what Submit refuses to freeze over.

Layout decisions (roadmap.md -> *Output*, decided 2026-10-05):

- `article`, 11pt, 1in margins; the cover fields form the title block.
- A quantity is `$4.00\\,\\mathrm{in}$`: no siunitx, so what prints is what
  the console prints.
- A plot is a pgfplots `axis` with its solved points inline, one axis per
  unit band. Vector, no Python plotting dependency.
- A cross-reference is frozen as its text ("Equation 2"), like a value.
  LaTeX numbers equations, figures and tables by itself, in the same
  document order the note's own numbering uses.
"""
import base64
import re
import sqlite3
from dataclasses import dataclass, field

from core import document as document_repo
from core.projects import Cover

IMAGE_DIR = "images"

_SPECIALS = {
    "\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#",
    "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
# Characters Latin Modern text fonts lack, as the math command that draws
# them. Greek is how a note names a stress or an angle.
_SYMBOLS = {
    "±": r"\ensuremath{\pm}", "×": r"\ensuremath{\times}", "·": r"\ensuremath{\cdot}",
    "−": r"\ensuremath{-}", "≈": r"\ensuremath{\approx}", "≤": r"\ensuremath{\leq}",
    "≥": r"\ensuremath{\geq}", "°": r"\ensuremath{^\circ}", "√": r"\ensuremath{\sqrt{}}",
    "Σ": r"\ensuremath{\Sigma}", "Δ": r"\ensuremath{\Delta}", "Ω": r"\ensuremath{\Omega}",
    "α": r"\ensuremath{\alpha}", "β": r"\ensuremath{\beta}", "γ": r"\ensuremath{\gamma}",
    "δ": r"\ensuremath{\delta}", "ε": r"\ensuremath{\varepsilon}", "θ": r"\ensuremath{\theta}",
    "λ": r"\ensuremath{\lambda}", "μ": r"\ensuremath{\mu}", "ν": r"\ensuremath{\nu}",
    "π": r"\ensuremath{\pi}", "ρ": r"\ensuremath{\rho}", "σ": r"\ensuremath{\sigma}",
    "τ": r"\ensuremath{\tau}", "φ": r"\ensuremath{\varphi}", "ω": r"\ensuremath{\omega}",
    "²": r"\ensuremath{^2}", "³": r"\ensuremath{^3}", "⁻": r"\ensuremath{^-}",
    "¹": r"\ensuremath{^1}", "→": r"\ensuremath{\to}",
}
_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻", "0123456789-")
_SUPERSCRIPT_RUN_RE = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻]+")

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
# What `calc.Quantity.text` writes: `4.00`, `4.00 in`, `4.00 ± 0.05 in`.
_QUANTITY_RE = re.compile(
    rf"^(?P<value>{_NUMBER})(?: ± (?P<sigma>{_NUMBER}))?(?: (?P<unit>\S.*))?$"
)
_SCI_RE = re.compile(r"^([-+]?[\d.]+)[eE]([-+]?\d+)$")
_UNIT_EXPONENT_RE = re.compile(r"\^(-?\d+)")


@dataclass(frozen=True)
class TexBundle:
    """What an export produced: the document, the files it includes (path
    relative to the .tex -> bytes), and everything that was not exportable
    cleanly. `problems` empty means the note resolved completely."""
    tex: str
    files: dict[str, bytes] = field(default_factory=dict)
    problems: tuple[str, ...] = ()


# --- escaping --------------------------------------------------------

def escape(text: str) -> str:
    """Plain text as LaTeX text. Unicode symbols a text font lacks become
    their math command; everything else passes through, which XeTeX
    (tectonic's engine) reads natively."""
    out = []
    for ch in text:
        out.append(_SPECIALS.get(ch) or _SYMBOLS.get(ch) or ch)
    return "".join(out)


def unit_tex(unit: str) -> str:
    """A unit as the body of a `\\mathrm{}`: `m/s^2`, `N*m`, `m/s²`."""
    unit = _SUPERSCRIPT_RUN_RE.sub(lambda m: "^" + m.group(0).translate(_SUPERSCRIPTS), unit)
    unit = unit.replace("·", " ").replace("*", r"\,")
    unit = _UNIT_EXPONENT_RE.sub(r"^{\1}", unit)
    return unit.replace("%", r"\%")


def quantity(text: str) -> str:
    """A `calc.Quantity` reading, as maths: `4.00 in` -> `$4.00\\,\\mathrm{in}$`.
    Text that is not number-led (a name, an error) is escaped as text."""
    match = _QUANTITY_RE.match(text.strip())
    if match is None:
        return escape(text)
    body = _number(match.group("value"))
    if match.group("sigma"):
        body += rf"\pm {_number(match.group('sigma'))}"
    if match.group("unit"):
        body += rf"\,\mathrm{{{unit_tex(match.group('unit'))}}}"
    return f"${body}$"


def _number(text: str) -> str:
    sci = _SCI_RE.match(text)
    if sci:
        return rf"{sci.group(1)}\times 10^{{{int(sci.group(2))}}}"
    return text


# --- inline ----------------------------------------------------------

def inline(nodes: list[dict], problems: list[str]) -> str:
    return "".join(_node(n, problems) for n in nodes)


def _node(n: dict, problems: list[str]) -> str:
    kind = n["type"]
    if kind == "text":
        return escape(n["text"])
    if kind == "styled":
        command = {"textbf": "textbf", "bold": "textbf", "emph": "emph", "texttt": "texttt"}.get(
            n["style"], "emph")
        return rf"\{command}{{{escape(n['text'])}}}"
    if kind == "math":
        return f"${n['tex']}$"
    if kind == "ref":
        if n.get("error"):
            problems.append(f"\\ref{{{n['name']}}}: {n['error']}")
            return r"\textbf{[?ref]}"
        return escape(n["text"]).replace(" ", "~", 1)
    if kind == "link":
        if n.get("error"):
            problems.append(f"\\mote{{{n['ref']}}}: {n['error']}")
        return escape(n["text"])
    if kind == "value":
        if n.get("error"):
            problems.append(f"\\val{{{n['expression']}}}: {n['error']}")
            return rf"\textbf{{[{escape(n['expression'])}: error]}}"
        return quantity(n["text"])
    return ""


# --- blocks ----------------------------------------------------------

def export(
    conn: sqlite3.Connection, note_id: str, source: str | None = None,
    cover: Cover | None = None, draw=None,
) -> TexBundle:
    """The note `note_id` as a bundle. `source` overrides the stored note
    (an unsaved draft); `cover` supplies the title block."""
    from core import modoc  # local: modoc imports nothing from here
    text = source if source is not None else modoc.read_source(conn, note_id)
    rendered = document_repo.render(conn, note_id, text, draw=draw)
    return from_render(rendered, cover)


def from_render(rendered: dict, cover: Cover | None = None) -> TexBundle:
    """The pure half of `export`: resolved blocks in, `.tex` out."""
    problems: list[str] = [
        f"line {p['line']}: {p['message']}" for p in rendered.get("problems", [])
    ]
    files: dict[str, bytes] = {}
    body = "\n\n".join(
        part for part in (_block(b, files, problems) for b in rendered["blocks"]) if part
    )
    title = cover.name if cover else rendered["anchor"]["name"]
    tex = _PREAMBLE + _title_block(title, cover) + "\n" + body + "\n\n\\end{document}\n"
    return TexBundle(tex=tex, files=files, problems=tuple(problems))


_PREAMBLE = r"""\documentclass[11pt]{article}
\usepackage[margin=1in]{geometry}
\usepackage{amsmath}
\usepackage{graphicx}
\usepackage{pgfplots}
\usepgfplotslibrary{fillbetween}
\pgfplotsset{compat=1.17}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0.6em}
\begin{document}
"""


def _title_block(title: str, cover: Cover | None) -> str:
    lines = [rf"\title{{{escape(title)}}}"]
    lines.append(rf"\author{{{escape(cover.author)}}}" if cover and cover.author else r"\author{}")
    dated = []
    if cover and cover.due:
        dated.append(f"Due {cover.due}")
    if cover and cover.revision:
        dated.append(f"Revision {cover.revision}")
    lines.append(rf"\date{{{escape(', '.join(dated))}}}" if dated else r"\date{}")
    lines.append(r"\maketitle")
    return "\n".join(lines)


def _block(b: dict, files: dict[str, bytes], problems: list[str]) -> str:
    kind = b["type"]
    if kind == "heading":
        command = "section" if b["level"] <= 1 else "subsection"
        return rf"\{command}*{{{inline(b['content'], problems)}}}"
    if kind == "paragraph":
        return inline(b["content"], problems)
    if kind == "list":
        env = "enumerate" if b["ordered"] else "itemize"
        items = "\n".join(rf"\item {inline(i, problems)}" for i in b["items"])
        return f"\\begin{{{env}}}\n{items}\n\\end{{{env}}}"
    if kind == "equation":
        return f"\\begin{{equation}}\n{b['tex']}\n\\end{{equation}}"
    if kind == "cited":
        if b.get("error"):
            problems.append(f"\\rule{{{b['rule']}}}: {b['error']}")
            return rf"\textbf{{[{escape(b['rule'])}: {escape(str(b['error']))}]}}"
        body = b.get("tex") or rf"\texttt{{{escape(b['equation'])}}}"
        return f"\\begin{{equation}}\n{body}\n\\end{{equation}}"
    if kind == "graph":
        return _graph(b, problems)
    if kind == "image":
        return _image(b, files, problems)
    if kind == "table":
        return _table(b, problems)
    if kind == "work":
        return _work(b, problems)
    if kind == "section":
        return _section(b, problems)
    problems.append(f"line {b.get('line', 0)}: cannot export a {kind} block")
    return ""


def _captioned(b: dict, inner: str, problems: list[str], env: str = "figure") -> str:
    """A numbered, captioned float, or the bare content centred."""
    if not b.get("number"):
        return f"\\begin{{center}}\n{inner}\n\\end{{center}}"
    caption = inline(b.get("caption", []), problems)
    return (f"\\begin{{{env}}}[htbp]\n\\centering\n{inner}\n"
            f"\\caption{{{caption}}}\n\\end{{{env}}}")


def _image(b: dict, files: dict[str, bytes], problems: list[str]) -> str:
    if b.get("error"):
        problems.append(f"\\image{{{b['ref']}}}: {b['error']}")
        return rf"\textbf{{[image {escape(b['ref'])}: {escape(str(b['error']))}]}}"
    if b["mime"] != "image/png":
        problems.append(f"\\image{{{b['ref']}}}: {b['mime']} cannot be exported to LaTeX yet")
        return rf"\textbf{{[image {escape(b['ref'])}: {escape(b['mime'])} not exported]}}"
    path = f"{IMAGE_DIR}/{_file_stem(b['name'])}.png"
    files[path] = base64.b64decode(b["src"].split(",", 1)[1])
    width = (rf"{b['width'] / 100:g}\linewidth" if b.get("width") else r"\linewidth")
    inner = rf"\includegraphics[width={width},keepaspectratio]{{{path}}}"
    return _captioned(b, inner, problems)


def _file_stem(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "image"


def _table(b: dict, problems: list[str]) -> str:
    columns = max([len(b["header"])] + [len(r) for r in b["rows"]] or [1])

    def row(cells):
        padded = [inline(c, problems) for c in cells] + [""] * (columns - len(cells))
        return " & ".join(padded) + r" \\"

    inner = "\n".join(
        [rf"\begin{{tabular}}{{{'l' * columns}}}", r"\hline", row(b["header"]), r"\hline"]
        + [row(r) for r in b["rows"]] + [r"\hline", r"\end{tabular}"]
    )
    return _captioned(b, inner, problems, env="table")


def _work(b: dict, problems: list[str]) -> str:
    if b.get("error") or "steps" not in b:
        problems.append(f"\\work{{{b['target']}}}: {b.get('error') or 'unresolved'}")
        return rf"\textbf{{[work {escape(b['target'])}: error]}}"
    if not b["steps"]:
        return f"{escape(b['label'])}: {quantity(b['result'])}" + (
            f" ({escape(b['note'])})" if b.get("note") else "")
    lines = [_work_step(s) + r" \\" for s in b["steps"][:-1]]
    lines.append(_work_step(b["steps"][-1]))
    return ("\\begin{flushleft}\n" + f"\\textbf{{{escape(b['label'])}}} \\\\\n"
            + "\n".join(lines) + "\n\\end{flushleft}")


def _work_step(step: dict) -> str:
    """A symbolic step the converter could set is maths; the rest (values
    with units in them) stay the monospace line they were. A step's note
    (`converted to ohm`, `phi: limit r → 0 cm`) follows it, as it does on
    screen: it says what was done to the numbers."""
    line = f"${step['tex']}$" if step.get("tex") else rf"\texttt{{{escape(step['text'])}}}"
    return line + (rf" \quad\emph{{({escape(step['note'])})}}" if step.get("note") else "")


def _section(b: dict, problems: list[str]) -> str:
    if b.get("error"):
        problems.append(f"\\{b['kind']}: {b['error']}")
    heading = rf"\subsection*{{{escape(b['heading'])}}}"
    if b["kind"] == "checks":
        head, rows = ["Check", "Result", "Detail"], [
            [c["label"] or c["equation"], c["status"], c.get("detail") or ""]
            for c in b.get("checks", [])]
    elif b["kind"] == "verification":
        head, rows = ["Value", "Hand", "External", "Error"], [
            [c["ref"], c["hand"], c["external"],
             "" if c["error"] is None else f"{c['error'] * 100:.2f}\\%"]
            for c in b.get("comparisons", [])]
    else:
        head, rows = ["Quantity", "Value", "Source"], [
            [r["ref"], r["value"], r["source"]] for r in b.get("rows", [])]
    if not rows:
        return heading + "\n" + r"\textit{None.}"
    quantity_columns = {1} if b["kind"] not in ("verification",) else {1, 2}

    def cell(i, text):
        return quantity(text) if i in quantity_columns and b["kind"] != "checks" else (
            text if text.endswith("\\%") else escape(text))

    body = "\n".join(" & ".join(cell(i, c) for i, c in enumerate(r)) + r" \\" for r in rows)
    return (f"{heading}\n\\begin{{tabular}}{{{'l' * len(head)}}}\n\\hline\n"
            + " & ".join(rf"\textbf{{{h}}}" for h in head) + " \\\\\n\\hline\n"
            + body + "\n\\hline\n\\end{tabular}")


# --- plots -----------------------------------------------------------

def _graph(b: dict, problems: list[str]) -> str:
    fig = b["figure"]
    if fig.get("error"):
        problems.append(f"\\graph{{{b['view']}}}: {fig['error']}")
        return rf"\textbf{{[graph {escape(b['view'])}: {escape(str(fig['error']))}]}}"
    if fig["mode"] != "sweep":
        problems.append(f"\\graph{{{b['view']}}}: {fig['mode']} figures are not exported yet")
        return rf"\textbf{{[graph {escape(b['view'])}: {fig['mode']} not exported]}}"
    return _captioned(b, sweep_axes(fig), problems)


def sweep_axes(fig: dict) -> str:
    """A solved sweep as pgfplots (core/plotexport.py draws it: one axis
    per unit band; a failed point breaks the line)."""
    from core import plotexport
    return plotexport.sweep_tikz(fig)
