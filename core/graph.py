# core/graph.py
import sqlite3

from core.entities import Mote, Relationship
from core.motes import get_motes, list_motes
from core.relationships import list_relationships


def relationships_touching(conn: sqlite3.Connection, mote_id: str) -> list[Relationship]:
    """All relationships where mote_id is either endpoint."""
    return [r for r in list_relationships(conn) if mote_id in (r.from_id, r.to_id)]


def whole_garden(conn: sqlite3.Connection) -> tuple[list[Mote], list[Relationship]]:
    """Every mote and relationship. Unlike `neighborhood`, this reaches
    motes with no connections at all, which no amount of hopping can."""
    return list_motes(conn), list_relationships(conn)


def neighborhood(
    conn: sqlite3.Connection, start_id: str, hops: int = 2
) -> tuple[list[Mote], list[Relationship]]:
    """BFS outward from start_id up to `hops` edges.

    Adjacency is undirected for hop-counting, so a relationship pointing
    into start_id still makes its source reachable when navigating outward.
    Returned relationships keep their real from/to direction for rendering.
    """
    all_rels = list_relationships(conn)
    adjacency: dict[str, set[str]] = {}
    for r in all_rels:
        adjacency.setdefault(r.from_id, set()).add(r.to_id)
        adjacency.setdefault(r.to_id, set()).add(r.from_id)

    visited = {start_id}
    frontier = {start_id}
    for _ in range(hops):
        frontier = {n for m in frontier for n in adjacency.get(m, ())} - visited
        if not frontier:
            break
        visited |= frontier

    motes = get_motes(conn, list(visited))
    rels = [r for r in all_rels if r.from_id in visited and r.to_id in visited]
    return motes, rels
