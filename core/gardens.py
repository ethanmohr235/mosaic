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


def resolve_garden(name: str | None) -> str:
    if name is None:
        if not garden_exists(DEFAULT_GARDEN):
            create_garden(DEFAULT_GARDEN)
        return DEFAULT_GARDEN

    if not garden_exists(name):
        raise LookupError(
            f"No such garden: {name!r} (run `garden create {name}` first)"
        )
    return name
