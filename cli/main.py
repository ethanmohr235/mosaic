# cli/main.py
import argparse
import json
import logging

from core.motes import create_mote, list_motes, get_mote, delete_mote, update_mote
from core.relationships import (
    create_relationship,
    list_relationships,
    get_relationship,
    update_relationship,
    delete_relationship,
)
from core.gardens import create_garden, list_gardens, resolve_garden
from core.db import get_connection, initialize_database

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

    return parser


def _handle_garden(args) -> None:
    if args.action == "create":
        create_garden(args.name)
        print(f"Garden created: {args.name}")
    elif args.action == "list":
        for name in list_gardens():
            print(name)
    else:
        print("Specify a garden action: create, list")


def _handle_mote(conn, args) -> None:
    if args.action == "create":
        props = _parse_properties_json(getattr(args, "properties", None))
        mote_id = create_mote(conn, props)
        print(f"Mote created with id={mote_id}")

    elif args.action == "get":
        mote = get_mote(conn, args.id)
        print(f"[{mote.id}] {mote.properties}" if mote else "No mote found.")

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
        finally:
            conn.close()
    except Exception as e:
        logger.exception("monoss command failed")
        print(f"Error: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    main()
