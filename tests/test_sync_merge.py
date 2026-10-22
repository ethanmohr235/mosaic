"""The merge (core/merge.py): devices that have seen the same ops hold the
same garden, whatever order the ops arrived in. Runs against the
in-memory server in tests/sync_fakes.py, so no Docker."""
import random
from datetime import timedelta

import pytest

from core import sync, undo
from core.gardens import create_garden
from core.motes import create_mote, delete_mote, delete_property, get_mote, list_motes, update_mote
from core.relationships import (
    create_relationship,
    delete_relationship,
    list_relationships,
    update_relationship,
)
from tests.sync_fakes import URL, Device, FakeServer, snapshot


@pytest.fixture
def server(monkeypatch):
    fake = FakeServer()
    fake.install(monkeypatch)
    return fake


def _devices(tmp_path, monkeypatch, *skews: float) -> list[Device]:
    return [Device(tmp_path / f"d{i}", monkeypatch, timedelta(seconds=s)) for i, s in enumerate(skews)]


def _join(devices: list[Device]) -> list:
    """The first device creates and shares the garden; the rest clone it."""
    first, *rest = devices
    with first.active():
        create_garden(first.garden)
        sync.init(first.garden, URL)
    conns = [first.conn()]
    for device in rest:
        with device.active():
            sync.init(device.garden, URL, clone=True)
        conns.append(device.conn())
    return conns


def _settle(devices: list[Device], conns: list) -> None:
    for _ in range(2):
        for device, conn in zip(devices, conns):
            device.run(conn)


def test_a_link_to_a_mote_deleted_elsewhere_holds_nothing_up(tmp_path, monkeypatch, server):
    a, b = devices = _devices(tmp_path, monkeypatch, 0, 0)
    ca, cb = conns = _join(devices)
    with a.active():
        m, n = create_mote(ca, {"name": "M"}), create_mote(ca, {"name": "N"})
        a.run(ca)
    b.run(cb)
    with b.active():
        create_relationship(cb, m, n, "related_to")
    with a.active():
        delete_mote(ca, m)
        a.run(ca)
    b.run(cb)
    with b.active():
        later = create_mote(cb, {"name": "later work on B"})
    _settle(devices, conns)
    assert get_mote(ca, later) is not None
    assert snapshot(ca) == snapshot(cb)
    assert get_mote(ca, m) is None and list_relationships(ca) == []


def test_an_edit_racing_a_property_delete_converges(tmp_path, monkeypatch, server):
    a, b = devices = _devices(tmp_path, monkeypatch, 0, 0)
    ca, cb = conns = _join(devices)
    with a.active():
        m = create_mote(ca, {"name": "M", "k": "old"})
        a.run(ca)
    b.run(cb)
    with a.active():
        update_mote(ca, m, {"k": "new"})
    with b.active():
        delete_property(cb, m, "k")  # later, so it wins
    _settle(devices, conns)
    assert snapshot(ca) == snapshot(cb)
    assert "k" not in get_mote(ca, m).properties


def test_the_newer_edit_wins_on_a_device_that_saw_the_older_first(tmp_path, monkeypatch, server):
    a, b, c = devices = _devices(tmp_path, monkeypatch, 0, 0, 0)
    ca, cb, cc = conns = _join(devices)
    with a.active():
        m = create_mote(ca, {"k": 0})
        a.run(ca)
    _settle(devices, conns)
    with a.active():
        update_mote(ca, m, {"k": "A (older)"})
    with b.active():
        update_mote(cb, m, {"k": "B (newer)"})
    a.run(ca)
    c.run(cc)
    b.run(cb)
    _settle(devices, conns)
    assert {get_mote(conn, m).properties["k"] for conn in conns} == {"B (newer)"}


def test_an_edit_wins_against_what_it_overwrote_despite_a_slow_clock(tmp_path, monkeypatch, server):
    a, b = devices = _devices(tmp_path, monkeypatch, 3600, 0)  # A's clock is an hour fast
    ca, cb = conns = _join(devices)
    with a.active():
        m = create_mote(ca, {"k": "from the future"})
        a.run(ca)
    b.run(cb)
    with b.active():
        update_mote(cb, m, {"k": "B saw it and changed it"})
    _settle(devices, conns)
    assert get_mote(ca, m).properties["k"] == "B saw it and changed it"


def test_undoing_a_delete_brings_back_what_was_newer_than_it(tmp_path, monkeypatch, server):
    a, b = devices = _devices(tmp_path, monkeypatch, 0, 0)
    ca, cb = conns = _join(devices)
    with a.active():
        m = create_mote(ca, {"name": "M"})
        a.run(ca)
    b.run(cb)
    with a.active():
        command = undo.for_delete_mote(ca, m, get_mote(ca, m).properties, [], "delete M")
        delete_mote(ca, m)
    with b.active():
        update_mote(cb, m, {"note": "B, after A's delete"})
    _settle(devices, conns)
    assert get_mote(ca, m) is None and get_mote(cb, m) is None
    with a.active():
        command.undo()
    _settle(devices, conns)
    assert snapshot(ca) == snapshot(cb)
    assert get_mote(ca, m).properties == {"name": "M", "note": "B, after A's delete"}


def test_switching_servers_starts_progress_over(tmp_path, monkeypatch, server):
    (a,) = devices = _devices(tmp_path, monkeypatch, 0)
    (ca,) = _join(devices)
    with a.active():
        create_mote(ca, {})
        sync.push(ca, a.garden)
        server.ops.clear()  # a fresh, empty server
        sync.init(a.garden, URL + "other")
        assert sync.push(ca, a.garden)["pushed"] == 1


# ---- every interleaving: random edits, random syncs ----

KEYS = ("k0", "k1", "k2")
TYPES = ("t0", "t1")


class _Actor:
    """One device's random edits, and the undo commands it could run."""

    def __init__(self, device: Device, conn, rng: random.Random):
        self.device, self.conn, self.rng = device, conn, rng
        self.undos: list[tuple[str, undo.Command]] = []

    def step(self) -> None:
        motes = [m.id for m in list_motes(self.conn)]
        rels = [r.id for r in list_relationships(self.conn)]
        choices = ["create", "sync"] + (["set", "set", "unset", "delete", "link"] if motes else [])
        choices += ["retype", "move", "unlink"] if rels else []
        choices += ["revive"] if self.undos else []
        getattr(self, "_" + self.rng.choice(choices))(motes, rels)

    def _create(self, motes, rels):
        create_mote(self.conn, {self.rng.choice(KEYS): self.rng.randint(0, 9)})

    def _sync(self, motes, rels):
        sync.run(self.conn, self.device.garden, URL)

    def _set(self, motes, rels):
        update_mote(self.conn, self.rng.choice(motes), {self.rng.choice(KEYS): self.rng.randint(0, 99)})

    def _unset(self, motes, rels):
        delete_property(self.conn, self.rng.choice(motes), self.rng.choice(KEYS))

    def _delete(self, motes, rels):
        mote_id = self.rng.choice(motes)
        touching = [{"id": r.id, "fromId": r.from_id, "toId": r.to_id, "type": r.type}
                    for r in list_relationships(self.conn) if mote_id in (r.from_id, r.to_id)]
        command = undo.for_delete_mote(self.conn, mote_id, get_mote(self.conn, mote_id).properties, touching, "")
        delete_mote(self.conn, mote_id)
        self.undos.append((mote_id, command))

    def _revive(self, motes, rels):
        mote_id, command = self.undos.pop(self.rng.randrange(len(self.undos)))
        if get_mote(self.conn, mote_id) is None:
            command.undo()

    def _link(self, motes, rels):
        create_relationship(self.conn, self.rng.choice(motes), self.rng.choice(motes), self.rng.choice(TYPES))

    def _retype(self, motes, rels):
        update_relationship(self.conn, self.rng.choice(rels), type_=self.rng.choice(TYPES))

    def _move(self, motes, rels):
        update_relationship(self.conn, self.rng.choice(rels), to_id=self.rng.choice(motes))

    def _unlink(self, motes, rels):
        delete_relationship(self.conn, self.rng.choice(rels))


@pytest.mark.parametrize("seed", range(40))
def test_random_edits_on_three_devices_converge(seed, tmp_path, monkeypatch, server):
    rng = random.Random(seed)
    devices = _devices(tmp_path, monkeypatch, 0, 2.5, -4)  # clocks disagree
    conns = _join(devices)
    actors = [_Actor(d, c, rng) for d, c in zip(devices, conns)]
    for _ in range(150):
        actor = rng.choice(actors)
        with actor.device.active():
            actor.step()
    _settle(devices, conns)
    first = snapshot(conns[0])
    assert first[0], "converged on an empty garden proves nothing"
    for conn in conns[1:]:
        assert snapshot(conn) == first
