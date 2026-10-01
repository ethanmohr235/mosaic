"""console/state.py -- the per-garden layouts/history sidecar -- and the
core/gardens.py plumbing that carries it with its garden."""
import json
from datetime import datetime

import pytest

from console import state as state_repo
from console.state import HISTORY_LIMIT, ConsoleState
from core.gardens import (
    console_state_path,
    create_garden,
    delete_garden,
    rename_garden,
    restore_garden,
)

TREE = {"split": "row", "sizes": [0.5, 0.5], "children": [{"command": "log"}, {"command": ""}]}


@pytest.fixture
def garden():
    create_garden("work")
    return "work"


def test_missing_file_loads_as_empty(garden):
    assert state_repo.load(garden) == ConsoleState()


def test_round_trip(garden):
    state = ConsoleState()
    state.layout_put("two", TREE)
    state.layout_put("one", {"command": "log"})
    state.working = TREE
    state.add_history("cd work", when=datetime(2026, 9, 29, 17, 5))
    state_repo.save(garden, state)

    loaded = state_repo.load(garden)
    assert loaded == state
    assert loaded.layout_names() == ["two", "one"]
    assert loaded.active == "one"
    assert loaded.history == [{"time": "2026-09-29T17:05:00", "command": "cd work"}]

    on_disk = json.loads(console_state_path(garden).read_text())
    assert on_disk["version"] == 1
    assert set(on_disk) == {"version", "layouts", "active", "working", "history", "live", "pins"}
    assert on_disk["live"] is False


def test_save_leaves_no_temp_file_behind(garden):
    state_repo.save(garden, ConsoleState())
    assert [p.name for p in console_state_path(garden).parent.iterdir()
            if p.name.endswith(".tmp")] == []


@pytest.mark.parametrize("content", ["{not json", "[]", '{"version": 7}',
                                     '{"version": 1, "layouts": [{"name": 3}]}'])
def test_corrupt_file_loads_as_empty_with_a_warning(garden, content, caplog):
    console_state_path(garden).write_text(content)
    assert state_repo.load(garden) == ConsoleState()
    assert "console state" in caplog.text


def test_history_is_capped(garden):
    state = ConsoleState()
    for i in range(HISTORY_LIMIT + 20):
        state.add_history(f"cmd {i}")
    assert len(state.history) == HISTORY_LIMIT
    assert state.history[0]["command"] == "cmd 20"
    assert state.history[-1]["command"] == f"cmd {HISTORY_LIMIT + 19}"


def test_overwriting_a_layout_keeps_its_position(garden):
    state = ConsoleState()
    state.layout_put("a", TREE)
    state.layout_put("b", TREE)
    state.layout_put("a", {"command": "log"})
    assert state.layout_names() == ["a", "b"]
    assert state.layout_get("a") == {"command": "log"}


def test_deleting_the_active_layout_clears_active(garden):
    state = ConsoleState()
    state.layout_put("a", TREE)
    assert state.layout_delete("a") is True
    assert state.active is None
    assert state.layout_delete("a") is False


@pytest.mark.parametrize("tree", [None, "log", [1], {"command": "x" * 70_000}, {"x": float("nan")}])
def test_bad_layout_trees_are_refused(tree):
    with pytest.raises(ValueError):
        ConsoleState().layout_put("a", tree)


@pytest.mark.parametrize("name", ["", "two words", "x" * 65, 3])
def test_bad_layout_names_are_refused(name):
    with pytest.raises(ValueError):
        ConsoleState().layout_put(name, TREE)


# ---- the sidecar follows its garden ----------------------------------------

def _saved(garden: str) -> ConsoleState:
    state = ConsoleState()
    state.layout_put("two", TREE)
    state_repo.save(garden, state)
    return state


def test_rename_carries_the_sidecar(garden):
    state = _saved(garden)
    rename_garden(garden, "life")
    assert not console_state_path(garden).exists()
    assert state_repo.load("life") == state


def test_trash_and_restore_carry_the_sidecar(garden):
    state = _saved(garden)
    delete_garden(garden)
    assert not console_state_path(garden).exists()

    # A new garden under the same name starts clean...
    create_garden(garden)
    assert state_repo.load(garden) == ConsoleState()
    delete_garden(garden)

    # ...and trashing it drops the older copy's trashed state rather than
    # pairing it with this .db on restore.
    restore_garden(garden)
    assert state_repo.load(garden) == ConsoleState()


def test_restore_brings_the_sidecar_back(garden):
    state = _saved(garden)
    delete_garden(garden)
    restore_garden(garden)
    assert state_repo.load(garden) == state
