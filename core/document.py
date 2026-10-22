# core/document.py
"""A note as the console's panes need it (docs/console.md -> Settled for U6).

    render(conn, note_id, source)   the whole document, resolved, as JSON:
                                    every block core/modoc.py parses, with
                                    what core/modoc_bind.py resolved it to
                                    inlined, plus its problems and counts
    notes(conn, text)               every note, for the `notes` pane
    target(conn, ref, selection)    which mote `edit X` / `read X` names
    save(conn, note_id, source)     write a note as one undo step

The read pane draws this and decides nothing: what a `\\val` says, what a
`\\ref` reads as, which rows `\\given` lists -- all of it is the same answer
`monoss modoc render` prints, because both come from here and
core/modoc_bind.py. Typography is the pane's.

A draft is rendered, not the stored note: the source comes in with every
call, so the read pane shows unsaved typing. `\\include`d notes are read
as stored."""
import base64
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from core import calc
from core import derivations
from core import formula_tex
from core import images as images_repo
from core import modoc
from core import modoc_bind
from core import motes as motes_repo
from core import plotview
from core import simulation
from core import table as table_repo
from core import undo
from core.command_bar import CommandError
from core.modoc_bind import Resolution


# ---- which note --------------------------------------------------------

@dataclass(frozen=True)
class Target:
    """What `edit X` names: a mote, or -- for a name no mote has -- the
    name a first save will create one under."""
    mote_id: str | None
    name: str


def target(conn: sqlite3.Connection, ref: str, selection=()) -> Target:
    if ref == "@sel":
        if not selection:
            raise CommandError("@sel is empty -- select a mote first")
        ref = f"#{selection[0]}"
    if ref.startswith("#"):
        mote = motes_repo.get_mote(conn, ref[1:])
        if mote is None:
            raise CommandError(f"no mote {ref}")
        return Target(mote.id, _name(mote.properties, mote.id))
    named = [m for m in motes_repo.list_motes(conn) if m.properties.get("name") == ref]
    if len(named) > 1:
        raise CommandError(f"{len(named)} motes are named {ref!r} -- use #id")
    if named:
        return Target(named[0].id, ref)
    return Target(None, ref)


def _name(properties: dict, mote_id: str) -> str:
    name = properties.get("name")
    return name if isinstance(name, str) and name else f"#{mote_id[:8]}"


# ---- saving ----------------------------------------------------------

def save(conn: sqlite3.Connection, note_id: str | None, source: str,
         name: str | None = None) -> tuple[str, "undo.Command"]:
    """Write `source` as the note, and the write as one undo step. With no
    `note_id`, the mote is made first, named `name` -- `edit Homework 5`
    on a name nothing has yet. Returns (the note's id, its undo)."""
    if note_id is None:
        if not name:
            raise CommandError("a new note needs a name")
        properties = {"name": name, modoc.MODOC_KEY: source}
        note_id = motes_repo.create_mote(conn, properties)
        return note_id, undo.for_create_mote(conn, note_id, properties, label=f"write {name}")
    mote = motes_repo.get_mote(conn, note_id)
    if mote is None:
        raise CommandError("that note's mote no longer exists")
    before = {modoc.MODOC_KEY: mote.properties[modoc.MODOC_KEY]} if modoc.MODOC_KEY in mote.properties else {}
    modoc.write_source(conn, note_id, source)
    label = f"write {_name(mote.properties, note_id)}"
    return note_id, undo.for_update_mote(conn, note_id, before, {modoc.MODOC_KEY: source}, label=label)


# ---- the notes list --------------------------------------------------

def notes(conn: sqlite3.Connection, text: str = "") -> list[dict]:
    """Every mote carrying a note, by name: its word count, how many
    problems it parses with (resolving every note would re-solve every
    figure; parse problems are free), and its first line of prose.
    `text` narrows to notes whose name or prose contains it."""
    needle = (text or "").casefold()
    out = []
    for mote in motes_repo.list_motes(conn):
        if not modoc.has_modoc(mote.properties):
            continue
        document = modoc.parse(mote.properties[modoc.MODOC_KEY] or "")
        prose = modoc.plain_text(document)
        name = _name(mote.properties, mote.id)
        if needle and needle not in name.casefold() and needle not in prose.casefold():
            continue
        first = next((" ".join(line.split()) for line in prose.splitlines() if line.strip()), "")
        out.append({"id": mote.id, "name": name, "words": modoc.word_count(document),
                    "problems": len(document.diagnostics), "first": first[:120]})
    return sorted(out, key=lambda n: n["name"].casefold())


# ---- rendering -------------------------------------------------------

class GardenMemo:
    """What a typing pane reads off the garden, worked out once per state
    of it: every `\\graph` drawn, the one live solve a note's values,
    `\\work` and sections read, and the units a worksheet line shows. The
    edit and read panes re-render on every pause in typing, the worksheet
    re-previews on every pause, and typing does not move the garden -- so
    without this each pause re-solves it, and re-runs every sweep in the
    note. Invalidated by any write to the garden, so no writer has to
    remember to say so."""

    def __init__(self) -> None:
        self._drawn: dict[str, plotview.Figure] = {}
        self._explainer: derivations.Explainer | None = None
        self._inferred: dict[str, dict[str, str]] | None = None
        self._at: tuple | None = None

    @staticmethod
    def _fingerprint(conn: sqlite3.Connection) -> tuple:
        # `total_changes` counts this connection's writes, `data_version`
        # moves on another's commit (the MCP server, a sync): between them,
        # every write. The connection itself, so a garden switch is one too.
        return (conn, conn.total_changes, conn.execute("PRAGMA data_version").fetchone()[0])

    def _check(self, conn: sqlite3.Connection) -> None:
        fingerprint = self._fingerprint(conn)
        if fingerprint != self._at:
            self._drawn.clear()
            self._explainer = None
            self._inferred = None
            self._at = fingerprint

    @contextmanager
    def keeping(self, conn: sqlite3.Connection) -> Iterator[None]:
        """Around a write none of this reads -- a note's own text, which is
        a string and so never a quantity -- so the autosave on every pause
        doesn't re-solve the garden and re-run every sweep after it. Only
        what was current before the write is carried past it."""
        current = self._fingerprint(conn) == self._at
        yield
        if current:
            self._at = self._fingerprint(conn)

    def __call__(self, conn, ref: str, *, motes=None) -> plotview.Figure:
        self._check(conn)
        if ref not in self._drawn:
            self._drawn[ref] = plotview.figure(conn, ref, motes=motes)
        return self._drawn[ref]

    def explainer(self, conn: sqlite3.Connection) -> "derivations.Explainer":
        self._check(conn)
        if self._explainer is None:
            self._explainer = derivations.Explainer(conn)
        return self._explainer

    def inferred_units(self, conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
        self._check(conn)
        if self._inferred is None:
            self._inferred = simulation.inferred_units(conn)
        return self._inferred


def render(conn: sqlite3.Connection, note_id: str, source: str, draw=None) -> dict:
    """The document `source` says, as the note `note_id`, against the
    garden as it stands. `draw` is a GardenMemo, or a bare figure drawer."""
    document = modoc_bind.assemble(conn, note_id, source=source)
    explainer = draw.explainer(conn) if isinstance(draw, GardenMemo) else None
    resolution = modoc_bind.resolve(conn, note_id, document, draw=draw, explainer=explainer)
    motes, relationships, index = table_repo.read(conn)
    shape = _Shaper(document, resolution, index)
    found = modoc_bind.problems(document, resolution)
    return {
        "noteId": note_id,
        "anchor": table_repo.describe(index, resolution.anchor_id),
        "blocks": [shape.block(b) for b in document.blocks],
        "problems": [{"line": d.line, "kind": d.kind, "message": d.message} for d in found],
        "notices": [{"line": d.line, "kind": d.kind, "message": d.message}
                    for d in modoc_bind.notices(document, resolution)],
        "stats": {
            "words": modoc.word_count(document),
            "links": len(modoc.links(document)),
            "values": len(modoc.values(document)),
            "lines": len(source.splitlines()) or 1,
        },
    }


class _Shaper:
    def __init__(self, document: modoc.Document, resolution: Resolution, index) -> None:
        self.document = document
        self.resolution = resolution
        self.index = index

    # -- blocks --

    def block(self, b) -> dict:
        if isinstance(b, modoc.Heading):
            return {"type": "heading", "level": b.level, "content": self.inline(b.content)}
        if isinstance(b, modoc.Paragraph):
            return {"type": "paragraph", "content": self.inline(b.content)}
        if isinstance(b, modoc.ItemList):
            return {"type": "list", "ordered": b.ordered, "items": [self.inline(i) for i in b.items]}
        if isinstance(b, modoc.Equation):
            return {"type": "equation", "tex": b.text, "number": b.number, "label": b.label}
        if isinstance(b, modoc.Cited):
            rule = self.resolution.rules.get(b.rule)
            return {"type": "cited", "rule": b.rule, "number": b.number,
                    "name": rule.name if rule else b.rule,
                    "moteId": rule.mote_id if rule else None,
                    "equation": rule.equation if rule else "",
                    "tex": formula_tex.to_tex(rule.equation) if rule and rule.equation else None,
                    "error": (rule.error if rule else "unresolved rule")}
        if isinstance(b, modoc.Graph):
            drawn = self.resolution.graphs.get(modoc.graph_key(b))
            figure = ({"mode": "sweep", "error": "unresolved graph"} if drawn is None
                      else plotview.figure_json(drawn))
            return {"type": "graph", "view": b.view, "figure": figure,
                    **self.caption(modoc.FIGURE_LABEL, b)}
        if isinstance(b, modoc.Image):
            return self.image(self.resolution.images.get(modoc.image_key(b)), b)
        if isinstance(b, modoc.Table):
            return {"type": "table", "header": [self.inline(c) for c in b.header],
                    "rows": [[self.inline(c) for c in row] for row in b.rows],
                    **self.caption(modoc.TABLE_LABEL, b)}
        if isinstance(b, modoc.Work):
            return self.work(self.resolution.works.get(modoc.work_key(b)), b)
        if isinstance(b, modoc.Section):
            return self.section(self.resolution.sections.get(modoc.section_key(b)), b)
        return {"type": "unknown", "line": getattr(b, "line", 0)}

    def caption(self, kind: str, b) -> dict:
        if not b.number and not b.caption:
            return {"number": 0, "caption": []}
        word = modoc.LABEL_WORDS.get(kind, kind)
        return {"number": b.number, "word": word, "caption": self.inline(b.caption)}

    def image(self, found, node: modoc.Image) -> dict:
        """A stored picture as the data URI a webview can show directly.
        One that cannot be found is a visible error block, never a gap."""
        out = {"type": "image", "ref": node.ref, "width": node.width,
               **self.caption(modoc.FIGURE_LABEL, node)}
        if not isinstance(found, images_repo.Picture):
            return {**out, "error": found or "unresolved image"}
        uri = f"data:{found.mime};base64,{base64.b64encode(found.data).decode('ascii')}"
        return {**out, "name": found.name, "moteId": found.mote_id, "mime": found.mime,
                "src": uri, "alt": found.alt or found.name, "error": None}

    def work(self, shown: calc.Work | None, node: modoc.Work) -> dict:
        if shown is None:
            return {"type": "work", "target": node.target, "error": "unresolved"}
        return {
            "type": "work", "target": node.target, "label": shown.label,
            "result": shown.quantity.text(node.format.figures),
            "steps": [{"kind": s.kind, "text": s.text, "note": s.note,
                       "tex": formula_tex.to_tex(s.text) if s.kind == calc.SYMBOLIC else None}
                      for s in shown.steps],
            "alsoDetermined": list(shown.also_determined),
            "note": shown.note, "error": shown.error,
        }

    def section(self, result, node: modoc.Section) -> dict:
        if result is None:
            return {"type": "section", "kind": node.kind, "heading": node.kind, "error": "unresolved"}
        out = {"type": "section", "kind": result.kind, "heading": result.heading,
               "error": result.error}
        if result.kind == "checks":
            out["checks"] = [{"equation": c.equation, "label": c.label, "status": c.status,
                              "passed": c.passed, "detail": c.detail} for c in result.checks]
        elif result.kind == "verification":
            out["comparisons"] = [{
                "ref": c.label, "moteId": c.mote_id,
                "hand": c.hand.text(result.figures), "external": c.external.text(result.figures),
                "error": None if c.error is None else round(c.error, 4),
                "note": c.note, "problem": c.problem,
            } for c in result.comparisons]
        else:
            out["rows"] = [{
                "ref": r.label, "moteId": r.mote_id, "value": r.text(result.figures),
                "source": (r.source.note if r.source and r.source.note else ""),
            } for r in result.rows]
        return out

    # -- inline --

    def inline(self, nodes) -> list[dict]:
        return [self.node(n) for n in nodes]

    def node(self, n) -> dict:
        if isinstance(n, modoc.Text):
            return {"type": "text", "text": n.text}
        if isinstance(n, modoc.Styled):
            return {"type": "styled", "style": n.style, "text": n.text}
        if isinstance(n, modoc.Math):
            return {"type": "math", "tex": n.text}
        if isinstance(n, modoc.Ref):
            return {"type": "ref", "name": n.name, "text": modoc.reference_text(self.document, n),
                    "error": None if n.name in self.document.labels else "no such label"}
        if isinstance(n, modoc.Link):
            t = self.resolution.links.get(n.ref)
            if t is None or t.mote_id is None:
                return {"type": "link", "ref": n.ref, "moteId": None, "text": n.label or n.ref,
                        "error": t.error if t else "unresolved reference"}
            described = table_repo.describe(self.index, t.mote_id)
            return {"type": "link", "ref": n.ref, "moteId": t.mote_id,
                    "text": n.label or described["name"], "speciesId": described["speciesId"],
                    "error": None}
        result = self.resolution.values.get(modoc.value_key(n))
        return {"type": "value", "expression": n.expression,
                "text": result.text if result else f"?{n.expression}",
                "note": result.note if result else "",
                "error": result.error if result else "unresolved value"}

