# console/mora.py
"""`api.mora`: the note and worksheet panes' bridge (docs/console.md ->
Settled for U6).

    paste_image(data_base64)     store a pasted screenshot as an image mote
                                 (auto-named shot-NNN), one undo step
    open(command, selection)     which note an `edit …`/`read …` line names,
                                 and its stored source
    render(note_id, source)      the document a draft says (core/document.py)
    save(note_id, name, source, session)
                                 write a note; one editing session is one
                                 undo step, however many pauses it saves on
    notes(command)               the `notes …` list
    worksheet()                  the garden's worksheet text
    preview(text)                each line's kind, problem and value
    apply(text)                  write the worksheet, one undo step

The one place a view writes through its namespace rather than a command
line: a note's text is too long for a line, and saving it on a pause is
the user's decision (Settled for U6). Every write here is still one undo
step on the session's stack, and `apply` honours `live`."""
import base64
import binascii
import logging
import shlex
import sqlite3
from typing import Any, Callable

from console.session import Session, locked
from core import document
from core import images
from core import modoc
from core import motion as motion_repo
from core import worksheet as worksheet_repo
from core.command_bar import CommandError

logger = logging.getLogger(__name__)

_ERRORS = (CommandError, ValueError, LookupError, sqlite3.OperationalError)  # ImageError is a ValueError
# Every backslash-word the parser reads outside maths, for the editor's
# highlighter: anything else is coloured as a typo.
COMMANDS = sorted({*modoc.INLINE_COMMANDS, *modoc.BLOCK_COMMANDS})
# One editing session folds into one undo step: the coalescing window is
# longer than any pause, and a different write in between ends it anyway.
_SESSION_WINDOW = 24 * 3600.0


class MoraApi:
    def __init__(self, session: Session, live: Callable[[], bool] = lambda: False):
        self._session = session
        self._live = live
        self._memo = document.GardenMemo()

    @locked
    def open(self, command: str, selection: list[str] | None = None) -> dict[str, Any]:
        def answer():
            words = shlex.split(command or "")
            if len(words) < 2:
                return {"empty": True}
            if len(words) > 2:
                raise CommandError(f"{words[0]} takes one note -- quote a name with spaces")
            if words[1] == "@sel" and not selection:
                return {"empty": True}
            found = document.target(self._session.conn, words[1], selection or [])
            source = modoc.read_source(self._session.conn, found.mote_id) if found.mote_id else ""
            return {"noteId": found.mote_id, "name": found.name, "source": source,
                    "exists": found.mote_id is not None, "commands": COMMANDS}
        return self._answer(command, answer)

    @locked
    def paste_image(self, data_base64: str, name: str = "") -> dict[str, Any]:
        """A screenshot from the clipboard, stored like `image add` does
        and named `shot-NNN` unless `name` says otherwise. The editor
        inserts `\\image{name}` at the caret from the answer."""
        def answer():
            try:
                data = base64.b64decode(data_base64 or "", validate=True)
            except (binascii.Error, ValueError) as e:
                raise CommandError(f"the pasted image is not valid base64: {e}") from e
            shot, command = images.add_undoable(self._session.conn, data, name or None)
            self._session.undo.push(command)
            return {"name": shot.name, "moteId": shot.mote_id, "bytes": len(shot.data),
                    "mime": shot.mime}
        return self._answer("paste_image", answer)

    @locked
    def render(self, note_id: str | None, source: str) -> dict[str, Any]:
        def answer():
            if note_id is None:
                # A note not written yet renders against no mote of its own.
                return document.render(self._session.conn, "", source or "", draw=self._memo)
            return document.render(self._session.conn, note_id, source or "", draw=self._memo)
        return self._answer("render", answer)

    @locked
    def save(self, note_id: str | None, name: str, source: str, session: str = "") -> dict[str, Any]:
        def answer():
            saved_id, command = document.save(self._session.conn, note_id, source or "", name=name)
            key = ("note", saved_id, session) if session else None
            self._session.undo.push(command, coalesce_key=key, coalesce_window=_SESSION_WINDOW)
            return {"noteId": saved_id, "created": note_id is None}
        return self._answer("save", answer)

    @locked
    def notes(self, command: str = "notes") -> dict[str, Any]:
        def answer():
            words = shlex.split(command or "")
            return {"notes": document.notes(self._session.conn, " ".join(words[1:]))}
        return self._answer(command, answer)

    @locked
    def worksheet(self) -> dict[str, Any]:
        return self._answer("worksheet", lambda: {"text": worksheet_repo.read(self._session.conn)})

    @locked
    def preview(self, text: str) -> dict[str, Any]:
        return self._answer("preview", lambda: {"lines": [
            {"number": p.number, "kind": p.kind, "problem": p.problem, "value": p.value,
             "title": p.title}
            for p in worksheet_repo.preview(self._session.conn, text or "",
                                             self._memo.inferred_units(self._session.conn))]})

    @locked
    def apply(self, text: str) -> dict[str, Any]:
        def answer():
            conn = self._session.conn
            applied, command = worksheet_repo.apply_undoable(conn, text or "")
            live = ""
            if command is not None:
                if self._live():
                    live, command = motion_repo.live_solve(conn, command)
                self._session.undo.push(command)
            parts = [f"{applied.givens_written} given{'' if applied.givens_written == 1 else 's'}",
                     f"{applied.rules_written} rule{'' if applied.rules_written == 1 else 's'}"]
            if applied.motes_created:
                parts.append(f"made {', '.join(applied.motes_created)}")
            if applied.deleted:
                parts.append(f"deleted {', '.join(applied.deleted)}")
            return {"message": "applied the worksheet: " + " · ".join(parts) + live,
                    "wrote": command is not None, "problems": list(applied.problems),
                    "text": worksheet_repo.read(conn)}
        return self._answer("apply", answer)

    def _answer(self, what: str, compute) -> dict[str, Any]:
        try:
            return compute()
        except _ERRORS as e:
            return {"error": self._session.describe(e)}
        except Exception as e:  # never a traceback across the bridge
            logger.exception("mora failed: %r", what)
            return {"error": f"internal error: {e}"}
