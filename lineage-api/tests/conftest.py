import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test_lineage.db"
    monkeypatch.setattr(db, "DEFAULT_DB_PATH", str(db_file))
    db.init_db(str(db_file))
    with TestClient(app) as c:
        yield c
