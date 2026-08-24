# mora/payload.py
"""A resolved note, shaped for the JS bridge.

Mora renders from a tree the *backend* produced, rather than parsing
.modoc a second time in JavaScript. That is the whole reason the
rendered pane and `monoss modoc render` can never come to disagree about
what a note means: there is one parser (core/modoc.py), one resolver
(core/modoc_bind.py), and this, which only reshapes their answers into
JSON-able dicts.

The one thing JavaScript does read for itself is the source pane's
syntax highlighting -- colour is presentation, not meaning, and running
it locally is what keeps typing feel immediate.

Resolution is inlined onto each node rather than sent alongside as a
keyed table. A `\\val` node arrives already carrying the text it renders
as, so the frontend never has to reconstruct core/modoc.py's resolution
keys or agree with it about traversal order.
"""
from dataclasses import dataclass

from core import modoc
from core import kinds as kinds_repo
from core import species as species_repo
from core.entities import Mote, Relationship
from core.modoc_bind import LinkTarget, Resolution, problems

NAME_KEY = "name"


@dataclass(frozen=True)
class Directory:
    """Everything the payloads need to say about a mote beyond its id:
    its name, its species (which is what colours it, in both windows) and
    its kinds. Built once per request from one read of the garden."""
    motes: dict[str, Mote]
    relationships: list[Relationship]
    kinds: dict[str, list[str]]

    @staticmethod
    def of(motes: list[Mote], relationships: list[Relationship]) -> "Directory":
        return Directory(
            {m.id: m for m in motes}, relationships, kinds_repo.kinds_in(motes, relationships)
        )

    def describe(self, mote_id: str | None) -> dict:
        mote = self.motes.get(mote_id) if mote_id else None
        if mote is None:
            return {"id": mote_id, "name": mote_id or "", "speciesId": None,
                    "speciesName": None, "kinds": []}
        species_id = self._species_id(mote.id)
        species = self.motes.get(species_id) if species_id else None
        return {
            "id": mote.id,
            "name": name_of(mote),
            "speciesId": species_id,
            # The name as well as the id: the id colours the mote, the
            # name is what "what is this?" actually wants -- "Project",
            # not a UUID, and not the bare word "mote".
            "speciesName": name_of(species) if species else None,
            "kinds": self.kinds.get(mote.id, []),
        }

    def _species_id(self, mote_id: str) -> str | None:
        """The first species a mote is an instance of -- the one the main
        app's canvas colours it by (ui/frontend/graph.js::_speciesModeColor),
        so a mote reads the same colour in Mora as it does in the graph."""
        for relationship in self.relationships:
            if relationship.from_id == mote_id and relationship.type == species_repo.INSTANCE_OF:
                return relationship.to_id
        return None


def name_of(mote: Mote) -> str:
    name = mote.properties.get(NAME_KEY)
    return name if isinstance(name, str) and name else mote.id[:8]


def note_payload(
    document: modoc.Document, resolution: Resolution, directory: Directory, source: str
) -> dict:
    """One edit's worth of answer: the rendered tree, what is wrong with
    it, and the counts the status bar shows."""
    return {
        "anchor": directory.describe(resolution.anchor_id),
        "step": resolution.step,
        "projected": resolution.projected,
        "blocks": [_block(b, resolution, directory) for b in document.blocks],
        "diagnostics": [
            {"line": d.line, "kind": d.kind, "message": d.message}
            for d in problems(document, resolution)
        ],
        "linked": _linked(document, resolution, directory),
        "stats": {
            "words": modoc.word_count(document),
            "links": len(modoc.links(document)),
            "values": len(modoc.values(document)),
            "lines": len(source.splitlines()) or 1,
        },
    }


def _block(block, resolution: Resolution, directory: Directory) -> dict:
    if isinstance(block, modoc.Heading):
        return {"type": "heading", "level": block.level,
                "content": _content(block.content, resolution, directory)}
    if isinstance(block, modoc.Paragraph):
        return {"type": "paragraph", "content": _content(block.content, resolution, directory)}
    if isinstance(block, modoc.ItemList):
        return {"type": "list",
                "items": [_content(i, resolution, directory) for i in block.items]}
    if isinstance(block, modoc.Equation):
        return {"type": "equation", "spans": _spans(block.text)}
    return _graph_block(block, resolution)


def _graph_block(block: modoc.Graph, resolution: Resolution) -> dict:
    series = resolution.graphs.get(modoc.graph_key(block))
    spec = block.spec
    return {
        "type": "graph",
        "kind": spec.kind if spec else "",
        "expression": spec.expression if spec else "",
        "key": spec.key if spec else None,
        "labels": series.labels if series else [],
        "values": series.values if series else [],
        "error": series.error if series else "unresolved graph",
    }


def _content(nodes, resolution: Resolution, directory: Directory) -> list[dict]:
    return [_inline(n, resolution, directory) for n in nodes]


def _inline(node, resolution: Resolution, directory: Directory) -> dict:
    if isinstance(node, modoc.Text):
        return {"type": "text", "text": node.text}
    if isinstance(node, modoc.Styled):
        return {"type": "styled", "style": node.style, "text": node.text}
    if isinstance(node, modoc.Math):
        return {"type": "math", "spans": _spans(node.text)}
    if isinstance(node, modoc.Link):
        target = resolution.links.get(node.ref)
        return {"type": "link", "label": node.label,
                **_target_payload(node.ref, target, directory)}
    result = resolution.values.get(modoc.value_key(node))
    return {
        "type": "value",
        "expression": node.expression,
        "ref": node.ref,
        "text": result.text if result else f"?{node.expression}",
        "error": result.error if result else "unresolved value",
    }


def _target_payload(ref: str, target: LinkTarget | None, directory: Directory) -> dict:
    if target is None or target.mote_id is None:
        return {"ref": ref, "moteId": None, "name": ref, "speciesId": None, "kinds": [],
                "error": target.error if target else "unresolved reference"}
    return {"ref": ref, "error": None, "moteId": target.mote_id,
            **{k: v for k, v in directory.describe(target.mote_id).items() if k != "id"}}


def _linked(document: modoc.Document, resolution: Resolution, directory: Directory) -> list[dict]:
    """Every mote the text mentions, with how often -- the data panel's
    "Linked in text" list. Ordered by reference count so the note's
    subject matter sorts itself."""
    counts: dict[str, int] = {}
    for node in modoc.links(document):
        counts[node.ref] = counts.get(node.ref, 0) + 1
    entries = [
        {**_target_payload(ref, resolution.links.get(ref), directory), "count": count}
        for ref, count in counts.items()
    ]
    return sorted(entries, key=lambda e: (-e["count"], e["name"]))


def _spans(text: str) -> list[dict]:
    return [{"kind": s.kind, "text": s.text} for s in modoc.math_spans(text)]


def panel_payload(mote: Mote, directory: Directory) -> dict:
    """One mote as the data panel shows it: its properties, and every
    relationship touching it with the direction it points."""
    relationships = [
        r for r in directory.relationships if mote.id in (r.from_id, r.to_id)
    ]
    return {
        **directory.describe(mote.id),
        "properties": mote.properties,
        "relationships": [_relationship(r, mote.id, directory) for r in relationships],
    }


def _relationship(relationship: Relationship, mote_id: str, directory: Directory) -> dict:
    outgoing = relationship.from_id == mote_id
    other = relationship.to_id if outgoing else relationship.from_id
    return {
        "id": relationship.id,
        "type": relationship.type,
        "outgoing": outgoing,
        "other": directory.describe(other),
    }
