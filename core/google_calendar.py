# core/google_calendar.py
import json
from datetime import date, datetime, timedelta

import requests
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from core.db import PROJECT_ROOT

RESERVED_KEYS = {"date", "duration", "gcal_event_id", "url"}

CREDENTIALS_DIR = PROJECT_ROOT / ".credentials"
CLIENT_SECRET_PATH = CREDENTIALS_DIR / "client_secret.json"
TOKEN_PATH = CREDENTIALS_DIR / "token.json"
SCOPES = ["https://www.googleapis.com/auth/calendar.events"]

EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


def _label_for(mote: dict) -> str:
    """Similar fallback order to ui/frontend/app.js::nodeLabel (name,
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


def event_payload(mote: dict) -> dict:
    """Builds a Calendar v3 event body from a mote's properties. Raises
    ValueError if `date` is missing or unparseable."""
    props = mote.get("properties", {})
    raw_date = props.get("date")
    if not isinstance(raw_date, str) or not raw_date.strip():
        raise ValueError(f"Mote {mote.get('id')!r} has no usable date property")

    payload = {"summary": _label_for(mote), "description": _description_for(mote)}
    duration = props.get("duration")

    if "T" in raw_date:
        try:
            start_dt = datetime.fromisoformat(raw_date)
        except ValueError as e:
            raise ValueError(f"Mote {mote.get('id')!r} has an unparseable date: {raw_date!r}") from e
        # Attaches the system's local UTC offset (DST-correct for that
        # instant) so Google can interpret it unambiguously without also
        # needing an IANA timeZone name, which there's no portable
        # stdlib way to detect.
        start_dt = start_dt.astimezone()
        minutes = duration if isinstance(duration, (int, float)) else 60
        end_dt = start_dt + timedelta(minutes=minutes)
        payload["start"] = {"dateTime": start_dt.isoformat()}
        payload["end"] = {"dateTime": end_dt.isoformat()}
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

    return payload


def dated_motes(motes: list[dict]) -> list[dict]:
    """Every mote with a non-empty `date` property."""
    return [
        m for m in motes
        if isinstance(m.get("properties", {}).get("date"), str) and m["properties"]["date"].strip()
    ]


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
