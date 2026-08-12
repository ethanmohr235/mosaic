# Exporting to Google Calendar

Any mote with a `date` property can be pushed to Google Calendar via the
topbar's "Export to Calendar" button. This is one-way (MonOSS → Calendar)
and manual: clicking it walks every dated mote in the current garden and
creates or updates a matching event. Nothing runs in the background --
export only happens when you click the button.

Every export also deletes any event MonOSS previously created whose mote
is no longer dated -- either because the mote itself was deleted, or its
`date` property was removed. MonOSS finds these by asking Google Calendar
directly for everything it's tagged as managing (no local record of past
exports is kept), so this stays correct even across restarts, and even if
you edited or deleted an event by hand in Calendar in the meantime.

## Properties

- **`date`** (required to be exported) -- ISO 8601. A bare date
  (`2026-08-15`) makes an all-day event; a full datetime
  (`2026-08-15T14:00:00`) makes a timed one, interpreted in your system's
  local timezone.
- **`duration`** (optional) -- a plain number. Minutes for a timed event
  (default 60); days for an all-day event (default 1).
- **`recurrence`** (optional) -- a raw [RFC 5545 RRULE](https://icalendar.org/iCalendar-RFC-5545/3-8-5-3-recurrence-rule.html)
  string, e.g. `FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261215`. Pasted straight
  through to Google as-is, not parsed or validated by MonOSS -- an
  invalid rule surfaces as an error toast from Google's own response
  rather than failing locally. Free RRULE-builder tools exist online if
  you don't want to write one by hand; if what you paste already includes
  a leading `RRULE:`, that's stripped and re-added, so either form works.
  One Calendar event with a rule represents the *whole* series, so this
  is still just one mote → one event → one `gcal_event_id`, same as a
  non-recurring mote.
- **`gcal_event_id`** -- written back onto the mote automatically after
  its first export. Its presence is what makes a second export update the
  same event (or, for a recurring one, the whole series) instead of
  creating a duplicate. You can delete this property yourself if you ever
  want the next export to create a fresh event instead of updating the
  old one -- note that since the mote is still dated, its previous event
  won't be picked up by the delete-orphan pass either, so the old one is
  left behind rather than replaced; delete it by hand in Calendar if you
  don't want it hanging around.

## One-time setup (you have to do this part)

MonOSS needs its own OAuth credentials to talk to your Google account --
this is a few minutes of clicking through Google's own console, and only
you can do it (it needs your Google login):

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and
   create a project (or reuse an existing one).
2. Under **APIs & Services → Library**, enable the **Google Calendar API**.
3. Under **APIs & Services → OAuth consent screen**, configure it (choose
   "External," fill in the required fields, and add your own Google
   account as a test user -- "Testing" publishing status is fine for
   personal use, no Google review needed).
4. Under **APIs & Services → Credentials**, create an **OAuth client ID**
   with application type **Desktop app**.
5. Download the resulting JSON and save it as:
   ```
   <this repo>/.credentials/client_secret.json
   ```
   (`.credentials/` is gitignored -- this file and the token MonOSS later
   saves next to it never get committed.)

## First export

Click "Export to Calendar." Since no token exists yet, your browser opens
to Google's consent screen -- approve it, and MonOSS saves the resulting
token to `.credentials/token.json` for next time (refreshed automatically
as needed; you shouldn't need to sign in again after this). The button
stays showing "Exporting…" until this finishes, so a slow sign-in doesn't
look broken -- the rest of the app stays fully usable while it waits, since
this network/OAuth work never holds the same lock every other action needs.

Events are created on your **primary** calendar. The title comes from the
mote's `name` property (or, failing that, its first other property, or its
id); the description lists its remaining properties.
