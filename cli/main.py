# cli/main.py
import argparse
import json
import logging
import shlex
import sys
from pathlib import Path

from core import calc
from core import csv_import
from core import images
from core import submit as submit_repo
from core import materials_provider
from core import uncertainty
from core import fit as fit_repo
from core import health
from core import projects
from core import search
from core import modoc
from core import worksheet
from core.modoc_bind import assemble as assemble_modoc
from core.modoc_bind import resolve as resolve_modoc
from core.motes import create_mote, list_motes, get_mote, delete_mote, search_motes, update_mote
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
from core.gardens import (
    create_garden,
    delete_garden,
    rename_garden,
    get_default_garden,
    list_gardens,
    resolve_garden,
    restore_garden,
)
from core import sync as sync_repo
from core.db import DEFAULT_GARDEN, get_connection, initialize_database
from cli.render import (
    BASE_UNIT_NOTE,
    change_lines,
    equation_lines,
    explanation_lines,
    impact_lines,
    requirement_lines,
    scenario_lines,
    variable,
)
from core import derivations
from core import model_resolver
from core.scenarios import get_scenario, list_scenarios
from core.providers import PropertyModelError
from core.simulation import (
    SimulationError,
    check_garden,
    check_roles,
    incidence_index,
    run_simulation,
)
from core.units import UnitError, convert, describe, dimension_of, list_units
from cli import render

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
    garden_rename = garden_sub.add_parser("rename", help="Rename a garden")
    garden_rename.add_argument("old", help="Current garden name")
    garden_rename.add_argument("new", help="New garden name")

    # ------------------ SYNC COMMANDS ------------------
    # A sibling of `mote`/`rel`, not `garden`: push/pull/run/status need
    # an open connection to the garden's own oplog, the same way those
    # do. `init` is the one action that may need to create the garden
    # first (cloning from existing sync history), so it's special-cased
    # in main() the same way `garden` itself is.
    sync_parser = subparsers.add_parser("sync", help="Sync a garden between devices")
    sync_sub = sync_parser.add_subparsers(dest="action")
    sync_init = sync_sub.add_parser("init", help="Point this garden at a sync server")
    sync_init.add_argument(
        "url",
        help="The sync server's URL, as http(s)://<token>@host:port/ "
             "(see docker-compose.yml for running one)",
    )
    sync_init.add_argument(
        "--clone", action="store_true",
        help="If the target already has history for this garden and it doesn't exist "
             "locally yet, create it here from that history",
    )
    sync_push = sync_sub.add_parser("push", help="Write local changes out to the sync target")
    sync_push.add_argument(
        "--resend", action="store_true",
        help="Resend this device's entire history -- only needed when moving an "
             "already-synced garden onto a new, empty sync_dir",
    )
    sync_sub.add_parser("pull", help="Apply other devices' changes from the sync target")
    sync_run = sync_sub.add_parser("run", help="push, then pull (the everyday one-shot sync)")
    sync_run.add_argument("--resend", action="store_true", help="See `sync push --resend`")
    sync_sub.add_parser("status", help="Show sync progress and any conflicts from the last pull")

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

    # ------------------ RELATIONSHIP COMMANDS ------------------
    modoc_parser = subparsers.add_parser("modoc", help="Notes written about the graph")
    modoc_sub = modoc_parser.add_subparsers(dest="action")
    modoc_render = modoc_sub.add_parser("render", help="Render a mote's note, values resolved")
    modoc_render.add_argument("id")
    modoc_check = modoc_sub.add_parser("check", help="Report a note's unresolved links and values")
    modoc_check.add_argument("id")
    modoc_values = modoc_sub.add_parser("values", help="Every \\val in a note and what it resolves to")
    modoc_values.add_argument("id")
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
    model_sub.add_parser(
        "materials", help="The bundled materials the `materials` provider knows")
    model_caps = model_sub.add_parser("capabilities", help="List a property model's capabilities")
    model_caps.add_argument("--id", required=True, help="Property-model mote id")

    # ------------------ UNIT COMMANDS ------------------
    unit_parser = subparsers.add_parser("unit", help="Inspect and convert units")
    unit_sub = unit_parser.add_subparsers(dest="action")
    unit_sub.add_parser("list", help="List registered units, grouped by dimension")
    unit_convert = unit_sub.add_parser("convert", help="Convert a value between units")
    unit_convert.add_argument("--value", type=float, required=True, help="Value to convert")
    unit_convert.add_argument("--from", dest="from_unit", required=True, help="Unit to convert from")
    unit_convert.add_argument("--to", dest="to_unit", required=True, help="Unit to convert to")
    unit_check = unit_sub.add_parser(
        "check", help="Check the garden's rules for dimensional consistency"
    )
    unit_check.add_argument("--id", help="Check only this rule mote id")

    # ------------------ SIMULATE COMMANDS ------------------
    simulate_parser = subparsers.add_parser(
        "simulate", help="Solve the garden's simulation rules and write the answer back"
    )
    simulate_sub = simulate_parser.add_subparsers(dest="action")
    run_parser = simulate_sub.add_parser("run", help="Run the simulation (default)")
    run_parser.add_argument(
        "--solver", default="linear",
        help="Solver backend for whatever the substitution loop can't resolve alone "
             "(default: linear; also available: newton, for equations genuinely "
             "nonlinear in their unknowns)",
    )
    run_parser.add_argument(
        "--strict-units", action="store_true",
        help="Fail the run on a dimensional mismatch instead of only warning",
    )
    run_parser.add_argument(
        "--strict-constraints", action="store_true",
        help="Fail the run on an unsatisfied constraint instead of only reporting it",
    )
    run_parser.add_argument(
        "--scenario",
        help="Solve under this scenario mote's overrides. Never writes to the graph -- "
             "reports what would change",
    )

    ### Scenarios
    scenario_parser = subparsers.add_parser(
        "scenario", help="Inspect named perturbations of the garden"
    )
    scenario_sub = scenario_parser.add_subparsers(dest="action")
    scenario_sub.add_parser("list", help="List scenarios and what they vary")
    scenario_show = scenario_sub.add_parser("show", help="Show one scenario's overrides")
    scenario_show.add_argument("--id", required=True, help="Scenario mote id")

    ### Roles
    roles_parser = subparsers.add_parser(
        "roles", help="Inspect how assemblies are wired to their parts"
    )
    roles_sub = roles_parser.add_subparsers(dest="action")
    roles_sub.add_parser("check", help="Check every declared role's wiring")

    ### Worksheet
    worksheet_parser = subparsers.add_parser(
        "worksheet", help="The garden's calculation as lines of text"
    )
    worksheet_sub = worksheet_parser.add_subparsers(dest="action")
    worksheet_sub.add_parser("read", help="Print the garden's givens and equations as lines")
    worksheet_apply = worksheet_sub.add_parser(
        "apply",
        help="Write lines into motes, rules and freedoms, deleting the rules whose lines are gone",
    )
    worksheet_apply.add_argument(
        "--file", help="Read the lines from this file instead of standard input"
    )

    ### Health
    health_parser = subparsers.add_parser(
        "health", help="Whether the model is solvable, and which values are not"
    )
    health_parser.add_argument("--scenario", help="Ask the same question of a scenario")

    ### Uncertainty
    unc_parser = subparsers.add_parser(
        "uncertainty", help="Each result's ±, propagated from the givens' ±, with its budget"
    )
    unc_parser.add_argument("--solver", default="linear", help="Solver backend (default linear)")

    ### Fit
    fit_parser = subparsers.add_parser(
        "fit", help="Fit inputs to measured points: fit m.y vs m.x --data Y vs X --free m.k … [--apply]"
    )
    fit_parser.add_argument("words", nargs=argparse.REMAINDER,
                            help="the console's fit line after `fit` (see docs/manual.md)")

    ### Search
    search_all = subparsers.add_parser(
        "search", help="Search every garden at once (read-only)"
    )
    search_all.add_argument("query", help="Text, or `key = value`")
    search_all.add_argument("--per-garden", type=int, default=20)
    search_all.add_argument("--no-archived", action="store_true",
                            help="Leave archived gardens out")

    ### Import
    import_parser = subparsers.add_parser(
        "import", help="Bring measured data in as motes"
    )
    import_sub = import_parser.add_subparsers(dest="action")
    import_csv = import_sub.add_parser(
        "csv", help="One mote per row; `load [kN]` in the header gives a unit"
    )
    import_csv.add_argument("file", help="The CSV file")
    import_csv.add_argument("--name", help="What to call the import (defaults to the file name)")
    import_csv.add_argument("--delimiter", default=",", help="Cell separator (default ',')")
    import_csv.add_argument(
        "--dry-run", action="store_true", help="Show what would be imported, and write nothing"
    )

    ### Image
    image_parser = subparsers.add_parser(
        "image", help="Pictures for notes: PNG or SVG, shown with \\image{name}"
    )
    image_sub = image_parser.add_subparsers(dest="action")
    image_add = image_sub.add_parser(
        "add", help="Store a picture (up to 4 MB); `-` reads it from stdin"
    )
    image_add.add_argument("file", help="A PNG or SVG file, or - for stdin")
    image_add.add_argument("--name", help="Defaults to the next shot-NNN")
    image_add.add_argument("--alt", help="Alt text")
    image_sub.add_parser("list", help="Every image in the garden")
    image_rename = image_sub.add_parser(
        "rename", help="Rename an image and every \\image{...} that names it"
    )
    image_rename.add_argument("image", help="Name or id")
    image_rename.add_argument("new_name")
    image_export = image_sub.add_parser("export", help="Write an image back out as a file")
    image_export.add_argument("image", help="Name or id")
    image_export.add_argument("path", nargs="?", help="Defaults to <name>.<png|svg>")

    ### Project
    project_parser = subparsers.add_parser(
        "project", help="Which motes own which gardens: start a project, see the tree"
    )
    project_sub = project_parser.add_subparsers(dest="action")
    project_start = project_sub.add_parser(
        "start", help="Give a mote a garden of its own, creating it (the mote becomes its cover)"
    )
    project_start.add_argument("mote_id", help="The mote that will own the project")
    project_start.add_argument(
        "--name", help="The garden's name; a loose garden of this name is adopted. "
        "Derived from the mote's name when omitted"
    )
    project_stop = project_sub.add_parser(
        "stop", help="Drop a mote's claim on its garden, leaving the garden loose"
    )
    project_stop.add_argument("mote_id")
    project_sub.add_parser("tree", help="Every garden, nested under the mote that owns it")
    for verb, text in (("archive", "Make a garden and everything beneath it read-only"),
                       ("unarchive", "Make an archived garden writable again")):
        verb_parser = project_sub.add_parser(verb, help=text)
        verb_parser.add_argument("name", help="The garden")
    project_sub.add_parser(
        "check", help="Gardens with two owners, claims on missing gardens, and loops"
    )

    ### Calc
    plot_parser = subparsers.add_parser(
        "plot", help="Export a plot as a publication figure (svg, pdf, tex) or its points (csv)"
    )
    plot_sub = plot_parser.add_subparsers(dest="action")
    plot_export = plot_sub.add_parser("export", help="Re-solve a plot line and write it out")
    plot_export.add_argument("format", choices=["svg", "pdf", "tex", "csv"])
    plot_export.add_argument("words", nargs=argparse.REMAINDER,
                             help="The plot line, e.g. bd.l vs bd.x 1:9 --data L vs X; "
                                  "end with --out PATH to choose the file")

    export_parser = subparsers.add_parser(
        "export", help="Write the project's document as .tex (with figure files) or a PDF"
    )
    export_parser.add_argument("action", choices=["tex", "pdf"])
    export_parser.add_argument("--note", help="Which note (default: the garden's root note)")
    export_parser.add_argument("--out", help="Path (default: <note>.tex/.pdf; tex also writes images/)")

    submit_parser = subparsers.add_parser(
        "submit", help="Freeze the document as the next revision (PDF, .tex, values) and mark the owner submitted"
    )
    submit_parser.add_argument("--note", help="Which note (default: the garden's root note)")
    submit_parser.add_argument("--force", action="store_true",
                               help="Freeze even with failed checks or unresolved values")

    revisions_parser = subparsers.add_parser("revisions", help="Frozen submissions of this project")
    revisions_sub = revisions_parser.add_subparsers(dest="action")
    revisions_sub.add_parser("list", help="Every revision")
    revisions_diff = revisions_sub.add_parser("diff", help="Values that changed between two revisions")
    revisions_diff.add_argument("a")
    revisions_diff.add_argument("b")
    revisions_get = revisions_sub.add_parser("export", help="Write a revision's PDF back out")
    revisions_get.add_argument("revision")
    revisions_get.add_argument("path", nargs="?", help="Defaults to <revision>.pdf")
    revisions_get.add_argument("--tex", action="store_true", help="Write the .tex instead")

    calc_parser = subparsers.add_parser(
        "calc", help="A calculation's givens, answers, assumptions, checks and shown work"
    )
    calc_sub = calc_parser.add_subparsers(dest="action")
    for name, help_text in (
        ("given", "Every number the equations read and cannot determine"),
        ("answers", "Every value the solver is free to determine"),
        ("assumptions", "Every value marked `source: assumed`"),
        ("external", "Every value marked `source: external`"),
        ("checks", "Every comparison relation, and whether it holds"),
        ("verification", "Hand values beside the external values that verify them"),
    ):
        section = calc_sub.add_parser(name, help=help_text)
        if name != "checks":
            _add_format_arguments(section)
    calc_work = calc_sub.add_parser(
        "work", help="The equation, the equation with values in it, and the result"
    )
    calc_work.add_argument("mote_id", help="Mote whose property to show the work for")
    calc_work.add_argument("key", help="Property key to show the work for")
    _add_format_arguments(calc_work)

    ### Why
    why_parser = subparsers.add_parser(
        "why", help="Explain how a solved property value was determined"
    )
    why_parser.add_argument("mote_id", help="Mote whose property to explain")
    why_parser.add_argument("key", help="Property key to explain")
    why_parser.add_argument(
        "--depth", type=int, default=derivations.DEFAULT_DEPTH,
        help=f"How far back to follow derived inputs (default: {derivations.DEFAULT_DEPTH})",
    )

    ### Dependency queries
    # All four read the same derived index (core/incidence.py) and take
    # the same target, so they are built from one loop rather than four
    # near-identical blocks.
    for name, help_text in (
        ("impact", "What could move if a value changed"),
        ("requires", "What must be known before a value can be determined"),
        ("producers", "Which equations could determine a value"),
        ("consumers", "Which equations read a value"),
    ):
        query = subparsers.add_parser(name, help=help_text)
        query.add_argument("mote_id", help="Mote the property belongs to")
        query.add_argument("key", help="Property key")
        if name == "impact":
            query.add_argument(
                "--distance", type=int,
                help="Stop after this many hops through the equation graph",
            )

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
    elif args.action == "rename":
        rename_garden(args.old, args.new)
        print(f"Garden renamed: {args.old} -> {args.new}")
    else:
        print("Specify a garden action: create, list, delete, restore, rename")


def _handle_sync_init(garden: str, args) -> None:
    result = sync_repo.init(garden, args.url, clone=args.clone)
    if result["cloned"]:
        print(f"Garden {garden!r} synced to {args.url} (cloned existing history)")
    else:
        print(f"Garden {garden!r} now syncs through {args.url}")


def _print_sync_notes(skipped: list[dict]) -> None:
    for note in skipped:
        print(f"  {note['reason']}: {note['entity']} (from device {note['device_id']})")


def _handle_sync(conn, garden: str, args) -> None:
    if args.action == "push":
        result = sync_repo.push(conn, garden, resend=args.resend)
        print(f"Pushed {result['pushed']} change(s)")

    elif args.action == "pull":
        result = sync_repo.pull(conn, garden)
        print(f"Applied {result['applied']} change(s)")
        _print_sync_notes(result["skipped"])

    elif args.action == "run":
        result = sync_repo.run(conn, garden, resend=args.resend)
        print(f"Pushed {result['push']['pushed']} change(s), applied {result['pull']['applied']} change(s)")
        _print_sync_notes(result["pull"]["skipped"])

    elif args.action == "status":
        result = sync_repo.status(conn, garden)
        if result["sync_dir"] is None:
            print(f"Garden {garden!r} is not syncing -- run `sync init <url>` first")
            return
        print(f"Sync server: {result['sync_dir']}")
        print(f"Local changes: {result['local_ops']} total, {result['local_ops_pending']} not yet pushed")
        if not result["remotes"]:
            print("No other device has synced with this garden yet.")
        for remote in result["remotes"]:
            print(f"  device {remote['device_id']}: {remote['last_applied_seq']} change(s) applied from it")

    else:
        print("Specify a sync action: init, push, pull, run, status")


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
            render.print_models(conn, mote.id)

    elif args.action == "list":
        render.print_motes(list_motes(conn))

    elif args.action == "search":
        render.print_motes(search_motes(conn, args.query, args.limit))

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
        render.print_motes(list_species(conn))

    elif args.action == "of":
        render.print_motes(species_of(conn, args.id))

    elif args.action == "instances":
        render.print_motes(instances_of(conn, args.id))

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

    # assemble(), not parse(): a note's `\include`s are spliced in and the
    # whole assembled document is numbered as one.
    document = assemble_modoc(conn, args.id)
    resolution = resolve_modoc(conn, args.id, document)
    if args.action == "check":
        if render.print_diagnostics(document, resolution) == 0:
            print("No problems.")
    elif args.action == "values":
        _print_modoc_values(document, resolution)
    else:
        render.print_modoc(document, resolution)


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
        render.print_model_list(conn)
    elif args.action in ("show", "capabilities"):
        render.print_model_detail(conn, args.id, header=args.action == "show")
    elif args.action == "materials":
        for m in materials_provider.list_materials():
            print(f"{m['name']:<16} {m['description']}  "
                  f"(E {m['youngs_modulus'] / 1e9:g} GPa, nu {m['poisson_ratio']:g}, "
                  f"Sy {m['yield_strength'] / 1e6:g} MPa, rho {m['density']:g} kg/m^3)")
    else:
        print("Specify a model action: list, show, capabilities, materials")


def _handle_unit(conn, args) -> None:
    if args.action == "list":
        by_dimension: dict[str, list[str]] = {}
        for name in list_units():
            by_dimension.setdefault(describe(dimension_of(name)), []).append(name)
        for dimension, names in sorted(by_dimension.items()):
            print(f"{dimension}: {', '.join(names)}")

    elif args.action == "convert":
        try:
            result = convert(args.value, args.from_unit, args.to_unit)
        except UnitError as e:
            print(f"Error: {e}")
            return
        print(f"{args.value:g} {args.from_unit} = {result:g} {args.to_unit}")

    elif args.action == "check":
        _print_dimension_check(conn, getattr(args, "id", None))

    else:
        print("Specify a unit action: list, convert, check")


def _print_dimension_check(conn, rule_id: str | None) -> None:
    report = check_garden(conn, rule_id)
    if report.pairs_checked == 0:
        print("No rules to check.")
        return
    if not report.problems:
        print(f"OK -- no dimensional problems in {report.pairs_checked} rule/instance pair(s).")
        return
    print(f"{len(report.problems)} dimensional problem(s):")
    for problem in report.problems:
        print(f"  {problem}")


def _handle_simulate(conn, args) -> None:
    action = args.action or "run"

    if action == "run":
        try:
            result = run_simulation(
                conn,
                solver=getattr(args, "solver", "linear"),
                strict_units=getattr(args, "strict_units", False),
                strict_constraints=getattr(args, "strict_constraints", False),
                scenario=getattr(args, "scenario", None),
            )
        except SimulationError as e:
            print(f"Simulation failed: {e}")
            return
        print(result.summary)
        for warning in result.dimension_warnings:
            print(f"  warning: {warning}")
        for violation in result.constraint_violations:
            print(f"  constraint: {violation}")
        for line in change_lines(result.changes):
            print(line)

    else:
        print("Specify a simulate action: run")


def _handle_why(conn, args) -> None:
    explanation = derivations.explain(conn, args.mote_id, args.key, depth=args.depth)
    if explanation.derived:
        print(BASE_UNIT_NOTE)
    for line in explanation_lines(conn, explanation):
        print(line)


def _handle_scenario(conn, args) -> None:
    if args.action == "show":
        scenario = get_scenario(conn, args.id)
        if scenario is None:
            print("No scenario found. A scenario is a mote with a 'varies' relationship.")
            return
        for line in scenario_lines(conn, scenario):
            print(line)
        return

    if args.action not in (None, "list"):
        print("Specify a scenario action: list, show")
        return

    scenarios = list_scenarios(conn)
    if not scenarios:
        print("No scenarios in this garden.")
        return
    for scenario in scenarios:
        motes = len(scenario.target_ids)
        print(
            f"[{scenario.mote_id}] {scenario.name} "
            f"({len(scenario.overrides)} override(s) over {motes} mote(s))"
        )


def _handle_roles(conn, args) -> None:
    if args.action not in (None, "check"):
        print("Specify a roles action: check")
        return

    report = check_roles(conn)
    if report.motes_checked == 0:
        print("No motes declare roles.")
    elif not report.problems:
        print(f"OK -- {report.motes_checked} mote(s) with declared roles are wired correctly.")
    else:
        print(f"{len(report.problems)} role problem(s):")
        for problem in report.problems:
            print(f"  {problem}")

    if report.once_scoped:
        print(f"{len(report.once_scoped)} rule(s) asserted about a single mote:")
        for line in report.once_scoped:
            print(f"  {line}")


def _add_format_arguments(parser) -> None:
    """`--unit` and `--figures`, which are `\\val{x}{in, 3}`'s two halves
    spelled out -- a flag reads better than a comma list on a command
    line, and both reach the same core/calc.py formatting."""
    parser.add_argument("--unit", help="Convert into this unit for display")
    parser.add_argument(
        "--figures", type=int, help="Quote to this many significant figures"
    )


_CALC_SECTIONS = {
    "given": calc.givens,
    "answers": calc.answers,
    "assumptions": calc.assumptions,
    "external": calc.external,
}


def _handle_calc(conn, args) -> None:
    action = args.action
    if action == "work":
        render.print_work(calc.work(
            conn, args.mote_id, args.key,
            getattr(args, "unit", None), getattr(args, "figures", None),
        ))
        return
    if action == "checks":
        render.print_checks(calc.checks(conn))
        return
    if action == "verification":
        render.print_verification(
            calc.verification(conn), getattr(args, "unit", None), getattr(args, "figures", None)
        )
        return
    reader = _CALC_SECTIONS.get(action)
    if reader is None:
        print(f"Specify a calc action: {', '.join([*_CALC_SECTIONS, 'checks', 'verification', 'work'])}")
        return
    render.print_calc_rows(
        reader(conn), getattr(args, "unit", None), getattr(args, "figures", None)
    )


def _handle_worksheet(conn, args) -> None:
    action = args.action or "read"
    if action == "read":
        text = worksheet.read(conn)
        print(text if text else "Nothing to show -- this garden has no equations.")
        return
    if action != "apply":
        print("Specify a worksheet action: read, apply")
        return

    path = getattr(args, "file", None)
    text = Path(path).read_text() if path else sys.stdin.read()
    applied = worksheet.apply(conn, text)
    _print_applied(applied)


def _print_applied(applied: worksheet.Applied) -> None:
    print(
        f"Wrote {applied.givens_written} given(s) and {applied.rules_written} rule(s)."
        + (f" Created {', '.join(applied.motes_created)}." if applied.motes_created else "")
    )
    if applied.deleted:
        print(f"{len(applied.deleted)} rule(s) had no line and were deleted:")
        for label in applied.deleted:
            print(f"  {label}")
    if applied.untouched:
        print(f"{applied.untouched} rule(s) about a species were left alone.")
    for problem in applied.problems:
        print(f"  problem: {problem}")


def _handle_health(conn, args) -> None:
    report = health.check(conn, getattr(args, "scenario", None))
    print(report.summary)
    for group in report.groups:
        print(f"  {group.message}")
    for culprit in report.culprits:
        print(f"  {culprit.label}: {culprit.reason}")
    for problem in report.unit_problems:
        print(f"  units: {problem}")
    if report.inert:
        print(f"{len(report.inert)} relation(s) determine nothing (a check, or all givens):")
        for inert in report.inert:
            print(f"  {inert.label} on {inert.anchor}")


def _handle_fit(conn, args) -> None:
    line = fit_repo.parse(args.words)
    result = fit_repo.run(conn, line)
    for param in result.params:
        sigma = "" if param.sigma is None else f" ± {param.sigma:.3g}"
        unit = f" {param.unit}" if param.unit else ""
        print(f"{param.ref} = {param.value:.6g}{sigma}{unit}   (was {param.start:g})")
    if result.rms is not None:
        r2 = "" if result.r2 is None else f", R² {result.r2:.5f}"
        state = "converged" if result.converged else "did not converge"
        print(f"{len(result.points) - len(result.skipped)} points, RMS {result.rms:.4g}{r2}; "
              f"{state} in {result.iterations} iteration(s), {result.solves} solves")
    for name, why in result.skipped:
        print(f"  left out {name}: {why}")
    if result.message:
        print(f"  {result.message}")
    if line.apply:
        print(fit_repo.apply(conn, result)[0])


def _handle_uncertainty(conn, args) -> None:
    report = uncertainty.propagate(conn, args.solver)
    for problem in report.problems:
        print(f"  problem: {problem}")
    if not report.inputs:
        print("No given carries a ± -- write one as `uncertainty: {x: 0.5}` or "
              "`bd.x = 80 ± 0.5 in` in the worksheet.")
        return
    for result in report.results:
        shown = calc.Quantity(result.value, result.unit, result.sigma)
        print(f"{result.label} = {shown.text(4)}")
        for part in result.contributions:
            print(f"    {part.share * 100:5.1f}%  {part.label} (± {part.sigma:g}, "
                  f"sensitivity {part.sensitivity:.4g})")


def _handle_import(conn, args) -> None:
    path = Path(args.file)
    parsed = csv_import.parse(
        path.read_text(encoding="utf-8-sig"), path.name, delimiter=args.delimiter
    )
    columns = ", ".join(
        f"{c.key} [{c.unit}]" if c.unit else c.key for c in parsed.columns
    )
    print(f"{path.name}: {len(parsed.rows)} row(s); columns {columns or '(none)'}")
    for problem in parsed.problems:
        print(f"  problem: {problem}")
    if parsed.problems:
        print("Nothing was imported.")
        return
    if args.dry_run:
        return
    imported, _ = csv_import.apply(conn, parsed, args.name)
    print(f"Imported {len(imported.row_ids)} row(s) under {imported.import_id}.")


def _handle_plot(conn, args) -> None:
    import csv as csv_module
    from core import plotexport
    words = list(args.words)
    out = None
    if "--out" in words:
        at = words.index("--out")
        out = words[at + 1] if at + 1 < len(words) else None
        words = words[:at] + words[at + 2:]
    if args.action != "export" or not words:
        print("usage: monoss plot export svg|pdf|tex|csv <plot line> [--out PATH]")
        return
    line = "plot " + " ".join(shlex.quote(w) for w in words)
    try:
        if args.format == "csv":
            pub = plotexport.build(conn, line)
            columns = [pub.figure["xLabel"]] + [s["label"] for s in pub.figure["series"]]
            xs = sorted({p[0] for s in pub.figure["series"] for p in s["points"] if p})
            by_x = [{p[0]: p[1] for p in s["points"] if p} for s in pub.figure["series"]]
            path = Path(out or f"{pub.name}.csv")
            with path.open("w", newline="") as handle:
                writer = csv_module.writer(handle)
                writer.writerow(columns)
                writer.writerows([[x] + [v.get(x) for v in by_x] for x in xs])
        else:
            data, pub = plotexport.export(conn, line, args.format)
            path = Path(out or f"{pub.name}.{args.format}")
            (path.write_bytes if isinstance(data, bytes) else path.write_text)(data)
    except plotexport.PlotExportError as error:
        print(f"Error: {error}")
        return
    print(f"Wrote {path}")
    for problem in pub.problems:
        print(f"  warning: {problem}")


def _handle_export(conn, garden, args) -> None:
    if args.action == "tex":
        bundle = submit_repo.export(conn, garden, args.note)
        path = Path(args.out or "document.tex")
        path.write_text(bundle.tex, encoding="utf-8")
        for relative, data in bundle.files.items():
            target = path.parent / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        print(f"Wrote {path}" + (f" and {len(bundle.files)} file(s)" if bundle.files else ""))
    else:
        data, bundle = submit_repo.pdf(conn, garden, args.note)
        path = Path(args.out or "document.pdf")
        path.write_bytes(data)
        print(f"Wrote {path}")
    for problem in bundle.problems:
        print(f"  warning: {problem}")


def _handle_submit(conn, garden, args) -> None:
    try:
        frozen, _ = submit_repo.submit(conn, garden, args.note, args.force)
    except submit_repo.SubmitError as error:
        print(f"Error: {error}")
        return
    print(f"Submitted {frozen.revision} ({frozen.date}){' [forced]' if frozen.forced else ''}; "
          f"{len(frozen.values)} value(s) frozen.")


def _handle_revisions(conn, args) -> None:
    if args.action in (None, "list"):
        found = submit_repo.revisions(conn)
        if not found:
            print("No revisions.")
        for r in found:
            print(f"{r.revision}  {r.date}  {len(r.values)} values{'  forced' if r.forced else ''}")
    elif args.action == "diff":
        changes = submit_repo.diff(submit_repo.find(conn, args.a), submit_repo.find(conn, args.b))
        if not changes:
            print("No values changed.")
        for c in changes:
            print(f"{c.ref}: {c.before if c.before is not None else '-'} -> "
                  f"{c.after if c.after is not None else '-'}  ({c.kind})")
    elif args.action == "export":
        found = submit_repo.find(conn, args.revision)
        name = submit_repo.TEX_NAME if args.tex else submit_repo.PDF_NAME
        path = Path(args.path or f"{found.revision}{Path(name).suffix}")
        path.write_bytes(found.files[name])
        print(f"Wrote {path}")


def _handle_image(conn, args) -> None:
    if args.action == "add":
        data = sys.stdin.buffer.read() if args.file == "-" else Path(args.file).read_bytes()
        mote_id = images.add(conn, data, args.name, args.alt)
        shot = images.get_bytes(conn, mote_id)
        print(f"Stored {shot.name} ({shot.mime}, {len(shot.data) // 1024} KB) id={mote_id}")
        print(f"Use it in a note with \\image{{{shot.name}}}")
    elif args.action == "list":
        found = images.list_images(list_motes(conn))
        if not found:
            print("No images.")
        for mote in found:
            shot = images.picture(mote)
            print(f"{shot.mote_id}  {shot.name}  {shot.mime}  {len(shot.data) // 1024} KB")
    elif args.action == "rename":
        notes = images.rename(conn, args.image, args.new_name)
        print(f"Renamed to {args.new_name}; rewrote \\image in {len(notes)} note(s).")
    elif args.action == "export":
        shot = images.get_bytes(conn, args.image)
        path = Path(args.path or f"{shot.name}.{images.EXTENSIONS[shot.mime]}")
        path.write_bytes(shot.data)
        print(f"Image written to {path}")
    else:
        print("Specify an image action: add, list, rename, export")


def _handle_project(args) -> None:
    """tree and check read every garden, so they take no --garden; start
    and stop write one mote, in the garden --garden names."""
    if args.action == "tree":
        roots = projects.tree()
        if not roots:
            print("No gardens.")
        for root in roots:
            _print_project_node(root, 0)
        return
    if args.action in ("archive", "unarchive"):
        print(projects.set_archived(args.name, args.action == "archive"))
        return
    if args.action == "check":
        problems = projects.check()
        if not problems:
            print("Every garden has at most one owner.")
        for problem in problems:
            print(f"  {problem.kind}: {problem.message}")
        return

    garden = resolve_garden(args.garden)
    conn = get_connection(garden)
    try:
        initialize_database(conn)
        if args.action == "start":
            created, _ = projects.start_project(conn, args.mote_id, args.name)
            print(f"Started project {created} (owned by {args.mote_id} in {garden}).")
        elif args.action == "stop":
            claimed, _ = projects.stop_owning(conn, args.mote_id)
            print(f"{args.mote_id} no longer owns {claimed}; the garden is loose."
                  if claimed else f"{args.mote_id} owns no garden.")
        else:
            print("Usage: monoss project start|stop|tree|check")
    finally:
        conn.close()


def _handle_search(args) -> None:
    results = search.search_all(args.query, args.per_garden, not args.no_archived)
    if not results.hits:
        print("No matches in any garden.")
    garden = None
    for hit in results.hits:
        if hit.garden != garden:
            garden = hit.garden
            tag = "  (archived)" if hit.archived else ""
            print(f"{hit.project}  [{garden}]{tag}")
        print(f"  {hit.mote_id}  {hit.name}")
    for garden in results.truncated:
        print(f"  ... more in {garden}; raise --per-garden to see them")


# (command, action) pairs that write to the garden --garden names. An
# archived garden refuses them (core/projects.py::ensure_writable).
_WRITES = {
    ("mote", "create"), ("mote", "update"), ("mote", "delete"),
    ("rel", "create"), ("rel", "update"), ("rel", "delete"),
    ("species", "set"), ("species", "template"), ("species", "new"),
    ("modoc", "import"), ("worksheet", "apply"), ("import", "csv"),
    ("image", "add"), ("image", "rename"),
    ("project", "start"), ("project", "stop"), ("submit", None),
}


def _writes(args) -> bool:
    action = getattr(args, "action", None)
    if args.command == "fit":
        return "--apply" in (args.words or [])
    if args.command == "simulate":
        return action in (None, "run") and not getattr(args, "scenario", None)
    return (args.command, action) in _WRITES


def _print_project_node(node, depth: int) -> None:
    if node.loose:
        label = node.garden
    else:
        cover = node.owner.cover
        extra = [f"{key} {getattr(cover, key)}" for key in ("status", "due") if getattr(cover, key)]
        label = f"{node.name}  [{node.garden}]" + (f"  ({', '.join(extra)})" if extra else "")
    if node.archived:
        label += "  (archived)"
    print("  " * depth + label)
    for child in node.children:
        _print_project_node(child, depth + 1)


def _handle_incidence(conn, args) -> None:
    """impact / requires / producers / consumers -- one index, four reads
    of it. The index is rebuilt per invocation on purpose: it is derived
    from the rules, and a stored copy would be a second account of
    them."""
    index = incidence_index(conn)
    target = (args.mote_id, args.key)

    if args.command == "impact":
        lines = impact_lines(
            conn, target, index.impact(target, max_distance=getattr(args, "distance", None))
        )
    elif args.command == "requires":
        lines = requirement_lines(conn, index.requires(target))
    elif args.command == "producers":
        lines = [f"Equations that could determine {variable(conn, target)}:"]
        lines += equation_lines(
            conn, index.producers(target),
            "none -- it is not free to move" if not index.is_free(target)
            else "none -- it is free, but no equation constrains it",
        )
    else:
        lines = [f"Equations that read {variable(conn, target)}:"]
        lines += equation_lines(conn, index.consumers(target), "none")

    for line in lines:
        print(line)


def main() -> None:
    args = build_parser().parse_args()

    try:
        if args.command == "garden":
            _handle_garden(args)
            return

        if args.command == "search":
            _handle_search(args)
            return

        if _writes(args):
            projects.ensure_writable(resolve_garden(args.garden))

        if args.command == "project":
            _handle_project(args)
            return

        if args.command == "sync" and args.action == "init":
            # Not resolve_garden(): that raises if the name doesn't exist
            # yet, but init's whole job may be to create it (cloning from
            # existing sync history onto a brand-new garden name).
            garden = args.garden or get_default_garden() or DEFAULT_GARDEN
            _handle_sync_init(garden, args)
            return

        garden = resolve_garden(args.garden)
        conn = get_connection(garden)
        try:
            initialize_database(conn)
            if args.command == "mote":
                _handle_mote(conn, args)
            elif args.command == "rel":
                _handle_rel(conn, args)
            elif args.command == "sync":
                _handle_sync(conn, garden, args)
            elif args.command == "species":
                _handle_species(conn, args)
            elif args.command == "modoc":
                _handle_modoc(conn, args)
            elif args.command == "model":
                _handle_model(conn, args)
            elif args.command == "unit":
                _handle_unit(conn, args)
            elif args.command == "simulate":
                _handle_simulate(conn, args)
            elif args.command == "scenario":
                _handle_scenario(conn, args)
            elif args.command == "roles":
                _handle_roles(conn, args)
            elif args.command == "worksheet":
                _handle_worksheet(conn, args)
            elif args.command == "health":
                _handle_health(conn, args)
            elif args.command == "calc":
                _handle_calc(conn, args)
            elif args.command == "uncertainty":
                _handle_uncertainty(conn, args)
            elif args.command == "fit":
                _handle_fit(conn, args)
            elif args.command == "import":
                _handle_import(conn, args)
            elif args.command == "image":
                _handle_image(conn, args)
            elif args.command == "plot":
                _handle_plot(conn, args)
            elif args.command == "export":
                _handle_export(conn, garden, args)
            elif args.command == "submit":
                _handle_submit(conn, garden, args)
            elif args.command == "revisions":
                _handle_revisions(conn, args)
            elif args.command == "why":
                _handle_why(conn, args)
            elif args.command in ("impact", "requires", "producers", "consumers"):
                _handle_incidence(conn, args)
        finally:
            conn.close()
    except Exception as e:
        logger.exception("monoss command failed")
        print(f"Error: {e}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    main()
