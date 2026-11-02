"""core/tectonic.py: finding, fetching and running tectonic. The download
is replaced by a fake archive; a real compile runs only if one is on PATH."""
import hashlib
import io
import shutil
import tarfile

import pytest

from core import tectonic


@pytest.fixture(autouse=True)
def cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/nonexistent")
    return tmp_path


def archive(payload=b"#!/bin/sh\necho hi\n", name="tectonic"):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buffer.getvalue()


def pin(monkeypatch, data):
    key = (tectonic.platform.system().lower(), tectonic.platform.machine().lower())
    monkeypatch.setitem(tectonic.ASSETS, key,
                        ("fake.tar.gz", hashlib.sha256(data).hexdigest()))


def test_nothing_found_means_nothing_downloaded():
    assert tectonic.find() is None


def test_ensure_downloads_verifies_and_caches(monkeypatch):
    data = archive()
    pin(monkeypatch, data)
    calls = []
    path = tectonic.ensure(fetch=lambda url: calls.append(url) or data)
    assert path.is_file() and path.stat().st_mode & 0o100
    assert calls[0].endswith("/fake.tar.gz")
    assert tectonic.ensure(fetch=lambda url: pytest.fail("fetched twice")) == path


def test_wrong_checksum_is_refused(monkeypatch):
    pin(monkeypatch, archive())
    with pytest.raises(tectonic.TectonicError, match="checksum"):
        tectonic.ensure(fetch=lambda url: archive(b"tampered"))
    assert tectonic.find() is None


def test_download_failure_says_how_to_recover(monkeypatch):
    pin(monkeypatch, archive())
    def boom(url):
        raise OSError("offline")
    with pytest.raises(tectonic.TectonicError, match="PATH"):
        tectonic.ensure(fetch=boom)


def test_unpinned_platform(monkeypatch):
    monkeypatch.setattr(tectonic, "ASSETS", {})
    with pytest.raises(tectonic.TectonicError, match="no tectonic build"):
        tectonic.ensure()


@pytest.mark.skipif(shutil.which("tectonic") is None and not __import__("os").environ.get("MONOSS_TECTONIC"),
                    reason="needs tectonic and network")
def test_real_compile(monkeypatch):
    monkeypatch.setenv("PATH", __import__("os").environ["PATH"])
    pdf = tectonic.compile_pdf("\\documentclass{article}\\begin{document}Hi\\end{document}")
    assert pdf.startswith(b"%PDF")


UNDEFINED_LOG = r"""[1] [2]
! Undefined control sequence.
<argument> k_\infin
                    -1
l.107 ... \frac{\pi^2}{R^2}=\frac{k_\infin-1}{L^2}

Output written on main.xdv (2 pages, 23440 bytes).
"""


def test_failure_reads_the_first_error_and_its_line():
    found = tectonic.failure(UNDEFINED_LOG, "")
    assert found.reason == r"Undefined control sequence \infin"
    assert found.tex_line == 107 and found.excerpt.endswith(r"{L^2}")


def test_a_missing_glyph_is_a_failure_at_the_line_that_has_it():
    log = 'Missing character: There is no \u2603 (U+2603) in font lmroman10-regular!\n'
    found = tectonic.failure(log, "a\nb \u2603\nc")
    assert found == tectonic.Failure("the PDF font has no '\u2603' (U+2603)", 2)


def test_a_clean_log_is_no_failure():
    assert tectonic.failure("Output written on main.xdv (1 page).", "") is None
