# core/materials_provider.py
"""Structural material properties -- E, ν, Sy, ρ -- as a property provider.

Shaped like core/coolprop_provider.py on purpose (decided, `roadmap.md` ->
*Materials provider*): a property-model mote says which material,

    provider:       materials
    configuration:  {"material": "6061-T6"}

and a rule reads a property through the relationship that reaches it,

    sigma <= property('material', 'yield_strength')

so the number is never copied onto a mote, where it would drift from the
material it came from. Changing the material is editing one
configuration, and every rule that reads it follows.

The data is a bundled file, `core/data/materials.json`, not code: adding
an alloy is adding an entry, and the file says where its numbers come
from. A garden that needs something the file lacks gives `values` in the
configuration -- on their own for a custom material, or beside
`material` to override one property of a listed one. Values are nominal,
room temperature and in SI base units, like CoolProp's.

Operations take no inputs. Temperature-dependent properties would be
`youngs_modulus_t(temperature)` in CoolProp's naming, and are left out
until there is published data to put behind them."""
import difflib
import json
import re
from functools import lru_cache
from pathlib import Path

from core.capability import Capability
from core.providers import InvalidModelConfiguration, PropertyProvider

DATA_PATH = Path(__file__).parent / "data" / "materials.json"

# Stored properties, in the file's (SI base) units.
_STORED = {
    "youngs_modulus": "Pa",
    "poisson_ratio": "dimensionless",
    "yield_strength": "Pa",
    "density": "kg/m^3",
}

_CAPABILITIES = (
    *(Capability(operation=name, inputs=(), output=name, units=units)
      for name, units in _STORED.items()),
    # G = E / (2 (1 + ν)) for an isotropic material, unless given.
    Capability(operation="shear_modulus", inputs=(), output="shear_modulus", units="Pa"),
)

_FOLD_RE = re.compile(r"[\s_\-]+")


def _fold(name: str) -> str:
    """A material name as it is looked up: case, spaces, hyphens and
    underscores ignored, so `6061-T6`, `6061 t6` and `6061_T6` agree."""
    return _FOLD_RE.sub("", name).casefold()


@lru_cache(maxsize=1)
def _table() -> dict[str, dict]:
    with DATA_PATH.open(encoding="utf-8") as f:
        return json.load(f)["materials"]


def list_materials() -> list[dict]:
    """Every bundled material: its name, description, aliases and values,
    for a picker or `monoss model materials`."""
    return [{"name": name, **entry} for name, entry in sorted(_table().items())]


def find_material(name: str) -> tuple[str, dict] | None:
    """A bundled material by name or alias, or None."""
    wanted = _fold(name)
    for key, entry in _table().items():
        if wanted in {_fold(key), *(_fold(a) for a in entry.get("aliases", ()))}:
            return key, entry
    return None


class MaterialsProvider(PropertyProvider):
    DESCRIPTION = (
        "Structural material properties (Young's modulus, Poisson's ratio, yield "
        "strength, density, and shear modulus derived from them) for common alloys, "
        "from the bundled core/data/materials.json. Nominal, room temperature, SI. "
        "Operations take no inputs: property('material', 'yield_strength')."
    )
    CONFIGURATION = {
        "material": "a bundled material by name or alias, e.g. '6061-T6', 'A36', "
                    "'304-annealed' (see `monoss model materials`)",
        "values": "optional {property: number} in SI base units -- a custom material "
                  "on its own, or overrides for a listed one",
    }
    CAPABILITIES = _CAPABILITIES

    def __init__(self, configuration: dict):
        super().__init__(configuration)
        values: dict[str, float] = {}
        name = configuration.get("material")
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise InvalidModelConfiguration(
                    "materials provider's 'material' must be a material name, e.g. '6061-T6'")
            found = find_material(name)
            if found is None:
                raise InvalidModelConfiguration(_unknown(name))
            self.material, entry = found
            values.update({k: float(entry[k]) for k in _STORED if k in entry})
        else:
            self.material = "custom"

        given = configuration.get("values", {})
        if not isinstance(given, dict):
            raise InvalidModelConfiguration("materials provider's 'values' must be a map")
        for key, value in given.items():
            if key not in _STORED and key != "shear_modulus":
                raise InvalidModelConfiguration(
                    f"materials provider has no property {key!r}; it knows "
                    f"{', '.join([*_STORED, 'shear_modulus'])}")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise InvalidModelConfiguration(
                    f"materials provider's value for {key!r} must be a number in SI base units")
            values[key] = float(value)

        if name is None and not values:
            raise InvalidModelConfiguration(
                "materials provider needs a 'material' (e.g. '6061-T6') or 'values' "
                "in its configuration")
        self.values = values

    def evaluate(self, operation: str, state: dict) -> float:
        self.capability(operation)  # raises CapabilityNotSupported if unknown
        if operation in self.values:
            return self.values[operation]
        if operation == "shear_modulus" and {"youngs_modulus", "poisson_ratio"} <= set(self.values):
            return self.values["youngs_modulus"] / (2 * (1 + self.values["poisson_ratio"]))
        raise InvalidModelConfiguration(
            f"material {self.material!r} has no {operation.replace('_', ' ')} -- give it in "
            f"the configuration's 'values'")


def _unknown(name: str) -> str:
    names = [*_table(), *(a for e in _table().values() for a in e.get("aliases", ()))]
    close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
    hint = f" -- did you mean {', '.join(repr(c) for c in close)}?" if close else ""
    return (f"no bundled material {name!r}{hint} (`monoss model materials` lists them; "
            f"or give 'values' for a custom one)")
