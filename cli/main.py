# cli/main.py
import argparse
import json
import logging
from pathlib import Path

from cli import output
from core.folders import folder_members, folders_of, list_folders, set_folder
from core.library import LIBRARY_GARDEN, reuse_from_library, save_to_library
from core import modoc
from core.modoc_bind import resolve as resolve_modoc
from core.motes import create_mote, list_motes, get_mote, delete_mote, search_motes, update_mote
from core.portals import create_portal, resolve_portal
from core.species import (
    create_instance,
    instances_of,
    list_species,
    set_species,
    set_template,
    species_of,
)
from core.relationships import (
    create_relationship,
    list_relationships,
    get_relationship,
    update_relationship,
    delete_relationship,
)
from core.gardens import create_garden, delete_garden, list_gardens, resolve_garden, restore_garden
from core.db import get_connection, initialize_database
from core.simulation import SimulationError, list_runs, run_simulation

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

    search_parser = mote_sub.add_parser("search", help="Find motes by text")
    search_parser.add_argument("query", help='Text, or "key = value" for an exact property match')
    search_parser.add_argument("--limit", type=int, default=60, help="Maximum results (default 60)")

    # ------------------ SPECIES COMMANDS ------------------
    species_parser = subparsers.add_parser("species", help="Species, templates and instances")
    species_sub = species_parser.add_subparsers(dest="action")
    species_sub.add_parser("list", help="List every mote acting as a species")
    species_of_parser = species_sub.add_parser("of", help="Species a mote is an instance of")
    species_of_parser.add_argument("--id", required=True, help="Mote id")
    instances_parser = species_sub.add_parser("instances", help="Instances of a species")
    instances_parser.add_argument("--id", required=True, help="Species mote id")
    species_set = species_sub.add_parser("set", help="Declare a mote an instance of a species")
    species_set.add_argument("--id", required=True, help="Mote id")
    species_set.add_argument("--species", required=True, help="Species mote id")
    template_set = species_sub.add_parser("template", help="Make a mote a species' template")
    template_set.add_argument("--id", required=True, help="Template mote id")
    template_set.add_argument("--species", required=True, help="Species mote id")
    instance_new = species_sub.add_parser("new", help="Create an instance, seeded from the template")
    instance_new.add_argument("--species", required=True, help="Species mote id")
    instance_new.add_argument("--properties", help="JSON overrides applied on top of the template")

    # ------------------ FOLDER COMMANDS ------------------
    folder_parser = subparsers.add_parser("folder", help="Organize motes into folders")
    folder_sub = folder_parser.add_subparsers(dest="action")
    folder_sub.add_parser("list", help="List every mote acting as a folder")
    folder_of_parser = folder_sub.add_parser("of", help="Folders a mote is filed under")
    folder_of_parser.add_argument("--id", required=True, help="Mote id")
    members_parser = folder_sub.add_parser("members", help="Motes filed under a folder")
    members_parser.add_argument("--id", required=True, help="Folder mote id")
    folder_set = folder_sub.add_parser("set", help="File a mote under a folder")
    folder_set.add_argument("--id", required=True, help="Mote id")
    folder_set.add_argument("--folder", required=True, help="Folder mote id")

    # ------------------ PORTAL COMMANDS ------------------
    portal_parser = subparsers.add_parser("portal", help="One-way references into another garden")
    portal_sub = portal_parser.add_subparsers(dest="action")
    portal_create = portal_sub.add_parser("create", help="Create a portal mote")
    portal_create.add_argument("--to-garden", required=True, help="Target garden name")
    portal_create.add_argument("--to-mote", required=True, help="Target mote id in that garden")
    portal_create.add_argument("--properties", help="JSON properties for the portal mote itself")
    portal_resolve = portal_sub.add_parser("resolve", help="Show what a portal points at")
    portal_resolve.add_argument("--id", required=True, help="Portal mote id")

    # ------------------ LIBRARY COMMANDS ------------------
    library_parser = subparsers.add_parser("library", help="Reusable motes, shared across gardens")
    library_sub = library_parser.add_subparsers(dest="action")
    library_sub.add_parser("list", help="List library entries")
    library_save = library_sub.add_parser("save", help="Save a mote's properties to the library")
    library_save.add_argument("--id", required=True, help="Mote id in the current garden")
    library_use = library_sub.add_parser("use", help="Copy a library entry into this garden")
    library_use.add_argument("--id", required=True, help="Library entry id")
    library_use.add_argument("--properties", help="JSON overrides applied on top of the entry")

    # ------------------ RELATIONSHIP COMMANDS ------------------
    modoc_parser = subparsers.add_parser("modoc", help="Notes written about the graph")
    modoc_sub = modoc_parser.add_subparsers(dest="action")
    modoc_render = modoc_sub.add_parser("render", help="Render a mote's note, values resolved")
    modoc_render.add_argument("id")
    modoc_render.add_argument("--step", type=int, default=0, help="Resolve at t+N (a preview; never written)")
    modoc_check = modoc_sub.add_parser("check", help="Report a note's unresolved links and values")
    modoc_check.add_argument("id")
    modoc_values = modoc_sub.add_parser("values", help="Every \\val in a note and what it resolves to")
    modoc_values.add_argument("id")
    modoc_values.add_argument("--step", type=int, default=0)
    modoc_export = modoc_sub.add_parser("export", help="Write a note out as a .modoc file")
    modoc_export.add_argument("id")
    modoc_export.add_argument("--out", help="File to write (default: <id>.modoc)")
    modoc_import = modoc_sub.add_parser("import", help="Read a .modoc file onto a mote")
    modoc_import.add_argument("file")
    modoc_import.add_argument("--mote", help="Existing mote to write onto (default: create one)")

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
            output.print_models(conn, mote.id)

    elif args.action == "list":
        output.print_motes(list_motes(conn))

    elif args.action == "search":
        output.print_motes(search_motes(conn, args.query, args.limit))

    elif args.action == "update":
        props = _parse_properties_json(args.properties)
        update_mote(conn, args.id, props)
        print(f"Mote with id={args.id} updated with props={props}")

    elif args.action == "delete":
        found = delete_mote(conn, args.id)
        print(f"Mote deleted with id={args.id}" if found else "No mote found.")

    else:
        print("Specify a mote action: create, get, list, search, update, delete")


def _handle_species(conn, args) -> None:
    if args.action == "list":
        output.print_motes(list_species(conn))

    elif args.action == "of":
        output.print_motes(species_of(conn, args.id))

    elif args.action == "instances":
        output.print_motes(instances_of(conn, args.id))

    elif args.action == "set":
        set_species(conn, args.id, args.species)
        print(f"Mote {args.id} is now an instance of {args.species}")

    elif args.action == "template":
        set_template(conn, args.id, args.species)
        print(f"Mote {args.id} is now the template for {args.species}")

    elif args.action == "new":
        overrides = _parse_properties_json(getattr(args, "properties", None))
        mote_id = create_instance(conn, args.species, overrides)
        print(f"Instance created with id={mote_id}")

    else:
        print("Specify a species action: list, of, instances, set, template, new")


def _handle_folder(conn, args) -> None:
    if args.action == "list":
        output.print_motes(list_folders(conn))

    elif args.action == "of":
        output.print_motes(folders_of(conn, args.id))

    elif args.action == "members":
        output.print_motes(folder_members(conn, args.id))

    elif args.action == "set":
        set_folder(conn, args.id, args.folder)
        print(f"Mote {args.id} filed under {args.folder}")

    else:
        print("Specify a folder action: list, of, members, set")


def _handle_portal(conn, args) -> None:
    if args.action == "create":
        properties = _parse_properties_json(getattr(args, "properties", None))
        mote_id = create_portal(conn, args.to_garden, args.to_mote, properties)
        print(f"Portal created with id={mote_id} -> {args.to_garden}/{args.to_mote}")

    elif args.action == "resolve":
        mote = get_mote(conn, args.id)
        if mote is None:
            print("No mote found.")
            return
        garden, target = resolve_portal(mote.properties)
        print(f"Garden: {garden}")
        print(f"[{target.id}] {target.properties}")

    else:
        print("Specify a portal action: create, resolve")


def _handle_library(conn, args) -> None:
    """The library is a reserved garden, so `library list` reads it
    through its own short-lived connection rather than the one the rest
    of this command is using -- same pattern core/library.py itself
    follows for save/reuse."""
    if args.action == "list":
        library_conn = get_connection(LIBRARY_GARDEN)
        try:
            initialize_database(library_conn)
            output.print_motes(list_motes(library_conn))
        finally:
            library_conn.close()

    elif args.action == "save":
        entry_id = save_to_library(conn, args.id)
        print(f"Saved to library with id={entry_id}")

    elif args.action == "use":
        overrides = _parse_properties_json(getattr(args, "properties", None))
        mote_id = reuse_from_library(conn, args.id, overrides)
        print(f"Mote created with id={mote_id}")

    else:
        print("Specify a library action: list, save, use")


def _handle_modoc(conn, args) -> None:
    if args.action == "export":
        path = Path(args.out) if args.out else Path(f"{args.id}.modoc")
        print(f"Note written to {modoc.export_file(conn, args.id, path)}")
        return

    if args.action == "import":
        mote_id = modoc.import_file(conn, Path(args.file), args.mote)
        print(f"Note read onto mote id={mote_id}")
        return

    if args.action not in ("render", "check", "values"):
        print("Specify a modoc action: render, check, values, export, import")
        return

    document = modoc.parse(modoc.read_source(conn, args.id))
    resolution = resolve_modoc(conn, args.id, document, step=getattr(args, "step", 0))
    if args.action == "check":
        if output.print_diagnostics(document, resolution) == 0:
            print("No problems.")
    elif args.action == "values":
        _print_modoc_values(document, resolution)
    else:
        output.print_modoc(document, resolution)


def _print_modoc_values(document, resolution) -> None:
    """Every \\val a note contains, flat -- the scriptable form of what
    `render` shows in prose."""
    for node in modoc.values(document):
        result = resolution.values[modoc.value_key(node)]
        where = f"[{node.ref}]" if node.ref else ""
        detail = f"  ({result.error})" if result.error else ""
        print(f"{where}{node.expression} = {result.text}{detail}")


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


def _handle_model(conn, args) -> None:
    if args.action == "list":
        output.print_model_list(conn)
    elif args.action in ("show", "capabilities"):
        output.print_model_detail(conn, args.id, header=args.action == "show")
    else:
        print("Specify a model action: list, show, capabilities")


def _handle_simulate(conn, args) -> None:
    action = args.action or "run"

    if action == "run":
        try:
            result = run_simulation(conn)
        except SimulationError as e:
            print(f"Simulation failed: {e}")
            return
        print(result.summary)
        output.print_changes(result.changes)

    elif action == "history":
        for run in list_runs(conn, limit=getattr(args, "limit", None)):
            output.print_run_summary_line(run)

    elif action == "show":
        output.print_run_detail(conn, args.id)

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
            elif args.command == "species":
                _handle_species(conn, args)
            elif args.command == "folder":
                _handle_folder(conn, args)
            elif args.command == "portal":
                _handle_portal(conn, args)
            elif args.command == "library":
                _handle_library(conn, args)
            elif args.command == "modoc":
                _handle_modoc(conn, args)
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
