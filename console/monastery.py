# console/monastery.py
"""`api.monastery`: the calendar panes' bridge (docs/console.md -> Settled
for U7).

    view(command)                    what an `agenda`/`week`/`month` line shows
    rewrite(command, change)         that line paged, back to today, or a key toggled
    edit(mote, key, occurrence, start, end)
                                     the `set` line a move or resize writes
    skip(mote, key, occurrence)      the `set` line that drops one occurrence
    slot(start, end, all_day)        how a new event in that slot is written
    follow(mote)                     where `o` goes: a note, a project, the mote

Everything is answered by core/agenda.py. Nothing here writes: a pane
runs the line it is handed (ctx.runCommand), so every edit is in the
history, the log and undo."""
import logging
from typing import Any

from console.session import Session, locked
from core import agenda
from core import calendar as calendar_repo
from core import modoc
from core import motes as motes_repo
from core import projects as projects_repo
from core.command_bar import CommandError

logger = logging.getLogger(__name__)

_ERRORS = (CommandError, ValueError, LookupError)


class MonasteryApi:
    def __init__(self, session: Session):
        self._session = session

    @locked
    def view(self, command: str) -> dict[str, Any]:
        return self._answer(command, lambda: agenda.run(self._session.conn, agenda.parse_text(command)))

    @locked
    def rewrite(self, command: str, change: dict) -> dict[str, Any]:
        def answer():
            change_ = dict(change)
            if "key" in change_:
                change_["all_keys"] = calendar_repo.default_bindings(motes_repo.list_motes(self._session.conn))
            return {"command": agenda.rewrite(agenda.parse_text(command), change_).text()}
        return self._answer(command, answer)

    @locked
    def edit(self, mote_id: str, key: str, occurrence: str, start: str, end: str) -> dict[str, Any]:
        return self._answer("edit", lambda: {
            "line": agenda.edit_line(self._session.conn, mote_id, key, occurrence, start, end)})

    @locked
    def skip(self, mote_id: str, key: str, occurrence: str) -> dict[str, Any]:
        return self._answer("skip", lambda: {
            "line": agenda.skip_line(self._session.conn, mote_id, key, occurrence)})

    def slot(self, start: str, end: str, all_day: bool = False) -> dict[str, Any]:
        return self._answer("slot", lambda: {"value": agenda.slot_value(start, end, bool(all_day))})

    @locked
    def follow(self, mote_id: str) -> dict[str, Any]:
        """`o`: a note opens `read` on it, a project owner `cd`s to its
        garden, anything else opens its props."""
        def answer():
            mote = motes_repo.get_mote(self._session.conn, mote_id)
            if mote is None:
                raise CommandError("that mote no longer exists")
            garden = projects_repo.garden_named_by(mote.properties)
            if garden:
                return {"command": f"cd {garden}", "newPane": False}
            if modoc.has_modoc(mote.properties):
                return {"command": f"read #{mote_id}", "newPane": True}
            return {"command": f"open #{mote_id}", "newPane": True}
        return self._answer("follow", answer)

    def _answer(self, what: str, compute) -> dict[str, Any]:
        try:
            return compute()
        except _ERRORS as e:
            return {"error": str(e)}
        except Exception as e:  # never a traceback across the bridge
            logger.exception("monastery failed: %r", what)
            return {"error": f"internal error: {e}"}
