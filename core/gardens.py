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


def restore_garden(name: str) -> None:
    trash = _trash_path(name)
    if not trash.exists():
        raise LookupError(f"Nothing to restore for garden: {name!r}")
    if garden_exists(name):
        raise FileExistsError(f"Garden already exists: {name!r}")
    trash.rename(garden_db_path(name))


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
