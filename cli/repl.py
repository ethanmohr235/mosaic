# cli/repl.py
import json
import logging
import shlex

from core.db import get_connection, initialize_database
from core.motes import create_mote, list_motes, get_mote, delete_mote, update_mote
from core.relationships import (
    create_relationship,
    list_relationships,
    get_relationship,
    update_relationship,
    delete_relationship,
)
from core.gardens import create_garden, list_gardens, garden_exists, resolve_garden

logger = logging.getLogger(__name__)

HELP_TEXT = """
    Commands:
    garden create <name>                                  Create a new garden
    garden list                                            List all gardens
    garden use <name>                                      Switch to a garden

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
                    print(f"[{mote.id}] {mote.properties}" if mote else "No mote found.")

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
