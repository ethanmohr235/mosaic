from datetime import datetime, timedelta

import pytest

import core.google_calendar as gcal
from core.google_calendar import dated_motes, event_payload


def _mote(properties, id="m1"):
    return {"id": id, "properties": properties}


def test_event_payload_timed_defaults_to_a_60_minute_duration():
    mote = _mote({"name": "Standup", "date": "2026-08-15T14:00:00"})
    payload = event_payload(mote)

    start = datetime.fromisoformat(payload["start"]["dateTime"])
    end = datetime.fromisoformat(payload["end"]["dateTime"])
    assert end - start == timedelta(minutes=60)
    assert start.utcoffset() is not None, "dateTime should carry an explicit UTC offset"
    assert payload["summary"] == "Standup"


def test_event_payload_timed_uses_duration_in_minutes():
    mote = _mote({"name": "Lecture", "date": "2026-08-15T09:00:00", "duration": 90})
    payload = event_payload(mote)

    start = datetime.fromisoformat(payload["start"]["dateTime"])
    end = datetime.fromisoformat(payload["end"]["dateTime"])
    assert end - start == timedelta(minutes=90)


def test_event_payload_all_day_defaults_to_a_1_day_span_with_exclusive_end():
    mote = _mote({"name": "Deadline", "date": "2026-08-15"})
    payload = event_payload(mote)

    assert payload["start"] == {"date": "2026-08-15"}
    # End is exclusive per the Calendar API: a single-day event on the
    # 15th has end.date == the 16th, not the 15th.
    assert payload["end"] == {"date": "2026-08-16"}


def test_event_payload_all_day_uses_duration_in_days():
    mote = _mote({"name": "Trip", "date": "2026-08-15", "duration": 3})
    payload = event_payload(mote)

    assert payload["start"] == {"date": "2026-08-15"}
    assert payload["end"] == {"date": "2026-08-18"}


def test_event_payload_missing_date_raises():
    with pytest.raises(ValueError):
        event_payload(_mote({"name": "No date"}))


def test_event_payload_unparseable_date_raises():
    with pytest.raises(ValueError):
        event_payload(_mote({"name": "Bad date", "date": "not-a-date"}))


def test_event_payload_summary_falls_back_to_first_property_then_id():
    payload = event_payload(_mote({"date": "2026-08-15", "age": 21}))
    assert payload["summary"] == "age: 21"

    payload = event_payload(_mote({"date": "2026-08-15"}, id="abcdefgh-1234"))
    assert payload["summary"] == "abcdefgh…"


def test_event_payload_description_excludes_reserved_keys():
    mote = _mote({
        "name": "Exam",
        "date": "2026-08-15T09:00:00",
        "duration": 120,
        "gcal_event_id": "already-exported",
        "url": "https://example.com/syllabus",
        "room": "MEGN 212",
    })
    payload = event_payload(mote)
    assert payload["description"] == "room: MEGN 212"


def test_dated_motes_filters_to_motes_with_a_date_property():
    motes = [
        _mote({"date": "2026-08-15"}, id="a"),
        _mote({"name": "no date"}, id="b"),
        _mote({"date": ""}, id="c"),
        _mote({"date": 12345}, id="d"),
    ]
    assert [m["id"] for m in dated_motes(motes)] == ["a"]


class _FakeCredentials:
    token = "fake-token"


class _FakeResponse:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise gcal.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._body


def test_push_event_creates_when_no_existing_id(monkeypatch):
    calls = []

    def fake_post(url, json, headers, timeout):
        calls.append(("POST", url))
        return _FakeResponse(200, {"id": "new-id"})

    monkeypatch.setattr(gcal.requests, "post", fake_post)

    event_id = gcal.push_event(_FakeCredentials(), {"summary": "x"}, None)

    assert event_id == "new-id"
    assert calls == [("POST", gcal.EVENTS_URL)]


def test_push_event_patches_when_existing_id_present(monkeypatch):
    calls = []

    def fake_patch(url, json, headers, timeout):
        calls.append(("PATCH", url))
        return _FakeResponse(200, {"id": "existing-id"})

    monkeypatch.setattr(gcal.requests, "patch", fake_patch)

    event_id = gcal.push_event(_FakeCredentials(), {"summary": "x"}, "existing-id")

    assert event_id == "existing-id"
    assert calls == [("PATCH", f"{gcal.EVENTS_URL}/existing-id")]


def test_push_event_falls_back_to_create_if_the_existing_event_was_deleted(monkeypatch):
    calls = []

    def fake_patch(url, json, headers, timeout):
        calls.append("PATCH")
        return _FakeResponse(404, {})

    def fake_post(url, json, headers, timeout):
        calls.append("POST")
        return _FakeResponse(200, {"id": "fresh-id"})

    monkeypatch.setattr(gcal.requests, "patch", fake_patch)
    monkeypatch.setattr(gcal.requests, "post", fake_post)

    event_id = gcal.push_event(_FakeCredentials(), {"summary": "x"}, "stale-id")

    assert event_id == "fresh-id"
    assert calls == ["PATCH", "POST"]
