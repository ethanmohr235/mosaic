# gardens_app/api.py
"""JS-facing bridge for the Gardens window.

Unlike the six app Apis, this one holds no garden connection. It is not
open *on* a garden -- it manages all of them, and a garden it is about
to rename, delete or import into is one it must not be holding open.
Every method that needs to read a garden therefore opens a short-lived
connection and closes it in a finally, the same shape Monarch's old
bridge used for exporting a garden other than its own.

That also keeps this window honest about the other windows: Motion and
Monarch may each have the same garden open in their own process while
this one runs, so nothing here assumes it is the only writer.
"""
import json
import logging
import sqlite3
import threading
from pathlib import Path

import webview

from api_common import open_app as _open_app
from api_common import synchronized as _synchronized
from core import garden_io
from core import gardens as gardens_repo
from core import sync as sync_repo
from core.db import garden_db_path, get_connection, initialize_database

logger = logging.getLogger(__name__)


class Api:
    """The garden-management surface every MonOSS window used to either
    carry a private copy of (Monarch) or go without (the other five).

    `select`/`open_sync` are the window's opening state rather than
    behavior: whichever app opened this window passes its own garden
    along (run_gardens.py --garden) so the list comes up with that row
    already selected, and Tools -> Sync passes --sync so it comes up on
    that garden's sync panel instead of making the user find it again."""

    def __init__(self, select: str | None = None, open_sync: bool = False):
        self._lock = threading.Lock()
        self._select = select
        self._open_sync = open_sync
        # Set by gardens_app/app.py once the window exists -- the native
        # file dialogs for import/export/sync-folder need it.
        self._window: webview.Window | None = None

    def shutdown(self) -> None:
        """Nothing to close: this window holds no connection. Defined
        because window_launcher.launch_window() wires it to the window's
        closed event for every app."""

    # ---- reading the list ----

    @_synchronized
    def initial(self) -> dict:
        return {
            "gardens": self._gardens(),
            "select": self._select,
            "open_sync": self._open_sync,
        }

    @_synchronized
    def list_gardens(self) -> list[dict]:
        return self._gardens()

    def _gardens(self) -> list[dict]:
        """One row per garden, with the two numbers that tell them apart
        at a glance (how much is in it, when it last changed) and the
        one flag that changes what the row can do (whether it syncs).

        Sync *status* -- pending op counts, known remote devices -- is
        deliberately not here: it needs a connection per garden, and
        this runs on every refresh of a list that is mostly names."""
        rows = []
        for name in gardens_repo.list_gardens():
            path = garden_db_path(name)
            rows.append({
                "name": name,
                **self._counts(name),
                "bytes": path.stat().st_size if path.exists() else 0,
                "modified": path.stat().st_mtime if path.exists() else None,
                "sync_dir": gardens_repo.get_sync_dir(name),
            })
        return rows

    def _counts(self, name: str) -> dict:
        """Mote and relationship counts, or None for both if the file
        can't be read as a garden.

        A .db in gardens/ is not guaranteed to be one of ours -- a
        half-written copy from a folder-sync tool lands there looking
        exactly like a garden -- and the list is the wrong place to
        discover that with a traceback. A row with no counts still
        renames, exports and deletes."""
        try:
            conn = get_connection(name)
        except Exception:
            logger.exception("could not open garden %r", name)
            return {"motes": None, "relationships": None}
        try:
            motes = conn.execute("SELECT COUNT(*) AS n FROM motes").fetchone()["n"]
            rels = conn.execute("SELECT COUNT(*) AS n FROM relationships").fetchone()["n"]
            return {"motes": motes, "relationships": rels}
        except sqlite3.DatabaseError:
            logger.exception("could not count %r", name)
            return {"motes": None, "relationships": None}
        finally:
            conn.close()

    # ---- creating, renaming, removing ----

    @_synchronized
    def create_garden(self, name: str) -> dict:
        gardens_repo.create_garden(name)
        return {"garden": name}

    @_synchronized
    def rename_garden(self, old: str, new: str) -> dict:
        gardens_repo.rename_garden(old, new)
        return {"garden": new}

    @_synchronized
    def delete_garden(self, name: str) -> dict:
        """Deletes any garden, including the last one and one another
        window has open -- neither is a case this window can honestly
        refuse. core/gardens.py moves the file aside rather than
        removing it (restore_garden brings it back, and the frontend
        offers exactly that as an undo), a window already holding it
        open keeps working off its own file handle until it closes, and
        resolve_garden() makes a fresh `default` if the last one goes.

        Monarch's modal blocked both cases because it *was* a garden
        window: it had a current garden to be standing in and to be
        left with nothing to switch to. This window stands in none."""
        gardens_repo.delete_garden(name)
        return {"deleted": name}

    @_synchronized
    def restore_garden(self, name: str) -> dict:
        gardens_repo.restore_garden(name)
        return {"restored": name}

    # ---- import and export ----

    @_synchronized
    def export_garden_to_file(self, name: str) -> dict:
        """Asks for a path and writes the file -- named for that, since
        core/garden_io.py's export_garden() builds the document and
        returns it rather than putting it anywhere."""
        path = self._window.create_file_dialog(
            webview.FileDialog.SAVE, save_filename=f"{name}.json"
        )
        if not path:
            return {"cancelled": True}
        target = Path(path if isinstance(path, str) else path[0])

        conn = get_connection(name)
        try:
            data = garden_io.export_garden(conn)
        finally:
            conn.close()
        target.write_text(json.dumps(data, indent=2))
        return {"path": str(target)}

    @_synchronized
    def import_garden_into_new(self, new_garden_name: str) -> dict:
        """Imports always land in a brand-new garden: core/garden_io.py
        refuses to merge into one that already holds any of the incoming
        mote ids, so asking for the name up front is the honest flow
        rather than discovering the conflict half way in.

        The new garden is removed again if the import fails -- otherwise
        a bad file leaves an empty garden behind under the name the user
        wanted, and the retry then fails on the name instead of the
        file."""
        paths = self._window.create_file_dialog(webview.FileDialog.OPEN)
        if not paths:
            return {"cancelled": True}
        data = json.loads(Path(paths[0]).read_text())

        gardens_repo.create_garden(new_garden_name)
        conn = get_connection(new_garden_name)
        try:
            initialize_database(conn)
            motes, rels = garden_io.import_garden(conn, data)
        except Exception:
            conn.close()
            gardens_repo.delete_garden(new_garden_name)
            raise
        finally:
            conn.close()
        return {"garden": new_garden_name, "motes": motes, "relationships": rels}

    # ---- sync ----
    #
    # The same sync controls Monarch carried in Tools -> Sync, except per
    # garden rather than for whichever one that window happened to be
    # open on. core/sync.py does the real work; this is enable/disable,
    # a manual run, and the auto-sync flag that gates each app's
    # lifecycle sync points (open, garden switch, close) -- "automatic"
    # means those call sites, not a timer.

    @_synchronized
    def sync_status(self, name: str) -> dict:
        conn = get_connection(name)
        try:
            status = sync_repo.status(conn, name)
        finally:
            conn.close()
        status["auto_sync"] = gardens_repo.get_auto_sync_enabled(name)
        return status

    @_synchronized
    def enable_sync(self, name: str, url: str) -> dict:
        # clone=True is always safe here: the only thing it covers is
        # "the garden doesn't exist locally yet," which can't be true of
        # one this window just listed.
        return sync_repo.init(name, url, clone=True)

    @_synchronized
    def disable_sync(self, name: str) -> dict:
        # Returns a value rather than None so the frontend's call()
        # wrapper can tell "it worked" from "it raised and was toasted" --
        # a bridge method that resolves to null gives it no way to.
        gardens_repo.clear_sync_dir(name)
        return {"garden": name, "sync_dir": None}

    @_synchronized
    def set_auto_sync(self, name: str, enabled: bool) -> dict:
        gardens_repo.set_auto_sync_enabled(name, enabled)
        return {"garden": name, "auto_sync": enabled}

    @_synchronized
    def sync_now(self, name: str) -> dict:
        """A manual sync doesn't swallow errors, unlike the apps'
        best-effort lifecycle syncs -- the frontend's call() surfaces
        the rejection as a toast."""
        conn = get_connection(name)
        try:
            return sync_repo.run(conn, name)
        finally:
            conn.close()

    # ---- opening a garden in an app ----

    @_synchronized
    def open_app(self, app_id: str, garden: str) -> dict:
        """Launch one of the six windows on a chosen garden.

        This is what makes the manager standalone rather than a modal:
        it doesn't switch anybody's garden (it is its own process and
        can't reach into theirs), it opens a window already on the one
        you picked. An app that is already running picks a new garden up
        through its own switcher -- list_gardens reads the directory, so
        a garden created here is in that menu the next time it opens."""
        return _open_app(app_id, garden)
