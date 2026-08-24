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


def test_event_payload_includes_timezone_for_timed_events_when_detectable(monkeypatch):
    # Google requires an explicit timeZone for recurring events specifically
    # (a bare UTC offset can't project future occurrences across DST) --
    # this is what made a recurring event fail on its first-ever export.
    monkeypatch.setattr(gcal, "_local_timezone_name", lambda: "America/Denver")
    mote = _mote({"date": "2026-08-15T09:00:00"})
    payload = event_payload(mote)

    assert payload["start"]["timeZone"] == "America/Denver"
    assert payload["end"]["timeZone"] == "America/Denver"


def test_event_payload_omits_timezone_when_undetectable(monkeypatch):
    monkeypatch.setattr(gcal, "_local_timezone_name", lambda: None)
    mote = _mote({"date": "2026-08-15T09:00:00"})
    payload = event_payload(mote)

    assert "timeZone" not in payload["start"]
    assert "timeZone" not in payload["end"]


def test_event_payload_all_day_events_have_no_timezone(monkeypatch):
    # date-only start/end has nothing for a timeZone to qualify.
    monkeypatch.setattr(gcal, "_local_timezone_name", lambda: "America/Denver")
    mote = _mote({"date": "2026-08-15"})
    payload = event_payload(mote)

    assert "timeZone" not in payload["start"]
    assert "timeZone" not in payload["end"]


def test_local_timezone_name_reads_the_etc_localtime_symlink(monkeypatch):
    monkeypatch.setattr(
        gcal.os.path, "realpath", lambda path: "/usr/share/zoneinfo/America/Denver"
    )
    assert gcal._local_timezone_name() == "America/Denver"


def test_local_timezone_name_returns_none_outside_zoneinfo(monkeypatch):
    monkeypatch.setattr(gcal.os.path, "realpath", lambda path: "/etc/localtime")
    assert gcal._local_timezone_name() is None


def test_local_timezone_name_returns_none_on_oserror(monkeypatch):
    def raise_oserror(path):
        raise OSError("no such file")

    monkeypatch.setattr(gcal.os.path, "realpath", raise_oserror)
    assert gcal._local_timezone_name() is None


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
        "recurrence": "FREQ=WEEKLY;BYDAY=TU,TH",
        "room": "MEGN 212",
    })
    payload = event_payload(mote)
    assert payload["description"] == "room: MEGN 212"


def test_event_payload_recurrence_is_explicit_null_when_absent():
    # Not just an absent key: push_event's PATCH only touches keys present
    # in the payload, so clearing a mote's recurrence property needs an
    # explicit null to actually clear it on an existing event.
    payload = event_payload(_mote({"date": "2026-08-15"}))
    assert payload["recurrence"] is None


def test_event_payload_passes_a_bare_rrule_through():
    mote = _mote({"date": "2026-08-15T09:00:00", "recurrence": "FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261215"})
    payload = event_payload(mote)
    assert payload["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261215"]


def test_event_payload_strips_a_redundant_rrule_prefix():
    # Some RRULE-builder tools hand you the full line, not just the rule.
    mote = _mote({"date": "2026-08-15T09:00:00", "recurrence": "RRULE:FREQ=DAILY;COUNT=5"})
    payload = event_payload(mote)
    assert payload["recurrence"] == ["RRULE:FREQ=DAILY;COUNT=5"]


def test_event_payload_recurrence_works_on_all_day_events_too():
    mote = _mote({"date": "2026-08-15", "recurrence": "FREQ=YEARLY"})
    payload = event_payload(mote)
    assert payload["recurrence"] == ["RRULE:FREQ=YEARLY"]


def test_event_payload_tags_the_event_with_the_mote_id():
    payload = event_payload(_mote({"date": "2026-08-15"}, id="mote-123"))
    assert payload["extendedProperties"] == {
        "private": {"monoss_mote_id": "mote-123", "managed_by": "monoss"}
    }


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


def _event(event_id, mote_id):
    return {"id": event_id, "extendedProperties": {"private": {"monoss_mote_id": mote_id, "managed_by": "monoss"}}}


def test_list_managed_events_returns_a_mote_id_to_event_id_map(monkeypatch):
    def fake_get(url, params, headers, timeout):
        assert params["privateExtendedProperty"] == "managed_by=monoss"
        return _FakeResponse(200, {"items": [_event("evt-1", "mote-a"), _event("evt-2", "mote-b")]})

    monkeypatch.setattr(gcal.requests, "get", fake_get)

    assert gcal.list_managed_events(_FakeCredentials()) == {"mote-a": "evt-1", "mote-b": "evt-2"}


def test_list_managed_events_follows_pagination(monkeypatch):
    pages = [
        {"items": [_event("evt-1", "mote-a")], "nextPageToken": "page2"},
        {"items": [_event("evt-2", "mote-b")]},
    ]
    seen_page_tokens = []

    def fake_get(url, params, headers, timeout):
        seen_page_tokens.append(params.get("pageToken"))
        return _FakeResponse(200, pages.pop(0))

    monkeypatch.setattr(gcal.requests, "get", fake_get)

    result = gcal.list_managed_events(_FakeCredentials())

    assert result == {"mote-a": "evt-1", "mote-b": "evt-2"}
    assert seen_page_tokens == [None, "page2"]


def test_delete_event_calls_the_right_url(monkeypatch):
    calls = []

    def fake_delete(url, headers, timeout):
        calls.append(url)
        return _FakeResponse(204, {})

    monkeypatch.setattr(gcal.requests, "delete", fake_delete)

    gcal.delete_event(_FakeCredentials(), "evt-1")

    assert calls == [f"{gcal.EVENTS_URL}/evt-1"]


def test_delete_event_tolerates_a_404(monkeypatch):
    monkeypatch.setattr(gcal.requests, "delete", lambda url, headers, timeout: _FakeResponse(404, {}))
    gcal.delete_event(_FakeCredentials(), "already-gone")  # must not raise


# ---- the export itself ----
#
# This used to live in ui/api.py, where it could only be tested through a
# GUI bridge; it is core's now, so these drive it directly.

class _FakeCredentials:
    token = "fake-token"


def _exporter(monkeypatch, managed=None, push=None):
    """Stubs the three network calls export_events() makes and records
    what it did. Returns (pushed, deleted) lists that fill in as it runs."""
    pushed, deleted = [], []
    monkeypatch.setattr(gcal, "list_managed_events", lambda credentials: dict(managed or {}))
    monkeypatch.setattr(
        gcal, "push_event",
        push or (lambda credentials, payload, existing: pushed.append((payload["summary"], existing))
                 or f"event-for-{payload['summary']}"),
    )
    monkeypatch.setattr(gcal, "delete_event", lambda credentials, event_id: deleted.append(event_id))
    return pushed, deleted


def test_exportable_motes_keeps_dated_motes_and_drops_templates():
    motes = [
        _mote({"name": "Exam", "date": "2026-08-15"}, id="exam"),
        _mote({"name": "No date"}, id="plain"),
        _mote({"name": "Template", "date": "2026-08-15"}, id="template"),
    ]
    assert [m["id"] for m in gcal.exportable_motes(motes, {"template"})] == ["exam"]


def test_exportable_motes_drops_every_id_in_the_excluded_set():
    # excluded_ids is a union the caller builds -- templates
    # (core/species.py::template_mote_ids) and scenario overrides
    # (core/scenarios.py::override_mote_ids) both belong in it, since
    # both hold replacement values rather than real data.
    motes = [
        _mote({"name": "Exam", "date": "2026-08-15"}, id="exam"),
        _mote({"name": "Template", "date": "2026-08-15"}, id="template"),
        _mote({"name": "Winter override", "date": "2026-12-01"}, id="override"),
    ]
    assert [m["id"] for m in gcal.exportable_motes(motes, {"template", "override"})] == ["exam"]


def test_export_events_creates_and_records_new_event_ids(monkeypatch):
    pushed, _ = _exporter(monkeypatch)
    recorded = []
    motes = [_mote({"name": "Exam", "date": "2026-08-15T09:00:00"}, id="exam")]

    summary = gcal.export_events(
        motes, _FakeCredentials(), record_event_id=lambda m, e: recorded.append((m, e))
    )

    assert summary == gcal.ExportSummary(created=1, updated=0, skipped=0, deleted=0, total=1)
    assert pushed == [("Exam", None)]
    assert recorded == [("exam", "event-for-Exam")]


def test_export_events_updates_a_mote_that_already_has_an_event_id(monkeypatch):
    pushed, _ = _exporter(
        monkeypatch, push=lambda credentials, payload, existing: pushed_id(pushed, existing)
    )
    recorded = []
    motes = [_mote({"name": "Exam", "date": "2026-08-15", "gcal_event_id": "evt-1"}, id="exam")]

    summary = gcal.export_events(
        motes, _FakeCredentials(), record_event_id=lambda m, e: recorded.append((m, e))
    )

    assert summary.updated == 1 and summary.created == 0
    # Unchanged id: nothing to write back, so the mote is left alone.
    assert recorded == []


def pushed_id(pushed, existing):
    pushed.append(existing)
    return existing


def test_export_events_skips_an_unparseable_date(monkeypatch):
    pushed, _ = _exporter(monkeypatch)
    motes = [_mote({"name": "Bad", "date": "not-a-date"}, id="bad")]

    summary = gcal.export_events(motes, _FakeCredentials(), record_event_id=lambda m, e: None)

    assert summary == gcal.ExportSummary(created=0, updated=0, skipped=1, deleted=0, total=1)
    assert pushed == []


def test_export_events_deletes_an_event_whose_mote_is_gone(monkeypatch):
    _, deleted = _exporter(monkeypatch, managed={"vanished": "evt-9", "exam": "evt-1"})
    motes = [_mote({"name": "Exam", "date": "2026-08-15"}, id="exam")]

    summary = gcal.export_events(motes, _FakeCredentials(), record_event_id=lambda m, e: None)

    assert deleted == ["evt-9"]
    assert summary.deleted == 1


def test_export_events_stops_writing_back_once_still_exporting_goes_false(monkeypatch):
    """The UI's mid-export garden switch, as core sees it: the push has
    already gone out, but nothing may be written into whatever garden is
    open now."""
    pushed, deleted = _exporter(monkeypatch, managed={"vanished": "evt-9"})
    recorded = []
    motes = [_mote({"name": "Exam", "date": "2026-08-15"}, id="exam")]

    summary = gcal.export_events(
        motes, _FakeCredentials(),
        record_event_id=lambda m, e: recorded.append((m, e)),
        still_exporting=lambda: False,
    )

    assert recorded == []
    assert deleted == [], "a stale event must not be deleted either once the world moved on"
    assert summary == gcal.ExportSummary(created=0, updated=0, skipped=0, deleted=0, total=1)
