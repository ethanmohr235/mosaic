# api_common.py
"""Shared pieces of the pywebview Api bridge every desktop app (monarch,
mora, monastery, momentum) exposes as its js_api. Before this, each
app's api.py carried its own byte-for-byte copy of `synchronized` and,
where properties are edited as text, `coerce`/`coerce_properties` --
see window_launcher.py for the other half of the boilerplate that used
to get re-copied per app.
"""
import functools
import json


def synchronized(method):
    """pywebview invokes exposed methods from a worker thread, and not
    the same one each time. Each Api holds one long-lived sqlite3
    connection (opened with check_same_thread=False), so calls onto it
    must be serialized here rather than left to sqlite3 to police.
    Assumes `self._lock`, a threading.Lock every Api.__init__ sets up."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def coerce(raw):
    """A property value as the garden should store it: text-field input
    interpreted as JSON where possible, so a duration typed as "90" is
    stored as a number rather than a string core/calendar.py would
    ignore. Only strings need coercing -- copy/paste sends a mote's
    already-typed properties straight back through create_mote, already
    the right type, and json.loads() raises TypeError on anything that
    isn't str/bytes/bytearray."""
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def coerce_properties(raw_properties: dict) -> dict:
    return {k: coerce(v) for k, v in raw_properties.items()}
