# Vendored from core/db.py's _GARDEN_NAME_RE rather than imported, so this
# server's image doesn't have to pull in core/db.py's sqlite3 import (and
# everything else core/ drags in) just for one regex.
import re

GARDEN_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def validate_garden_name(name: str) -> None:
    if not GARDEN_NAME_RE.match(name):
        raise ValueError(
            f"Invalid garden name: {name!r} (use 1-64 letters, digits, '_' or '-')"
        )
