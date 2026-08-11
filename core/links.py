# core/links.py
import re
from pathlib import Path

_URL_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
_WINDOWS_DRIVE = re.compile(r"^[a-zA-Z]:[\\/]")


def _looks_like_a_local_path(target: str) -> bool:
    return target.startswith(("/", "~", "./", "../")) or bool(_WINDOWS_DRIVE.match(target))


def classify_link(target: str) -> tuple[str, str]:
    """Classifies a url property's value as ("url", <url>) or
    ("file", <absolute path>) -- kept separate so a URL can be handed to
    a web browser while a local file is handed to the OS's file-type
    default instead (a .py file should open in whatever normally opens
    .py files, not as text in a browser tab).

    - Already has a URL scheme (http://, https://, ...) -> a URL.
    - Starts with /, ~, ./, ../, or a Windows drive letter -> an explicit
      local path; raises if it doesn't exist.
    - Anything else (e.g. a bare "youtube.com") -> assumed to be a web
      address missing its scheme, and given one. This means a relative
      path with no leading indicator (e.g. "notes/todo.txt") is treated
      as a URL, not a file -- local paths need to be absolute or start
      with ~/./.. to be recognized as such.
    """
    target = target.strip()
    if _URL_SCHEME.match(target):
        return ("url", target)
    if _looks_like_a_local_path(target):
        path = Path(target).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"No such file: {target!r}")
        return ("file", str(path.resolve()))
    return ("url", f"https://{target}")
