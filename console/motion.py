# console/motion.py
"""`api.motion`: the Motion panes' bridge (docs/console.md -> Settled for U5).

    sim(command)                 the run table a `sim …` line asks for:
                                 rows with their whole series, and markers
    rewrite(command, change)     that line with a gesture applied (`d`)
    rule(command, selection, run)
                                 the cards a `rule …` line shows; with
                                 `run` ({ticks, scenario, solver}, the
                                 shared playhead's run) each binding
                                 carries its series
    why(command)                 the explanation a `why m.k` line shows
    scenarios()                  every scenario and its overrides

Everything is answered by core/motion.py. Nothing here writes: `solve`,
`set --scenario` and the live toggle are commands the root runs
(console/api.py), so they are in the history, the log and undo.

Errors come back as `{"error": message}`, as for every pane."""
import logging
from typing import Any

from console.session import Session, locked
from core import commands
from core import motion
from core.command_bar import CommandError

logger = logging.getLogger(__name__)

_ERRORS = (CommandError, ValueError, LookupError)


class MotionApi:
    def __init__(self, session: Session):
        self._session = session

    @locked
    def sim(self, command: str) -> dict[str, Any]:
        return self._answer(command, lambda: motion.run_json(
            motion.run(self._session.conn, motion.parse_text(command))))

    @locked
    def rewrite(self, command: str, change: dict) -> dict[str, Any]:
        return self._answer(command, lambda: {
            "command": motion.rewrite(motion.parse_text(command), change).text()})

    @locked
    def rule(self, command: str, selection: list[str] | None = None,
             run: dict | None = None) -> dict[str, Any]:
        def answer():
            parsed = commands.parse(command)
            if parsed.head == motion.WHY_VERB:
                return self._why(parsed.args)
            if parsed.head != motion.RULE_VERB:
                raise CommandError(f"{command!r} is not a rule line")
            if not parsed.args:
                return {"empty": True, "usage": "rule @sel|#id|‹rule›  ·  why ‹m.k›"}
            if len(parsed.args) != 1:
                raise CommandError("rule takes one rule (quote a name with spaces)")
            ref = parsed.args[0]
            if ref == "@sel":
                if not selection:
                    return {"empty": True, "usage": "select a rule, or a mote a rule touches"}
                ref = f"#{selection[0]}"
            settings = run or {}
            cards = motion.rule_cards(
                self._session.conn, ref, ticks=settings.get("ticks"),
                scenario=settings.get("scenario"), solver=settings.get("solver"),
                sets=settings.get("sets") or ())
            return {"kind": "rule", "cards": [motion.card_json(c) for c in cards],
                    "run": settings or None}
        return self._answer(command, answer)

    @locked
    def scenarios(self) -> dict[str, Any]:
        return self._answer("scenarios", lambda: {"scenarios": motion.scenarios(self._session.conn)})

    # ---- private ----

    def _why(self, args) -> dict:
        if len(args) != 1:
            raise CommandError("usage: why ‹m.k›")
        return {"kind": "why", "explanation": motion.why(self._session.conn, args[0])}

    def _answer(self, command: str, compute) -> dict[str, Any]:
        try:
            return compute()
        except _ERRORS as e:
            return {"error": str(e)}
        except Exception as e:  # never a traceback across the bridge
            logger.exception("motion failed: %r", command)
            return {"error": f"internal error: {e}"}
