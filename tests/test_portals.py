import pytest

from core.db import get_connection
from core.gardens import create_garden
from core.motes import create_mote, get_mote
from core.portals import (
    PORTAL_GARDEN_KEY,
    PORTAL_MOTE_KEY,
    PortalGardenMissing,
    PortalMoteMissing,
    create_portal,
    portal_target,
    resolve_portal,
)


def test_portal_target_extracts_from_properties():
    assert portal_target({PORTAL_GARDEN_KEY: "other", PORTAL_MOTE_KEY: "abc"}) == ("other", "abc")


def test_portal_target_none_when_absent():
    assert portal_target({}) is None


def test_portal_target_none_when_only_half_present():
    assert portal_target({PORTAL_GARDEN_KEY: "other"}) is None
    assert portal_target({PORTAL_MOTE_KEY: "abc"}) is None


def test_create_portal_stores_target_and_extra_properties(conn):
    create_garden("other")
    other_conn = get_connection("other")
    target_id = create_mote(other_conn, {"name": "Target"})
    other_conn.close()

    portal_id = create_portal(conn, "other", target_id, {"name": "Target"})
    mote = get_mote(conn, portal_id)
    assert mote.properties[PORTAL_GARDEN_KEY] == "other"
    assert mote.properties[PORTAL_MOTE_KEY] == target_id
    assert mote.properties["name"] == "Target"


def test_create_portal_rejects_missing_garden(conn):
    with pytest.raises(LookupError):
        create_portal(conn, "nonexistent", "some-id")


def test_create_portal_rejects_missing_target_mote(conn):
    create_garden("other")
    with pytest.raises(LookupError):
        create_portal(conn, "other", "no-such-mote")


def test_resolve_portal_returns_live_target():
    create_garden("other")
    other_conn = get_connection("other")
    target_id = create_mote(other_conn, {"name": "Target"})
    other_conn.close()

    garden, mote = resolve_portal({PORTAL_GARDEN_KEY: "other", PORTAL_MOTE_KEY: target_id})
    assert garden == "other"
    assert mote.properties["name"] == "Target"


def test_resolve_portal_rejects_non_portal_properties():
    with pytest.raises(ValueError):
        resolve_portal({"name": "Plain mote"})


def test_resolve_portal_raises_when_target_garden_gone():
    with pytest.raises(PortalGardenMissing):
        resolve_portal({PORTAL_GARDEN_KEY: "gone", PORTAL_MOTE_KEY: "x"})


def test_resolve_portal_raises_when_target_mote_gone():
    create_garden("other")
    with pytest.raises(PortalMoteMissing):
        resolve_portal({PORTAL_GARDEN_KEY: "other", PORTAL_MOTE_KEY: "gone"})
