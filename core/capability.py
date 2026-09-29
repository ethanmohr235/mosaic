# core/capability.py
"""A capability describes one complete operation a property provider can
perform -- not just an output name, but the specific combination of
inputs that produces it (thermodynamics routinely needs several: h(P,T),
h(P,s), h(P,x) are all different operations that happen to share an
output). See docs/simulation.md, Physical property models.
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Capability:
    operation: str
    inputs: tuple[str, ...]
    output: str
    units: str | None = None
    metadata: dict = field(default_factory=dict)
