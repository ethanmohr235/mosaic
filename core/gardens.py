# core/gardens.py
import fcntl
import sqlite3
from pathlib import Path
from typing import IO

from core import db
from core.db import (
    DEFAULT_GARDEN,
    garden_db_path,
    get_connection,
    initialize_database,
    validate_garden_name,
)


def garden_exists(name: str) -> bool:
    return garden_db_path(name).exists()


def list_gardens() -> list[str]:
    """The user's gardens."""
    if not db.GARDENS_DIR.exists():
        return []
    return sorted(p.stem for p in db.GARDENS_DIR.glob("*.db"))


def _trash_path(name: str) -> Path:
    path = garden_db_path(name)
    return path.with_name(path.name + ".trash")


def delete_garden(name: str) -> None:
    # Moved aside, not removed, so restore_garden() can bring it back.
    # Only the most recently trashed copy of a given name is kept.
    if not garden_exists(name):
        raise LookupError(f"No such garden: {name!r}")
    trash = _trash_path(name)
    if trash.exists():
        trash.unlink()
    garden_db_path(name).rename(trash)

    # The sidecar (the console's UI state) is trashed alongside the .db,
    # not left behind -- otherwise a brand-new, unrelated garden later
    # created under this same name would silently inherit it.
    # restore_garden() below brings it back with the rest. A trashed
    # sidecar from an older copy is dropped even when this copy has none,
    # so a restore never pairs this .db with another's.
    for sidecar in _sidecar_paths(name):
        sidecar_trash = _trashed(sidecar)
        sidecar_trash.unlink(missing_ok=True)
        if sidecar.exists():
            sidecar.rename(sidecar_trash)


def restore_garden(name: str) -> None:
    trash = _trash_path(name)
    if not trash.exists():
        raise LookupError(f"Nothing to restore for garden: {name!r}")
    if garden_exists(name):
        raise FileExistsError(f"Garden already exists: {name!r}")
    trash.rename(garden_db_path(name))

    for sidecar in _sidecar_paths(name):
        sidecar_trash = _trashed(sidecar)
        if sidecar_trash.exists():
            sidecar_trash.rename(sidecar)


def _sidecar_paths(name: str) -> tuple[Path, ...]:
    """Every file beside a garden's .db that is named after it, and so
    belongs to it: deleted, restored and renamed with it."""
    return (console_state_path(name),)


def _trashed(path: Path) -> Path:
    return path.with_name(path.name + ".trash")


def console_state_path(name: str) -> Path:
    """The console's per-garden UI state -- saved layouts and command
    history (console/state.py). UI state, not knowledge: a sidecar
    rather than motes, so it never syncs, but named after the garden so
    it follows the garden through rename, trash and restore."""
    path = garden_db_path(name)
    return path.with_name(path.name + ".console.json")


def create_garden(name: str) -> str:
    validate_garden_name(name)
    if garden_exists(name):
        raise FileExistsError(f"Garden already exists: {name!r}")

    conn = get_connection(name)
    try:
        initialize_database(conn)
    finally:
        conn.close()

    return name


def rename_garden(old: str, new: str) -> str:
    """Rename a garden in place, sidecars and all.

    The .db is only half of a garden on disk: _sidecar_paths() sit
    beside it named after it, so a rename that moved only the database
    would silently strand them, and a later garden reusing the old name
    would inherit them. Same reasoning as delete_garden()'s trashing of
    the sidecars.

    A synced garden renames freely: the server knows it by its id
    (garden_id), and the next sync carries the new name to every device.
    """
    validate_garden_name(new)
    if not garden_exists(old):
        raise LookupError(f"No such garden: {old!r}")
    if garden_exists(new):
        raise FileExistsError(f"Garden already exists: {new!r}")
    garden_db_path(old).rename(garden_db_path(new))

    for old_sidecar, new_sidecar in zip(_sidecar_paths(old), _sidecar_paths(new)):
        if old_sidecar.exists():
            old_sidecar.rename(new_sidecar)

    # The default-garden pointer holds a name, not a path, so a rename
    # would otherwise leave it aimed at a garden that no longer exists --
    # which resolve_garden() treats as "never chose one" and quietly
    # auto-creates an empty `default` for.
    if get_default_garden() == old:
        set_default_garden(new)

    return new


def _default_garden_config_path() -> Path:
    return db.GARDENS_DIR / ".default_garden"


def get_default_garden() -> str | None:
    """The garden set_default_garden() last pointed at, or None if the
    user has never chosen one -- in which case resolve_garden() falls
    back to auto-creating/using DEFAULT_GARDEN, same as before this
    preference existed."""
    path = _default_garden_config_path()
    if not path.exists():
        return None
    return path.read_text().strip() or None


def set_default_garden(name: str) -> None:
    if not garden_exists(name):
        raise LookupError(f"No such garden: {name!r}")
    db.GARDENS_DIR.mkdir(parents=True, exist_ok=True)
    _default_garden_config_path().write_text(name)


# ---- sync identity (core/sync_all.py) ----
#
# A garden's sync identity lives inside its own .db, in `garden_meta`, so
# it follows the file through rename, trash and restore with no sidecar to
# keep in step. Never oplogged: it says which garden on the server this
# file is, and whether this machine shares it at all -- facts about this
# copy, not knowledge in the garden.

_META_TABLE = "CREATE TABLE IF NOT EXISTS garden_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
GARDEN_ID, LOCAL_ONLY = "garden_id", "local_only"


def _read_meta(path: Path, key: str) -> str | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("SELECT value FROM garden_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None
    except sqlite3.DatabaseError:  # no table yet, or not a garden at all
        return None
    finally:
        conn.close()


def _write_meta(name: str, key: str, value: str | None) -> None:
    conn = get_connection(name)
    try:
        conn.execute(_META_TABLE)
        if value is None:
            conn.execute("DELETE FROM garden_meta WHERE key = ?", (key,))
        else:
            conn.execute("INSERT INTO garden_meta (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
        conn.commit()
    finally:
        conn.close()


def garden_id(name: str) -> str | None:
    """Which garden on the sync server this one is, if it has been given
    an id yet."""
    return _read_meta(garden_db_path(name), GARDEN_ID)


def ensure_garden_id(name: str) -> str:
    found = garden_id(name)
    if found is None:
        found = db.generate_uuid()
        _write_meta(name, GARDEN_ID, found)
    return found


def set_garden_id(name: str, value: str) -> None:
    _write_meta(name, GARDEN_ID, value)


def trashed_garden_id(name: str) -> str | None:
    """The sync id of the copy of `name` in the trash, if there is one."""
    return _read_meta(_trash_path(name), GARDEN_ID)


def is_local_only(name: str) -> bool:
    """Whether this machine keeps the garden to itself (`sync local`)."""
    return _read_meta(garden_db_path(name), LOCAL_ONLY) == "1"


def set_local_only(name: str, local: bool) -> None:
    _write_meta(name, LOCAL_ONLY, "1" if local else None)


def hold(name: str) -> IO[bytes]:
    """Mark the garden as open here until the returned file is closed: the
    sync worker (core/sync_all.py) won't rename or trash a garden that is
    held. A shared flock on the .db itself, so it travels with the file
    and dies with the process; SQLite's own locks are a different kind and
    unaffected."""
    handle = garden_db_path(name).open("rb")
    fcntl.flock(handle, fcntl.LOCK_SH)
    return handle


def is_held(name: str) -> bool:
    path = garden_db_path(name)
    if not path.exists():
        return False
    with path.open("rb") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(handle, fcntl.LOCK_UN)
        return False


def legacy_sync_url(name: str) -> str | None:
    """The per-garden server URL sync used before accounts (a
    `‹name›.db.sync_dir` file), until core/sync_all.py adopts the garden."""
    path = garden_db_path(name).with_name(f"{name}.db.sync_dir")
    return (path.read_text().strip() or None) if path.exists() else None


def drop_legacy_sync(name: str) -> None:
    for suffix in (".sync_dir", ".auto_sync"):
        garden_db_path(name).with_name(f"{name}.db{suffix}").unlink(missing_ok=True)


def resolve_garden(name: str | None) -> str:
    if name is None:
        preferred = get_default_garden()
        # A preferred garden that's since been deleted falls through to
        # the old auto-create behavior rather than erroring -- the stale
        # preference is left in place for restore_garden() to make live
        # again, not cleared out from under the caller.
        if preferred is not None and garden_exists(preferred):
            return preferred
        if not garden_exists(DEFAULT_GARDEN):
            create_garden(DEFAULT_GARDEN)
        return DEFAULT_GARDEN

    if not garden_exists(name):
        raise LookupError(
            f"No such garden: {name!r} (run `garden create {name}` first)"
        )
    return name
