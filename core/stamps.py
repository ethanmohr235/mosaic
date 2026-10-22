# core/stamps.py
"""The order sync's merge (core/merge.py) puts every write in.

A stamp is (ts, device_id): when the write was made, by whom. Every row a
write touches keeps the stamp of the write that last set it --
`mote_properties.updated_at/updated_by` for a value,
`motes.created_at/created_by` for the create that brought a mote into
being, `relationships.updated_at/updated_by` for a relationship's last
create or update (a relationship is one value) -- and every delete leaves
its stamp in `tombstones`. Replay writes the op's own stamp, never the time
it happened to arrive (core/oplog.py::replaying), so two devices that
have seen the same ops compare the same stamps and pick the same winner.

A local write is stamped just after whatever it overwrites, even when
this machine's clock is behind the one that wrote it (next_local). That
keeps "I edited what I saw" winning against what I saw, without any
clock state of its own.

`ts` is always UTC ISO 8601 with microseconds, which sorts as text;
older rows wrote it without them (or with none at all), and normalize()
reads both.
"""
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.device import get_device_id

_TICK = timedelta(microseconds=1)
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def format_ts(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def normalize(ts: str | None) -> str:
    """Any ts this codebase has ever written, as format_ts writes it. None
    (a row from before stamps) reads as the epoch: older than anything."""
    if not ts:
        return format_ts(_EPOCH)
    moment = datetime.fromisoformat(ts)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return format_ts(moment)


@dataclass(frozen=True, order=True)
class Stamp:
    ts: str
    device: str

    @classmethod
    def of(cls, ts: str | None, device: str | None) -> "Stamp":
        return cls(normalize(ts), device or "")


def next_local(*floors: "Stamp | None") -> Stamp:
    """A stamp for a write made here, now: later than the clock and later
    than every stamp it has to beat."""
    moment = datetime.now(timezone.utc)
    for floor in floors:
        if floor is not None:
            moment = max(moment, datetime.fromisoformat(floor.ts) + _TICK)
    return Stamp(format_ts(moment), get_device_id())


def latest(*stamps: "Stamp | None") -> Stamp | None:
    present = [s for s in stamps if s is not None]
    return max(present) if present else None


def _stamp(row: sqlite3.Row | None, ts: str, by: str) -> Stamp | None:
    return Stamp.of(row[ts], row[by]) if row is not None else None


def mote_life(conn: sqlite3.Connection, mote_id: str) -> Stamp | None:
    """The create a live mote was last brought into being by; None when
    it isn't here."""
    row = conn.execute("SELECT created_at, created_by FROM motes WHERE id = ?", (mote_id,)).fetchone()
    return _stamp(row, "created_at", "created_by")


def property_stamp(conn: sqlite3.Connection, mote_id: str, key: str) -> Stamp | None:
    row = conn.execute(
        "SELECT updated_at, updated_by FROM mote_properties WHERE mote_id = ? AND key = ?",
        (mote_id, key),
    ).fetchone()
    return _stamp(row, "updated_at", "updated_by")


def relationship_stamp(conn: sqlite3.Connection, rel_id: str) -> Stamp | None:
    """The last create or update of a live relationship, else None."""
    row = conn.execute("SELECT updated_at, updated_by FROM relationships WHERE id = ?", (rel_id,)).fetchone()
    return _stamp(row, "updated_at", "updated_by")


def newest_child(conn: sqlite3.Connection, mote_id: str) -> Stamp | None:
    """The latest stamp on anything a delete of this mote takes with it."""
    rows = conn.execute(
        "SELECT updated_at AS ts, updated_by AS by FROM mote_properties WHERE mote_id = ? "
        "UNION ALL SELECT updated_at, updated_by FROM relationships WHERE from_id = ? OR to_id = ? "
        "UNION ALL SELECT created_at, created_by FROM motes WHERE id = ?",
        (mote_id, mote_id, mote_id, mote_id),
    ).fetchall()
    return latest(*(Stamp.of(r["ts"], r["by"]) for r in rows))


def death(conn: sqlite3.Connection, kind: str, entity_id: str, key: str = "") -> Stamp | None:
    """The latest delete of a mote, a relationship or one property
    (`kind` "mote", "relationship", "property" with its `key`)."""
    row = conn.execute(
        "SELECT ts, device_id FROM tombstones WHERE entity_type = ? AND entity_id = ? AND key = ?",
        (kind, entity_id, key),
    ).fetchone()
    return _stamp(row, "ts", "device_id")


def newest_property_death(conn: sqlite3.Connection, mote_id: str) -> Stamp | None:
    rows = conn.execute(
        "SELECT ts, device_id FROM tombstones WHERE entity_type = 'property' AND entity_id = ?",
        (mote_id,),
    ).fetchall()
    return latest(*(Stamp.of(r["ts"], r["device_id"]) for r in rows))


def bury(conn: sqlite3.Connection, kind: str, entity_id: str, stamp: Stamp, key: str = "") -> None:
    """Record a delete, keeping the latest one. Does not commit."""
    if (known := death(conn, kind, entity_id, key)) is not None and known >= stamp:
        return
    conn.execute(
        "INSERT INTO tombstones (entity_type, entity_id, key, ts, device_id) VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(entity_type, entity_id, key) DO UPDATE SET ts = excluded.ts, device_id = excluded.device_id",
        (kind, entity_id, key, stamp.ts, stamp.device),
    )
