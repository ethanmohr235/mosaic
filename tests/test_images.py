"""Images in notes: core/images.py (storage, sniffing, SVG cleaning,
rename), the `\\image` macro (core/modoc.py), its rendering
(core/document.py), and the CLI/MCP surfaces."""
import base64
import struct
import zlib

import pytest

from core import document, images, modoc
from core.motes import create_mote, get_mote, list_motes
from core.images import ImageError


def png(width=2, height=3, pad=0):
    """A real, minimal PNG."""
    def chunk(kind, body):
        crc = zlib.crc32(kind + body) & 0xFFFFFFFF
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(raw + b"\x00" * pad)) + chunk(b"IEND", b""))


SVG = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"><rect width="5" height="5"/></svg>'


def note(conn, source, name="Report"):
    return create_mote(conn, {"name": name, modoc.MODOC_KEY: source})


# ---- storage ----

def test_png_is_stored_with_its_size_and_read_back_identical(conn):
    data = png(4, 5)
    mote_id = images.add(conn, data, "wing")
    spec = get_mote(conn, mote_id).properties[images.IMAGE_KEY]
    assert (spec["mime"], spec["width"], spec["height"]) == (images.PNG, 4, 5)
    shot = images.get_bytes(conn, "wing")
    assert shot.data == data and shot.mote_id == mote_id


def test_names_default_to_the_next_free_shot_number(conn):
    assert images.get_bytes(conn, images.add(conn, png())).name == "shot-001"
    assert images.get_bytes(conn, images.add(conn, png())).name == "shot-002"
    create_mote(conn, {"name": "shot-003"})
    assert images.get_bytes(conn, images.add(conn, png())).name == "shot-004"


def test_a_duplicate_image_name_is_refused(conn):
    images.add(conn, png(), "wing")
    with pytest.raises(ImageError, match="already exists"):
        images.add(conn, png(), "wing")


def test_only_png_and_svg_and_by_content_not_extension(conn):
    with pytest.raises(ImageError, match="PNG or SVG"):
        images.add(conn, b"\xff\xd8\xff\xe0 jpeg bytes")
    with pytest.raises(ImageError, match="PNG or SVG"):
        images.add(conn, b"just some text")


def test_the_size_cap(conn):
    with pytest.raises(ImageError, match="limit is 4 MB"):
        images.add(conn, png() + b"\x00" * images.MAX_BYTES)
    assert list_motes(conn) == []


def test_a_truncated_png_is_refused(conn):
    with pytest.raises(ImageError, match="truncated"):
        images.add(conn, b"\x89PNG\r\n\x1a\n" + b"\x00" * 4)


def test_svg_is_cleaned_of_scripts_handlers_and_external_references(conn):
    dirty = (
        b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
        b'onload="steal()"><script>alert(1)</script><foreignObject/>'
        b'<a xlink:href="javascript:alert(1)"><rect onclick="x()" width="1" height="1"/></a>'
        b'<image href="http://evil.example/x.png"/>'
        b'<use href="#ok"/><rect id="ok" style="fill:url(http://evil.example/g)"/>'
        b'<circle fill="url(#grad)" r="1"/></svg>'
    )
    clean = images.get_bytes(conn, images.add(conn, dirty, "x")).data.decode()
    for bad in ("script", "foreignObject", "onload", "onclick", "javascript", "evil.example"):
        assert bad not in clean
    assert 'href="#ok"' in clean and "url(#grad)" in clean


def test_svg_with_entities_or_a_wrong_root_is_refused(conn):
    with pytest.raises(ImageError, match="entities"):
        images.add(conn, b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "b">]><svg/>')
    with pytest.raises(ImageError, match="not <svg>|does not parse"):
        images.add(conn, b'<?xml version="1.0"?><html xmlns="x"/> <svg')


def test_find_matches_images_only_and_names_the_ambiguity(conn):
    create_mote(conn, {"name": "wing"})                 # a plain mote of the same name
    images.add(conn, png(), "Wing")
    mote, error = images.find("wing", list_motes(conn))
    assert error is None and mote.properties["name"] == "Wing"      # case-insensitive, images only
    _, error = images.find("missing", list_motes(conn))
    assert "no image named" in error
    plain = next(m for m in list_motes(conn) if not images.has_image(m.properties))
    _, error = images.find(plain.id, list_motes(conn))
    assert "carries no image" in error


# ---- rename ----

def test_rename_rewrites_every_note_that_names_it(conn):
    images.add(conn, png(), "shot-001")
    a = note(conn, "\\image{shot-001}\n\\image{shot-001}{width=50}\n\\image{other}")
    b = note(conn, "nothing here")
    assert images.rename(conn, "shot-001", "wing") == [a]
    assert get_mote(conn, a).properties[modoc.MODOC_KEY] == (
        "\\image{wing}\n\\image{wing}{width=50}\n\\image{other}")
    assert get_mote(conn, b).properties[modoc.MODOC_KEY] == "nothing here"
    assert images.get_bytes(conn, "wing").name == "wing"


def test_rename_refuses_a_taken_or_empty_name(conn):
    images.add(conn, png(), "a")
    images.add(conn, png(), "b")
    with pytest.raises(ImageError, match="already exists"):
        images.rename(conn, "a", "b")
    with pytest.raises(ImageError, match="needs a name"):
        images.rename(conn, "a", "  ")


# ---- the macro ----

def test_image_macro_bare_width_and_figure():
    doc = modoc.parse(
        "\\image{a}\n\n\\image{b}{width=60}\n\n"
        "\\begin{figure}\n\\image{c}\n\\caption{Wing}\n\\label{fig:wing}\n\\end{figure}\n"
        "See \\ref{fig:wing}.")
    a, b, c, _para = doc.blocks
    assert (a.ref, a.width, a.number) == ("a", 0, 0)            # bare: unnumbered
    assert (b.ref, b.width, b.number) == ("b", 60, 0)
    assert (c.ref, c.number, c.label) == ("c", 1, "fig:wing")
    assert doc.labels["fig:wing"].kind == modoc.FIGURE_LABEL
    assert doc.diagnostics == []


def test_images_and_graphs_share_the_figure_counter():
    doc = modoc.parse(
        "\\begin{figure}\n\\graph{Load}\n\\caption{One}\n\\end{figure}\n"
        "\\begin{figure}\n\\image{x}\n\\caption{Two}\n\\end{figure}")
    assert [b.number for b in doc.blocks] == [1, 2]


def test_image_macro_diagnostics():
    doc = modoc.parse(
        "\\image{}\n\\image{a}{width=500}\n\\image{a}{height=3}\n"
        "\\begin{figure}\n\\image{a}\n\\graph{g}\n\\end{figure}\n"
        "text \\image{a} inline")
    kinds = [d.kind for d in doc.diagnostics]
    assert kinds.count("bad_image") == 1
    assert kinds.count("bad_image_option") == 2
    assert "bad_figure" in kinds
    assert "unknown_command" in kinds           # a block command in a sentence


def test_a_percent_sign_starts_a_comment_so_width_takes_a_bare_number():
    doc = modoc.parse("\\image{a}{width=50%}")
    assert not modoc.images(doc)                    # cut to `\\image{a}{width=50`
    assert [d.kind for d in doc.diagnostics] == ["unknown_command"]


# ---- rendering ----

def test_render_embeds_the_picture_and_numbers_the_figure(conn):
    images.add(conn, png(), "wing", alt="A wing")
    source = ("\\begin{figure}\n\\image{wing}\n\\caption{The wing}\n\\label{f}\n\\end{figure}\n\n"
              "As \\ref{f} shows.")
    shown = document.render(conn, note(conn, ""), source)
    image, para = shown["blocks"]
    assert image["type"] == "image" and image["error"] is None
    assert image["src"].startswith("data:image/png;base64,")
    assert base64.b64decode(image["src"].split(",", 1)[1]) == png()
    assert (image["alt"], image["number"], image["word"]) == ("A wing", 1, "Figure")
    assert shown["problems"] == []
    assert para["content"][1]["text"] == "Figure 1"


def test_a_missing_image_is_a_visible_error_and_a_problem(conn):
    shown = document.render(conn, note(conn, ""), "\\image{ghost}")
    (image,) = shown["blocks"]
    assert image["type"] == "image" and "no image named" in image["error"]
    assert "src" not in image
    assert [p["kind"] for p in shown["problems"]] == ["unresolved_image"]


def test_an_included_note_brings_its_images(conn):
    images.add(conn, png(), "wing")
    create_mote(conn, {"name": "Method", modoc.MODOC_KEY: "\\image{wing}"})
    shown = document.render(conn, note(conn, ""), "\\include{Method}")
    assert [b["type"] for b in shown["blocks"]] == ["image"]


# ---- CLI and MCP ----

def test_cli_add_list_rename_export(conn, tmp_path, capsys):
    from cli import main as cli
    shot = tmp_path / "in.png"
    shot.write_bytes(png())
    args = cli.build_parser().parse_args(["image", "add", str(shot), "--alt", "x"])
    cli._handle_image(conn, args)
    assert "shot-001" in capsys.readouterr().out
    cli._handle_image(conn, cli.build_parser().parse_args(["image", "rename", "shot-001", "wing"]))
    cli._handle_image(conn, cli.build_parser().parse_args(["image", "list"]))
    assert "wing" in capsys.readouterr().out
    out = tmp_path / "out.png"
    cli._handle_image(conn, cli.build_parser().parse_args(["image", "export", "wing", str(out)]))
    assert out.read_bytes() == png()


def test_mcp_add_list_and_pixels_are_left_out_of_mote_dumps():
    from core.gardens import create_garden
    from mcp_server import server
    create_garden("pics")
    added = server.add_image(base64.b64encode(png()).decode(), garden="pics")
    assert added["name"] == "shot-001"
    assert server.list_images("pics") == [
        {"id": added["id"], "name": "shot-001", "mime": images.PNG, "bytes": len(png())}]
    dumped = server.get_mote(added["id"], "pics")
    assert dumped["properties"][images.IMAGE_KEY]["data"].endswith("bytes omitted>")
    assert server.rename_image("shot-001", "wing", "pics")["name"] == "wing"
    with pytest.raises(ValueError, match="base64"):
        server.add_image("not base64!!", garden="pics")
