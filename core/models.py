### Imports
from dataclasses import dataclass, field
from uuid import uuid4

### Mote class
@dataclass
class Mote:
    properties: dict
    id: str = field(default_factory=lambda: str(uuid4()))

### Relationship class
@dataclass
class Relationship:
    from_id: str
    to_id: str
    type: str
    id: str = field(default_factory=lambda: str(uuid4()))