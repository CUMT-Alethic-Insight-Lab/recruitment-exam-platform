import hashlib
import secrets
from datetime import timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app import auth, config, db
from app.org_auth import READ_SCOPES, require_org_identity
from app.routes import accounts, org_auth
from app.routes import auth as web_auth


@pytest.fixture
def org_client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "org.db")
    db.init_db()
    db.init_db()
    app = FastAPI()
    app.include_router(org_auth.router)
    app.include_router(accounts.router)
    app.include_router(web_auth.router)

    @app.get("/identity")
    def identity(request: Request):
        who = require_org_identity(request, scope="profile:read:self")
        return {"id": who.principal_id, "role": who.role, "auth_type": who.auth_type}

    @app.get("/write")
    def write(request: Request):
        require_org_identity(request, scope="applications:create")
        return {"ok": True}

    @app.get("/exam/{cid}")
    def exam(request: Request, cid: str):
        return {"id": auth.require_applicant(request, cid)}

    with TestClient(app) as client:
        yield client


def session(student_id, role="member", kind=None, changed=1):
    with db.connection() as conn:
        now = auth._utcnow().isoformat()
        conn.execute(
            "INSERT INTO account (student_id, role, pw_hash, pw_changed, created_at, updated_at)"
            " VALUES (?, ?, 'stored-password', ?, ?, ?)",
            (student_id, role, changed, now, now),
        )
        token = auth.issue_session(conn, kind or role, "2026", student_id)
    return {"Authorization": "Bearer " + token}


def grant(client, headers, scopes=None):
    request_id = secrets.token_urlsafe(24)
    payload = {"request_id": request_id}
    if scopes is not None:
        payload["scopes"] = scopes
    code = client.post("/api/v1/auth/cli/authorization-requests", headers=headers, json=payload)
    assert code.status_code == 200, code.text
    response = client.post("/api/v1/auth/cli/token", json={"request_id": request_id, "code": code.json()["code"]})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    return {"Authorization": "Bearer " + response.json()["token"]}, response.json(), request_id, code.json()["code"]


def test_admin_downgrade_revokes_old_web_and_cli(org_client):
    admin = session("31000001", "admin")
    target = session("31000002", "admin")
    cli, _, _, _ = grant(org_client, target)
    response = org_client.post("/api/admin/accounts/31000002/role", headers=admin, json={"role": "member"})
    assert response.status_code == 200
    assert org_client.get("/api/admin/accounts", headers=target).status_code == 401
    assert org_client.get("/api/auth/me", headers=target).status_code == 401
    assert org_client.get("/identity", headers=cli).status_code == 401
    with db.connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM login_session WHERE student_id = '31000002'").fetchone()[0] == 0


def test_current_role_not_session_snapshot_and_exam_compatibility(org_client):
    old = session("31000003", "admin")
    applicant = session("31000004", "applicant", changed=0)
    assert org_client.get("/exam/2026", headers=applicant).status_code == 200
    assert org_client.get("/identity", headers=applicant).status_code == 403
    with db.connection() as conn:
        conn.execute("UPDATE account SET role = 'applicant' WHERE student_id = '31000003'")
        conn.execute("UPDATE account SET role = 'member' WHERE student_id = '31000004'")
    assert org_client.get("/api/admin/accounts", headers=old).status_code == 401
    assert org_client.get("/api/auth/me", headers=old).status_code == 401
    assert org_client.get("/exam/2026", headers=applicant).status_code == 401


def test_cli_scopes_isolation_replay_hash_and_revoke(org_client):
    web = session("31000005", "admin")
    cli, token_data, request_id, code = grant(org_client, web)
    assert set(token_data["scopes"]) == READ_SCOPES
    identity = org_client.get("/identity", headers=cli)
    assert identity.status_code == 200
    assert identity.json()["id"].startswith("prn_")
    assert "31000005" not in identity.text
    assert org_client.get("/identity", headers=web).json()["id"] == identity.json()["id"]
    assert org_client.get("/write", headers=cli).status_code == 403
    assert org_client.get("/api/admin/accounts", headers=cli).status_code == 401
    assert org_client.get("/api/auth/me", headers=cli).status_code == 401
    assert org_client.get("/api/v1/auth/cli/grants", headers=cli).status_code == 403
    assert org_client.post("/api/v1/auth/cli/token", json={"request_id": request_id, "code": code}).status_code == 401
    with db.connection() as conn:
        row = conn.execute("SELECT * FROM org_cli_grant").fetchone()
        assert row["token_hash"] == hashlib.sha256(token_data["token"].encode()).hexdigest()
        assert token_data["token"] not in str(dict(row))
        assert code not in str(dict(conn.execute("SELECT * FROM org_cli_authorization").fetchone()))
    listed = org_client.get("/api/v1/auth/cli/grants", headers=web).json()
    assert listed["next_cursor"] is None
    assert "token_hash" not in str(listed)
    other = session("31000006")
    gid = listed["items"][0]["id"]
    assert org_client.delete(f"/api/v1/auth/cli/grants/{gid}", headers=other).status_code == 404
    assert org_client.delete(f"/api/v1/auth/cli/grants/{gid}", headers=web).status_code == 200
    assert org_client.get("/identity", headers=cli).status_code == 401


def test_cli_expiry_membership_and_password_revocation(org_client):
    web = session("31000007")
    cli, _, _, _ = grant(org_client, web, ["profile:read:self", "applications:create"])
    assert org_client.get("/write", headers=cli).status_code == 200
    with db.connection() as conn:
        conn.execute("UPDATE org_cli_grant SET expires_at = ?", ((auth._utcnow() - timedelta(seconds=1)).isoformat(),))
    assert org_client.get("/identity", headers=cli).status_code == 401
    cli, _, _, _ = grant(org_client, web)
    with db.connection() as conn:
        conn.execute("UPDATE account SET role = 'applicant' WHERE student_id = '31000007'")
    assert org_client.get("/identity", headers=cli).status_code == 403
    assert org_client.get("/identity", headers=web).status_code == 401
    with db.connection() as conn:
        conn.execute("UPDATE account SET role = 'member', pw_hash = 'new-password' WHERE student_id = '31000007'")
    assert org_client.get("/identity", headers=cli).status_code == 401


def test_authorization_attempt_limit_expiry_and_web_only(org_client):
    web = session("31000008")
    request_id = secrets.token_urlsafe(24)
    result = org_client.post("/api/v1/auth/cli/authorization-requests", headers=web, json={"request_id": request_id})
    assert result.status_code == 200
    for _ in range(config.ORG_CLI_CODE_ATTEMPTS):
        assert org_client.post("/api/v1/auth/cli/token", json={"request_id": request_id, "code": "wrong"}).status_code == 401
    assert org_client.post("/api/v1/auth/cli/token", json={"request_id": request_id, "code": result.json()["code"]}).status_code == 401
    assert org_client.post("/api/v1/auth/cli/authorization-requests", headers=web, json={"request_id": request_id}).status_code == 409
    assert org_client.post("/api/v1/auth/cli/authorization-requests", headers=web, json={"request_id": secrets.token_urlsafe(24), "scopes": ["admin"]}).status_code == 400
    cli, _, _, _ = grant(org_client, web)
    assert org_client.post("/api/v1/auth/cli/authorization-requests", headers=cli, json={"request_id": secrets.token_urlsafe(24)}).status_code == 403
    rid = secrets.token_urlsafe(24)
    code = org_client.post("/api/v1/auth/cli/authorization-requests", headers=web, json={"request_id": rid}).json()["code"]
    with db.connection() as conn:
        conn.execute("UPDATE org_cli_authorization SET expires_at = ? WHERE request_id = ?", ((auth._utcnow() - timedelta(seconds=1)).isoformat(), rid))
    assert org_client.post("/api/v1/auth/cli/token", json={"request_id": rid, "code": code}).status_code == 401


@pytest.mark.parametrize("operation,payload", [("role", {"role": "applicant"}), ("reset_password", {})])
def test_member_exit_and_reset_revoke_web_grant_and_pending_code(org_client, operation, payload):
    admin = session("31000009", "admin")
    web = session("31000010")
    cli, _, _, _ = grant(org_client, web)
    rid = secrets.token_urlsafe(24)
    code = org_client.post("/api/v1/auth/cli/authorization-requests", headers=web,
                           json={"request_id": rid}).json()["code"]
    result = org_client.post(f"/api/admin/accounts/31000010/{operation}",
                             headers=admin, json=payload)
    assert result.status_code == 200, result.text
    assert org_client.get("/api/auth/me", headers=web).status_code == 401
    assert org_client.get("/identity", headers=cli).status_code == 401
    assert org_client.post("/api/v1/auth/cli/token", json={"request_id": rid, "code": code}).status_code == 401


def test_initial_password_and_authorization_rate_limit(org_client):
    initial = session("31000011", changed=0)
    assert org_client.get("/identity", headers=initial).status_code == 403
    web = session("31000012")
    for _ in range(10):
        assert org_client.post("/api/v1/auth/cli/authorization-requests", headers=web,
                               json={"request_id": secrets.token_urlsafe(24)}).status_code == 200
    limited = org_client.post("/api/v1/auth/cli/authorization-requests", headers=web,
                              json={"request_id": secrets.token_urlsafe(24)})
    assert limited.status_code == 429
    assert limited.headers["retry-after"] == "600"
