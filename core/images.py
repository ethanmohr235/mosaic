# core/images.py
"""Images in notes: a mote that carries a picture.

An image is an ordinary mote with a reserved `image` property, so it
rides sync, undo, garden export and search with no machinery of its own
(the same reason a note is a property rather than a file). The property
holds `{"mime", "data", "alt"?, "width"?, "height"?}`, `data` being the
file's bytes in base64. PNG and SVG only.

Everything that reads or writes those bytes goes through this module --
`add`, `get_bytes`, `find`, `rename`. That is the one seam: if base64 in
the op log ever proves too heavy, side-file storage replaces the inside
of these functions and `\\image`, the renderer and the exporters never
notice.

An SVG is sanitized on the way in, because the console renders it in a
webview: scripts, event handlers and anything that reaches outside the
file are dropped. Nothing is trusted on its extension; the bytes are
sniffed.
"""
import base64
import re
import sqlite3
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass

from core import undo
from core.motes import Mote, create_mote, get_mote, list_motes, update_mote

IMAGE_KEY = "image"
NAME_KEY = "name"
MODOC_KEY = "modoc"

PNG = "image/png"
SVG = "image/svg+xml"
EXTENSIONS = {PNG: "png", SVG: "svg"}

# Past this a screenshot is better scaled down than carried in every sync
# op. A cap with a clear error beats silently bloating a garden.
MAX_BYTES = 4 * 1024 * 1024

AUTO_PREFIX = "shot-"

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_SVG_NS = "http://www.w3.org/2000/svg"
_XLINK_NS = "http://www.w3.org/1999/xlink"
# Elements that run code or pull in a foreign document.
_DROPPED_TAGS = {"script", "foreignObject", "iframe", "object", "embed", "audio", "video"}
_LINK_ATTRS = {"href", f"{{{_XLINK_NS}}}href"}


class ImageError(ValueError):
    """A picture that cannot be stored, with the reason in plain words."""


@dataclass(frozen=True)
class Picture:
    mote_id: str
    name: str
    mime: str
    data: bytes
    alt: str = ""


def has_image(properties: dict) -> bool:
    spec = properties.get(IMAGE_KEY)
    return isinstance(spec, dict) and isinstance(spec.get("data"), str)


def list_images(motes: list[Mote]) -> list[Mote]:
    return [m for m in motes if has_image(m.properties)]


# --- sniffing and cleaning --------------------------------------------

def sniff(data: bytes) -> str:
    """The mime type the bytes actually are, or ImageError."""
    if data.startswith(_PNG_MAGIC):
        return PNG
    head = data[:1024].lstrip(b"\xef\xbb\xbf \t\r\n")
    if head.startswith((b"<?xml", b"<svg", b"<!--", b"<!DOCTYPE svg")) and b"<svg" in data[:8192]:
        return SVG
    raise ImageError("not a PNG or SVG -- those are the only formats images support")


def png_size(data: bytes) -> tuple[int, int]:
    """Pixel width and height from the IHDR chunk."""
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise ImageError("PNG has no header chunk -- the file looks truncated")
    return struct.unpack(">II", data[16:24])


def sanitize_svg(data: bytes) -> bytes:
    """The SVG without scripts, event handlers or external references.

    Internal references (`#id`) and embedded `data:image/` URIs are kept,
    since gradients and inlined bitmaps need them. A doctype is refused
    outright: entity expansion is the one way a parse can be turned on
    its reader."""
    if re.search(rb"<!ENTITY", data, re.IGNORECASE):
        raise ImageError("SVG declares entities -- refusing it")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as error:
        raise ImageError(f"SVG does not parse: {error}") from error
    if root.tag != f"{{{_SVG_NS}}}svg" and root.tag != "svg":
        raise ImageError("XML file whose root is not <svg>")
    _clean(root)
    ET.register_namespace("", _SVG_NS)
    ET.register_namespace("xlink", _XLINK_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


def _clean(element: ET.Element) -> None:
    for child in list(element):
        if _local(child.tag) in _DROPPED_TAGS:
            element.remove(child)
        else:
            _clean(child)
    for attribute in list(element.attrib):
        value = element.attrib[attribute].strip()
        lowered = value.lower().replace(" ", "")
        if _local(attribute).lower().startswith("on"):
            del element.attrib[attribute]
        elif attribute in _LINK_ATTRS and not (
            value.startswith("#") or lowered.startswith("data:image/")
        ):
            del element.attrib[attribute]
        elif "javascript:" in lowered or re.search(r"url\(\s*['\"]?(?!#|data:image/)", value):
            del element.attrib[attribute]


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# --- storage ----------------------------------------------------------

def next_auto_name(motes: list[Mote]) -> str:
    """`shot-001`, `shot-002`, ...: the first number nobody has used."""
    taken = {m.properties.get(NAME_KEY) for m in motes}
    number = 1
    while f"{AUTO_PREFIX}{number:03d}" in taken:
        number += 1
    return f"{AUTO_PREFIX}{number:03d}"


def add(
    conn: sqlite3.Connection, data: bytes, name: str | None = None, alt: str | None = None
) -> str:
    """Store a picture as a new mote and return its id. `name` defaults to
    the next `shot-NNN`; a name already on another image is refused, since
    the name is what `\\image{...}` finds it by."""
    if len(data) > MAX_BYTES:
        raise ImageError(
            f"image is {len(data) / 1024 / 1024:.1f} MB; the limit is "
            f"{MAX_BYTES // 1024 // 1024} MB -- scale it down first"
        )
    mime = sniff(data)
    spec: dict = {"mime": mime}
    if mime == PNG:
        spec["width"], spec["height"] = png_size(data)
    else:
        data = sanitize_svg(data)
    spec["data"] = base64.b64encode(data).decode("ascii")
    if alt:
        spec["alt"] = alt

    motes = list_motes(conn)
    name = (name or "").strip() or next_auto_name(motes)
    clash = [m for m in list_images(motes) if m.properties.get(NAME_KEY) == name]
    if clash:
        raise ImageError(f"an image named {name!r} already exists -- pick another name")
    return create_mote(conn, {NAME_KEY: name, IMAGE_KEY: spec})


def add_undoable(
    conn: sqlite3.Connection, data: bytes, name: str | None = None, alt: str | None = None
) -> tuple[Picture, undo.Command]:
    """`add`, with the one undo step that takes it back out: what the
    console pushes for `image add` and for a pasted screenshot."""
    mote_id = add(conn, data, name, alt)
    mote = get_mote(conn, mote_id)
    shot = picture(mote)
    return shot, undo.for_create_mote(conn, mote_id, mote.properties, f"add image {shot.name}")


def find(ref: str, motes: list[Mote]) -> tuple[Mote | None, str | None]:
    """The image a reference names: an exact id, else a unique name among
    the images only (so a note called `wing` does not make `wing` the
    image ambiguous), matched exactly and then case-insensitively."""
    ref = (ref or "").strip()
    if not ref:
        return None, "name an image, e.g. \\image{shot-001}"
    for mote in motes:
        if mote.id == ref:
            if has_image(mote.properties):
                return mote, None
            return None, f"{_name(mote)!r} carries no image -- add one with `monoss image add`"
    images = list_images(motes)
    exact = [m for m in images if m.properties.get(NAME_KEY) == ref]
    matches = exact or [
        m for m in images
        if isinstance(m.properties.get(NAME_KEY), str)
        and m.properties[NAME_KEY].casefold() == ref.casefold()
    ]
    if len(matches) == 1:
        return matches[0], None
    if matches:
        return None, f"{len(matches)} images are named {ref!r} -- reference one by id instead"
    return None, f"no image named {ref!r}"


def picture(mote: Mote) -> Picture:
    spec = mote.properties[IMAGE_KEY]
    return Picture(
        mote.id, _name(mote), spec.get("mime", PNG),
        base64.b64decode(spec["data"]), spec.get("alt", ""),
    )


def get_bytes(conn: sqlite3.Connection, ref: str) -> Picture:
    mote, error = find(ref, list_motes(conn))
    if mote is None:
        raise ImageError(error)
    return picture(mote)


def rename(conn: sqlite3.Connection, ref: str, new_name: str) -> list[str]:
    """Give an image a new name and carry every `\\image{old}` in every
    note with it, so a rename cannot orphan a figure. Returns the ids of
    the notes rewritten."""
    return rename_undoable(conn, ref, new_name)[0]


def rename_undoable(
    conn: sqlite3.Connection, ref: str, new_name: str
) -> tuple[list[str], undo.Command | None]:
    """`rename`, plus the one undo step covering the image and every note
    it rewrote (None when the name did not change)."""
    new_name = (new_name or "").strip()
    if not new_name:
        raise ImageError("an image needs a name")
    motes = list_motes(conn)
    mote, error = find(ref, motes)
    if mote is None:
        raise ImageError(error)
    old = mote.properties.get(NAME_KEY)
    if old == new_name:
        return [], None
    if any(m.id != mote.id and m.properties.get(NAME_KEY) == new_name
           for m in list_images(motes)):
        raise ImageError(f"an image named {new_name!r} already exists")
    label = f"rename image {_name(mote)}"
    steps = []
    before = {NAME_KEY: old} if old is not None else {}
    update_mote(conn, mote.id, {NAME_KEY: new_name})
    steps.append(undo.for_update_mote(conn, mote.id, before, {NAME_KEY: new_name}, label))

    changed = []
    if isinstance(old, str) and old:
        pattern = re.compile(r"(\\image\{)" + re.escape(old) + r"(\})")
        for note in motes:
            source = note.properties.get(MODOC_KEY)
            if isinstance(source, str):
                rewritten = pattern.sub(lambda m: m.group(1) + new_name + m.group(2), source)
                if rewritten != source:
                    update_mote(conn, note.id, {MODOC_KEY: rewritten})
                    steps.append(undo.for_update_mote(
                        conn, note.id, {MODOC_KEY: source}, {MODOC_KEY: rewritten}, label))
                    changed.append(note.id)
    return changed, undo.combine(label, steps)


def _name(mote: Mote) -> str:
    name = mote.properties.get(NAME_KEY)
    return name if isinstance(name, str) and name else mote.id[:8]
