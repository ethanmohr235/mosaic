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
