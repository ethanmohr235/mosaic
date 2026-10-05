# console/monarch.py
"""`api.monarch`: the graph pane's bridge (docs/console.md -> Settled for
U8).

    view(command, selection)     what a `graph …` line draws
    flow(command, selection, run, folds)
                                 what a `graph … --flow` line draws: the
                                 solve under the playhead's run
                                 ({ticks, scenario, solver}), with the
                                 pane's fold state ({fold id: folded})
    rewrite(command, change)     that line one hop deeper or shallower, or re-rooted
    pins()                       the garden's pinned node positions {id: [x, y]}
    pin(positions)               pin (or move) nodes: {id: [x, y]}
    unpin(ids)                   unpin those nodes; None unpins every one

Lines are answered by core/graphview.py. Pins are UI state, not
knowledge: they live in the console's sidecar beside the garden
(console/state.py), never in the .db. Nothing here writes the garden: a
gesture in the pane puts a statement on the command line."""
import logging
from typing import Any, Callable

from console import state as state_repo
from console.session import Session, locked
from core import flowview
from core import graphview
from core.command_bar import CommandError

logger = logging.getLogger(__name__)

_ERRORS = (CommandError, ValueError, LookupError)


class MonarchApi:
    def __init__(self, session: Session, state: Callable[[], state_repo.ConsoleState],
                 save: Callable[[], None]):
        self._session = session
        self._state = state
        self._save = save

    @locked
    def view(self, command: str, selection: list[str] | None = None) -> dict[str, Any]:
        return self._answer(command, lambda: graphview.run(
            self._session.conn, graphview.parse_text(command), selection or []))

    @locked
    def flow(self, command: str, selection: list[str] | None = None,
             run: dict | None = None, folds: dict | None = None) -> dict[str, Any]:
        return self._answer(command, lambda: flowview.run(
            self._session.conn, graphview.parse_text(command), selection or [],
            dict(run or {}), dict(folds or {})))

    @locked
    def rewrite(self, command: str, change: dict) -> dict[str, Any]:
        return self._answer(command, lambda: {
            "command": graphview.rewrite(graphview.parse_text(command), dict(change)).text()})

    @locked
    def pins(self) -> dict[str, list[float]]:
        return dict(self._state().pins)

    @locked
    def pin(self, positions: dict) -> dict[str, Any]:
        def answer():
            self._state().pin(positions)
            self._save()
            return {"pins": len(self._state().pins)}
        return self._answer("pin", answer)

    @locked
    def unpin(self, ids: list[str] | None = None) -> dict[str, Any]:
        def answer():
            self._state().unpin(ids)
            self._save()
            return {"pins": len(self._state().pins)}
        return self._answer("unpin", answer)

    def _answer(self, what: str, compute) -> dict[str, Any]:
        try:
            return compute()
        except _ERRORS as e:
            return {"error": str(e)}
        except Exception as e:  # never a traceback across the bridge
            logger.exception("monarch failed: %r", what)
            return {"error": f"internal error: {e}"}
