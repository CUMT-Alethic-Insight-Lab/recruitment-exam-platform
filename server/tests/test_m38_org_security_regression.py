from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, config, db, org_errors
from app.routes import accounts, org_auth, org_meta, org_requests
from scripts.sync_org_knowledge import sync_documents


@pytest.fixture
def security_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "security.db")
    monkeypatch.setattr(config, "IS_EDGE", False)
    db.init_db()
    app = FastAPI()
    app.include_router(org_auth.router)
    app.include_router(org_meta.router)
    app.include_router(org_requests.router)
    app.include_router(accounts.router)
    org_errors.install_org_error_handlers(app)

    @app.get("/api/v1/boom")
    def boom():
        raise RuntimeError("synthetic failure must not leak")

    with TestClient(app, raise_server_exceptions=False) as client:
        yield client


def account(student_id, role):
    now = auth._utcnow().isoformat()
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO account (student_id,role,pw_hash,pw_changed,created_at,updated_at)"
            " VALUES (?,?,'password',1,?,?)", (student_id, role, now, now),
        )
        token = auth.issue_session(conn, role, None, student_id)
    return {"Authorization": "Bearer " + token}


def recruiting_project():
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO org_project (id,slug,title,description,lifecycle_status,visibility,recruiting,updated_at)"
            " VALUES ('p','p','P','','recruiting','member',1,'now')"
        )


def draft_body():
    return {"kind": "project-join", "form_revision": "1", "fields": {
        "project_id": "p", "motivation": "validate the private draft boundary", "weekly_hours": 2,
    }}


def test_edge_get_never_creates_principal(security_client, monkeypatch):
    headers = account("edge-user", "member")
    monkeypatch.setattr(config, "IS_EDGE", True)
    response = security_client.get("/api/v1/me", headers=headers)
    assert response.status_code == 503
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM org_principal").fetchone()[0] == 0


def test_last_admin_survives_concurrent_cross_demotion(security_client):
    first = account("admin-a", "admin")
    second = account("admin-b", "admin")

    def demote(headers, target):
        return security_client.post(f"/api/admin/accounts/{target}/role",
                                    headers=headers, json={"role": "member"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(
            lambda args: demote(*args),
            ((first, "admin-b"), (second, "admin-a")),
        ))
    with db.connection() as conn:
        roles = dict(conn.execute("SELECT student_id, role FROM account").fetchall())
        admin_count = conn.execute("SELECT COUNT(*) FROM account WHERE role = 'admin'").fetchone()[0]
    details = [(response.status_code, response.text) for response in responses]
    assert any(response.status_code == 200 for response in responses), (details, roles)
    assert admin_count == 1, (details, roles)


def test_admin_cannot_read_unsubmitted_draft(security_client):
    member = account("draft-owner", "member")
    admin = account("draft-admin", "admin")
    recruiting_project()
    response = security_client.post("/api/v1/applications/drafts", headers=member, json=draft_body())
    draft_id = response.json()["id"]
    assert security_client.get(f"/api/v1/applications/{draft_id}", headers=admin).status_code == 404
    proof = security_client.post(f"/api/v1/applications/{draft_id}/confirmations", headers=member).json()
    response = security_client.post(
        "/api/v1/applications", headers={**member, "Idempotency-Key": "draft-privacy"},
        json={"draft_id": draft_id, "confirmation": proof["confirmation"]},
    )
    assert response.status_code == 201
    assert security_client.get(f"/api/v1/applications/{draft_id}", headers=admin).status_code == 200


def test_sync_refuses_edge_without_deleting_documents(security_client, tmp_path, monkeypatch):
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO org_document (id,source_repo,path,revision,visibility,source_url,title,content,indexed_at,updated_at)"
            " VALUES ('d','governance-public','keep.md','r','public','https://example.invalid','keep','safe','now','now')"
        )
    monkeypatch.setattr(config, "IS_EDGE", True)
    with pytest.raises(ValueError, match="authoritative"):
        sync_documents(tmp_path, {"source_repo": "governance-public", "documents": []})
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM org_document").fetchone()[0] == 1


def test_unhandled_org_error_has_envelope_and_matching_request_id(security_client):
    response = security_client.get("/api/v1/boom")
    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "INTERNAL_ERROR", "message": "Internal server error", "retryable": True,
    }
    assert response.json()["request_id"] == response.headers["x-request-id"]
    assert "synthetic failure" not in response.text


def test_mcp_query_limit_matches_api():
    adapter = Path(__file__).resolve().parents[2] / "mcp_adapter"
    sys.path.insert(0, str(adapter))
    from alethic_mcp.server import TOOL_MAP, InvalidParams, validate

    schema = TOOL_MAP["org_search_knowledge"]["inputSchema"]
    validate({"query": "x" * 200}, schema)
    with pytest.raises(InvalidParams):
        validate({"query": "x" * 201}, schema)
