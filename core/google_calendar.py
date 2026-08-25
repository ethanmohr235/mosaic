# core/google_calendar.py
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from core.db import PROJECT_ROOT
from core.links import URL_PROPERTY

RESERVED_KEYS = {"date", "duration", "gcal_event_id", "recurrence", URL_PROPERTY}

CREDENTIALS_DIR = PROJECT_ROOT / ".credentials"
CLIENT_SECRET_PATH = CREDENTIALS_DIR / "client_secret.json"
TOKEN_PATH = CREDENTIALS_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def _label_for(mote: dict) -> str:
    """Similar fallback order to monarch/frontend/app.js::nodeLabel (name,
    else first property, else the mote id), but skips RESERVED_KEYS when
    picking a fallback property -- unlike the graph label, a calendar
    event's title shouldn't just repeat the date/duration that's already
    on the event natively."""
    props = mote.get("properties", {})
    name = props.get("name")
    if isinstance(name, str) and name.strip():
        return name
    keys = sorted(k for k in props if k not in RESERVED_KEYS)
    if keys:
        key = keys[0]
        value = props[key]
        value_str = value if isinstance(value, str) else json.dumps(value)
        return f"{key}: {value_str}"
    return mote.get("id", "")[:8] + "…"


def _description_for(mote: dict) -> str:
    # "name" is excluded too, not just RESERVED_KEYS -- it's already
    # shown as the event's title whenever present, so repeating it here
    # would be redundant.
    props = mote.get("properties", {})
    lines = [f"{k}: {v}" for k, v in sorted(props.items()) if k not in RESERVED_KEYS and k != "name"]
    return "\n".join(lines)


def _local_timezone_name() -> str | None:
    """Best-effort IANA zone name (e.g. "America/Denver"). Google requires
    an explicit timeZone -- not just a UTC offset -- for a *recurring*
    event, since a fixed offset can't project future occurrences across
    DST; this is why creating a recurring event used to fail on its very
    first export (no existing event to inherit a timeZone from) while a
    later update of that same event, once it already had one, succeeded.
    Reads the /etc/localtime symlink standard on Linux/macOS rather than
    adding a dependency (docs/rules.md) just for this; returns None
    (falling back to the offset-only payload below) wherever that
    doesn't resolve to one."""
    try:
        target = os.path.realpath("/etc/localtime")
    except OSError:
        return None
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else None


def _recurrence_lines(recurrence: str, timezone_name: str | None) -> list[str]:
    """A mote's `recurrence` property as the Calendar API's list of RFC
    5545 lines.

    A list rather than one rule, because the property can carry
    exceptions now: deleting a single occurrence in Monastery adds an
    `EXDATE` line (core/calendar.py::with_exception), and forwarding only
    the RRULE would resurrect that occurrence on the next export. A
    leading `RRULE:` is stripped and re-added on the rule line, since
    some RRULE-builder tools hand you the full line rather than the rule.

    A timed `EXDATE` is tagged with the same `TZID` the start and end
    carry. MonOSS stores naive local times and Google reads an untagged
    EXDATE as UTC -- which would except an occurrence that was never
    there and leave the one you deleted standing.
    """
    lines = []
    for raw_line in recurrence.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        upper = line.upper()
        if upper.startswith(("EXDATE", "RDATE", "EXRULE")):
            if timezone_name and "VALUE=DATE" not in upper and "TZID=" not in upper:
                keyword, _, value = line.partition(":")
                line = f"{keyword};TZID={timezone_name}:{value}"
            lines.append(line)
            continue
        rule = line[len("RRULE:"):] if upper.startswith("RRULE:") else line
        lines.append(f"RRULE:{rule}")
    return lines


def event_payload(mote: dict) -> dict:
    """Builds a Calendar v3 event body from a mote's properties. Raises
    ValueError if `date` is missing or unparseable."""
    props = mote.get("properties", {})
    raw_date = props.get("date")
    if not isinstance(raw_date, str) or not raw_date.strip():
        raise ValueError(f"Mote {mote.get('id')!r} has no usable date property")

    payload = {"summary": _label_for(mote), "description": _description_for(mote)}
    duration = props.get("duration")
    # Held past the timed branch below so an EXDATE can be tagged with the
    # same zone the start and end are; an all-day event has none, and its
    # exceptions are `VALUE=DATE` anyway.
    timezone_name = None

    if "T" in raw_date:
        try:
            start_dt = datetime.fromisoformat(raw_date)
        except ValueError as e:
            raise ValueError(f"Mote {mote.get('id')!r} has an unparseable date: {raw_date!r}") from e
        # Attaches the system's local UTC offset (DST-correct for that
        # instant) so Google can interpret it unambiguously even without
        # an IANA timeZone name below (_local_timezone_name() is only
        # best-effort).
        start_dt = start_dt.astimezone()
        minutes = duration if isinstance(duration, (int, float)) else 60
        end_dt = start_dt + timedelta(minutes=minutes)
        payload["start"] = {"dateTime": start_dt.isoformat()}
        payload["end"] = {"dateTime": end_dt.isoformat()}
        timezone_name = _local_timezone_name()
        if timezone_name:
            payload["start"]["timeZone"] = timezone_name
            payload["end"]["timeZone"] = timezone_name
    else:
        try:
            start_d = date.fromisoformat(raw_date)
        except ValueError as e:
            raise ValueError(f"Mote {mote.get('id')!r} has an unparseable date: {raw_date!r}") from e
        days = duration if isinstance(duration, (int, float)) else 1
        # All-day events' end date is exclusive per the Calendar API --
        # a single-day event on the 15th has end.date == the 16th.
        end_d = start_d + timedelta(days=days)
        payload["start"] = {"date": start_d.isoformat()}
        payload["end"] = {"date": end_d.isoformat()}

    recurrence = props.get("recurrence")
    if isinstance(recurrence, str) and recurrence.strip():
        # Pasted in as-is (RFC 5545 syntax) -- not parsed or validated
        # here, just forwarded; Google's own 400 response for a malformed
        # rule already surfaces as a toast via the existing error path.
        payload["recurrence"] = _recurrence_lines(recurrence, timezone_name)
    else:
        # Explicit null, not an absent key: push_event's PATCH only touches
        # keys present in the payload, so a mote that lost its recurrence
        # property (or never had one) needs this to actually clear
        # recurrence on an existing event rather than leaving it in place.
        payload["recurrence"] = None

    # managed_by is a constant sentinel (same value on every MonOSS-
    # created event) so list_managed_events() can find all of them with
    # one exact-match query; monoss_mote_id records which mote each one
    # belongs to, read back out of that query's results. This is the
    # registry that lets a next export detect and delete events whose
    # mote is gone or no longer dated -- no local tracking file needed.
    payload["extendedProperties"] = {"private": {"monoss_mote_id": mote["id"], "managed_by": "monoss"}}

    return payload


def dated_motes(motes: list[dict]) -> list[dict]:
    """Every mote with a non-empty `date` property."""
    return [
        m for m in motes
        if isinstance(m.get("properties", {}).get("date"), str) and m["properties"]["date"].strip()
    ]


def exportable_motes(motes: list[dict], excluded_ids: set[str]) -> list[dict]:
    """The dated motes an export should actually push. `excluded_ids` is
    every mote that holds replacement or starting values rather than real
    data -- a species' template (core/species.py::template_mote_ids) and a
    scenario's override (core/scenarios.py::override_mote_ids) both
    qualify, and the caller is expected to pass their union: a dated
    template or a dated override would otherwise export as a nonsense
    event for something that never actually happened."""
    return [m for m in dated_motes(motes) if m["id"] not in excluded_ids]


@dataclass(frozen=True)
class ExportSummary:
    created: int = 0
    updated: int = 0
    skipped: int = 0
    deleted: int = 0
    total: int = 0


def get_credentials() -> Credentials:
    """Loads a saved token, refreshing it if expired; otherwise runs the
    installed-app OAuth flow (opens a browser, blocks until the user
    finishes it there), then saves the resulting token."""
    credentials = None
    if TOKEN_PATH.exists():
        credentials = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(GoogleAuthRequest())

    if not credentials or not credentials.valid:
        if not CLIENT_SECRET_PATH.exists():
            raise FileNotFoundError(
                f"No Google Calendar credentials found at {CLIENT_SECRET_PATH} -- "
                "see docs/google-calendar.md for one-time setup."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
        credentials = flow.run_local_server(port=0)
        CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(credentials.to_json())

    return credentials


def push_event(credentials: Credentials, payload: dict, existing_event_id: str | None) -> str:
    """POSTs a new event, or PATCHes existing_event_id if given. Falls
    back to creating a fresh event if existing_event_id no longer exists
    on the Calendar side (e.g. the user deleted it by hand) rather than
    failing the whole export over one stale id."""
    headers = {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
    }
    response = None
    if existing_event_id:
        response = requests.patch(f"{EVENTS_URL}/{existing_event_id}", json=payload, headers=headers, timeout=15)
        if response.status_code == 404:
            response = None
    if response is None:
        response = requests.post(EVENTS_URL, json=payload, headers=headers, timeout=15)
    response.raise_for_status()
    return response.json()["id"]


def list_managed_events(credentials: Credentials) -> dict[str, str]:
    """mote_id -> event_id for every event MonOSS currently manages
    (tagged with managed_by=monoss at creation time), across all pages.
    This is queried fresh on every export rather than trusted from local
    state, so it stays correct even if events were edited or deleted by
    hand in Calendar."""
    headers = {"Authorization": f"Bearer {credentials.token}"}
    params = {"privateExtendedProperty": "managed_by=monoss", "maxResults": 2500}
    result = {}
    while True:
        response = requests.get(EVENTS_URL, params=params, headers=headers, timeout=15)
        response.raise_for_status()
        data = response.json()
        for item in data.get("items", []):
            mote_id = item.get("extendedProperties", {}).get("private", {}).get("monoss_mote_id")
            if mote_id:
                result[mote_id] = item["id"]
        next_page = data.get("nextPageToken")
        if not next_page:
            return result
        params["pageToken"] = next_page


def delete_event(credentials: Credentials, event_id: str) -> None:
    headers = {"Authorization": f"Bearer {credentials.token}"}
    response = requests.delete(f"{EVENTS_URL}/{event_id}", headers=headers, timeout=15)
    if response.status_code not in (200, 204, 404):
        response.raise_for_status()  # 404 = already gone, treated as success


def _delete_orphaned_events(
    credentials: Credentials, keep_ids: set[str], still_exporting: Callable[[], bool]
) -> int:
    """Deletes every event MonOSS previously created whose mote is now
    gone, or no longer has a `date`. Calendar itself is the registry --
    events are found by the extendedProperties tag event_payload() sets,
    not a local record -- so this stays correct even if events were
    edited or deleted by hand."""
    deleted = 0
    for mote_id, event_id in list_managed_events(credentials).items():
        if not still_exporting():
            break
        if mote_id not in keep_ids:
            delete_event(credentials, event_id)
            deleted += 1
    return deleted


def export_events(
    motes: list[dict],
    credentials: Credentials,
    *,
    record_event_id: Callable[[str, str], None],
    still_exporting: Callable[[], bool] = lambda: True,
) -> ExportSummary:
    """Push `motes` (already filtered by exportable_motes) to Calendar and
    clean up anything stale, returning what happened.

    This is the export, and it lives here rather than in monarch/api.py --
    where it used to -- so it is testable without a GUI and reachable
    from any adapter. What the caller still owns is the two things only
    it can know: `record_event_id`, how to persist a new event id back
    onto a mote (the UI does that through its own connection lock), and
    `still_exporting`, whether the world this export started in is still
    the current one (the UI's user can switch gardens while a network
    call is in flight, and an id written into the wrong garden would be
    a real corruption)."""
    keep_ids = {m["id"] for m in motes}
    deleted = _delete_orphaned_events(credentials, keep_ids, still_exporting)

    created = updated = skipped = 0
    for mote in motes:
        try:
            payload = event_payload(mote)
        except ValueError:
            skipped += 1
            continue

        existing_id = mote["properties"].get("gcal_event_id")
        event_id = push_event(credentials, payload, existing_id)

        if not still_exporting():
            continue
        if existing_id:
            updated += 1
        else:
            created += 1
        if event_id != existing_id:
            record_event_id(mote["id"], event_id)

    return ExportSummary(created, updated, skipped, deleted, len(motes))
