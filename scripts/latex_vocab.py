#!/usr/bin/env python3
"""Regenerate core/data/latex_math.json: the maths a note may use
(core/latex_vocab.py, docs/modoc.md -> Maths).

Reads every command name and every maths-mode Unicode symbol out of the
vendored KaTeX, asks tectonic which names are defined under the export
preamble, and writes what both sides share. Run it after upgrading
KaTeX or changing `latex_vocab.PACKAGES` / `SHIMS`:

    .venv/bin/python scripts/latex_vocab.py

Needs tectonic (on PATH or in the cache; see core/tectonic.py).
tests/test_latex_vocab.py checks the file is current when it can run
tectonic too.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core import latex_vocab, tectonic  # noqa: E402

KATEX_JS = PROJECT_ROOT / "console" / "frontend" / "vendor" / "katex" / "katex.min.js"

_VERSION_RE = re.compile(r'version:"(?P<version>[0-9.]+)"')
_NAME_RE = re.compile(r'"\\\\(?P<name>[a-zA-Z]+)"')
# defineSymbol(mode, font, group, replace, name, acceptUnicodeChar), minified.
_SYMBOL_RE = re.compile(
    r'\((?P<mode>\w{1,3}),\w{1,3},\w{1,3},"(?P<char>(?:\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2})+|[^"\\]{1,2})",'
    r'"(?P<name>\\\\[a-zA-Z]+|[!-~])",!0\)')
# defineMacro("\u2260", "\\neq"): a symbol KaTeX reads as a plain command.
_MACRO_RE = re.compile(
    r'\w{1,3}\("(?P<char>\\u[0-9a-fA-F]{4}|\\x[0-9a-fA-F]{2})",'
    r'"(?P<name>\\\\[a-zA-Z]+(?:\{[A-Za-z]\})?)"\)')


def katex_names(js: str) -> set[str]:
    return set(_NAME_RE.findall(js))


def katex_symbols(js: str) -> dict[str, str]:
    """Maths-mode Unicode symbols KaTeX reads, to the command (or ASCII)
    it reads each as. The maths mode's minified name is whatever
    `\\equiv`'s entry uses."""
    found = [m for m in _SYMBOL_RE.finditer(js)]
    math = next(m.group("mode") for m in found if m.group("name") == "\\\\equiv")
    out: dict[str, str] = {}
    for m in found:
        char = _decode(m.group("char")) if m.group("char").startswith(("\\u", "\\x")) \
            else m.group("char")
        if m.group("mode") != math or len(char) != 1 or char.isascii():
            continue
        out.setdefault(char, m.group("name").replace("\\\\", "\\"))
    for m in _MACRO_RE.finditer(js):
        out.setdefault(_decode(m.group("char")), m.group("name").replace("\\\\", "\\"))
    return out


def _decode(escaped: str) -> str:
    return escaped.encode("ascii").decode("unicode_escape")


def undefined(names: set[str]) -> set[str]:
    """The names LaTeX does not define under the export preamble: one
    compile, one `\\ifcsname` probe per name."""
    probes = "\n".join(rf"\ifcsname {n}\endcsname\else\typeout{{UNDEF:{n}}}\fi" for n in sorted(names))
    log = _compile(probes)
    if log is None:
        raise SystemExit("tectonic could not compile the definedness probe")
    return set(re.findall(r"UNDEF:([A-Za-z]+)", log))


def unusable(symbols: dict[str, str]) -> set[str]:
    """The symbols whose command does not compile in maths (a text-only
    command such as `\\copyright`), found by halving: one compile when
    all is well."""
    if not symbols or _compile("$" + " ".join(f"{{{t}}}" for t in symbols.values()) + "$"):
        return set()
    if len(symbols) == 1:
        return set(symbols)
    items = list(symbols.items())
    half = len(items) // 2
    return unusable(dict(items[:half])) | unusable(dict(items[half:]))


def _compile(body: str) -> str | None:
    """The log of `body` compiled under the export preamble, or None if it
    did not compile cleanly."""
    tex = ("\\documentclass{article}\n" + latex_vocab.preamble()
           + "\\providecommand{\\htmlClass}[2]{#2}\n\\begin{document}\nx\n"
           + body + "\n\\end{document}\n")
    with tempfile.TemporaryDirectory(prefix="monoss-vocab-") as work:
        (Path(work) / "main.tex").write_text(tex, encoding="utf-8")
        run = subprocess.run([str(tectonic.ensure()), "-X", "compile", "main.tex", "--keep-logs"],
                             cwd=work, capture_output=True, text=True)
        log = Path(work) / "main.log"
        if run.returncode != 0 or not log.is_file():
            return None
        text = log.read_text(errors="replace")
        return None if "Missing character" in text else text


def generate() -> dict:
    js = KATEX_JS.read_text(encoding="utf-8")
    names = katex_names(js) | set(latex_vocab.SHIMS)
    commands = names - undefined(names) - latex_vocab.DENIED
    unicode = {**katex_symbols(js), **latex_vocab.UNICODE_EXTRAS}
    allowed = {ch: cmd for ch, cmd in unicode.items()
               if not cmd.startswith("\\") or _command_of(cmd) in commands}
    for ch in unusable(allowed):
        del allowed[ch]
    return {
        "katex": _VERSION_RE.search(js).group("version"),
        "commands": sorted(commands),
        "katex_only": sorted(names - commands),
        "unicode": dict(sorted(allowed.items())),
    }


def _command_of(tex: str) -> str:
    return re.match(r"\\([a-zA-Z]+)", tex).group(1)


def main() -> None:
    data = generate()
    latex_vocab.DATA_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n",
                                     encoding="utf-8")
    print(f"{latex_vocab.DATA_PATH}: KaTeX {data['katex']}, {len(data['commands'])} commands, "
          f"{len(data['katex_only'])} KaTeX-only, {len(data['unicode'])} symbols")


if __name__ == "__main__":
    main()
