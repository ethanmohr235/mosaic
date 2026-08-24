# core/device.py
"""This install's own identity for sync purposes. One device_id per
machine, covering every garden on it -- scoping by garden happens in
sync_state (see core/db.py), not here.

Stored beside the garden databases rather than under a per-user config
location: the same choice core/gardens.py already made for
`.default_garden`, and this project has no other per-user config to be
consistent with. Deliberately not synced along with a garden's own
data -- a device's id is what tells two devices apart, so it can never
be one of the things they share.
"""
from pathlib import Path
from uuid import uuid4

from core import db


def _device_id_path() -> Path:
    return db.GARDENS_DIR / ".device_id"


def get_device_id() -> str:
    path = _device_id_path()
    if path.exists():
        return path.read_text().strip()
    db.GARDENS_DIR.mkdir(parents=True, exist_ok=True)
    device_id = str(uuid4())
    path.write_text(device_id)
    return device_id
