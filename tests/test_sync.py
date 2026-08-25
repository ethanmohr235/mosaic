"""Two-device sync, simulated in one process: each "device" is its own
gardens/ directory (so it gets its own database and its own device_id),
and both point core/sync.py at one shared tmp folder standing in for
whatever a real Syncthing/Dropbox/iCloud folder would sync between two
real machines.

core.device.get_device_id() and every core.gardens.py/core.db.py path
read core.db.GARDENS_DIR fresh on every call, so switching which
"device" is active is just repointing that one module attribute right
before the calls that belong to it -- see as_device() below. An already-
open sqlite3.Connection is unaffected by that switch; only which
device_id a *new* write is attributed to depends on it.
"""
import time
from contextlib import contextmanager

import core.db as db
from core import sync
from core.gardens import create_garden
from core.motes import create_mote, delete_mote, get_mote, list_motes, update_mote
from core.relationships import create_relationship, list_relationships, update_relationship


@contextmanager
def as_device(monkeypatch, root):
    monkeypatch.setattr(db, "GARDENS_DIR", root)
    yield


def _new_device(monkeypatch, root):
    with as_device(monkeypatch, root):
        create_garden("g")
        return db.get_connection("g")


def test_second_device_clones_existing_history(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        mote_a = create_mote(conn_a, {"name": "from A"})
        sync.init("g", sync_dir)
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        result = sync.init("g", sync_dir, clone=True)
        assert result["cloned"] is True
        conn_b = db.get_connection("g")
        assert get_mote(conn_b, mote_a) is not None

    conn_a.close()
    conn_b.close()


def test_cloning_without_permission_is_refused(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        create_mote(conn_a, {"name": "from A"})
        sync.init("g", sync_dir)
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        try:
            sync.init("g", sync_dir)
            assert False, "expected a ValueError without clone=True"
        except ValueError:
            pass

    conn_a.close()


def test_disjoint_edits_converge_both_ways(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        sync.init("g", sync_dir)
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        sync.init("g", sync_dir, clone=True)
        conn_b = db.get_connection("g")

    with as_device(monkeypatch, tmp_path / "a"):
        mote_a = create_mote(conn_a, {"name": "from A"})
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        mote_b = create_mote(conn_b, {"name": "from B"})
        sync.push(conn_b, "g", sync_dir)
        pulled = sync.pull(conn_b, "g", sync_dir)
        # B's own history, echoed back by the shared folder, is skipped.
        assert pulled["applied"] == 1
        assert get_mote(conn_b, mote_a) is not None

    with as_device(monkeypatch, tmp_path / "a"):
        pulled = sync.pull(conn_a, "g", sync_dir)
        assert pulled["applied"] == 1
        assert get_mote(conn_a, mote_b) is not None

    with as_device(monkeypatch, tmp_path / "a"):
        assert len(list_motes(conn_a)) == 2
    with as_device(monkeypatch, tmp_path / "b"):
        assert len(list_motes(conn_b)) == 2

    conn_a.close()
    conn_b.close()


def test_concurrent_edits_to_different_properties_both_survive(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        mote_id = create_mote(conn_a, {"name": "shared", "status": "draft"})
        sync.init("g", sync_dir)
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        sync.init("g", sync_dir, clone=True)
        conn_b = db.get_connection("g")

    with as_device(monkeypatch, tmp_path / "a"):
        update_mote(conn_a, mote_id, {"status": "final"})
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        update_mote(conn_b, mote_id, {"name": "renamed by B"})
        sync.push(conn_b, "g", sync_dir)
        sync.pull(conn_b, "g", sync_dir)
        merged = get_mote(conn_b, mote_id)
        assert merged.properties == {"name": "renamed by B", "status": "final"}

    with as_device(monkeypatch, tmp_path / "a"):
        sync.pull(conn_a, "g", sync_dir)
        merged = get_mote(conn_a, mote_id)
        assert merged.properties == {"name": "renamed by B", "status": "final"}

    conn_a.close()
    conn_b.close()


def test_same_property_conflict_resolves_last_write_wins(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        mote_id = create_mote(conn_a, {"status": "draft"})
        sync.init("g", sync_dir)
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        sync.init("g", sync_dir, clone=True)
        conn_b = db.get_connection("g")

    with as_device(monkeypatch, tmp_path / "a"):
        update_mote(conn_a, mote_id, {"status": "from A"})
        sync.push(conn_a, "g", sync_dir)

    time.sleep(0.01)  # ensure B's write has a strictly later timestamp

    with as_device(monkeypatch, tmp_path / "b"):
        update_mote(conn_b, mote_id, {"status": "from B"})
        sync.push(conn_b, "g", sync_dir)
        pulled = sync.pull(conn_b, "g", sync_dir)
        # A's now-older edit to the same key is shadowed, not applied.
        assert pulled["skipped"] and pulled["skipped"][0]["reason"] == "shadowed"
        assert get_mote(conn_b, mote_id).properties["status"] == "from B"

    with as_device(monkeypatch, tmp_path / "a"):
        sync.pull(conn_a, "g", sync_dir)
        assert get_mote(conn_a, mote_id).properties["status"] == "from B"

    conn_a.close()
    conn_b.close()


def test_delete_wins_over_a_racing_edit(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        mote_id = create_mote(conn_a, {"status": "draft"})
        sync.init("g", sync_dir)
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        sync.init("g", sync_dir, clone=True)
        conn_b = db.get_connection("g")

    with as_device(monkeypatch, tmp_path / "a"):
        delete_mote(conn_a, mote_id)
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        # B hasn't seen A's delete yet, so this edit succeeds locally --
        # the race is only visible once each side pulls the other's op.
        update_mote(conn_b, mote_id, {"status": "edited by B, too late"})
        sync.push(conn_b, "g", sync_dir)
        pulled = sync.pull(conn_b, "g", sync_dir)
        # B only pulls A's delete_mote op here, which applies cleanly --
        # no skip note on this side.
        assert pulled["skipped"] == []
        assert get_mote(conn_b, mote_id) is None

    with as_device(monkeypatch, tmp_path / "a"):
        pulled = sync.pull(conn_a, "g", sync_dir)
        assert pulled["skipped"] and pulled["skipped"][0]["reason"] == "delete_won"
        assert get_mote(conn_a, mote_id) is None

    conn_a.close()
    conn_b.close()


def test_relationship_sync_and_update(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        alice = create_mote(conn_a, {"name": "Alice"})
        bob = create_mote(conn_a, {"name": "Bob"})
        rel_id = create_relationship(conn_a, alice, bob, "knows")
        sync.init("g", sync_dir)
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        sync.init("g", sync_dir, clone=True)
        conn_b = db.get_connection("g")
        assert len(list_relationships(conn_b)) == 1

    with as_device(monkeypatch, tmp_path / "a"):
        update_relationship(conn_a, rel_id, type_="likes")
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        sync.pull(conn_b, "g", sync_dir)
        rels = list_relationships(conn_b)
        assert len(rels) == 1
        assert rels[0].type == "likes"

    conn_a.close()
    conn_b.close()


def test_push_with_nothing_new_is_a_noop(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        create_mote(conn_a, {})
        sync.init("g", sync_dir)
        first = sync.push(conn_a, "g", sync_dir)
        assert first["pushed"] == 1
        second = sync.push(conn_a, "g", sync_dir)
        assert second == {"pushed": 0}

    conn_a.close()


# ---- backfilling history for motes/relationships that predate oplog
# logging (core/sync.py::_backfill_orphans) ----
#
# A garden that had data before the oplog table's logging existed has
# entities in `motes`/`relationships` with no corresponding create_*
# oplog row -- never logged, because the logging code didn't exist yet.
# `DELETE FROM oplog WHERE entity_id = ?` after an ordinary create is
# how these tests fake that: the row/entity still exists, only its
# create op is gone, which is the actual invariant push() has to repair
# (not *why* it's missing).

def test_backfill_logs_an_orphan_mote_and_relationship(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        safe = create_mote(conn_a, {"name": "safe"})
        orphan = create_mote(conn_a, {"name": "orphan"})
        rel_id = create_relationship(conn_a, orphan, safe, "knows")
        conn_a.execute("DELETE FROM oplog WHERE entity_id IN (?, ?)", (orphan, rel_id))
        conn_a.commit()

        sync.init("g", sync_dir)
        sync.push(conn_a, "g", sync_dir)

        mote_rows = conn_a.execute(
            "SELECT action, ts FROM oplog WHERE entity_id = ?", (orphan,)
        ).fetchall()
        assert len(mote_rows) == 1 and mote_rows[0]["action"] == "create_mote"

        safe_ts = conn_a.execute(
            "SELECT ts FROM oplog WHERE entity_id = ? AND action = 'create_mote'", (safe,)
        ).fetchone()["ts"]
        assert mote_rows[0]["ts"] < safe_ts  # backfilled anchor sorts before real history

        rel_rows = conn_a.execute(
            "SELECT action FROM oplog WHERE entity_id = ?", (rel_id,)
        ).fetchall()
        assert len(rel_rows) == 1 and rel_rows[0]["action"] == "create_relationship"

    conn_a.close()


def test_orphan_backfill_end_to_end_matches_source(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        safe = create_mote(conn_a, {"name": "safe"})
        orphan = create_mote(conn_a, {"name": "orphan"})
        rel_id = create_relationship(conn_a, orphan, safe, "knows")
        conn_a.execute("DELETE FROM oplog WHERE entity_id IN (?, ?)", (orphan, rel_id))
        conn_a.commit()

        sync.init("g", sync_dir)
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        sync.init("g", sync_dir, clone=True)
        conn_b = db.get_connection("g")
        # Not just the one earliest mote -- the whole garden, which is
        # the actual regression this fix targets.
        assert len(list_motes(conn_b)) == 2
        assert len(list_relationships(conn_b)) == 1
        assert get_mote(conn_b, orphan).properties == {"name": "orphan"}

    conn_a.close()
    conn_b.close()


def test_backfill_is_idempotent(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        orphan = create_mote(conn_a, {"name": "orphan"})
        conn_a.execute("DELETE FROM oplog WHERE entity_id = ?", (orphan,))
        conn_a.commit()

        sync.init("g", sync_dir)
        sync.push(conn_a, "g", sync_dir)
        sync.push(conn_a, "g", sync_dir)

        count = conn_a.execute(
            "SELECT COUNT(*) AS c FROM oplog WHERE entity_id = ? AND action = 'create_mote'",
            (orphan,),
        ).fetchone()["c"]
        assert count == 1

    conn_a.close()


def test_self_heal_repairs_a_device_that_already_stalled(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        orphan = create_mote(conn_a, {"name": "orphan"})
        safe = create_mote(conn_a, {"name": "safe"})
        create_relationship(conn_a, orphan, safe, "knows")
        # Only the mote predates logging -- the relationship pointing at
        # it was created (and logged) normally, exactly like the
        # real-world garden this was diagnosed against.
        conn_a.execute("DELETE FROM oplog WHERE entity_id = ?", (orphan,))
        conn_a.commit()

        sync.init("g", sync_dir)
        # Simulate a push from before this fix existed: the gap ships
        # as-is, with nothing to repair it downstream.
        with monkeypatch.context() as old_push:
            old_push.setattr(sync, "_backfill_orphans", lambda conn, device_id: None)
            sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        sync.init("g", sync_dir, clone=True)
        conn_b = db.get_connection("g")
        # Stalled: the relationship's FK failure on the missing orphan
        # skips everything queued after it in this pull.
        assert len(list_motes(conn_b)) == 1
        assert list_relationships(conn_b) == []

    with as_device(monkeypatch, tmp_path / "a"):
        sync.push(conn_a, "g", sync_dir)  # backfill runs for real this time

    with as_device(monkeypatch, tmp_path / "b"):
        sync.pull(conn_b, "g", sync_dir)
        assert len(list_motes(conn_b)) == 2
        assert len(list_relationships(conn_b)) == 1

    conn_a.close()
    conn_b.close()


def test_backfill_relationship_referencing_a_non_orphan_mote(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        mote1 = create_mote(conn_a, {"name": "one"})
        mote2 = create_mote(conn_a, {"name": "two"})
        rel_id = create_relationship(conn_a, mote1, mote2, "knows")
        # Only the relationship's own oplog row is missing -- both
        # endpoints' create_mote rows are real and intact, so the
        # backfilled ts must sort after both of *those*, not just after
        # the generic anchor (the max()-not-flat-epsilon fix).
        conn_a.execute("DELETE FROM oplog WHERE entity_id = ?", (rel_id,))
        conn_a.commit()

        sync.init("g", sync_dir)
        sync.push(conn_a, "g", sync_dir)

    with as_device(monkeypatch, tmp_path / "b"):
        sync.init("g", sync_dir, clone=True)
        conn_b = db.get_connection("g")
        assert len(list_motes(conn_b)) == 2
        rels = list_relationships(conn_b)
        assert len(rels) == 1
        assert rels[0].from_id == mote1 and rels[0].to_id == mote2

    conn_a.close()
    conn_b.close()


def test_status_reports_local_and_remote_progress(tmp_path, monkeypatch):
    sync_dir = tmp_path / "shared"

    conn_a = _new_device(monkeypatch, tmp_path / "a")
    with as_device(monkeypatch, tmp_path / "a"):
        create_mote(conn_a, {})
        create_mote(conn_a, {})
        sync.init("g", sync_dir)
        before = sync.status(conn_a, "g", sync_dir)
        assert before["local_ops"] == 2
        assert before["local_ops_pending"] == 2

        sync.push(conn_a, "g", sync_dir)
        after = sync.status(conn_a, "g", sync_dir)
        assert after["local_ops_pending"] == 0
        assert after["sync_dir"] == str(sync_dir)

    conn_a.close()
