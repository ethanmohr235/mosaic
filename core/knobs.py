# core/knobs.py
"""Knobs: values to try, never written.

A knob is `--set m.k=value [unit]` on a pane's command line. The pane
re-solves the garden as if `m.k` held that value and writes nothing, so a
what-if costs no scenario mote and no edit to undo. `plot`, `sim`, `fit`
and `graph --flow` all take them, the same way, and a pane that holds
knobs is still just its command: a layout that restores the line
restores the knobs.

    plot bd.sigma vs bd.l 4:24 --set bd.E=200e9
    sim --ticks 40 --set "tank.level=2 m" --scenario hot
    fit … --set bd.w=3

The value reads the way `set` reads it (core/command_bar.py::
property_update), so `200 GPa` works. Unlike `set`, a unit never relabels
the property: a knob typed in a unit other than the property's own is
*converted* into it, because a trial value must not change what a number
on the garden means. A unit on a property that declares none is refused.

`keep` is the way out: it writes every knob through `set`'s own writer, as
one undo step. A pane reaches it with `--keep` (the key is `W`), the same
shape as `fit --apply`.

Pure of any surface. `resolve` takes any object with `ref(text)` and
`unit(mote_id, key)` (core/plotcmd.py's `_Garden`), so nothing here
imports a pane's grammar."""
import dataclasses
import re
import sqlite3
from dataclasses import dataclass
from typing import Mapping, Sequence

from core import batch
from core import command_bar
from core import undo
from core import units as units_repo
from core.command_bar import CommandError

SET_FLAG, KEEP_FLAG = "--set", "--keep"

_NUMBER = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_VALUE_RE = re.compile(rf"^({_NUMBER})\s*(\S.*)?$")


def usage() -> str:
    return "--set takes ‹mote.key›=‹number› [unit], e.g. --set bd.E=200 GPa"


@dataclass(frozen=True)
class KnobSpec:
    """One knob as typed: a name as the line wrote it (`bd.E`), a number,
    and the unit it was typed in (None: the property's own)."""
    ref: str
    value: float
    unit: str | None = None

    @property
    def assignment(self) -> str:
        """`bd.E=200 GPa`, the form `--set` takes."""
        value = f"{number_text(self.value)} {self.unit}" if self.unit else number_text(self.value)
        return f"{self.ref}={value}"


@dataclass(frozen=True)
class ResolvedKnob:
    """A knob met with the garden: the mote and key it names, and its value
    in the unit that property declares (the unit its stored number is in)."""
    ref: str
    mote: str
    key: str
    value: float
    unit: str | None = None


def number_text(value: float) -> str:
    if float(value).is_integer() and abs(value) < 1e15:
        return str(int(value))
    return format(value, ".6g")


def parse_set(text: str) -> KnobSpec:
    """`bd.E=200e9`, `bd.E=200GPa` or `bd.E=200 GPa`. Raises CommandError
    for anything else, naming what was typed."""
    ref, equals, rhs = (text or "").partition("=")
    ref = ref.strip()
    found = _VALUE_RE.match(rhs.strip()) if equals and ref else None
    if not found:
        raise CommandError(f"{usage()}, not {text!r}")
    unit_text = (found.group(2) or "").strip()
    unit = None
    if unit_text:
        value, unit = command_bar.parse_value(f"{found.group(1)} {unit_text}")
        if unit is None:
            raise CommandError(f"{unit_text!r} is not a unit -- {usage()}")
    return KnobSpec(ref, float(found.group(1)), unit)


# ---- meeting the garden ---------------------------------------------------

def resolve(garden, specs: Sequence[KnobSpec]) -> tuple[ResolvedKnob, ...]:
    """Each knob's value in the unit its property declares. Raises
    CommandError for a name the garden doesn't have, or a unit that is
    wrong for the property."""
    out = []
    for spec in specs:
        mote, key = garden.ref(spec.ref)
        declared = garden.unit(mote, key)
        out.append(ResolvedKnob(spec.ref, mote, key, _in_declared(spec, declared), declared))
    return tuple(out)


def _in_declared(spec: KnobSpec, declared: str | None) -> float:
    if spec.unit is None or spec.unit == declared:
        return spec.value
    if declared is None:
        raise CommandError(f"{spec.ref} declares no unit, so {spec.unit} can't be converted -- "
                           f"give it one first (`{spec.ref} {spec.unit}`), or drop the unit")
    try:
        return units_repo.convert(spec.value, spec.unit, declared)
    except units_repo.UnitError as e:
        raise CommandError(f"{spec.ref}: {e}") from None


def overrides(knobs: Sequence[ResolvedKnob]) -> dict[str, dict[str, float]]:
    """The knobs as the solves take them: `{mote_id: {key: value}}`. A
    later knob for the same value wins."""
    out: dict[str, dict[str, float]] = {}
    for knob in knobs:
        out.setdefault(knob.mote, {})[knob.key] = knob.value
    return out


def merged(*layers: Mapping[str, Mapping[str, float]]) -> dict[str, dict[str, float]]:
    """Override layers as one, later layers over earlier."""
    out: dict[str, dict[str, float]] = {}
    for layer in layers:
        for mote_id, values in layer.items():
            out.setdefault(mote_id, {}).update(values)
    return out


# ---- the line -------------------------------------------------------------

def add_to_line(specs: Sequence[KnobSpec], spec: KnobSpec) -> tuple[KnobSpec, ...]:
    """`spec` added, or moved in place when its ref is already there."""
    at = next((i for i, s in enumerate(specs) if s.ref == spec.ref), None)
    if at is None:
        return (*specs, spec)
    return (*specs[:at], spec, *specs[at + 1:])


def words(specs: Sequence[KnobSpec]) -> list[str]:
    """The `--set` words of a line, unquoted: a flag then an assignment
    for each knob. The line quotes them when it joins its words."""
    out: list[str] = []
    for spec in specs:
        out += [SET_FLAG, spec.assignment]
    return out


def line_payload(line) -> dict:
    """What a pane shows of its line's knobs, for any line with `sets` and
    `text()`: each as typed, and -- while there are knobs -- the two
    commands `W` runs: `command` writes them (`--keep`), then `cleared`
    is the line without them, which the pane takes as its own."""
    specs = tuple(line.sets)
    keep = None
    if specs:
        keep = {"command": f"{line.text()} {KEEP_FLAG}",
                "cleared": dataclasses.replace(line, sets=()).text()}
    return {"sets": [s.assignment for s in specs], "keep": keep}


# ---- the way out ------------------------------------------------------------

def keep(conn: sqlite3.Connection, knobs: Sequence[ResolvedKnob]) -> tuple[str, undo.Command | None]:
    """Write every knob onto its property, as `set` would, as one undo
    step. Returns (message, the step), or a message and None when there
    was nothing to write."""
    commands = []
    kept = []
    for knob in knobs:
        raw = f"{number_text(knob.value)} {knob.unit}" if knob.unit else number_text(knob.value)
        count, command = batch.set_property(conn, [knob.mote], knob.key, raw)
        if count and command is not None:
            commands.append(command)
            kept.append(knob.ref)
    if not commands:
        return "no knobs to keep", None
    noun = "knob" if len(kept) == 1 else "knobs"
    message = f"kept {len(kept)} {noun}: {', '.join(kept)}"
    return message, undo.combine(message, commands)
