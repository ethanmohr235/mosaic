# cli/main.py
import argparse
import json
import logging
from pathlib import Path

from core import modoc
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
    run_summary_line,
    scenario_lines,
    variable,
)
from cli.render import label as _label_or_id
from core import derivations
from core import model_resolver
from core.scenarios import get_scenario, list_scenarios
from core.providers import PropertyModelError
from core.simulation import (
    SimulationError,
    check_garden,
    check_roles,
    get_run,
    incidence_index,
    list_runs,
    run_changes,
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
        "simulate", help="Run the garden's simulation rules, or inspect past runs"
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
             "the run is logged for comparison and reports what would change",
    )
    history_parser = simulate_sub.add_parser("history", help="List past simulation runs")
    history_parser.add_argument("--limit", type=int, help="Show only the N most recent runs")
    show_parser = simulate_sub.add_parser("show", help="Show one run's changes")
    show_parser.add_argument("--id", required=True, help="Run id")

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

    ### Why
    why_parser = subparsers.add_parser(
        "why", help="Explain how a solved property value was determined"
    )
    why_parser.add_argument("mote_id", help="Mote whose property to explain")
    why_parser.add_argument("key", help="Property key to explain")
    why_parser.add_argument(
        "--run", help="Explain as of this run id (default: the most recent successful run)"
    )
    why_parser.add_argument(
        "--step", type=int,
        help="Explain as of this step within the run (default: the last step that determined it)",
    )
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

    document = modoc.parse(modoc.read_source(conn, args.id))
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
    else:
        print("Specify a model action: list, show, capabilities")


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

    elif action == "history":
        for run in list_runs(conn, limit=getattr(args, "limit", None)):
            print(run_summary_line(conn, run))

    elif action == "show":
        run = get_run(conn, args.id)
        if run is None:
            print("No run found.")
            return
        applied = "applied to graph" if run.committed else "saved (not applied to graph)"
        print(f"Run {run.run_id}")
        print(f"run=#{run.run_number}  {run.ran_at}  status={run.status}  ({applied})")
        if run.scenario_id:
            print(f"scenario: {_label_or_id(conn, run.scenario_id)}")
        if run.detail:
            print(f"detail: {run.detail}")
        for line in change_lines(run_changes(conn, run.run_id)):
            print(line)

    else:
        print("Specify a simulate action: run, history, show")


def _handle_why(conn, args) -> None:
    explanation = derivations.explain(
        conn, args.mote_id, args.key,
        run_id=args.run, step=args.step, depth=args.depth,
    )
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
