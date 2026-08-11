# core/links.py
import re
from pathlib import Path

_URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def resolve_link(target: str) -> str:
    """Turns a url property's value into something webbrowser.open() can
    launch. Passed through unchanged if it already has a URL scheme
    (http://, https://, ...); otherwise treated as a local path and
    turned into a file:// URI."""
    if _URL_SCHEME.match(target):
        return target
    path = Path(target).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"No such file: {target!r}")
    return path.resolve().as_uri()
