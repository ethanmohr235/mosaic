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
from collections.abc import Iterable

from core import model_resolver
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
