# console/state.py
"""The console's per-garden UI state: saved layouts, the active one, the
autosaved working tree, and command history (docs/console.md ->
Persistence).

UI state, not knowledge -- never motes, never in the .db, never synced.
It lives in a sidecar beside the garden, `gardens/<name>.db.console.json`
(core/gardens.py::console_state_path), so renaming, trashing and
restoring a garden carries it along.

A tree (a layout, or the working tree) is opaque JSON here: the split
tree's shape belongs to the shell (console/frontend/js/tree.js). Python
checks only that it is plain JSON and not absurdly large."""
import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from core.gardens import console_state_path

logger = logging.getLogger(__name__)

VERSION = 1
HISTORY_LIMIT = 500
MAX_TREE_BYTES = 64 * 1024
MAX_LAYOUT_NAME = 64
MAX_PINS = 5000


@dataclass
class ConsoleState:
    layouts: list[dict] = field(default_factory=list)  # [{"name", "tree"}], save order
    active: str | None = None
    working: Any = None
    history: list[dict] = field(default_factory=list)  # [{"time", "command"}], oldest first
    # `solve --live on`: every write made from the window is followed by
    # a solve (docs/console.md -> Settled for U5). Off by default.
    live: bool = False
    # The graph pane's pinned nodes, {mote id: [x, y]} in graph space
    # (docs/console.md -> Settled for U8).
    pins: dict = field(default_factory=dict)

    def pin(self, positions: Any) -> None:
        if not isinstance(positions, dict):
            raise ValueError("pins are {mote id: [x, y]}")
        checked = {str(mote_id): _checked_xy(xy) for mote_id, xy in positions.items()}
        if len(self.pins.keys() | checked.keys()) > MAX_PINS:
            raise ValueError(f"at most {MAX_PINS} pinned nodes")
        self.pins.update(checked)

    def unpin(self, ids: Any = None) -> None:
        if ids is None:
            self.pins = {}
            return
        for mote_id in ids:
            self.pins.pop(str(mote_id), None)

    def layout_names(self) -> list[str]:
        return [layout["name"] for layout in self.layouts]

    def layout_get(self, name: str) -> Any:
        return next((layout["tree"] for layout in self.layouts if layout["name"] == name), None)

    def layout_put(self, name: str, tree: Any) -> None:
        """Save `tree` as `name` and make it active. Overwriting keeps
        the layout's original position; a new name appends."""
        validate_layout_name(name)
        validate_tree(tree, allow_none=False)
        for layout in self.layouts:
            if layout["name"] == name:
                layout["tree"] = tree
                break
        else:
            self.layouts.append({"name": name, "tree": tree})
        self.active = name

    def layout_delete(self, name: str) -> bool:
        before = len(self.layouts)
        self.layouts = [layout for layout in self.layouts if layout["name"] != name]
        if self.active == name:
            self.active = None
        return len(self.layouts) != before

    def set_active(self, name: str | None) -> None:
        if name is not None and name not in self.layout_names():
            raise ValueError(f"no layout named {name!r}")
        self.active = name

    def add_history(self, command: str, when: datetime | None = None, replace: bool = False) -> None:
        """`replace` swaps the newest entry for `command` instead: a pane
        that completes a short line into its full form (`plot save Load`
        -> `plot save Load ‹its line›`) keeps one entry, the full one."""
        stamp = (when or datetime.now()).isoformat(timespec="seconds")
        if replace and self.history:
            self.history[-1] = {"time": stamp, "command": command}
            return
        self.history.append({"time": stamp, "command": command})
        del self.history[:-HISTORY_LIMIT]

    def to_json(self) -> dict:
        return {
            "version": VERSION,
            "layouts": self.layouts,
            "active": self.active,
            "working": self.working,
            "history": self.history,
            "live": self.live,
            "pins": self.pins,
        }


def validate_layout_name(name: Any) -> None:
    if not isinstance(name, str) or not name or len(name) > MAX_LAYOUT_NAME:
        raise ValueError("a layout name is 1 to 64 characters")
    if any(ch.isspace() for ch in name):
        raise ValueError(f"a layout name is one word, got {name!r}")


def validate_tree(tree: Any, *, allow_none: bool = True) -> None:
    """Plain JSON (dicts, lists, strings, numbers, booleans, null) that
    serializes to at most MAX_TREE_BYTES. A layout must be an object; the
    working tree may also be null (nothing to restore)."""
    if tree is None and allow_none:
        return
    if not isinstance(tree, dict):
        raise ValueError("a layout tree is a JSON object")
    try:
        size = len(json.dumps(tree, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError) as e:
        raise ValueError(f"a layout tree must be plain JSON: {e}") from None
    if size > MAX_TREE_BYTES:
        raise ValueError(f"a layout tree is at most {MAX_TREE_BYTES} bytes, got {size}")


# ---- disk ---------------------------------------------------------------

def load(garden: str) -> ConsoleState:
    """The garden's saved state; empty when there is none. A file that
    doesn't parse, or isn't the shape this writes, loads as empty with a
    warning -- losing layouts is better than a console that won't open."""
    path = console_state_path(garden)
    if not path.exists():
        return ConsoleState()
    try:
        return from_json(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError, KeyError) as e:
        logger.warning("ignoring unreadable console state %s: %s", path, e)
        return ConsoleState()


def save(garden: str, state: ConsoleState) -> None:
    """Written atomically -- a temp file beside the target, then
    os.replace -- so a crash mid-write leaves the old file, not half of
    a new one."""
    _atomic_write(console_state_path(garden), json.dumps(state.to_json(), indent=1))


def from_json(data: Any) -> ConsoleState:
    if not isinstance(data, dict) or data.get("version") != VERSION:
        raise ValueError("not a version-1 console state object")
    layouts = [_checked_layout(layout) for layout in data.get("layouts") or []]
    names = [layout["name"] for layout in layouts]
    active = data.get("active")
    working = data.get("working")
    validate_tree(working)
    history = [_checked_history(h) for h in data.get("history") or []]
    return ConsoleState(
        layouts=layouts,
        active=active if active in names else None,
        working=working,
        history=history[-HISTORY_LIMIT:],
        live=data.get("live") is True,
        pins={str(k): _checked_xy(v) for k, v in (data.get("pins") or {}).items()},
    )


def _checked_layout(entry: Any) -> dict:
    validate_layout_name(entry["name"])
    validate_tree(entry["tree"], allow_none=False)
    return {"name": entry["name"], "tree": entry["tree"]}


def _checked_xy(xy: Any) -> list[float]:
    if (not isinstance(xy, (list, tuple)) or len(xy) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and abs(v) < 1e7 for v in xy)):
        raise ValueError(f"a pin is [x, y], got {xy!r}")
    return [round(float(xy[0]), 1), round(float(xy[1]), 1)]


def _checked_history(entry: Any) -> dict:
    if not isinstance(entry["time"], str) or not isinstance(entry["command"], str):
        raise ValueError("history entries are {time, command} strings")
    return {"time": entry["time"], "command": entry["command"]}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
