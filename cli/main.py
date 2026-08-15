# cli/main.py
import argparse
import json
import logging

from core.motes import create_mote, list_motes, get_mote, get_motes, delete_mote, update_mote
from core.relationships import (
    create_relationship,
    list_relationships,
    get_relationship,
    update_relationship,
    delete_relationship,
)
from core.gardens import create_garden, delete_garden, list_gardens, resolve_garden, restore_garden
from core.db import get_connection, initialize_database
from core import model_resolver
from core.providers import PropertyModelError
from core.simulation import SimulationError, get_run, list_runs, run_changes, run_simulation

logger = logging.getLogger(__name__)


def _parse_properties_json(raw: str | None) -> dict:
    if not raw:
        return {}
    props = json.loads(raw)
    if not isinstance(props, dict):
        raise ValueError("Properties must be a JSON object")
    return props


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="monoss")
    parser.add_argument("--garden", help="Garden to operate on (defaults to 'default')")
    subparsers = parser.add_subparsers(dest="command")

    # ------------------ GARDEN COMMANDS ------------------
    garden_parser = subparsers.add_parser("garden", help="Manage gardens")
    garden_sub = garden_parser.add_subparsers(dest="action")
    garden_create = garden_sub.add_parser("create", help="Create a new garden")
    garden_create.add_argument("name", help="Garden name")
    garden_sub.add_parser("list", help="List all gardens")
    garden_delete = garden_sub.add_parser("delete", help="Delete a garden (recoverable via restore)")
    garden_delete.add_argument("name", help="Garden name")
    garden_restore = garden_sub.add_parser("restore", help="Restore a deleted garden")
    garden_restore.add_argument("name", help="Garden name")

    # ------------------ MOTE COMMANDS ------------------
    mote_parser = subparsers.add_parser("mote", help="Manage motes")
    mote_sub = mote_parser.add_subparsers(dest="action")

    create_parser = mote_sub.add_parser("create", help="Create a new mote")
    create_parser.add_argument(
        "--properties",
        help='JSON string of properties, e.g. \'{"name":"Ethan","age":22}\''
    )

    get_parser = mote_sub.add_parser("get", help="Get a mote by id")
    get_parser.add_argument("--id", required=True, help="Mote id to fetch")

    update_parser = mote_sub.add_parser("update", help="Update mote properties")
    update_parser.add_argument("--id", required=True, help="Mote id to update")
    update_parser.add_argument(
        "--properties",
        required=True,
        help='JSON string of properties to set or update, e.g. \'{"age":23}\''
    )

    delete_parser = mote_sub.add_parser("delete", help="Delete a mote")
    delete_parser.add_argument("--id", required=True, help="Mote id to delete")

    mote_sub.add_parser("list", help="List all motes")

    # ------------------ RELATIONSHIP COMMANDS ------------------
    rel_parser = subparsers.add_parser("rel", help="Manage relationships")
    rel_sub = rel_parser.add_subparsers(dest="action")

    rel_create = rel_sub.add_parser("create", help="Create a relationship")
    rel_create.add_argument("--from_id", required=True, help="Source mote UUID")
    rel_create.add_argument("--to_id", required=True, help="Target mote UUID")
    rel_create.add_argument("--type", required=True, help="Relationship type")

    rel_get = rel_sub.add_parser("get", help="Get a relationship by id")
    rel_get.add_argument("--id", required=True, help="Relationship id to fetch")

    rel_list = rel_sub.add_parser("list", help="List relationships")
    rel_list.add_argument("--from_id", help="Filter by source mote UUID")
    rel_list.add_argument("--type", help="Filter by relationship type")

    rel_update = rel_sub.add_parser("update", help="Update a relationship")
    rel_update.add_argument("--id", required=True, help="Relationship id to update")
    rel_update.add_argument("--from_id", help="New source mote UUID")
    rel_update.add_argument("--to_id", help="New target mote UUID")
    rel_update.add_argument("--type", help="New relationship type")

    rel_delete = rel_sub.add_parser("delete", help="Delete a relationship")
    rel_delete.add_argument("--id", required=True, help="Relationship id to delete")

    # ------------------ MODEL COMMANDS ------------------
    model_parser = subparsers.add_parser("model", help="Inspect physical property models")
    model_sub = model_parser.add_subparsers(dest="action")
    model_sub.add_parser("list", help="List property models")
    model_show = model_sub.add_parser("show", help="Show a property model's details")
    model_show.add_argument("--id", required=True, help="Property-model mote id")
    model_caps = model_sub.add_parser("capabilities", help="List a property model's capabilities")
    model_caps.add_argument("--id", required=True, help="Property-model mote id")

    # ------------------ SIMULATE COMMANDS ------------------
    simulate_parser = subparsers.add_parser(
        "simulate", help="Run the garden's simulation rules, or inspect past runs"
    )
    simulate_sub = simulate_parser.add_subparsers(dest="action")
    simulate_sub.add_parser("run", help="Run the simulation (default)")
    history_parser = simulate_sub.add_parser("history", help="List past simulation runs")
    history_parser.add_argument("--limit", type=int, help="Show only the N most recent runs")
    show_parser = simulate_sub.add_parser("show", help="Show one run's changes")
    show_parser.add_argument("--id", required=True, help="Run id")

    return parser


def _handle_garden(args) -> None:
    if args.action == "create":
        create_garden(args.name)
        print(f"Garden created: {args.name}")
    elif args.action == "list":
        for name in list_gardens():
            print(name)
    elif args.action == "delete":
        delete_garden(args.name)
        print(f"Garden deleted: {args.name} (run `garden restore {args.name}` to undo)")
    elif args.action == "restore":
        restore_garden(args.name)
        print(f"Garden restored: {args.name}")
    else:
        print("Specify a garden action: create, list, delete, restore")


def _handle_mote(conn, args) -> None:
    if args.action == "create":
        props = _parse_properties_json(getattr(args, "properties", None))
        mote_id = create_mote(conn, props)
        print(f"Mote created with id={mote_id}")

    elif args.action == "get":
        mote = get_mote(conn, args.id)
        if mote is None:
            print("No mote found.")
        else:
            print(f"[{mote.id}] {mote.properties}")
            _print_models(conn, mote.id)

    elif args.action == "list":
        for m in list_motes(conn):
            print(f"[{m.id}] {m.properties}")

    elif args.action == "update":
        props = _parse_properties_json(args.properties)
        update_mote(conn, args.id, props)
        print(f"Mote with id={args.id} updated with props={props}")

    elif args.action == "delete":
        found = delete_mote(conn, args.id)
        print(f"Mote deleted with id={args.id}" if found else "No mote found.")

    else:
        print("Specify a mote action: create, get, list, update, delete")


def _handle_rel(conn, args) -> None:
    if args.action == "create":
        rel_id = create_relationship(conn, args.from_id, args.to_id, args.type)
        print(f"Relationship created: {args.from_id} -> {args.to_id} ({args.type}) [{rel_id}]")

    elif args.action == "get":
        rel = get_relationship(conn, args.id)
        print(f"[{rel.id}] {rel.from_id} -> {rel.to_id} ({rel.type})" if rel else "No relationship found.")

    elif args.action == "list":
        for r in list_relationships(conn, args.from_id, args.type):
            print(f"[{r.id}] {r.from_id} -> {r.to_id} ({r.type})")

    elif args.action == "update":
        found = update_relationship(conn, args.id, args.from_id, args.to_id, args.type)
        print(f"Relationship updated: {args.id}" if found else "No relationship found.")

    elif args.action == "delete":
        found = delete_relationship(conn, args.id)
        print(f"Relationship deleted: {args.id}" if found else "No relationship found.")

    else:
        print("Specify a rel action: create, get, list, update, delete")


def _model_mote_ids(conn) -> list[str]:
    """Every mote acting as a property model -- the target of at least one
    uses_model relationship. Mirrors the target-of-a-relationship pattern
    core/species.py's list_species() uses for instance_of/template_of."""
    seen: list[str] = []
    for rel in list_relationships(conn, type_=model_resolver.USES_MODEL):
        if rel.to_id not in seen:
            seen.append(rel.to_id)
    return seen


def _print_models(conn, mote_id: str) -> None:
    """`Models: <name>, ...` for a mote's outgoing uses_model relationships,
    printed after `mote get`/`mote show` so the property-model attached to
    a mote is visible without a separate lookup."""
    model_ids = [
        r.to_id for r in list_relationships(conn, from_id=mote_id, type_=model_resolver.USES_MODEL)
    ]
    if not model_ids:
        return
    names = [m.properties.get("name", m.id[:8]) for m in get_motes(conn, model_ids)]
    print(f"Models: {', '.join(names)}")


def _print_capabilities(provider) -> None:
    for cap in provider.capabilities():
        units = f" [{cap.units}]" if cap.units else ""
        print(f"    {cap.operation}({', '.join(cap.inputs)}) -> {cap.output}{units}")


def _handle_model(conn, args) -> None:
    if args.action == "list":
        for model_id in _model_mote_ids(conn):
            mote = get_mote(conn, model_id)
            if mote is None:
                continue
            name = mote.properties.get("name", mote.id[:8])
            provider = mote.properties.get(model_resolver.PROVIDER_KEY, "?")
            print(f"[{mote.id}] {name} (provider={provider})")

    elif args.action in ("show", "capabilities"):
        mote = get_mote(conn, args.id)
        if mote is None:
            print("No mote found.")
            return
        try:
            provider = model_resolver.construct(mote.id, mote.properties)
        except PropertyModelError as e:
            print(f"Error: {e}")
            return

        if args.action == "show":
            name = mote.properties.get("name", mote.id[:8])
            print(f"Model: {name} [{mote.id}]")
            print(f"Provider: {mote.properties.get(model_resolver.PROVIDER_KEY)}")
            configuration = mote.properties.get(model_resolver.CONFIGURATION_KEY, {})
            print(f"Configuration: {json.dumps(configuration)}")
            print("Capabilities:")
        _print_capabilities(provider)

    else:
        print("Specify a model action: list, show, capabilities")


def _print_changes(changes) -> None:
    for change in changes:
        old = "None" if change.old_value is None else f"{change.old_value:g}"
        print(f"  [{change.mote_id[:8]}] {change.key}: {old} -> {change.new_value:g}")


def _print_run_summary_line(run) -> None:
    applied = "applied" if run.committed else "saved"
    line = f"[{run.run_id}] run=#{run.run_number} {run.ran_at} {run.status} ({applied})"
    if run.status == "ok":
        line += f" ({run.change_count} change{'' if run.change_count == 1 else 's'})"
    else:
        line += f": {run.detail}"
    print(line)


def _handle_simulate(conn, args) -> None:
    action = args.action or "run"

    if action == "run":
        try:
            result = run_simulation(conn)
        except SimulationError as e:
            print(f"Simulation failed: {e}")
            return
        print(result.summary)
        _print_changes(result.changes)

    elif action == "history":
        for run in list_runs(conn, limit=getattr(args, "limit", None)):
            _print_run_summary_line(run)

    elif action == "show":
        run = get_run(conn, args.id)
        if run is None:
            print("No run found.")
            return
        applied = "applied to graph" if run.committed else "saved (not applied to graph)"
        print(f"Run {run.run_id}")
        print(f"run=#{run.run_number}  {run.ran_at}  status={run.status}  ({applied})")
        if run.detail:
            print(f"detail: {run.detail}")
        _print_changes(run_changes(conn, run.run_id))

    else:
        print("Specify a simulate action: run, history, show")


def main() -> None:
    args = build_parser().parse_args()

    try:
        if args.command == "garden":
            _handle_garden(args)
            return

        garden = resolve_garden(args.garden)
        conn = get_connection(garden)
        try:
            initialize_database(conn)
            if args.command == "mote":
                _handle_mote(conn, args)
            elif args.command == "rel":
                _handle_rel(conn, args)
            elif args.command == "model":
                _handle_model(conn, args)
            elif args.command == "simulate":
                _handle_simulate(conn, args)
        finally:
            conn.close()
    except Exception as e:
        logger.exception("monoss command failed")
        print(f"Error: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    main()
