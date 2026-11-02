# core/tectonic.py
"""Compiling a `.tex` bundle to a PDF with tectonic.

Tectonic is one self-contained binary, which is why it is the engine: no
TeX Live install. It is found, in order, on PATH, in a per-user cache, or
downloaded once -- a pinned release, checked against a SHA-256 recorded
here (`roadmap.md` -> *Output*, decided 2026-10-05). Nothing about the
download happens until someone asks for a PDF; `.tex` export never needs
it.

Tectonic itself fetches TeX packages (pgfplots, amsmath, ...) from its
bundle the first time they are used, so the first compile needs network
too and later ones are served from its own cache.
"""
import hashlib
import io
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

VERSION = "0.17.0"
_BASE = "https://github.com/tectonic-typesetting/tectonic/releases/download/tectonic%40" + VERSION
# (system, machine) -> (asset name, sha256). Static musl builds on Linux,
# so one binary runs on any distribution.
ASSETS = {
    ("linux", "x86_64"): (
        f"tectonic-{VERSION}-x86_64-unknown-linux-musl.tar.gz",
        "8533d07f9ccbd7a65824b9e0459041bca34af1eb33daba48f59215593753a3b7"),
    ("linux", "aarch64"): (
        f"tectonic-{VERSION}-aarch64-unknown-linux-musl.tar.gz",
        "b10954a95404f3ab2328d2fa59a5ebab8e657f893fab096f98be8db7c0c979b8"),
    ("darwin", "arm64"): (
        f"tectonic-{VERSION}-aarch64-apple-darwin.tar.gz",
        "a3f1cac7c5678f01661a92212f58480ae3b0634115d880dbc59e2953ded45667"),
    ("darwin", "x86_64"): (
        f"tectonic-{VERSION}-x86_64-apple-darwin.tar.gz",
        "7c90ef5b6ddb1eb1937e4337add5237b79338e4b9676459fa91187d24d6cdf80"),
    ("windows", "amd64"): (
        f"tectonic-{VERSION}-x86_64-pc-windows-msvc.zip",
        "f61ce51f0b0ade1015b7de7ef368541c5424e9756ecbd0d7af97d6d48030845f"),
}
COMPILE_TIMEOUT = 600  # the first run downloads packages


class TectonicError(RuntimeError):
    """Tectonic is missing, could not be fetched, or failed to compile.
    The message is the sentence a surface shows as-is. A compile failure
    read from the log also carries its `reason` alone and the `tex_line`
    of main.tex it points at, so core/submit.py can name the note line."""

    def __init__(self, message: str, tex_line: int | None = None, reason: str = "") -> None:
        super().__init__(message)
        self.tex_line = tex_line
        self.reason = reason


@dataclass(frozen=True)
class Failure:
    """What a compile log says went wrong: the first error, or a glyph
    the font did not have (which XeTeX only warns about, leaving a gap in
    the PDF)."""
    reason: str
    tex_line: int | None
    excerpt: str = ""


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "monoss" / "tectonic" / VERSION


def _binary_name() -> str:
    return "tectonic.exe" if platform.system().lower() == "windows" else "tectonic"


def find() -> Path | None:
    """A usable tectonic without downloading: PATH first (a user's own
    install wins), then the cache."""
    on_path = shutil.which("tectonic")
    if on_path:
        return Path(on_path)
    cached = cache_dir() / _binary_name()
    return cached if cached.is_file() else None


def ensure(fetch=None) -> Path:
    """`find()`, downloading the pinned release into the cache if there is
    none. `fetch(url) -> bytes` is the seam tests replace."""
    found = find()
    if found:
        return found
    key = (platform.system().lower(), platform.machine().lower())
    if key not in ASSETS:
        raise TectonicError(
            f"no tectonic build is pinned for {key[0]}/{key[1]}; install tectonic and put it on PATH")
    name, digest = ASSETS[key]
    try:
        data = (fetch or _fetch)(f"{_BASE}/{name}")
    except Exception as exc:  # network, DNS, HTTP status
        raise TectonicError(
            f"could not download tectonic {VERSION} ({exc}); install it and put it on PATH") from exc
    if hashlib.sha256(data).hexdigest() != digest:
        raise TectonicError(f"downloaded tectonic did not match its pinned checksum; refusing to run it")
    return _install(data, name)


def _fetch(url: str) -> bytes:
    import httpx
    response = httpx.get(url, follow_redirects=True, timeout=120)
    response.raise_for_status()
    return response.content


def _install(data: bytes, name: str) -> Path:
    target = cache_dir()
    target.mkdir(parents=True, exist_ok=True)
    wanted = _binary_name()
    if name.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            member = next((n for n in archive.namelist() if Path(n).name == wanted), None)
            payload = archive.read(member) if member else None
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            member = next((m for m in archive.getmembers() if m.isfile() and Path(m.name).name == wanted), None)
            payload = archive.extractfile(member).read() if member else None
    if payload is None:
        raise TectonicError(f"{name} did not contain a {wanted} binary")
    path = target / wanted
    path.write_bytes(payload)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path


def compile_pdf(tex: str, files: dict[str, bytes] | None = None, fetch=None) -> bytes:
    """The PDF for `tex`, with `files` (path -> bytes, relative to the
    document) alongside it. Raises TectonicError when the document does
    not compile, or compiles with a character its font lacks: the log's
    reason and line where it gives one (`failure`), else the tail of
    tectonic's output."""
    binary = ensure(fetch)
    with tempfile.TemporaryDirectory(prefix="monoss-tex-") as work:
        root = Path(work)
        (root / "main.tex").write_text(tex, encoding="utf-8")
        for relative, data in (files or {}).items():
            path = root / relative
            if root.resolve() not in path.resolve().parents:
                raise TectonicError(f"refusing to write {relative} outside the build folder")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        try:
            run = subprocess.run(
                [str(binary), "-X", "compile", "main.tex", "--untrusted", "--keep-logs"],
                cwd=root, capture_output=True, text=True, timeout=COMPILE_TIMEOUT)
        except subprocess.TimeoutExpired as exc:
            raise TectonicError("tectonic timed out") from exc
        pdf, log = root / "main.pdf", root / "main.log"
        found = failure(log.read_text(errors="replace"), tex) if log.is_file() else None
        if found is not None:
            raise _failed(found)
        if run.returncode != 0 or not pdf.is_file():
            tail = "\n".join((run.stderr or run.stdout or "").strip().splitlines()[-15:])
            raise TectonicError(f"tectonic failed:\n{tail}")
        return pdf.read_bytes()


_ERROR_RE = re.compile(r"^! (?P<reason>.*)$", re.M)
_LINE_RE = re.compile(r"^l\.(?P<line>\d+) ?(?P<excerpt>.*)$", re.M)
_COMMAND_RE = re.compile(r"\\[A-Za-z@]+")
_MISSING_RE = re.compile(r"Missing character: There is no (?P<char>.) \((?P<code>U\+[0-9A-F]+)\)")


def failure(log: str, tex: str) -> Failure | None:
    """The first thing wrong in a compile log, or None. Pure, so a test can
    hand it a log without running tectonic."""
    error = _ERROR_RE.search(log)
    if error:
        reason = error.group("reason").strip().rstrip(".")
        place = _LINE_RE.search(log, error.end())
        context = log[error.end():place.start() if place else None]
        if reason == "Undefined control sequence" and _COMMAND_RE.findall(context):
            reason += " " + _COMMAND_RE.findall(context)[-1]
        if place is None:
            return Failure(reason, None)
        return Failure(reason, int(place.group("line")), place.group("excerpt").strip())
    missing = _MISSING_RE.search(log)
    if missing:
        char = missing.group("char")
        line = next((n for n, text in enumerate(tex.splitlines(), 1) if char in text), None)
        return Failure(f"the PDF font has no '{char}' ({missing.group('code')})", line)
    return None


def _failed(found: Failure) -> TectonicError:
    where = f" at line {found.tex_line} of the .tex" if found.tex_line else ""
    excerpt = f"\n  {found.excerpt}" if found.excerpt else ""
    return TectonicError(f"tectonic failed{where}: {found.reason}{excerpt}",
                         found.tex_line, found.reason)
