import os
import sys
from pathlib import Path

import pytest

PILOT_TOKEN = "test-admin-token"
os.environ["ADMIN_TOKEN"] = PILOT_TOKEN
os.environ["ALE_THIC_DB_PATH"] = str(Path(__file__).parent / "test.db")

sys.path.insert(0, str(Path(__file__).parent.parent))

from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def clean_db():
    db_path = Path(os.environ["ALE_THIC_DB_PATH"])
    if db_path.exists():
        db_path.unlink()
    yield
    if db_path.exists():
        db_path.unlink()


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def admin_headers():
    return {"Authorization": f"Bearer {PILOT_TOKEN}"}


@pytest.fixture(scope="session")
def cid(client, admin_headers):
    res = client.post(
        "/api/campaigns",
        json={"id": "2026", "name": "2026 春季招新", "year": 2026},
        headers=admin_headers,
    )
    assert res.status_code in (201, 409)
    return "2026"
