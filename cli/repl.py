# cli/repl.py
import json
import logging
import shlex

from core.db import get_connection, initialize_database
from core.motes import create_mote, list_motes, get_mote, get_motes, delete_mote, update_mote
from core.relationships import (
    create_relationship,
    list_relationships,
    get_relationship,
    update_relationship,
    delete_relationship,
)
from core.gardens import (
    create_garden,
    delete_garden,
    list_gardens,
    garden_exists,
    resolve_garden,
    restore_garden,
)
from core import model_resolver
from core.providers import PropertyModelError
from core.simulation import SimulationError, get_run, list_runs, run_changes, run_simulation

logger = logging.getLogger(__name__)

HELP_TEXT = """
    Commands:
    garden create <name>                                  Create a new garden
    garden list                                            List all gardens
    garden use <name>                                      Switch to a garden
    garden delete <name>                                   Delete a garden (recoverable via restore)
    garden restore <name>                                  Restore a deleted garden

    mote create [key=value ...]                            Create a new mote with optional properties
    mote get id=<mote_id>                                  Get a mote by id
    mote list                                              List all motes
    mote update id=<mote_id> [key=value ...]               Update properties for a mote
    mote delete id=<mote_id>                                Delete a mote

    rel create from=<mote_id> to=<mote_id> type=<type>     Create a relationship
    rel get id=<rel_id>                                    Get a relationship by id
    rel list [from=<mote_id>] [type=<type>]                List relationships
    rel update id=<rel_id> [from=<id>] [to=<id>] [type=<t>] Update a relationship
    rel delete id=<rel_id>                                  Delete a relationship

    model list                                             List physical property models
    model show id=<model_id>                                Show a property model's details
    model capabilities id=<model_id>                       List a property model's capabilities

    simulate                                                Run the garden's simulation rules
    simulate history [limit=<n>]                            List past simulation runs
    simulate show id=<run_id>                               Show one run's changes

    exit / quit                                            Exit REPL

    Property values are parsed as JSON when possible (age=21 -> int, active=true -> bool,
    tags=[1,2] -> list); anything that isn't valid JSON is kept as a plain string. To force
    a string that looks like a number or keyword, quote it: phone='"12345"'.
"""


def parse_properties(args_list: list[str]) -> dict:
    """Parse key=value pairs into a dict, decoding JSON-typed values where possible."""
    props = {}
    for arg in args_list:
        if '=' not in arg:
            continue
        key, val = arg.split('=', 1)
        try:
            props[key] = json.loads(val)
        except json.JSONDecodeError:
            props[key] = val
    return props


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
    printed after `mote get` so the property-model attached to a mote is
    visible without a separate lookup."""
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


def repl(garden: str | None = None) -> None:
    current_garden = resolve_garden(garden)
    conn = get_connection(current_garden)
    initialize_database(conn)
    print("MonOSS v0")

    while True:
        try:
            line = input(f"monoss ({current_garden})> ").strip()
            if not line:
                continue

            tokens = shlex.split(line)
            cmd = tokens[0].lower()
            args = tokens[1:]

            if cmd in ("exit", "quit"):
                print("Goodbye.")
                break

            elif cmd == "help":
                print(HELP_TEXT)

            # --------- GARDEN COMMANDS ---------
            elif cmd == "garden":
                if not args:
                    print("Specify an action: create, list, use")
                    continue
                action = args[0].lower()
                action_args = args[1:]

                if action == "create":
                    if not action_args:
                        print("Error: garden name required")
                        continue
                    create_garden(action_args[0])
                    print(f"Garden created: {action_args[0]}")

                elif action == "list":
                    for name in list_gardens():
                        print(name)

                elif action in ("use", "switch"):
                    if not action_args:
                        print("Error: garden name required")
                        continue
                    name = action_args[0]
                    if not garden_exists(name):
                        print(f"No such garden: {name!r}")
                        continue
                    conn.close()
                    current_garden = name
                    conn = get_connection(current_garden)
                    initialize_database(conn)
                    print(f"Switched to garden: {current_garden}")

                elif action == "delete":
                    if not action_args:
                        print("Error: garden name required")
                        continue
                    name = action_args[0]
                    delete_garden(name)
                    print(f"Garden deleted: {name} (run `garden restore {name}` to undo)")

                elif action == "restore":
                    if not action_args:
                        print("Error: garden name required")
                        continue
                    name = action_args[0]
                    restore_garden(name)
                    print(f"Garden restored: {name}")

                else:
                    print(f"Unknown garden action: {action}")

            # --------- MOTE COMMANDS ---------
            elif cmd == "mote":
                if not args:
                    print("Specify an action: create, get, list, update, delete")
                    continue
                action = args[0].lower()
                action_args = args[1:]

                if action == "create":
                    props = parse_properties(action_args)
                    mote_id = create_mote(conn, props)
                    print(f"Mote created with id={mote_id}")

                elif action == "get":
                    props = parse_properties(action_args)
                    if "id" not in props:
                        print("Error: 'id' is required")
                        continue
                    mote = get_mote(conn, props["id"])
                    if mote is None:
                        print("No mote found.")
                    else:
                        print(f"[{mote.id}] {mote.properties}")
                        _print_models(conn, mote.id)

                elif action == "list":
                    for m in list_motes(conn):
                        print(f"[{m.id}] {m.properties}")

                elif action == "update":
                    props = parse_properties(action_args)
                    if "id" not in props:
                        print("Error: 'id' is required")
                        continue
                    mote_id = props.pop("id")
                    if not props:
                        print("Error: no properties provided")
                        continue
                    update_mote(conn, mote_id, props)
                    print(f"Mote with id={mote_id} updated with props={props}")

                elif action == "delete":
                    props = parse_properties(action_args)
                    if "id" not in props:
                        print("Error: 'id' is required")
                        continue
                    found = delete_mote(conn, props["id"])
                    print(f"Mote deleted with id={props['id']}" if found else "No mote found.")

                else:
                    print(f"Unknown mote action: {action}")

            # --------- RELATIONSHIP COMMANDS ---------
            elif cmd == "rel":
                if not args:
                    print("Specify an action: create, get, list, update, delete")
                    continue
                action = args[0].lower()
                action_args = parse_properties(args[1:])

                if action == "create":
                    if not all(k in action_args for k in ("from", "to", "type")):
                        print("Error: 'from', 'to', and 'type' are required")
                        continue
                    rel_id = create_relationship(
                        conn,
                        action_args["from"],
                        action_args["to"],
                        action_args["type"]
                    )
                    print(f"Relationship created: {action_args['from']} -> {action_args['to']} ({action_args['type']}) [{rel_id}]")

                elif action == "get":
                    if "id" not in action_args:
                        print("Error: 'id' is required")
                        continue
                    rel = get_relationship(conn, action_args["id"])
                    print(f"[{rel.id}] {rel.from_id} -> {rel.to_id} ({rel.type})" if rel else "No relationship found.")

                elif action == "list":
                    from_id = action_args.get("from")
                    type_ = action_args.get("type")
                    for r in list_relationships(conn, from_id, type_):
                        print(f"[{r.id}] {r.from_id} -> {r.to_id} ({r.type})")

                elif action == "update":
                    if "id" not in action_args:
                        print("Error: 'id' is required")
                        continue
                    rel_id = action_args.pop("id")
                    found = update_relationship(
                        conn,
                        rel_id,
                        action_args.get("from"),
                        action_args.get("to"),
                        action_args.get("type"),
                    )
                    print(f"Relationship updated: {rel_id}" if found else "No relationship found.")

                elif action == "delete":
                    if "id" not in action_args:
                        print("Error: 'id' is required")
                        continue
                    found = delete_relationship(conn, action_args["id"])
                    print(f"Relationship deleted: {action_args['id']}" if found else "No relationship found.")

                else:
                    print(f"Unknown rel action: {action}")

            # --------- MODEL COMMANDS ---------
            elif cmd == "model":
                if not args:
                    print("Specify an action: list, show, capabilities")
                    continue
                action = args[0].lower()
                action_args = parse_properties(args[1:])

                if action == "list":
                    for model_id in _model_mote_ids(conn):
                        mote = get_mote(conn, model_id)
                        if mote is None:
                            continue
                        name = mote.properties.get("name", mote.id[:8])
                        provider = mote.properties.get(model_resolver.PROVIDER_KEY, "?")
                        print(f"[{mote.id}] {name} (provider={provider})")

                elif action in ("show", "capabilities"):
                    if "id" not in action_args:
                        print("Error: 'id' is required")
                        continue
                    mote = get_mote(conn, action_args["id"])
                    if mote is None:
                        print("No mote found.")
                        continue
                    try:
                        provider = model_resolver.construct(mote.id, mote.properties)
                    except PropertyModelError as e:
                        print(f"Error: {e}")
                        continue

                    if action == "show":
                        name = mote.properties.get("name", mote.id[:8])
                        print(f"Model: {name} [{mote.id}]")
                        print(f"Provider: {mote.properties.get(model_resolver.PROVIDER_KEY)}")
                        configuration = mote.properties.get(model_resolver.CONFIGURATION_KEY, {})
                        print(f"Configuration: {json.dumps(configuration)}")
                        print("Capabilities:")
                    _print_capabilities(provider)

                else:
                    print(f"Unknown model action: {action}")

            # --------- SIMULATE COMMANDS ---------
            elif cmd == "simulate":
                action = args[0].lower() if args else "run"

                if action == "run":
                    action_args = parse_properties(args[1:])
                    solver = action_args.get("solver", "linear")
                    try:
                        result = run_simulation(conn, solver=solver)
                    except SimulationError as e:
                        print(f"Simulation failed: {e}")
                        continue
                    print(result.summary)
                    _print_changes(result.changes)

                elif action == "history":
                    action_args = parse_properties(args[1:])
                    limit = action_args.get("limit")
                    for run in list_runs(conn, limit=limit):
                        _print_run_summary_line(run)

                elif action == "show":
                    action_args = parse_properties(args[1:])
                    if "id" not in action_args:
                        print("Error: 'id' is required")
                        continue
                    run = get_run(conn, action_args["id"])
                    if run is None:
                        print("No run found.")
                        continue
                    applied = "applied to graph" if run.committed else "saved (not applied to graph)"
                    print(f"Run {run.run_id}")
                    print(f"run=#{run.run_number}  {run.ran_at}  status={run.status}  ({applied})")
                    if run.detail:
                        print(f"detail: {run.detail}")
                    _print_changes(run_changes(conn, run.run_id))

                else:
                    print(f"Unknown simulate action: {action}")

            else:
                print(f"Unknown command: {cmd}. Type 'help' for available commands.")

        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye.")
            break
        except Exception as e:
            logger.exception("Unhandled REPL error")
            print(f"Error: {e}")

    conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    repl()
