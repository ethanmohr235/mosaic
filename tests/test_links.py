import pytest

from core.links import resolve_link


def test_resolve_link_passes_urls_through_unchanged():
    assert resolve_link("https://example.com/page") == "https://example.com/page"
    assert resolve_link("http://example.com") == "http://example.com"


def test_resolve_link_resolves_an_existing_file_to_a_file_uri(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hi")
    assert resolve_link(str(f)) == f.resolve().as_uri()


def test_resolve_link_expands_a_tilde_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "note.txt"
    f.write_text("hi")
    assert resolve_link("~/note.txt") == f.resolve().as_uri()


def test_resolve_link_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        resolve_link("/no/such/file/anywhere")
