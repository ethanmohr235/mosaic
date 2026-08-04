import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest

import core.db as db
from core.gardens import create_garden


@pytest.fixture(autouse=True)
def isolated_gardens_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "GARDENS_DIR", tmp_path / "gardens")


@pytest.fixture
def conn():
    create_garden("test")
    connection = db.get_connection("test")
    yield connection
    connection.close()
