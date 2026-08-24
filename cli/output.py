# cli/output.py
"""Shared presentation for the two CLI front ends.

`cli/main.py` (one-shot argparse commands) and `cli/repl.py` (an
interactive loop) are two ways to type the same commands, so anything
that decides *how a result is printed* belongs here rather than being
written twice -- these were literally identical copies before. Argument
parsing stays with each front end, since that is the one thing genuinely
different between them.

Printing, not logging, on purpose: this is a CLI's actual output, which
docs/style.md's "no print debugging" rule is not about.
"""
import json
import sqlite3
import textwrap
from collections.abc import Iterable

from core import model_resolver
from core import modoc
from core import modoc_bind
from core.entities import Mote
from core.motes import get_mote, get_motes  # noqa: F401  (get_mote: print_model_detail)
from core.providers import PropertyModelError, PropertyProvider
from core.relationships import list_relationships
from core.simulation import Change, RunRecord, get_run, run_changes


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
    search`, `species instances`, `folder members`, `library list` are
    all the same answer to different questions."""
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


def print_changes(changes: Iterable[Change]) -> None:
    for change in changes:
        old = "None" if change.old_value is None else f"{change.old_value:g}"
        print(f"  [{change.mote_id[:8]}] {change.key}: {old} -> {change.new_value:g}")


def print_run_summary_line(run: RunRecord) -> None:
    applied = "applied" if run.committed else "saved"
    line = f"[{run.run_id}] run=#{run.run_number} {run.ran_at} {run.status} ({applied})"
    if run.status == "ok":
        line += f" ({run.change_count} change{'' if run.change_count == 1 else 's'})"
    else:
        line += f": {run.detail}"
    print(line)


def print_run_detail(conn: sqlite3.Connection, run_id: str) -> None:
    run = get_run(conn, run_id)
    if run is None:
        print("No run found.")
        return
    applied = "applied to graph" if run.committed else "saved (not applied to graph)"
    print(f"Run {run.run_id}")
    print(f"run=#{run.run_number}  {run.ran_at}  status={run.status}  ({applied})")
    if run.detail:
        print(f"detail: {run.detail}")
    print_changes(run_changes(conn, run.run_id))


# --- .modoc ----------------------------------------------------------
#
# The terminal's renderer. core/modoc.py hands out a tree and
# core/modoc_bind.py hands out what it resolves to; turning that into
# *something a reader sees* is each surface's own job, which is why
# Mora's renderer is JavaScript and this one is here. What the two must
# never disagree about -- what a macro means -- neither of them decides.

_WIDTH = 78
_BAR_WIDTH = 24
_SPARKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def print_modoc(document: "modoc.Document", resolution) -> None:
    for index, block in enumerate(document.blocks):
        if index:
            print()
        _print_block(block, resolution)


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


def _print_block(block, resolution) -> None:
    if isinstance(block, modoc.Heading):
        text = _inline_text(block.content, resolution)
        print(text if block.level == 1 else text.upper())
        if block.level == 1:
            print("=" * len(text))
    elif isinstance(block, modoc.Paragraph):
        print(textwrap.fill(_inline_text(block.content, resolution), _WIDTH))
    elif isinstance(block, modoc.ItemList):
        for item in block.items:
            print(textwrap.fill(
                _inline_text(item, resolution), _WIDTH,
                initial_indent="  - ", subsequent_indent="    ",
            ))
    elif isinstance(block, modoc.Equation):
        print(f"    {_math_text(block.text)}")
    elif isinstance(block, modoc.Graph):
        _print_series(resolution.graphs.get(modoc.graph_key(block)))


def _inline_text(nodes, resolution) -> str:
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
    return "".join(parts)


def _math_text(text: str) -> str:
    spans = modoc.math_spans(text)
    marker = {"sup": "^", "sub": "_", "text": ""}
    return "".join(marker[s.kind] + s.text for s in spans)


def _print_series(series) -> None:
    if series is None or series.error and not series.values:
        print(f"    [graph: {series.error if series else 'unresolved'}]")
        return
    if series.kind == modoc.OVER_T:
        _print_sparkline(series)
        return
    width = max((len(label) for label in series.labels), default=0)
    peak = max(series.values, default=0) or 1
    for label, value in zip(series.labels, series.values):
        filled = round(_BAR_WIDTH * value / peak)
        print(f"    {label:<{width}}  {'#' * filled:<{_BAR_WIDTH}}  {value:g}")


def _print_sparkline(series) -> None:
    """A projection is a shape before it is a table -- one line of it
    reads faster than nine rows, and the endpoints carry the numbers."""
    low, high = min(series.values), max(series.values)
    span = (high - low) or 1
    spark = "".join(
        _SPARKS[round((v - low) / span * (len(_SPARKS) - 1))] for v in series.values
    )
    print(f"    {spark}")
    print(f"    {series.labels[0]} {low:g}  ->  {series.labels[-1]} {series.values[-1]:g}")
