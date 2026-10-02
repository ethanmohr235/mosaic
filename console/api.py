# console/api.py
"""The console's root JS bridge: `window.pywebview.api` in the one
Mosaic window (docs/console.md -> Architecture).

It owns the Session (the one garden connection, its lock and its undo
stack) and the garden's ConsoleState (layouts and history), and it
answers the shell's shared concerns: the command line, completion,
layouts, history, undo and garden switching. Later port steps hang each
app's namespace here as a public attribute over the same session, e.g.
`self.monolith = MonolithApi(self._session)` in U2.

**Every attribute that is not a bridge method is private (`_x`).**
pywebview walks js_api recursively -- every public non-callable
attribute is descended into (webview/util.py::get_functions) -- and a
walk that reached the sqlite3 connection would expose it, or choke on
it. tests/test_console_api.py walks this class the same way to hold
that line.

The bridge's shapes are the U1 contract the shell is built against;
see run() for the effects. Grammar lives in core/commands.py, not here.
"""
import logging
import sqlite3
from typing import Any

from console import state as state_repo
from console.home import HomeApi
from console.monarch import MonarchApi
from console.monastery import MonasteryApi
from console.monolith import MonolithApi
from console.mora import MoraApi
from console.motion import MotionApi
from console.mote import MoteApi
from console.moxie import MoxieApi
from console.session import Session, best_effort_sync, locked
from core import commands
from core import gardens as gardens_repo
from core import home as home_repo
from core import motes as motes_repo
from core import fit as fit_repo
from core import motion as motion_repo
from core import plotcmd
from core.command_bar import CommandError
from core.reserved import RESERVED_VERBS

logger = logging.getLogger(__name__)

# What a statement, verb or app line may fail with that is the user's
# input rather than a bug: shown as an error effect, never raised.
_USER_ERRORS = (CommandError, LookupError, ValueError, FileExistsError, sqlite3.Error)


def _error(message: str) -> dict:
    return {"effect": "error", "message": message}


class Api:
    def __init__(self, garden: str | None = None):
        self._session = Session.open(garden)
        self._state = state_repo.load(self._session.garden)
        self._window = None  # set by window_launcher.launch_window
        # App namespaces over the same session (docs/console.md -> Architecture).
        self.monolith = MonolithApi(self._session)
        self.mote = MoteApi(self._session)
        self.moxie = MoxieApi(self._session, lambda: self._window)
        self.motion = MotionApi(self._session)
        self.mora = MoraApi(self._session, lambda: self._state.live)
        self.monastery = MonasteryApi(self._session)
        self.monarch = MonarchApi(self._session, lambda: self._state, self._save_state)
        self.home = HomeApi(self._session)

    # ---- boot and gardens ----

    @locked
    def boot(self) -> dict:
        self._state = state_repo.load(self._session.garden)
        return self._boot_payload()

    @locked
    def switch_garden(self, name: str) -> dict:
        """Same as `cd <name>`. Raises LookupError for an unknown garden
        (the shell offers only names from boot()["gardens"])."""
        self._switch(name)
        return self._boot_payload()

    @locked
    def shutdown(self) -> None:
        self._session.close()

    # ---- the command line ----

    @locked
    def run(self, line: str, selection: list[str] | None = None, confirm: bool = False,
            replace_history: bool = False) -> dict:
        """Run one command-line line and return exactly one effect:
        statement, open_view, layout, garden, undo/redo, refresh
        or error. The raw line enters this garden's history first, so
        even a failed line can be recalled -- except a confirm=True
        resubmission, which is the same line answering "press Enter
        again", not a new one."""
        text = (line or "").strip()
        if not text:
            return _error("")
        if not confirm:
            self._state.add_history(text, replace=replace_history)
            self._save_state()
        try:
            return self._dispatch(text, selection or [], confirm)
        except _USER_ERRORS as e:
            return _error(str(e))
        except Exception as e:  # never a traceback across the bridge
            logger.exception("console command failed: %r", text)
            return _error(f"internal error: {e}")

    @locked
    def resolve(self, command: str) -> dict:
        """Which view answers `command` -- no side effects, no history."""
        try:
            view, canonical = commands.resolve_view(command or "")
        except CommandError as e:
            return {"error": str(e)}
        return {"view": view, "command": canonical}

    @locked
    def complete(self, line: str, cursor: int | None = None) -> dict:
        result = commands.complete(
            self._session.conn, line or "", cursor,
            gardens=gardens_repo.list_gardens(),
            layouts=self._state.layout_names(),
            current_garden=self._session.garden,
        )
        return {
            "hint": result.hint,
            "rows": [
                {"value": r.value, "description": r.description,
                 "dim": r.dim, "current": r.current}
                for r in result.rows
            ],
            "ghost": result.ghost,
            "replaceFrom": result.replace_from,
        }

    @locked
    def history(self) -> list[dict]:
        return list(self._state.history)

    # ---- layouts ----

    @locked
    def layout_put(self, name: str, tree: Any) -> dict:
        self._state.layout_put(name, tree)
        self._save_state()
        return self._layouts_summary()

    @locked
    def layout_put_slot(self, slot: int, tree: Any) -> dict:
        """`^1`-`^9`: save the working tree into a slot (state.py)."""
        name = self._state.layout_put_slot(slot, tree)
        self._save_state()
        return {"name": name, **self._layouts_summary()}

    @locked
    def layout_get(self, name: str) -> Any:
        return self._state.layout_get(name)

    @locked
    def layout_delete(self, name: str) -> dict:
        self._state.layout_delete(name)
        self._save_state()
        return self._layouts_summary()

    @locked
    def layout_set_active(self, name: str | None) -> None:
        self._state.set_active(name)
        self._save_state()

    @locked
    def working_put(self, tree: Any) -> None:
        state_repo.validate_tree(tree)
        self._state.working = tree
        self._save_state()

    # ---- undo, refresh ----

    @locked
    def undo(self) -> dict:
        return {"label": self._session.undo.undo()}

    @locked
    def redo(self) -> dict:
        return {"label": self._session.undo.redo()}

    @locked
    def refresh(self) -> dict:
        return {"message": self._refresh()}

    # ---- private: dispatch ----

    def _dispatch(self, text: str, selection: list[str], confirm: bool) -> dict:
        # `set` is read before @sel is expanded: its `@sel` is the whole
        # selection, where everywhere else it is the primary.
        literal = commands.parse(text)
        if literal.kind == commands.VERB and literal.head in commands.WRITE_VERBS:
            return self._verb_write(literal.head, literal.args, selection)
        if literal.kind == commands.VERB and literal.head == plotcmd.PLOT_VERB:
            return self._verb_plot(literal)
        if literal.kind == commands.VERB and literal.head == fit_repo.FIT_VERB:
            return self._verb_fit(literal)
        if literal.kind == commands.VERB and literal.head == home_repo.GARDEN_VERB:
            return self._verb_garden(literal, confirm)
        if literal.kind == commands.VERB and literal.head in commands.LINE_VIEW_VERBS:
            # The literal line is the pane's command: `open @sel` keeps
            # following the selection rather than freezing on today's.
            return {"effect": "open_view", "view": commands.view_for(literal.head),
                    "command": literal.text}
        expanded = commands.expand_sel(text, selection)
        if expanded != text and motes_repo.get_mote(self._session.conn, selection[0]) is None:
            # command_bar reads an `#id` that matches nothing as a *name*
            # and would create a mote called "#<id>" -- never what a stale
            # selection meant.
            return _error("the selected mote no longer exists")
        parsed = commands.parse(expanded)
        if parsed.kind == commands.VERB:
            return self._run_verb(parsed)
        return self._run_statement(parsed.text, confirm)

    def _run_statement(self, text: str, confirm: bool) -> dict:
        # Every statement form returns its writes as one undo step
        # (core/command_bar.py's journal), so `undo` takes back a typed line.
        result = commands.run_statement(self._session.conn, text, confirm=confirm)
        live = self._push(result.command)
        return {
            "effect": "statement",
            "ok": result.ok,
            "message": result.message + live,
            "moteId": result.mote_id,
            "moteName": self._mote_name(result.mote_id),
            "created": result.created,
            "needsConfirm": result.needs_confirm,
            "choices": result.choices,
            "wrote": result.ok and not result.needs_confirm,
        }

    def _mote_name(self, mote_id: str | None) -> str | None:
        """What the top bar calls the selection (`sel bd`, not `sel #4541ef2b`)."""
        mote = motes_repo.get_mote(self._session.conn, mote_id) if mote_id else None
        name = mote.properties.get("name") if mote else None
        return name if isinstance(name, str) else None

    def _run_verb(self, parsed: commands.ParsedLine) -> dict:
        if parsed.head == "cd":
            return self._verb_cd(parsed.args)
        if parsed.head == "layout":
            return self._verb_layout(parsed.args)
        if parsed.head == motion_repo.SOLVE_VERB:
            return self._verb_solve(parsed.args)
        if parsed.head in (motion_repo.STEP_VERB, motion_repo.PLAY_VERB, motion_repo.PAUSE_VERB):
            return self._verb_playhead(parsed.head, parsed.args)
        if parsed.args:
            return _error(_no_arguments(parsed.head))
        if parsed.head in commands.BARE_VIEW_VERBS:
            return {"effect": "open_view", "view": commands.view_for(parsed.head),
                    "command": parsed.head}
        if parsed.head == "refresh":
            return {"effect": "refresh", "message": self._refresh()}
        if parsed.head == "undo":
            return {"effect": "undo", "label": self._session.undo.undo()}
        if parsed.head == "redo":
            return {"effect": "redo", "label": self._session.undo.redo()}
        return _error(f"{parsed.head} isn't available yet")

    def _verb_write(self, verb: str, args: tuple[str, ...], selection: list[str]) -> dict:
        result = commands.WRITE_VERBS[verb](self._session.conn, args, selection)
        live = self._push(result.command)
        return {"effect": "statement", "ok": True, "message": result.message + live,
                "moteId": None, "moteName": None, "created": False,
                "needsConfirm": False, "choices": [], "wrote": result.count > 0}

    def _verb_plot(self, parsed: commands.ParsedLine) -> dict:
        """`plot …` opens (or re-points) a plot pane with the literal
        line. `plot save ‹name› ‹line›` is the one write. `plot save
        ‹name›` and `plot export …` act on the focused plot pane, which
        only the shell knows, so they come back as a pane_action for it:
        the pane then runs the full `plot save ‹name› ‹its line›` (so the
        history holds the whole write) or exports what it shows."""
        command = plotcmd.parse(parsed.args)
        view = commands.view_for(plotcmd.PLOT_VERB)
        if command.kind == plotcmd.PLOT:
            return {"effect": "open_view", "view": view, "command": parsed.text}
        if command.kind == plotcmd.SAVE and command.line is not None:
            message, undo_command = plotcmd.run_save(self._session.conn, command)
            self._session.undo.push(undo_command)
            return {"effect": "statement", "ok": True, "message": message,
                    "moteId": None, "moteName": None, "created": False,
                    "needsConfirm": False, "choices": [], "wrote": True}
        return {"effect": "pane_action", "view": view, "action": command.kind,
                "name": command.name, "format": command.format, "path": command.path}

    def _verb_fit(self, parsed: commands.ParsedLine) -> dict:
        """`fit …` opens (or re-points) a fit pane with the literal line;
        `fit … --apply` runs the fit and writes it, one undo step."""
        line = fit_repo.parse(parsed.args)
        if not line.apply:
            return {"effect": "open_view", "view": commands.view_for(fit_repo.FIT_VERB),
                    "command": parsed.text}
        result = fit_repo.run(self._session.conn, line)
        message, undo_command = fit_repo.apply(self._session.conn, result)
        live = self._push(undo_command)
        return {"effect": "statement", "ok": True, "message": message + live,
                "moteId": None, "moteName": None, "created": False,
                "needsConfirm": False, "choices": [], "wrote": True}

    def _verb_garden(self, parsed: commands.ParsedLine, confirm: bool) -> dict:
        """`garden [‹name›]` opens (or re-points) the detail pane with the
        literal line. Every other form acts on gardens, outside any one
        garden's undo: `rm` asks for a second ⏎ first; `export`/`import`
        without a path ask for one with a file dialog; renaming the open
        garden reopens the workspace on the new name."""
        line = home_repo.parse(parsed.args)
        if line.shows:
            return {"effect": "open_view", "view": "garden", "command": parsed.text}
        if line.action == "rm" and not confirm:
            return {"effect": "statement", "ok": False, "needsConfirm": True,
                    "message": f"⏎ again to move {line.args[0]!r} aside (garden restore brings it back)",
                    "moteId": None, "moteName": None, "created": False, "choices": [], "wrote": False}
        if home_repo.needs_path(line):
            path = self._ask_path(line)
            if path is None:
                return _error("cancelled")
            line = home_repo.GardenLine(line.action, (line.args[0], path) if line.action == "export"
                                        else (path, line.args[0]))
        current = self._session.garden
        if line.action == "rename" and line.args[0] == current:
            self._session.rename(line.args[1])
            self._state = state_repo.load(self._session.garden)
            return {"effect": "garden", "boot": self._boot_payload(),
                    "message": f"renamed {current!r} to {line.args[1]!r}"}
        done = home_repo.run(line, current)
        return {"effect": "statement", "ok": True, "message": done.message,
                "moteId": None, "moteName": None, "created": False,
                "needsConfirm": False, "choices": [], "wrote": True}

    def _ask_path(self, line) -> str | None:
        """A native file dialog for `garden export ‹name›` (where to write)
        or `garden import ‹new name›` (what to read). None if cancelled or
        there is no window (tests)."""
        if self._window is None:
            return None
        import webview
        if line.action == "export":
            picked = self._window.create_file_dialog(webview.FileDialog.SAVE, save_filename=f"{line.args[0]}.json")
        else:
            picked = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=("JSON (*.json)",))
        if not picked:
            return None
        return picked if isinstance(picked, str) else picked[0]

    def _verb_solve(self, args: tuple[str, ...]) -> dict:
        """`solve` writes one static solve, one undo step; `solve --live
        on|off` is the workspace toggle, kept in the console sidecar."""
        live = motion_repo.parse_live(args)
        if live is not None:
            self._state.live = live
            self._save_state()
            return {"effect": "live", "live": live,
                    "message": "live: every edit made here is solved" if live else "live off"}
        solved = motion_repo.solve(self._session.conn)
        self._session.undo.push(solved.command)
        return {"effect": "statement", "ok": True, "message": solved.message,
                "moteId": None, "moteName": None, "created": False,
                "needsConfirm": False, "choices": [], "wrote": True}

    def _verb_playhead(self, verb: str, args: tuple[str, ...]) -> dict:
        """The playhead is the shell's (the store's), so these come back
        as an effect for it; nothing here knows where it stands."""
        if verb == motion_repo.STEP_VERB:
            mode, n = motion_repo.parse_step(args)
            return {"effect": "playhead", "op": "step", "mode": mode, "n": n}
        if args:
            return _error(_no_arguments(verb))
        return {"effect": "playhead", "op": verb}

    def _push(self, command) -> str:
        """Push a write's undo step -- after a live solve folded into it,
        when `live` is on. Returns what the live solve adds to the message."""
        if command is None:
            return ""
        suffix = ""
        if self._state.live:
            suffix, command = motion_repo.live_solve(self._session.conn, command)
        self._session.undo.push(command)
        return suffix

    def _verb_cd(self, args: tuple[str, ...]) -> dict:
        if len(args) != 1:
            return _error(f"usage: {RESERVED_VERBS['cd']['usage']}")
        self._switch(args[0])
        return {"effect": "garden", "boot": self._boot_payload()}

    def _verb_layout(self, args: tuple[str, ...]) -> dict:
        if len(args) == 1 and args[0] not in commands.LAYOUT_OPS:
            args = ("load", args[0])  # `layout calc`
        op = args[0] if args else None
        if op == "list" and len(args) == 1:
            return {"effect": "layout", "op": "list", **self._layouts_summary(),
                    "builtins": [b.name for b in commands.BUILTIN_LAYOUTS]}
        if op not in ("save", "load", "rm") or len(args) != 2:
            return _error(f"usage: {RESERVED_VERBS['layout']['usage']}")
        name = args[1]
        if op == "save":
            state_repo.validate_layout_name(name)
            return {"effect": "layout", "op": "save", "name": name}
        builtin = commands.builtin_layout(name)
        if op == "load" and builtin and self._state.layout_get(name) is None:
            return {"effect": "open_layout", "name": name, "tree": builtin.tree, "focus": builtin.focus}
        if self._state.layout_get(name) is None:
            return _error(f"no layout named {name!r}")
        if op == "load":
            self._state.set_active(name)
            self._save_state()
            return {"effect": "layout", "op": "load", "name": name,
                    "tree": self._state.layout_get(name)}
        self._state.layout_delete(name)
        self._save_state()
        return {"effect": "layout", "op": "rm", "name": name, **self._layouts_summary()}

    # ---- private: state ----

    def _switch(self, garden: str) -> None:
        self._session.switch(garden)
        self._state = state_repo.load(self._session.garden)

    def _refresh(self) -> str:
        """Kick a best-effort sync; the shell re-runs every pane, which is
        the "re-read the garden" half -- every read already goes to disk."""
        garden = self._session.garden
        best_effort_sync(garden, manual=True)
        if gardens_repo.get_sync_dir(garden) is None:
            return f"refreshed {garden}"
        return f"refreshed {garden}; syncing in the background"

    def _save_state(self) -> None:
        state_repo.save(self._session.garden, self._state)

    def _layouts_summary(self) -> dict:
        return {"layouts": list(self._state.layouts), "active": self._state.active}

    def _boot_payload(self) -> dict:
        return {
            "garden": self._session.garden,
            "gardens": gardens_repo.list_gardens(),
            "verbs": [{"verb": verb, **meta} for verb, meta in RESERVED_VERBS.items()],
            "layouts": list(self._state.layouts),
            "active": self._state.active,
            "working": self._state.working,
            "history": list(self._state.history),
            "live": self._state.live,
            "crumbs": home_repo.path_of(self._session.garden),
        }


def _no_arguments(word: str) -> str:
    return (f'{word} takes no arguments -- to reach a mote named "{word}", '
            f'quote it: "{word}"')
