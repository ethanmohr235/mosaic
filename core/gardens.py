# core/gardens.py
from pathlib import Path

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

    # The sidecars (the sync-dir pointer, the auto-sync flag, the
    # console's UI state) are trashed alongside the .db, not left behind
    # -- otherwise a brand-new, unrelated garden later created under this
    # same name would silently inherit them, and with the sync pointer
    # start pushing/pulling through a sync server that has nothing to do
    # with it. restore_garden() below brings them back with the rest. A
    # trashed sidecar from an older copy is dropped even when this copy
    # has none, so a restore never pairs this .db with another's.
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
    return (_sync_dir_path(name), _auto_sync_path(name), console_state_path(name))


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
    would silently strand them -- the renamed
    garden would come back unsynced, and a later garden reusing the old
    name would inherit a sync server that has nothing to do with it.
    Same reasoning as delete_garden()'s trashing of the sidecars.

    Refused while the garden syncs: the sync server keys a garden's ops
    by its name, so renaming here would point this device at a fresh,
    empty op stream while the other devices kept writing to the old one
    -- two halves of one garden that never converge again. Turning sync
    off, renaming, then re-initing is the flow that actually works, and
    it is better asked for than discovered."""
    validate_garden_name(new)
    if not garden_exists(old):
        raise LookupError(f"No such garden: {old!r}")
    if garden_exists(new):
        raise FileExistsError(f"Garden already exists: {new!r}")
    if get_sync_dir(old) is not None:
        raise ValueError(
            f"Garden {old!r} is syncing through a sync server, which tracks it "
            "by name. Disable sync, rename, then set sync up again."
        )

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


def clear_default_garden() -> None:
    _default_garden_config_path().unlink(missing_ok=True)


def _sync_dir_path(name: str) -> Path:
    path = garden_db_path(name)
    return path.with_name(path.name + ".sync_dir")


def get_sync_dir(name: str) -> str | None:
    """The sync server URL this garden syncs through (core/sync.py), or
    None if `sync init` has never been run for it. Per garden, not per device --
    a pointer file beside the garden's own .db, same pattern as
    _default_garden_config_path() above."""
    path = _sync_dir_path(name)
    if not path.exists():
        return None
    return path.read_text().strip() or None


def set_sync_dir(name: str, sync_dir: str) -> None:
    _sync_dir_path(name).write_text(str(sync_dir))


def clear_sync_dir(name: str) -> None:
    """Stop syncing this garden -- removes the pointer set by
    set_sync_dir() (core/sync.py::init) without touching the garden's own
    data or its oplog history, so re-enabling sync later picks up right
    where it left off."""
    _sync_dir_path(name).unlink(missing_ok=True)


def _auto_sync_path(name: str) -> Path:
    path = garden_db_path(name)
    return path.with_name(path.name + ".auto_sync")


def get_auto_sync_enabled(name: str) -> bool:
    """Whether the lifecycle sync points (console/session.py, via
    api_common.best_effort_sync)
    should fire automatically. Defaults to True when absent, so a garden
    that already had a sync dir configured keeps today's always-on
    behavior. False doesn't touch the sync dir itself -- "Sync now"
    still works either way."""
    path = _auto_sync_path(name)
    if not path.exists():
        return True
    return path.read_text().strip() != "0"


def set_auto_sync_enabled(name: str, enabled: bool) -> None:
    _auto_sync_path(name).write_text("1" if enabled else "0")


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
