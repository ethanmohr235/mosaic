# core/csv_import.py
"""Measured data from a CSV file, as ordinary motes.

One mote per row (decided, `roadmap.md` -> *Calculation*). The header
names the properties, and a unit rides along in brackets -- `load [kN]`
-- so a column of numbers arrives already knowing what it measures. Every
value is marked `source: measured` with the file as its note, which is
what a V&V table and `\\given` read to say where a number came from; and
every row is linked to one import mote that records the file, so an
import can be found, and removed, as the one thing it was.

Motes rather than one mote holding each column as a list, because a row
is then a thing the table lists, a query filters and `\\val` quotes, the
same as anything typed by hand.

`parse` is pure and never writes, so a surface can preview a file and
show its problems before anything lands. `apply` refuses a file with any
problem at all: half an import is worse than none, since the half that
landed looks complete."""
import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field

from core import motes as motes_repo, relationships as rel_repo, undo, units
from core.calc import MEASURED, SOURCE_KEY
from core.reserved import RESERVED_PROPERTIES
from core.units import UNITS_KEY

# row --imported_from--> the import mote. An ordinary relationship type,
# not a reserved one: nothing solves or renders differently because of
# it, it only lets the rows be found together.
IMPORTED_FROM = "imported_from"
NAME_KEY = "name"

_HEADER_RE = re.compile(r"^\s*(?P<key>[^\[\]]*?)\s*(?:\[\s*(?P<unit>[^\[\]]*?)\s*\])?\s*$")


@dataclass(frozen=True)
class Column:
    index: int
    header: str
    key: str
    unit: str | None = None


@dataclass(frozen=True)
class Parsed:
    """A file read but not yet written. `rows` holds each row's
    properties as they would be stored, `units` and `source` included."""
    file: str
    columns: tuple[Column, ...]
    rows: tuple[dict, ...]
    problems: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True)
class Imported:
    import_id: str
    row_ids: tuple[str, ...] = field(default_factory=tuple)


def key_of(header: str) -> str:
    """A header as a property key: `mesh size` -> `mesh_size`, so an
    equation can name it."""
    return re.sub(r"\s+", "_", header.strip())


def parse(text: str, file: str, delimiter: str = ",") -> Parsed:
    """Read CSV text into rows of properties. Never raises for a bad
    file; every problem is a line in `problems`, numbered as the file is
    (the header is line 1)."""
    reader = csv.reader(io.StringIO(text.lstrip("﻿")), delimiter=delimiter)
    lines = [row for row in reader]
    while lines and not any(cell.strip() for cell in lines[-1]):
        lines.pop()
    if not lines:
        return Parsed(file, (), (), ("the file is empty",))

    columns, problems = _columns(lines[0])
    rows: list[dict] = []
    for number, cells in enumerate(lines[1:], start=2):
        if not any(cell.strip() for cell in cells):
            continue
        if len(cells) > len(columns):
            problems.append(
                f"line {number}: {len(cells)} cells under {len(columns)} headers"
            )
            continue
        rows.append(_row(columns, cells, file))
    if not rows and not problems:
        problems.append("the file has a header and no rows")
    return Parsed(file, tuple(columns), tuple(rows), tuple(problems))


def _columns(header: list[str]) -> tuple[list[Column], list[str]]:
    columns: list[Column] = []
    problems: list[str] = []
    seen: dict[str, int] = {}
    for index, text in enumerate(header):
        match = _HEADER_RE.match(text)
        if match is None:
            problems.append(f"column {index + 1}: cannot read the header {text!r}")
            continue
        key = key_of(match["key"])
        unit = match["unit"] or None
        where = f"column {index + 1} ({text.strip()!r})"
        if not key:
            problems.append(f"{where}: has no name")
            continue
        if key in RESERVED_PROPERTIES:
            problems.append(f"{where}: {key!r} is a reserved property -- rename the column")
            continue
        if key in seen:
            problems.append(f"{where}: repeats column {seen[key] + 1}")
            continue
        if unit is not None and not units.is_unit(unit):
            problems.append(f"{where}: {unit!r} is not a unit")
            continue
        if unit is not None and key == NAME_KEY:
            problems.append(f"{where}: a name has no unit")
            continue
        seen[key] = index
        columns.append(Column(index, text.strip(), key, unit))
    return columns, problems


def _row(columns: list[Column], cells: list[str], file: str) -> dict:
    properties: dict = {}
    unit_map: dict[str, str] = {}
    source: dict[str, dict] = {}
    for column in columns:
        cell = cells[column.index].strip() if column.index < len(cells) else ""
        if not cell:
            continue  # an empty cell is no value, not a zero
        value = cell if column.key == NAME_KEY else _value(cell)
        properties[column.key] = value
        if column.key == NAME_KEY:
            continue
        if column.unit is not None and isinstance(value, (int, float)):
            unit_map[column.key] = column.unit
        source[column.key] = {"kind": MEASURED, "note": file}
    if unit_map:
        properties[UNITS_KEY] = unit_map
    if source:
        properties[SOURCE_KEY] = source
    return properties


def _value(cell: str):
    try:
        return int(cell)
    except ValueError:
        pass
    try:
        number = float(cell)
    except ValueError:
        return cell
    # `nan` and `inf` parse as floats but no measurement is either, and
    # neither survives JSON.
    return number if number == number and abs(number) != float("inf") else cell


def apply(
    conn: sqlite3.Connection, parsed: Parsed, name: str | None = None
) -> tuple[Imported, undo.Command]:
    """Write the rows, the import mote and the links between them, as one
    undo step. Refuses a file with problems."""
    if parsed.problems:
        raise ValueError(
            f"{parsed.file} was not imported: " + "; ".join(parsed.problems)
        )
    label = name or parsed.file
    import_props = {
        NAME_KEY: label,
        "file": parsed.file,
        "rows": len(parsed.rows),
        "columns": [column.header for column in parsed.columns],
    }
    import_id = motes_repo.create_mote(conn, import_props)
    commands = [undo.for_create_mote(conn, import_id, import_props, "import")]

    row_ids = []
    for number, row in enumerate(parsed.rows, start=1):
        properties = dict(row)
        properties.setdefault(NAME_KEY, f"{label} {number}")
        row_id = motes_repo.create_mote(conn, properties)
        rel_id = rel_repo.create_relationship(conn, row_id, import_id, IMPORTED_FROM)
        commands.append(undo.for_create_mote(conn, row_id, properties, "import"))
        commands.append(undo.for_create_relationship(
            conn, rel_id, row_id, import_id, IMPORTED_FROM, "import"))
        row_ids.append(row_id)

    command = undo.combine(f"import {parsed.file} ({len(row_ids)} rows)", commands)
    return Imported(import_id, tuple(row_ids)), command
