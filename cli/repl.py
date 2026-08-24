# cli/repl.py
import json
import logging
import shlex
from pathlib import Path

from cli import render
from core.db import get_connection, initialize_database
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
from core.gardens import (
    create_garden,
    delete_garden,
    list_gardens,
    garden_exists,
    resolve_garden,
    restore_garden,
)
from cli.render import (
    BASE_UNIT_NOTE,
    change_lines,
    equation_lines,
    explanation_lines,
    impact_lines,
    label,
    requirement_lines,
    run_summary_line,
    scenario_lines,
    variable,
)
from core import derivations
from core import model_resolver
from core.providers import PropertyModelError
from core.scenarios import get_scenario, list_scenarios
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
    mote search <text>                                     Find motes by text, or "key = value"

    species list                                           List every mote acting as a species
    species of id=<mote_id>                                Species a mote is an instance of
    species instances id=<species_id>                      Instances of a species
    species set id=<mote_id> species=<species_id>          Declare a mote an instance
    species template id=<mote_id> species=<species_id>     Make a mote a species' template
    species new species=<species_id> [key=value ...]       New instance, seeded from the template

    folder list                                            List every mote acting as a folder
    folder of id=<mote_id>                                 Folders a mote is filed under
    folder members id=<folder_id>                          Motes filed under a folder
    folder set id=<mote_id> folder=<folder_id>             File a mote under a folder

    portal create garden=<name> mote=<mote_id> [key=v ...] Create a portal into another garden
    portal resolve id=<portal_id>                          Show what a portal points at

    library list                                           List reusable-mote entries
    library save id=<mote_id>                              Save a mote's properties to the library
    library use id=<entry_id> [key=value ...]              Copy an entry into this garden

    modoc render id=<mote_id> [step=<n>]                   Render a mote's note, values resolved
    modoc check id=<mote_id>                               Report unresolved links and values
    modoc values id=<mote_id> [step=<n>]                   Every \\val and what it resolves to
    modoc export id=<mote_id> [out=<file>]                 Write the note out as a .modoc file
    modoc import file=<path> [id=<mote_id>]                Read a .modoc file onto a mote

    rel create from=<mote_id> to=<mote_id> type=<type>     Create a relationship
    rel get id=<rel_id>                                    Get a relationship by id
    rel list [from=<mote_id>] [type=<type>]                List relationships
    rel update id=<rel_id> [from=<id>] [to=<id>] [type=<t>] Update a relationship
    rel delete id=<rel_id>                                  Delete a relationship

    model list                                             List physical property models
    model show id=<model_id>                                Show a property model's details
    model capabilities id=<model_id>                       List a property model's capabilities

    unit list                                              List registered units, grouped by dimension
    unit convert value=<n> from=<unit> to=<unit>           Convert a value between units
    unit check [id=<rule_id>]                              Check rules for dimensional consistency

    scenario list                                          List scenarios and what they vary
    scenario show id=<scenario_id>                         Show one scenario's overrides

    simulate [strict_units=true] [strict_constraints=true]  Run the garden's simulation rules
        [scenario=<id>]
    simulate history [limit=<n>]                            List past simulation runs
    simulate show id=<run_id>                               Show one run's changes

    roles check                                            Check how assemblies are wired to their parts

    why id=<mote_id> key=<property> [run=<run_id>]         Explain how a solved value was determined
        [step=<n>] [depth=<n>]
    impact id=<mote_id> key=<property> [distance=<n>]      What could move if this value changed
    requires id=<mote_id> key=<property>                   What must be known to determine this value
    producers id=<mote_id> key=<property>                  Which equations could determine it
    consumers id=<mote_id> key=<property>                  Which equations read it

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


def _require(action_args: dict, *keys: str) -> bool:
    """True when every key is present; prints which one is missing
    otherwise. The REPL's key=value style means a typo shows up as a
    missing key rather than an argparse error."""
    for key in keys:
        if key not in action_args:
            print(f"Error: {key!r} is required")
            return False
    return True


def _handle_species(conn, action: str, action_args: dict) -> None:
    if action == "list":
        render.print_motes(list_species(conn))
    elif action == "of":
        if _require(action_args, "id"):
            render.print_motes(species_of(conn, action_args["id"]))
    elif action == "instances":
        if _require(action_args, "id"):
            render.print_motes(instances_of(conn, action_args["id"]))
    elif action == "set":
        if _require(action_args, "id", "species"):
            set_species(conn, action_args["id"], action_args["species"])
            print(f"Mote {action_args['id']} is now an instance of {action_args['species']}")
    elif action == "template":
        if _require(action_args, "id", "species"):
            set_template(conn, action_args["id"], action_args["species"])
            print(f"Mote {action_args['id']} is now the template for {action_args['species']}")
    elif action == "new":
        if _require(action_args, "species"):
            overrides = {k: v for k, v in action_args.items() if k != "species"}
            print(f"Instance created with id={create_instance(conn, action_args['species'], overrides)}")
    else:
        print(f"Unknown species action: {action}")


def _handle_folder(conn, action: str, action_args: dict) -> None:
    if action == "list":
        render.print_motes(list_folders(conn))
    elif action == "of":
        if _require(action_args, "id"):
            render.print_motes(folders_of(conn, action_args["id"]))
    elif action == "members":
        if _require(action_args, "id"):
            render.print_motes(folder_members(conn, action_args["id"]))
    elif action == "set":
        if _require(action_args, "id", "folder"):
            set_folder(conn, action_args["id"], action_args["folder"])
            print(f"Mote {action_args['id']} filed under {action_args['folder']}")
    else:
        print(f"Unknown folder action: {action}")


def _handle_portal(conn, action: str, action_args: dict) -> None:
    if action == "create":
        if _require(action_args, "garden", "mote"):
            extra = {k: v for k, v in action_args.items() if k not in ("garden", "mote")}
            mote_id = create_portal(conn, action_args["garden"], action_args["mote"], extra)
            print(f"Portal created with id={mote_id} -> {action_args['garden']}/{action_args['mote']}")
    elif action == "resolve":
        if _require(action_args, "id"):
            mote = get_mote(conn, action_args["id"])
            if mote is None:
                print("No mote found.")
                return
            garden, target = resolve_portal(mote.properties)
            print(f"Garden: {garden}")
            render.print_motes([target])
    else:
        print(f"Unknown portal action: {action}")


def _handle_library(conn, action: str, action_args: dict) -> None:
    """`library list` reads the reserved library garden through its own
    short-lived connection rather than the session's -- same pattern
    core/library.py follows for save/reuse."""
    if action == "list":
        library_conn = get_connection(LIBRARY_GARDEN)
        try:
            initialize_database(library_conn)
            render.print_motes(list_motes(library_conn))
        finally:
            library_conn.close()
    elif action == "save":
        if _require(action_args, "id"):
            print(f"Saved to library with id={save_to_library(conn, action_args['id'])}")
    elif action == "use":
        if _require(action_args, "id"):
            overrides = {k: v for k, v in action_args.items() if k != "id"}
            print(f"Mote created with id={reuse_from_library(conn, action_args['id'], overrides)}")
    else:
        print(f"Unknown library action: {action}")


def _handle_modoc(conn, action: str, action_args: dict) -> None:
    """The same five actions cli/main.py offers, so a note reads the same
    whichever front end you typed it into."""
    if action == "import":
        if _require(action_args, "file"):
            mote_id = modoc.import_file(conn, Path(action_args["file"]), action_args.get("id"))
            print(f"Note read onto mote id={mote_id}")
        return
    if action not in ("render", "check", "values", "export"):
        print(f"Unknown modoc action: {action}")
        return
    if not _require(action_args, "id"):
        return

    note_id = action_args["id"]
    if action == "export":
        path = Path(action_args.get("out") or f"{note_id}.modoc")
        print(f"Note written to {modoc.export_file(conn, note_id, path)}")
        return

    document = modoc.parse(modoc.read_source(conn, note_id))
    resolution = resolve_modoc(conn, note_id, document, step=int(action_args.get("step", 0)))
    if action == "check":
        if render.print_diagnostics(document, resolution) == 0:
            print("No problems.")
    elif action == "values":
        for node in modoc.values(document):
            result = resolution.values[modoc.value_key(node)]
            print(f"{f'[{node.ref}]' if node.ref else ''}{node.expression} = {result.text}")
    else:
        render.print_modoc(document, resolution)


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
                        render.print_models(conn, mote.id)

                elif action == "list":
                    render.print_motes(list_motes(conn))

                elif action == "search":
                    render.print_motes(search_motes(conn, " ".join(action_args)))

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

            # --------- SPECIES / FOLDER / PORTAL / LIBRARY / MODOC ---------
            elif cmd in ("species", "folder", "portal", "library", "modoc"):
                if not args:
                    print(f"Specify a {cmd} action")
                    continue
                handler = {
                    "species": _handle_species,
                    "folder": _handle_folder,
                    "portal": _handle_portal,
                    "library": _handle_library,
                    "modoc": _handle_modoc,
                }[cmd]
                handler(conn, args[0].lower(), parse_properties(args[1:]))

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
                    render.print_model_list(conn)

                elif action in ("show", "capabilities"):
                    if "id" not in action_args:
                        print("Error: 'id' is required")
                        continue
                    render.print_model_detail(conn, action_args["id"], header=action == "show")

                else:
                    print(f"Unknown model action: {action}")

            # --------- UNIT COMMANDS ---------
            elif cmd == "unit":
                if not args:
                    print("Specify an action: list, convert, check")
                    continue
                action = args[0].lower()
                action_args = parse_properties(args[1:])

                if action == "list":
                    by_dimension: dict[str, list[str]] = {}
                    for unit_name in list_units():
                        by_dimension.setdefault(
                            describe(dimension_of(unit_name)), []
                        ).append(unit_name)
                    for dimension, names in sorted(by_dimension.items()):
                        print(f"{dimension}: {', '.join(names)}")

                elif action == "convert":
                    missing = [k for k in ("value", "from", "to") if k not in action_args]
                    if missing:
                        print(f"Error: {', '.join(missing)} required")
                        continue
                    try:
                        result = convert(
                            float(action_args["value"]), action_args["from"], action_args["to"]
                        )
                    except (UnitError, ValueError) as e:
                        print(f"Error: {e}")
                        continue
                    print(
                        f"{float(action_args['value']):g} {action_args['from']} = "
                        f"{result:g} {action_args['to']}"
                    )

                elif action == "check":
                    report = check_garden(conn, action_args.get("id"))
                    if report.pairs_checked == 0:
                        print("No rules to check.")
                    elif not report.problems:
                        print(
                            f"OK -- no dimensional problems in "
                            f"{report.pairs_checked} rule/instance pair(s)."
                        )
                    else:
                        print(f"{len(report.problems)} dimensional problem(s):")
                        for problem in report.problems:
                            print(f"  {problem}")

                else:
                    print(f"Unknown unit action: {action}")

            # --------- SIMULATE COMMANDS ---------
            elif cmd == "simulate":
                action = args[0].lower() if args else "run"

                if action == "run":
                    action_args = parse_properties(args[1:])
                    solver = action_args.get("solver", "linear")
                    try:
                        result = run_simulation(
                            conn,
                            solver=solver,
                            strict_units=bool(action_args.get("strict_units", False)),
                            strict_constraints=bool(
                                action_args.get("strict_constraints", False)
                            ),
                            scenario=action_args.get("scenario"),
                        )
                    except SimulationError as e:
                        print(f"Simulation failed: {e}")
                        continue
                    print(result.summary)
                    for warning in result.dimension_warnings:
                        print(f"  warning: {warning}")
                    for violation in result.constraint_violations:
                        print(f"  constraint: {violation}")
                    for line in change_lines(result.changes):
                        print(line)

                elif action == "history":
                    action_args = parse_properties(args[1:])
                    limit = action_args.get("limit")
                    for run in list_runs(conn, limit=limit):
                        print(run_summary_line(conn, run))

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
                    if run.scenario_id:
                        print(f"scenario: {label(conn, run.scenario_id)}")
                    if run.detail:
                        print(f"detail: {run.detail}")
                    for line in change_lines(run_changes(conn, run.run_id)):
                        print(line)

                else:
                    print(f"Unknown simulate action: {action}")

            elif cmd == "why":
                action_args = parse_properties(args)
                if "id" not in action_args or "key" not in action_args:
                    print("Error: 'id' and 'key' are required")
                    continue
                explanation = derivations.explain(
                    conn,
                    str(action_args["id"]),
                    str(action_args["key"]),
                    run_id=action_args.get("run"),
                    step=action_args.get("step"),
                    depth=int(action_args.get("depth", derivations.DEFAULT_DEPTH)),
                )
                if explanation.derived:
                    print(BASE_UNIT_NOTE)
                for line in explanation_lines(conn, explanation):
                    print(line)

            elif cmd == "scenario":
                action = args[0].lower() if args else "list"
                if action == "show":
                    action_args = parse_properties(args[1:])
                    if "id" not in action_args:
                        print("Error: 'id' is required")
                        continue
                    scenario = get_scenario(conn, str(action_args["id"]))
                    if scenario is None:
                        print("No scenario found. A scenario is a mote with a 'varies' relationship.")
                        continue
                    for line in scenario_lines(conn, scenario):
                        print(line)
                elif action == "list":
                    scenarios = list_scenarios(conn)
                    if not scenarios:
                        print("No scenarios in this garden.")
                    for scenario in scenarios:
                        print(
                            f"[{scenario.mote_id}] {scenario.name} "
                            f"({len(scenario.overrides)} override(s) over "
                            f"{len(scenario.target_ids)} mote(s))"
                        )
                else:
                    print(f"Unknown scenario action: {action}")

            elif cmd == "roles":
                if args and args[0].lower() != "check":
                    print(f"Unknown roles action: {args[0]}")
                    continue
                report = check_roles(conn)
                if report.motes_checked == 0:
                    print("No motes declare roles.")
                elif not report.problems:
                    print(
                        f"OK -- {report.motes_checked} mote(s) with declared roles "
                        "are wired correctly."
                    )
                else:
                    print(f"{len(report.problems)} role problem(s):")
                    for problem in report.problems:
                        print(f"  {problem}")
                if report.once_scoped:
                    print(f"{len(report.once_scoped)} rule(s) asserted about a single mote:")
                    for line in report.once_scoped:
                        print(f"  {line}")

            elif cmd in ("impact", "requires", "producers", "consumers"):
                action_args = parse_properties(args)
                if "id" not in action_args or "key" not in action_args:
                    print("Error: 'id' and 'key' are required")
                    continue
                target = (str(action_args["id"]), str(action_args["key"]))
                index = incidence_index(conn)

                if cmd == "impact":
                    lines = impact_lines(
                        conn, target,
                        index.impact(target, max_distance=action_args.get("distance")),
                    )
                elif cmd == "requires":
                    lines = requirement_lines(conn, index.requires(target))
                elif cmd == "producers":
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
