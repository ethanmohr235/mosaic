# console/home.py
"""`api.home`: the Home panes' bridge (docs/console.md -> Settled for U9).

    gardens(command)     what a `gardens [--archived]` line shows
    garden(command)      what a `garden [‹name›]` line shows (none: the open one)
    sync(command)        what the `sync` line shows (core/sync_commands.py)

The first two are answered by core/home.py. Reading the tree opens every garden,
so a pane asks on run, never per keystroke. Nothing here writes: a
`garden …` or `sync …` action is a command the root API runs
(console/api.py)."""
import logging
from typing import Any

from console.session import Session, locked
from core import home
from core import sync_commands
from core.command_bar import CommandError

logger = logging.getLogger(__name__)

_ERRORS = (CommandError, ValueError, LookupError)


class HomeApi:
    def __init__(self, session: Session):
        self._session = session

    @locked
    def gardens(self, command: str) -> dict[str, Any]:
        def answer():
            words = (command or "").split()
            if not words or words[0] != home.GARDENS_VERB:
                raise CommandError("not a gardens line")
            return home.gardens_view(home.parse_gardens(words[1:]), current=self._session.garden)
        return self._answer(command, answer)

    @locked
    def garden(self, command: str) -> dict[str, Any]:
        def answer():
            line = home.parse_text(command)
            if not line.shows:
                raise CommandError(f"`garden {line.action}` is a command, not a pane")
            name = line.args[0] if line.args else self._session.garden
            return {**home.detail(name), "current": name == self._session.garden}
        return self._answer(command, answer)

    @locked
    def sync(self, command: str) -> dict[str, Any]:
        def answer():
            if sync_commands.parse((command or "").split()[1:]).action != sync_commands.SHOW:
                raise CommandError("`sync …` with an action is a command, not a pane")
            return {**sync_commands.pane(), "current": self._session.garden}
        return self._answer(command, answer)

    def _answer(self, what: str, compute) -> dict[str, Any]:
        try:
            return compute()
        except _ERRORS as e:
            return {"error": str(e)}
        except Exception as e:  # never a traceback across the bridge
            logger.exception("home failed: %r", what)
            return {"error": f"internal error: {e}"}
