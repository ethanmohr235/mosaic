import pytest

from core.links import classify_link


def test_classify_link_passes_urls_through_unchanged():
    assert classify_link("https://example.com/page") == ("url", "https://example.com/page")
    assert classify_link("http://example.com") == ("url", "http://example.com")


def test_classify_link_resolves_an_existing_file(tmp_path):
    f = tmp_path / "note.txt"
    f.write_text("hi")
    assert classify_link(str(f)) == ("file", str(f.resolve()))


def test_classify_link_expands_a_tilde_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "note.txt"
    f.write_text("hi")
    assert classify_link("~/note.txt") == ("file", str(f.resolve()))


def test_classify_link_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        classify_link("/no/such/file/anywhere")


def test_classify_link_treats_a_bare_domain_as_a_url():
    assert classify_link("youtube.com") == ("url", "https://youtube.com")


def test_classify_link_treats_a_bare_domain_with_a_path_as_a_url():
    assert classify_link("youtube.com/watch?v=xyz") == ("url", "https://youtube.com/watch?v=xyz")


def test_classify_link_a_relative_path_without_a_leading_indicator_is_treated_as_a_url():
    # Documented limitation: local paths need to be absolute or start
    # with ~/./.. to be recognized as local rather than a URL.
    assert classify_link("relative/notes.txt") == ("url", "https://relative/notes.txt")
