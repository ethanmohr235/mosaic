# console/mote.py
"""`api.mote`: what the props pane and the `p` float draw (docs/console.md
-> U3). One read:

    inspect(command, selection)   the mote an `open …` line names

The payload is core/inspect.py's, the same one the old windows' floating
panel draws, so a mote reads the same in both. Nothing here writes: every
edit in the pane is a command it runs on the command line (`set`,
`rename`, `A -> type -> B`, `unlink`, `template`), which is what puts it
in the history, the log and undo.
"""
import logging
from typing import Any

from console.session import Session, locked
from core import commands
from core import inspect as inspect_repo
from core.command_bar import CommandError

logger = logging.getLogger(__name__)

OPEN_VERB = "open"
SEL = "@sel"


def target_of(command: str) -> str:
    """The mote an `open` line names, `@sel` when it names none."""
    parsed = commands.parse(command or "")
    if parsed.head != OPEN_VERB:
        raise CommandError(f"not an open line: {command!r} -- usage: open @sel|#id|name")
    if len(parsed.args) > 1:
        raise CommandError("open takes one mote -- quote a name with spaces")
    return parsed.args[0] if parsed.args else SEL


class MoteApi:
    def __init__(self, session: Session):
        self._session = session

    @locked
    def inspect(self, command: str, selection: list[str] | None = None) -> dict[str, Any]:
        """`{"empty": message}` when the line follows an empty selection --
        not an error, just nothing to show yet; `{"error": message}` for a
        line that names nothing."""
        selection = list(selection or [])
        try:
            target = target_of(command)
            if target == SEL and not selection:
                return {"empty": "nothing selected"}
            (mote_id,) = commands.one_target(self._session.conn, target, selection)
            payload = inspect_repo.inspect(self._session.conn, mote_id)
        except (CommandError, ValueError) as e:
            return {"error": str(e)}
        except Exception as e:  # never a traceback across the bridge
            logger.exception("inspect failed: %r", command)
            return {"error": f"internal error: {e}"}
        if payload is None:
            return {"error": "that mote no longer exists"}
        payload["follows"] = target == SEL
        payload["selectionCount"] = len(selection) if target == SEL else 1
        return payload
