"""core/task_candidates.py -- which key means status, priority, estimate.

Everything here is pure: motes in, candidates out. No connection, no
window. Same contract core/calendar.py keeps, and these tests are what
it buys.

The recurring assertion is that nothing is *reserved*: a garden calling
its status `stage` and its values `icebox`/`shipped` gets exactly the
same board as one saying `status`/`todo`/`done`.
"""
from core.entities import Mote
from core.task_candidates import (
    DEFAULT_STATUSES,
    MAX_DISTINCT_VALUES,
    PREFERRED_PRIORITY_NAMES,
    PREFERRED_STATUS_NAMES,
    default_enum_key,
    default_estimate_key,
    enum_candidates,
    estimate_candidates,
    format_estimate,
    parse_estimate_value,
    statuses_for,
    total_estimate_minutes,
)


def _mote(properties, id="m1"):
    return Mote(properties=properties, id=id)


# ---- estimates ----

def test_an_estimate_is_read_the_way_people_type_it():
    assert parse_estimate_value("90m") == 90
    assert parse_estimate_value("5h") == 300
    assert parse_estimate_value("1.5h") == 90
    assert parse_estimate_value("2 hours") == 120


def test_a_day_of_estimate_is_a_day_of_work_not_of_clock():
    assert parse_estimate_value("1d") == 8 * 60


def test_a_bare_number_is_minutes():
    """The unit core/calendar.py's `duration` already uses for a timed
    mote -- two readings of `estimate: 90` in one app is indefensible."""
    assert parse_estimate_value(90) == 90
    assert parse_estimate_value("90") == 90


def test_things_that_are_not_estimates_say_so():
    assert parse_estimate_value("soon") is None
    assert parse_estimate_value("") is None
    assert parse_estimate_value(None) is None
    assert parse_estimate_value(0) is None
    assert parse_estimate_value(-5) is None


def test_a_boolean_is_never_an_estimate():
    """`True` is an int in Python, and a mote with `done: true` has not
    estimated an hour of work."""
    assert parse_estimate_value(True) is None
    assert parse_estimate_value(False) is None


def test_an_estimate_reads_back_the_way_a_board_shows_it():
    assert format_estimate(45) == "45m"
    assert format_estimate(240) == "4h"
    assert format_estimate(90) == "1.5h"
    assert format_estimate(0) == ""


# ---- discovering enum keys ----

def test_a_repeating_set_of_short_labels_is_an_enum_key():
    motes = [
        _mote({"status": "todo"}, id="a"),
        _mote({"status": "doing"}, id="b"),
        _mote({"status": "todo"}, id="c"),
    ]
    assert [c.key for c in enum_candidates(motes)] == ["status"]
    assert enum_candidates(motes)[0].count == 3


def test_an_enum_carries_the_values_that_become_columns():
    motes = [_mote({"stage": "icebox"}, id="a"), _mote({"stage": "shipped"}, id="b")]
    assert enum_candidates(motes)[0].values == ("icebox", "shipped")


def test_free_text_is_not_an_enum():
    """Above the distinct-value ceiling it is text that happens to
    repeat, and a board built from it would have a column per mote."""
    motes = [_mote({"note": f"value {i}"}, id=str(i)) for i in range(MAX_DISTINCT_VALUES + 2)]
    assert [c.key for c in enum_candidates(motes)] == []


def test_prose_is_not_a_label_however_few_motes_share_it():
    motes = [_mote({"summary": "a sentence far too long to be a column heading"})]
    assert [c.key for c in enum_candidates(motes)] == []


def test_a_date_is_never_a_status():
    """Short, repeating and string-shaped by every other test here --
    and core/calendar.py's, not a status column's."""
    motes = [_mote({"due": "2026-08-28"}, id="a"), _mote({"due": "2026-08-29"}, id="b")]
    assert [c.key for c in enum_candidates(motes)] == []


def test_the_calendars_own_machinery_is_never_a_task_key():
    motes = [_mote({"name": "A", "duration": 30, "url": "x"})]
    assert [c.key for c in enum_candidates(motes)] == []


def test_candidates_come_back_most_used_first():
    motes = [
        _mote({"status": "todo", "priority": "high"}, id="a"),
        _mote({"status": "doing"}, id="b"),
    ]
    assert [c.key for c in enum_candidates(motes)] == ["status", "priority"]


# ---- choosing a default ----

def test_a_preferred_name_wins_when_the_garden_has_one():
    motes = [
        _mote({"kind": "a", "status": "todo"}, id="1"),
        _mote({"kind": "b", "status": "doing"}, id="2"),
        _mote({"kind": "c"}, id="3"),
    ]
    assert default_enum_key(motes, PREFERRED_STATUS_NAMES) == "status"


def test_the_most_used_candidate_wins_when_no_name_is_recognized():
    """How a garden that names it something else still gets a working
    board on first open."""
    motes = [_mote({"stage": "icebox"}, id="a"), _mote({"stage": "shipped"}, id="b")]
    assert default_enum_key(motes, PREFERRED_STATUS_NAMES) == "stage"


def test_priority_never_defaults_to_the_key_status_already_took():
    motes = [_mote({"status": "todo", "priority": "high"})]
    status = default_enum_key(motes, PREFERRED_STATUS_NAMES)
    assert default_enum_key(motes, PREFERRED_PRIORITY_NAMES, exclude=status) == "priority"


def test_a_garden_with_nothing_to_discover_says_so():
    assert default_enum_key([], PREFERRED_STATUS_NAMES) is None
    assert default_estimate_key([]) is None


def test_an_estimate_key_is_discovered_by_shape():
    motes = [_mote({"effort": "5h"}, id="a"), _mote({"effort": "2h"}, id="b")]
    assert [c.key for c in estimate_candidates(motes)] == ["effort"]
    assert default_estimate_key(motes) == "effort"


# ---- columns ----

def test_a_gardens_own_statuses_become_its_columns():
    motes = [_mote({"status": "doing"}, id="a"), _mote({"status": "todo"}, id="b")]
    assert statuses_for(motes, "status") == ["todo", "doing"], "known order, not first-seen"


def test_an_unknown_status_keeps_its_place_rather_than_being_dropped():
    """Ordering it last is the most this can honestly say about it;
    dropping it would hide motes."""
    motes = [
        _mote({"status": "icebox"}, id="a"),
        _mote({"status": "done"}, id="b"),
        _mote({"status": "todo"}, id="c"),
    ]
    assert statuses_for(motes, "status") == ["todo", "done", "icebox"]


def test_a_garden_with_no_statuses_yet_starts_on_the_default_set():
    assert statuses_for([], "status") == list(DEFAULT_STATUSES)
    assert statuses_for([], None) == list(DEFAULT_STATUSES)


# ---- load ----

def test_a_load_is_the_sum_of_what_was_actually_estimated():
    motes = [
        _mote({"estimate": "5h"}, id="a"),
        _mote({"estimate": "90m"}, id="b"),
        _mote({"name": "unestimated"}, id="c"),
    ]
    assert total_estimate_minutes(motes, "estimate") == 390


def test_an_unestimated_mote_contributes_nothing_rather_than_a_guess():
    """Inventing an hour for it would make a day look full for a reason
    nobody wrote down."""
    assert total_estimate_minutes([_mote({"name": "x"})], "estimate") == 0
    assert total_estimate_minutes([_mote({"estimate": "5h"})], None) == 0
