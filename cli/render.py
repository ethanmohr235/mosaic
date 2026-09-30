# cli/render.py
"""Rendering shared by both command-line surfaces.

`cli/main.py` (argparse, one command per invocation) and `cli/repl.py`
(interactive) are two front doors onto the same functions, and each has
grown its own copy of the small printers. An explanation tree is too big
to copy: it is recursive, and two copies would drift into two different
accounts of the same run.

The explanation/impact/requirement helpers return lines rather than
printing them, so the caller decides where output goes and a test can
assert on the text without capturing stdout. The older `print_*` helpers
below (motes, property models, .modoc) still print directly -- they were
`cli/output.py` before the two modules were merged, and converting them
is a separate cleanup, not a merge.

Printing, not logging, on purpose: this is a CLI's actual output, which
docs/rules.md's "no print debugging" rule is not about.
"""
import json
import sqlite3
import textwrap
from collections.abc import Iterable

from core import calc
from core import model_resolver
from core import simulation
from core import modoc
from core import modoc_bind
from core.derivations import Explanation
from core.entities import Mote
from core.incidence import DERIVED, MISSING, STORED
from core.motes import get_mote, get_motes  # noqa: F401  (get_mote: print_model_detail)
from core.providers import PropertyModelError, PropertyProvider
from core.relationships import list_relationships

BASE_UNIT_NOTE = (
    "Values are in each dimension's base SI unit -- what the solver worked in."
)


def label(conn: sqlite3.Connection, mote_id: str) -> str:
    """A mote's name if it has one, else a short id -- the same choice
    core/simulation.py makes for its warnings, for the same reason: an
    explanation naming R1 is worth far more than one naming a UUID."""
    mote = get_mote(conn, mote_id)
    name = mote.properties.get("name") if mote is not None else None
    return str(name) if isinstance(name, str) and name.strip() else mote_id[:8]


def explanation_lines(
    conn: sqlite3.Connection, explanation: Explanation, indent: int = 0
) -> list[str]:
    """One derivation and everything it rests on, depth-first.

    A solver block prints its jointly-determined siblings before its
    equations, because "these four values were found together" is the
    part a reader is most likely to be surprised by, and the part a
    tree-shaped explanation would quietly omit.
    """
    pad = "  " * indent
    value = "?" if explanation.value is None else f"{explanation.value:g}"
    method = f"  [{explanation.method}]" if explanation.method else ""
    lines = [f"{pad}{label(conn, explanation.mote_id)}.{explanation.key} = {value}{method}"]

    if explanation.note:
        lines.append(f"{pad}  {explanation.note}")
    if explanation.detail:
        lines.append(f"{pad}  {explanation.detail}")
    if explanation.also_determined:
        together = ", ".join(
            f"{label(conn, mote_id)}.{key}" for mote_id, key in explanation.also_determined
        )
        lines.append(f"{pad}  found together with: {together}")
    for equation in explanation.equations:
        anchor = label(conn, equation.anchor_mote_id)
        lines.append(f"{pad}  <- {equation.rule_name} on {anchor}: {equation.equation}")
    for item in explanation.inputs:
        lines.append(
            f"{pad}  {item.kind} {label(conn, item.mote_id)}.{item.key} = {item.value:g}"
        )
        if item.derivation is not None:
            lines.extend(explanation_lines(conn, item.derivation, indent + 2))
    return lines


# Missing prerequisites first: they are the only part of a requirement
# listing that anyone has to do something about.
_URGENCY = {MISSING: 0, DERIVED: 1, STORED: 2}


def variable(conn: sqlite3.Connection, target: tuple[str, str]) -> str:
    """`R1.resistance` -- one value's address, named the way a user wrote it."""
    mote_id, key = target
    return f"{label(conn, mote_id)}.{key}"


def _equation_line(conn: sqlite3.Connection, touch, indent: str = "  ") -> str:
    return f"{indent}{touch.rule_name} on {label(conn, touch.anchor_mote_id)}"


def equation_lines(conn: sqlite3.Connection, touches, empty: str) -> list[str]:
    """A list of equations, deduplicated by (rule, instance) and sorted.

    The same rule reaches a value through several instances often enough
    -- three KCL equations on three nodes -- that leaving duplicates in
    would bury the answer."""
    if not touches:
        return [f"  {empty}"]
    seen = {_equation_line(conn, t) for t in touches}
    return sorted(seen)


def impact_lines(conn: sqlite3.Connection, target: tuple[str, str], effects) -> list[str]:
    """What could move if one value changed, grouped by how far the
    change has to travel.

    Distance is hops through the equation graph, not a causal ordering:
    everything in a coupled block moves at once. It is still worth
    showing, because the near values are the ones a reader is most likely
    to have meant.
    """
    if not effects:
        return [
            f"Nothing depends on {variable(conn, target)}.",
            "  No equation reads it, or nothing it reaches is free to move.",
        ]

    count = len(effects)
    lines = [
        f"Changing {variable(conn, target)} could move "
        f"{count} value{'' if count == 1 else 's'}:"
    ]
    for distance in sorted({e.distance for e in effects}):
        step = "1 step" if distance == 1 else f"{distance} steps"
        lines.append(f"  {step} away")
        for effect in (e for e in effects if e.distance == distance):
            through = ", ".join(sorted({
                f"{t.rule_name} on {label(conn, t.anchor_mote_id)}" for t in effect.through
            }))
            lines.append(f"    {variable(conn, effect.variable)}  via {through}")
    return lines


def requirement_lines(conn: sqlite3.Connection, requirement) -> list[str]:
    """What determining a value depends on, and what is stopping it.

    The two failure headlines come first because they are the two most
    common modelling mistakes, and both are silent otherwise: a property
    nobody declared free, and one declared free that no equation
    constrains."""
    name = variable(conn, requirement.variable)
    if not requirement.free:
        return [
            f"Nothing can determine {name}.",
            "  It is not among its mote's declared freedoms, so the solver "
            "may not change it.",
        ]
    if not requirement.producers:
        return [
            f"Nothing can determine {name}.",
            "  It is declared free, but no equation constrains it.",
        ]

    lines = [f"To determine {name}:", "", "  determined by:"]
    lines.extend(f"  {line}" for line in equation_lines(conn, requirement.producers, ""))

    missing = len(requirement.missing)
    heading = "  needs:" if not missing else f"  needs ({missing} missing):"
    lines.extend(["", heading])
    if not requirement.needs:
        lines.append("    nothing -- it is fully constrained on its own")
    # core/incidence.py sorts by mote id, which is UUID order and reads as
    # random. Sort for a human instead: what is missing first, since that
    # is the only part of this list anyone has to act on.
    for need in sorted(
        requirement.needs,
        key=lambda n: (_URGENCY.get(n.status, 9), variable(conn, n.variable)),
    ):
        lines.append(f"    {need.status:<8} {variable(conn, need.variable)}")
    return lines


def scenario_lines(conn: sqlite3.Connection, scenario) -> list[str]:
    """What one scenario changes, grouped by the mote it changes.

    Shows the value each property would take *and* what it currently
    holds, because the interesting thing about a scenario is almost
    always the difference rather than the absolute value."""
    lines = [f"{scenario.name} varies {len(scenario.target_ids)} mote(s):"]
    for override in scenario.overrides:
        current = get_mote(conn, override.target_id)
        existing = current.properties if current is not None else {}
        lines.append(f"  {label(conn, override.target_id)}")
        for key in sorted(override.properties):
            was = existing.get(key, "(unset)")
            lines.append(f"    {key}: {was} -> {override.properties[key]}")
    return lines


def change_lines(changes) -> list[str]:
    """One `old -> new` line per solved property."""
    return [
        f"  [{change.mote_id[:8]}] {change.key}: "
        f"{'None' if change.old_value is None else format(change.old_value, 'g')} -> "
        f"{change.new_value:g}"
        for change in changes
    ]


def print_models(conn: sqlite3.Connection, mote_id: str) -> None:
    """`Models: <name>, ...` for a mote's outgoing uses_model relationships,
    printed after `mote get` so the property model attached to a mote is
    visible without a separate lookup."""
    model_ids = [
        r.to_id for r in list_relationships(conn, from_id=mote_id, type_=model_resolver.USES_MODEL)
    ]
    if not model_ids:
        return
    names = [m.properties.get("name", m.id[:8]) for m in get_motes(conn, model_ids)]
    print(f"Models: {', '.join(names)}")


def print_motes(motes: Iterable[Mote]) -> None:
    """The one way a list of motes is printed -- `mote list`, `mote
    search`, `species instances` are all the same answer to different
    questions."""
    for mote in motes:
        print(f"[{mote.id}] {mote.properties}")


def print_capabilities(provider: PropertyProvider) -> None:
    for cap in provider.capabilities():
        units = f" [{cap.units}]" if cap.units else ""
        print(f"    {cap.operation}({', '.join(cap.inputs)}) -> {cap.output}{units}")


def print_model_list(conn: sqlite3.Connection) -> None:
    """Which motes are property models is core's answer
    (model_resolver.model_ids_in), not something the CLI works out for
    itself. Listed by name so the order is stable and readable, rather
    than in whatever order the relationships happened to be stored."""
    model_ids = model_resolver.model_ids_in(list_relationships(conn))
    models = sorted(
        get_motes(conn, sorted(model_ids)),
        key=lambda m: str(m.properties.get("name", m.id)),
    )
    for mote in models:
        name = mote.properties.get("name", mote.id[:8])
        provider = mote.properties.get(model_resolver.PROVIDER_KEY, "?")
        print(f"[{mote.id}] {name} (provider={provider})")


def print_model_detail(conn: sqlite3.Connection, model_id: str, *, header: bool) -> None:
    """Backs both `model show` (header=True: name/provider/configuration,
    then capabilities) and `model capabilities` (header=False: just the
    capabilities) -- the two differ only by that block."""
    mote = get_mote(conn, model_id)
    if mote is None:
        print("No mote found.")
        return
    try:
        provider = model_resolver.construct(mote.id, mote.properties)
    except PropertyModelError as e:
        print(f"Error: {e}")
        return

    if header:
        name = mote.properties.get("name", mote.id[:8])
        print(f"Model: {name} [{mote.id}]")
        print(f"Provider: {mote.properties.get(model_resolver.PROVIDER_KEY)}")
        configuration = mote.properties.get(model_resolver.CONFIGURATION_KEY, {})
        print(f"Configuration: {json.dumps(configuration)}")
        print("Capabilities:")
    print_capabilities(provider)


# --- .modoc ----------------------------------------------------------
#
# The terminal's renderer. core/modoc.py hands out a tree and
# core/modoc_bind.py hands out what it resolves to; turning that into
# *something a reader sees* is each surface's own job, which is why
# Mora's renderer is JavaScript and this one is here. What the two must
# never disagree about -- what a macro means -- neither of them decides.

_WIDTH = 78
_SPARKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def print_modoc(document: "modoc.Document", resolution) -> None:
    for index, block in enumerate(document.blocks):
        if index:
            print()
        _print_block(block, document, resolution)


def print_diagnostics(document: "modoc.Document", resolution) -> int:
    """Everything wrong with a note, and how many there were. What counts
    as a problem is core/modoc_bind.py::problems() -- one answer, so
    `modoc check` and Mora's problems panel can never disagree about
    whether a note is clean."""
    found = modoc_bind.problems(document, resolution)
    for problem in found:
        where = f"line {problem.line}" if problem.line else "note"
        print(f"{where}: {problem.kind}: {problem.message}")
    return len(found)


def _print_block(block, document, resolution) -> None:
    if isinstance(block, modoc.Heading):
        text = _inline_text(block.content, document, resolution)
        print(text if block.level == 1 else text.upper())
        if block.level == 1:
            print("=" * len(text))
    elif isinstance(block, modoc.Paragraph):
        print(textwrap.fill(_inline_text(block.content, document, resolution), _WIDTH))
    elif isinstance(block, modoc.ItemList):
        _print_items(block, document, resolution)
    elif isinstance(block, modoc.Equation):
        _print_equation(_math_text(block.text), block.number)
    elif isinstance(block, modoc.Cited):
        _print_cited(block, resolution)
    elif isinstance(block, modoc.Table):
        _print_table(block, document, resolution)
    elif isinstance(block, modoc.Graph):
        _print_figure(resolution.graphs.get(modoc.graph_key(block)))
        _print_caption(modoc.FIGURE_LABEL, block, document, resolution)
    elif isinstance(block, modoc.Work):
        print_work(resolution.works.get(modoc.work_key(block)))
    elif isinstance(block, modoc.Section):
        _print_section(resolution.sections.get(modoc.section_key(block)))


def _print_items(block, document, resolution) -> None:
    """`- ` for a bullet, `1. ` for an ordered list. The markers are padded
    to the same width so a ten-item list does not step in and out."""
    width = len(f"{len(block.items)}. ") if block.ordered else 2
    for index, item in enumerate(block.items, start=1):
        marker = f"{index}. ".ljust(width) if block.ordered else "- ".ljust(width)
        print(textwrap.fill(
            _inline_text(item, document, resolution), _WIDTH,
            initial_indent="  " + marker, subsequent_indent="  " + " " * width,
        ))


def _print_equation(text: str, number: int) -> None:
    """The number to the right, as LaTeX sets it -- far enough from the maths
    to read as a label rather than as part of it."""
    line = f"    {text}"
    if not number:
        print(line)
        return
    tag = f"({number})"
    padding = max(_WIDTH - len(line) - len(tag), 2)
    print(f"{line}{' ' * padding}{tag}")


def _print_cited(block, resolution) -> None:
    """A cited rule prints its equation and says whose it is -- the name is
    the point, since the citation exists to be referred to by it."""
    rule = resolution.rules.get(block.rule)
    if rule is None or rule.error:
        print(f"    [{rule.error if rule else 'unresolved'}]")
        return
    _print_equation(_math_text(rule.equation), block.number)
    print(f"      -- {rule.name}")


def _print_table(block, document, resolution) -> None:
    rows = [
        [_inline_text(cell, document, resolution) for cell in row]
        for row in (block.header, *block.rows)
    ]
    widths = [
        max(len(row[i]) if i < len(row) else 0 for row in rows)
        for i in range(max(len(row) for row in rows))
    ]
    for index, row in enumerate(rows):
        print("  " + "  ".join(
            cell.ljust(widths[i]) for i, cell in enumerate(row)
        ).rstrip())
        if index == 0:
            print("  " + "  ".join("-" * width for width in widths))
    _print_caption(modoc.TABLE_LABEL, block, document, resolution)


def _print_caption(kind: str, block, document, resolution) -> None:
    if not block.number and not block.caption:
        return
    word = modoc.LABEL_WORDS.get(kind, kind)
    text = _inline_text(block.caption, document, resolution)
    print(textwrap.fill(
        f"{word} {block.number}{': ' + text if text else ''}", _WIDTH,
        initial_indent="  ", subsequent_indent="  ",
    ))


def print_work(shown, indent: str = "    ") -> None:
    """The three lines, indented as display maths -- the same indent
    `\\begin{equation}` gets, because that is what they are."""
    if shown is None:
        return
    if shown.error:
        print(f"{indent}[{shown.error}]")
        return
    if not shown.shown:
        print(f"{indent}{shown.label} = {shown.quantity.text()}  [{shown.note}]")
        return
    for step in shown.steps:
        line = f"{indent}{_math_text(step.text)}"
        if step.kind == calc.RESULT:
            line = f"{indent}{shown.label} = {step.text}"
        print(f"{line}  [{step.note}]" if step.note else line)
    if shown.also_determined:
        print(f"{indent}found together with: {', '.join(shown.also_determined)}")


def print_calc_rows(rows, unit: str | None = None, figures: int | None = None) -> None:
    """A given/answer/assumption table as two aligned columns.

    Aligned here rather than in core/calc.py, because how wide a column is
    depends on the terminal and a rendered document has no columns at
    all."""
    if not rows:
        print("(none)")
        return
    width = max(len(row.label) for row in rows)
    for row in rows:
        text = row.quantity.converted(unit).text(figures) or "-"
        note = f"  ({row.source.note})" if row.source and row.source.note else ""
        print(f"  {row.label.ljust(width)}  {text}{note}")


def print_checks(results) -> None:
    """A green/red checklist, in a terminal's only two colours: a mark that
    lines up, and the word FAIL where it does not."""
    if not results:
        print("(no checks)")
        return
    for check in results:
        print(f"  {_check_mark(check)} {check.equation}"
              + (f"  -- {check.detail}" if check.detail else ""))


def _check_mark(check) -> str:
    if check.passed:
        return "ok  "
    return "FAIL" if check.status == simulation.FAIL else "??  "


def _print_section(section) -> None:
    if section is None:
        return
    print(section.heading.upper())
    if section.error:
        print(f"  [{section.error}]")
    if section.empty:
        print("  (none)")
        return
    if section.checks:
        print_checks(section.checks)
    if section.rows:
        print_calc_rows(section.rows, figures=section.figures)


def _inline_text(nodes, document, resolution) -> str:
    parts = []
    for node in nodes:
        if isinstance(node, modoc.Text):
            parts.append(node.text)
        elif isinstance(node, modoc.Styled):
            parts.append(node.text.upper() if node.style == "bold" else node.text)
        elif isinstance(node, modoc.Math):
            parts.append(_math_text(node.text))
        elif isinstance(node, modoc.Link):
            target = resolution.links.get(node.ref)
            parts.append(node.label or (target.name if target else node.ref))
        elif isinstance(node, modoc.Value):
            result = resolution.values.get(modoc.value_key(node))
            parts.append(result.text if result else f"?{node.expression}")
        elif isinstance(node, modoc.Ref):
            # Resolved against the document, not the garden -- what a
            # cross-reference says is a fact about the text.
            parts.append(modoc.reference_text(document, node))
    return "".join(parts)


def _math_text(text: str) -> str:
    spans = modoc.math_spans(text)
    marker = {"sup": "^", "sub": "_", "text": ""}
    return "".join(marker[s.kind] + s.text for s in spans)


def _print_figure(figure) -> None:
    """A saved Moxie view, in a terminal.

    A sparkline per curve rather than a table of points: a figure is a
    shape before it is numbers, one line of it reads faster than sixty
    rows, and the endpoints carry the values that a reader would look up
    anyway. The other two modes have no line to draw -- a surface and a
    scatter are stated instead."""
    if figure is None:
        print("    [graph: unresolved]")
        return
    head = f"    [{figure.name or 'graph'}]"
    if figure.error:
        print(f"{head} {figure.error}")
        return
    print(f"{head} {figure.x_label} → {figure.y_label}")
    if figure.mode == "correlate":
        print(f"    {len(figure.dots)} motes, one solve")
        return
    if figure.mode == "surface":
        print(f"    {figure.cols}x{figure.rows} grid, "
              f"{_number(figure.lo)} to {_number(figure.hi)}")
        return
    width = max((len(s.label) for s in figure.series), default=0)
    for series in figure.series:
        print(f"    {series.label:<{width}}  {_sparkline(series.points)}")


def _sparkline(points) -> str:
    """One curve as a row of blocks, with its own endpoints after it.
    Scaled to the series' own range, so a flat curve reads as flat rather
    than as noise magnified to fill the row."""
    values = [p[1] for p in points if p is not None]
    if not values:
        return "(nothing solved)"
    low, high = min(values), max(values)
    span = (high - low) or 1
    # A point that did not solve is a space, not a low bar: an absent
    # answer must not read as a small one.
    spark = "".join(
        " " if p is None else _SPARKS[round((p[1] - low) / span * (len(_SPARKS) - 1))]
        for p in points
    )
    return f"{spark}  {_number(values[0])} -> {_number(values[-1])}"


def _number(value) -> str:
    return "—" if value is None else f"{value:g}"
