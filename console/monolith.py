# console/monolith.py
"""`api.monolith`: the find table's bridge (docs/console.md -> U2).

Two calls, both answered from `core/` so the view holds no query logic:

    table(command)            the rows a `find …` line asks for
    rewrite(command, change)  that line with its sort or grouping changed

Edits don't come through here. A cell edit is a `set …` line the view
runs on the command line (core/commands.py::run_set), so it is in the
history, the log and undo like any other write.

Errors come back as `{"error": message}` rather than raising, because a
pane shows a bad line as its own fault and the rest of the window carries
on."""
import logging
from dataclasses import replace
from typing import Any

from console.session import Session, locked
from core import findtext
from core import table as table_repo
from core import views as views_repo
from core.command_bar import CommandError
from core.query import DESC
from core.reserved import RESERVED_VERBS

logger = logging.getLogger(__name__)

FIND_VERB = "find"


def find_text(command: str) -> str:
    """A pane command with its leading `find` taken off: what findtext reads."""
    text = (command or "").strip()
    head, _, rest = text.partition(" ")
    if head != FIND_VERB:
        raise CommandError(f"not a find line: {text!r} -- usage: {RESERVED_VERBS[FIND_VERB]['usage']}")
    return rest.strip()


class MonolithApi:
    def __init__(self, session: Session):
        self._session = session

    @locked
    def table(self, command: str) -> dict[str, Any]:
        try:
            return self._table(command)
        except (CommandError, ValueError, LookupError) as e:
            return {"error": str(e)}
        except Exception as e:  # never a traceback across the bridge
            logger.exception("find failed: %r", command)
            return {"error": f"internal error: {e}"}

    @locked
    def rewrite(self, command: str, change: dict) -> dict[str, Any]:
        """`change` is `{"sort": columnKey}` (the same column again flips
        its direction) or `{"group": columnKey | null}`."""
        try:
            kwargs = {}
            if "sort" in (change or {}):
                kwargs["sort"] = _field(change["sort"])
            if "group" in (change or {}):
                kwargs["group"] = _field(change["group"])
            rewritten = findtext.rewrite(find_text(command), **kwargs)
        except (CommandError, ValueError) as e:
            return {"error": str(e)}
        return {"command": f"{FIND_VERB} {rewritten}".strip()}

    def _table(self, command: str) -> dict[str, Any]:
        conn = self._session.conn
        spec = findtext.parse_find(find_text(command))
        the_query = spec.query
        if spec.view:
            saved = views_repo.find_view_by_name(conn, spec.view)
            if saved is None:
                raise LookupError(f"no saved view named {spec.view!r}")
            the_query = findtext.with_view(spec, views_repo.query_of(saved))
        if not the_query.columns:
            motes, relationships, index = table_repo.read(conn)
            default = table_repo.default_query(table_repo.Schema.of(index, motes, relationships))
            the_query = replace(the_query, columns=default.columns)
        payload = table_repo.table(conn, the_query)
        payload["sort"] = (
            {"key": table_repo.column_key(the_query.sort.field),
             "dir": "desc" if the_query.sort.direction == DESC else "asc"}
            if the_query.sort else None
        )
        payload["group"] = table_repo.column_key(the_query.group) if the_query.group else None
        return payload


def _field(key):
    return None if key is None else table_repo.field_of(key)
