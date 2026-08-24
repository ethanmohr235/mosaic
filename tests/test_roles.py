"""Roles: the named parts an assembly is wired to, and rule scope.

A role is an ordinary relationship whose type is the role's name --
nothing structural is added. What is under test is what the *declaration*
buys: addressing a part by name in a formula, an arity that is a stated
intention rather than an accident of wiring, and a check that catches a
mis-wired assembly before it is solved.
"""
import pytest

from core import roles as roles_repo
from core.formula import FormulaError
from core.motes import create_mote, get_mote, update_mote
from core.relationships import create_relationship, delete_relationship, list_relationships
from core.roles import MANY, ONE, ROLES_KEY, Role, arity_problem, parse_roles
from core.rules import APPLIES_TO, EACH, ONCE, list_rules
from core.simulation import SimulationError, check_roles, run_simulation
from core.species import set_species


def add_rule(conn, target_id, name, equation):
    rule_id = create_mote(conn, {"name": name, "equation": equation})
    create_relationship(conn, rule_id, target_id, APPLIES_TO)
    return rule_id


@pytest.fixture
def assembly(conn):
    """A two-part assembly: `whole` has roles `left` and `right`, each an
    instance of Part, and sums their values."""
    part_species = create_mote(conn, {"name": "Part"})
    left = create_mote(conn, {"name": "Left", "value": 3})
    right = create_mote(conn, {"name": "Right", "value": 4})
    set_species(conn, left, part_species)
    set_species(conn, right, part_species)

    whole_species = create_mote(conn, {
        "name": "Whole", "freedoms": "total",
        ROLES_KEY: {"left": "one", "right": "one"},
    })
    add_rule(conn, whole_species, "Total", "total == left.value + right.value")

    whole = create_mote(conn, {"name": "W1"})
    set_species(conn, whole, whole_species)
    create_relationship(conn, whole, left, "left")
    create_relationship(conn, whole, right, "right")
    return {"whole": whole, "left": left, "right": right,
            "species": whole_species, "part": part_species}


### parsing

def test_roles_accept_the_shapes_a_user_can_type():
    """The same range core/rules.py's freedom list accepts, for the same
    reason: the UI's plain property boxes make terse forms far easier."""
    assert parse_roles("turbine, boiler") == {
        "turbine": Role("turbine"), "boiler": Role("boiler"),
    }
    assert parse_roles(["turbine"]) == {"turbine": Role("turbine")}
    assert parse_roles({"heaters": "many"}) == {"heaters": Role("heaters", MANY)}
    assert parse_roles({"t": {"arity": "many", "species": "abc"}}) == {
        "t": Role("t", MANY, "abc")
    }


def test_an_unusable_roles_property_is_ignored_not_fatal():
    """A half-typed declaration shouldn't break a garden that isn't using
    roles yet."""
    assert parse_roles(None) == {}
    assert parse_roles(7) == {}
    assert parse_roles({"": "one"}) == {}


def test_an_unrecognized_arity_falls_back_to_the_stricter_one():
    assert parse_roles({"t": "several"})["t"].arity == ONE


def test_a_many_role_accepts_any_count_including_none():
    assert arity_problem(Role("h", MANY), 0) is None
    assert arity_problem(Role("h", MANY), 5) is None


def test_a_one_role_rejects_zero_and_several():
    assert arity_problem(Role("t"), 1) is None
    assert "reaches no mote" in arity_problem(Role("t"), 0)
    assert "reaches 2 motes" in arity_problem(Role("t"), 2)


### addressing a part by name

def test_a_role_reaches_its_part_in_a_formula(conn, assembly):
    run_simulation(conn)
    assert get_mote(conn, assembly["whole"]).properties["total"] == 7


def test_a_rule_on_the_assembly_can_solve_a_part_s_unknown(conn):
    """The point of the whole phase: a rule anchored on the assembly
    determines a value belonging to one of its parts."""
    part_species = create_mote(conn, {"name": "Part", "freedoms": "value"})
    part = create_mote(conn, {"name": "P1"})
    set_species(conn, part, part_species)

    whole_species = create_mote(conn, {"name": "Whole", ROLES_KEY: ["part"]})
    add_rule(conn, whole_species, "Supply", "part.value == 42")
    whole = create_mote(conn, {"name": "W1"})
    set_species(conn, whole, whole_species)
    create_relationship(conn, whole, part, "part")

    run_simulation(conn)
    assert get_mote(conn, part).properties["value"] == 42


def test_a_bare_name_is_still_a_property_not_a_role(conn, assembly):
    """Roles resolve only in the attribute position, so nothing that used
    to mean a property can start meaning a role."""
    update_mote(conn, assembly["whole"], {"left": 99})
    add_rule(conn, assembly["species"], "Bare", "total == left")

    with pytest.raises(SimulationError, match="contradictory|inconsistent"):
        run_simulation(conn)


def test_an_undeclared_name_in_the_attribute_position_is_rejected(conn, assembly):
    add_rule(conn, assembly["species"], "Bogus", "total == middle.value")
    with pytest.raises(SimulationError, match="middle"):
        run_simulation(conn)


def test_targets_navigation_is_unchanged_by_roles(conn, assembly):
    """The explicit form keeps working, and keeps collapsing a single
    mote to a scalar -- every existing garden relies on that."""
    add_rule(conn, assembly["species"], "Explicit", "total == targets('left').value * 7 / 3")
    run_simulation(conn)
    assert get_mote(conn, assembly["whole"]).properties["total"] == 7


### arity

def test_a_many_role_keeps_its_list_even_for_one_mote(conn):
    """So a formula that will need sum() the day a second part is wired
    needs it from the start, rather than silently changing meaning."""
    part_species = create_mote(conn, {"name": "Part"})
    part = create_mote(conn, {"name": "P1", "value": 5})
    set_species(conn, part, part_species)

    whole_species = create_mote(conn, {
        "name": "Whole", "freedoms": "total", ROLES_KEY: {"parts": "many"},
    })
    add_rule(conn, whole_species, "Total", "total == parts.value")
    whole = create_mote(conn, {"name": "W1"})
    set_species(conn, whole, whole_species)
    create_relationship(conn, whole, part, "parts")

    with pytest.raises(SimulationError, match="sum"):
        run_simulation(conn)


def test_a_many_role_sums_correctly(conn):
    part_species = create_mote(conn, {"name": "Part"})
    whole_species = create_mote(conn, {
        "name": "Whole", "freedoms": "total", ROLES_KEY: {"parts": "many"},
    })
    add_rule(conn, whole_species, "Total", "total == sum(parts.value)")
    whole = create_mote(conn, {"name": "W1"})
    set_species(conn, whole, whole_species)
    for value in (1, 2, 3):
        part = create_mote(conn, {"name": f"P{value}", "value": value})
        set_species(conn, part, part_species)
        create_relationship(conn, whole, part, "parts")

    run_simulation(conn)
    assert get_mote(conn, whole).properties["total"] == 6


def test_a_one_role_wired_twice_fails_the_run_naming_the_role(conn, assembly):
    extra = create_mote(conn, {"name": "Left 2", "value": 1})
    set_species(conn, extra, assembly["part"])
    create_relationship(conn, assembly["whole"], extra, "left")

    with pytest.raises(SimulationError, match="'left'"):
        run_simulation(conn)


def test_a_one_role_wired_to_nothing_fails_the_run(conn, assembly):
    rel = next(
        r for r in list_relationships(conn, from_id=assembly["whole"]) if r.type == "left"
    )
    delete_relationship(conn, rel.id)

    with pytest.raises(SimulationError, match="reaches no mote"):
        run_simulation(conn)


### where roles are declared

def test_a_species_declares_what_its_instances_have(conn, assembly):
    """The species carries `roles`; the instance carries the edges."""
    assert ROLES_KEY in get_mote(conn, assembly["species"]).properties
    assert ROLES_KEY not in get_mote(conn, assembly["whole"]).properties
    run_simulation(conn)
    assert get_mote(conn, assembly["whole"]).properties["total"] == 7


def test_a_mote_that_is_not_a_species_declares_its_own_roles(conn):
    """A one-off assembly should not have to invent a species with one
    instance in order to name its parts."""
    part_species = create_mote(conn, {"name": "Part", "freedoms": "value"})
    part = create_mote(conn, {"name": "P1"})
    set_species(conn, part, part_species)

    whole = create_mote(conn, {"name": "One-off", ROLES_KEY: ["part"]})
    create_relationship(conn, whole, part, "part")
    add_rule(conn, whole, "Supply", "part.value == 11")

    run_simulation(conn)
    assert get_mote(conn, part).properties["value"] == 11


def test_an_instance_may_add_a_role_of_its_own(conn, assembly):
    """One assembly has a part its species never declared. Its own
    declaration is added to the species', not weighed against it."""
    spare = create_mote(conn, {"name": "Spare", "value": 10})
    set_species(conn, spare, assembly["part"])
    create_relationship(conn, assembly["whole"], spare, "spare")
    update_mote(conn, assembly["whole"], {ROLES_KEY: ["spare"]})
    update_mote(conn, assembly["species"], {"freedoms": "total,reserve"})
    add_rule(conn, assembly["species"], "Reserve", "reserve == spare.value")

    run_simulation(conn)
    properties = get_mote(conn, assembly["whole"]).properties
    assert properties["total"] == 7        # from the species' own roles
    assert properties["reserve"] == 10     # from the role this instance added


### scope inference

def test_a_rule_pointed_at_a_species_applies_to_each_instance(conn, assembly):
    rule = next(r for r in list_rules(conn) if r.name == "Total")
    assert rule.scope == EACH


def test_a_rule_pointed_at_a_plain_mote_is_asserted_once(conn):
    mote = create_mote(conn, {"name": "Solo"})
    add_rule(conn, mote, "One-off", "x == 1")
    rule = next(r for r in list_rules(conn) if r.name == "One-off")

    assert rule.scope == ONCE
    assert rule.target_id == mote


def test_making_an_instance_flips_the_scope_and_the_check_says_so(conn):
    """Inferring scope means a rule's reach changes the first time
    someone makes an instance of the mote it was about. That has to be
    visible before it produces a different answer."""
    mote = create_mote(conn, {"name": "Solo", "freedoms": "x"})
    add_rule(conn, mote, "One-off", "x == 1")
    assert check_roles(conn).once_scoped == ["'One-off' is asserted once, about Solo"]

    instance = create_mote(conn, {"name": "I1"})
    set_species(conn, instance, mote)
    assert check_roles(conn).once_scoped == []
    assert next(r for r in list_rules(conn) if r.name == "One-off").scope == EACH


def test_a_once_scoped_rule_solves_the_mote_it_names(conn):
    species = create_mote(conn, {"name": "Machine", "freedoms": "x"})
    a = create_mote(conn, {"name": "A"})
    b = create_mote(conn, {"name": "B"})
    set_species(conn, a, species)
    set_species(conn, b, species)
    add_rule(conn, a, "Just A", "x == 5")

    run_simulation(conn)
    assert get_mote(conn, a).properties["x"] == 5
    assert "x" not in get_mote(conn, b).properties


### check_roles

def test_check_roles_passes_a_correctly_wired_assembly(conn, assembly):
    report = check_roles(conn)
    assert report.motes_checked == 1
    assert report.problems == []


def test_check_roles_reports_a_missing_part(conn, assembly):
    rel = next(
        r for r in list_relationships(conn, from_id=assembly["whole"]) if r.type == "right"
    )
    delete_relationship(conn, rel.id)

    problems = check_roles(conn).problems
    assert len(problems) == 1
    assert "W1" in problems[0] and "'right'" in problems[0]


def test_check_roles_reports_a_part_of_the_wrong_species(conn, assembly):
    other = create_mote(conn, {"name": "Other"})
    update_mote(conn, assembly["species"], {
        ROLES_KEY: {"left": {"species": assembly["part"]},
                    "right": {"species": other}},
    })

    problems = check_roles(conn).problems
    assert len(problems) == 1
    assert "declared species" in problems[0] and "Right" in problems[0]


def test_a_role_with_no_declared_species_accepts_anything(conn, assembly):
    loose = create_mote(conn, {"name": "Loose", "value": 1})
    create_relationship(conn, assembly["whole"], loose, "spare")
    update_mote(conn, assembly["whole"], {ROLES_KEY: ["spare"]})

    assert check_roles(conn).problems == []


def test_check_roles_is_quiet_in_a_garden_with_no_roles(conn):
    create_mote(conn, {"name": "Plain"})
    report = check_roles(conn)
    assert report.motes_checked == 0
    assert report.problems == []


### the checker agrees with the evaluator

def test_the_dimension_checker_understands_roles(conn, assembly):
    """core/dimensions.py walks the same grammar. A role it did not
    recognize would make every equation using one unverifiable."""
    from core.simulation import check_garden

    update_mote(conn, assembly["left"], {"units": {"value": "m"}})
    update_mote(conn, assembly["right"], {"units": {"value": "s"}})
    update_mote(conn, assembly["whole"], {"units": {"total": "m"}})

    report = check_garden(conn)
    assert report.pairs_checked == 1
    assert report.problems, "adding metres to seconds through roles should be caught"
