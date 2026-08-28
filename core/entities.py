# core/entities.py
"""The two things MonOSS stores: a mote, and a relationship between two
motes. Everything else in the system -- species, templates, rules,
property models -- is one of these two wearing a particular property or
relationship type, never a third dataclass.

Named `entities` rather than `models`, which in this codebase means a
*physical property model* everywhere else (`uses_model`,
core/model_resolver.py, core/providers.py, the CLI's `model` command).
One word, two unrelated meanings, in a project whose whole premise is
one object type is exactly the collision worth spending a rename on.
"""
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class Mote:
    properties: dict
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass
class Relationship:
    from_id: str
    to_id: str
    type: str
    id: str = field(default_factory=lambda: str(uuid4()))
